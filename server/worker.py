"""
Background worker — polls the DB for queued passes, spawns Docker
containers for ctrader-cli backtests, collects results.

Runs as an asyncio background task launched at FastAPI startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import docker
from docker.errors import DockerException, NotFound, APIError

from server.config import settings
from server.db import (
    get_db,
    get_all_jobs,
    get_queued_passes,
    update_pass_running,
    update_pass_done,
    update_pass_failed,
    update_job_error,
    update_job_progress,
    update_job_status,
    update_job_worker_pid,
    increment_completed,
    requeue_running_passes,
    get_done_passes_for_job,
    insert_passes,
    mark_orphaned_fsb_jobs_failed,
    upsert_fsb_pass,
)
from server.models import JobConfig, JobType, ParamRange, Strategy, utcnow_iso
from server.optimizer import next_generation
from server.parser import parse_report
from server.ranking import build_ranked_population

logger = logging.getLogger(__name__)

# Global Docker client (lazy init)
_docker_client: Optional[docker.DockerClient] = None


def _get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def check_docker() -> bool:
    """Return True if Docker is reachable."""
    try:
        _get_docker().ping()
        return True
    except Exception:
        return False


class FsbJobProgressState:
    def __init__(self, *, started_monotonic: float, total_passes: int) -> None:
        self.started_monotonic = started_monotonic
        self.total_passes = total_passes
        self.current_generation = 0
        self.generation_total = 0
        self.generation_completed = 0
        self.passed = 0
        self.rejected = 0
        self.running = 0
        self.durations: list[float] = []
        self.state = "running"

    def generation_started(self, generation: int, total_candidates: int) -> dict[str, Any]:
        self.current_generation = generation
        self.generation_total = total_candidates
        self.generation_completed = 0
        self.passed = 0
        self.rejected = 0
        self.running = 0
        return self.snapshot()

    def candidate_started(self) -> dict[str, Any]:
        self.running += 1
        return self.snapshot()

    def candidate_finished(self, *, status: str, duration_seconds: float) -> dict[str, Any]:
        self.generation_completed += 1
        self.running = max(0, self.running - 1)
        if status == "passed":
            self.passed += 1
        elif status == "rejected":
            self.rejected += 1
        self.durations.append(duration_seconds)
        return self.snapshot()

    def set_state(self, state: str) -> dict[str, Any]:
        self.state = state
        self.running = 0
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        elapsed_seconds = max(0.0, asyncio.get_event_loop().time() - self.started_monotonic)
        average = (sum(self.durations) / len(self.durations)) if self.durations else None
        remaining = max(0, self.generation_total - self.generation_completed - self.running)
        eta_seconds = (average * remaining) if average is not None else None
        return {
            "state": self.state,
            "current_generation": self.current_generation,
            "generation_total": self.generation_total,
            "generation_completed": self.generation_completed,
            "passed": self.passed,
            "rejected": self.rejected,
            "running": self.running,
            "remaining": remaining,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "completed_passes": len(self.durations),
            "planned_total_passes": self.total_passes,
        }


# ── .cbotset writer ─────────────────────────────────────────────────────────

def write_cbotset(params: Dict[str, Any], path: Path) -> None:
    """
    Write a .cbotset XML file for ctrader-cli.

    Format:
    <cbotset>
      <parameters>
        <parameter name="FastPeriod" value="10" />
        ...
      </parameters>
    </cbotset>
    """
    root = ET.Element("cbotset")
    parameters = ET.SubElement(root, "parameters")
    for name, value in params.items():
        p = ET.SubElement(parameters, "parameter")
        p.set("name", name)
        # Format: drop .0 for whole numbers
        if isinstance(value, float) and value == int(value):
            p.set("value", str(int(value)))
        else:
            p.set("value", str(value))
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(path), encoding="unicode", xml_declaration=True)


def _format_iso_utc_for_ctrader(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Expected timezone-aware ISO timestamp, got {value!r}")
    return parsed.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")


def _format_cli_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _build_optimization_environment(
    *,
    ctid: str,
    account: str,
    config: JobConfig,
) -> Dict[str, str]:
    return {
        "CTID": ctid,
        "PWD-FILE": "/mnt/pwd",
        "ACCOUNT": account,
        "SYMBOL": config.symbol,
        "PERIOD": config.period,
        "START": config.start,
        "END": config.end,
        "DATA-MODE": config.data_mode,
        "BALANCE": str(int(config.balance)),
        "COMMISSION": str(int(config.commission)),
        "SPREAD": str(int(config.spread)),
        "REPORT-JSON": "/mnt/results/report.json",
    }


def _build_optimization_command(job_id: str, params: Dict[str, Any]) -> List[str]:
    command = [
        "backtest",
        f"/mnt/algos/{job_id}.algo",
        "--environment-variables",
        "--exit-on-stop",
    ]
    for param_name, param_value in params.items():
        if isinstance(param_value, float) and param_value == int(param_value):
            command.append(f"--{param_name}={int(param_value)}")
        else:
            command.append(f"--{param_name}={param_value}")
    return command


def _build_export_environment(
    *,
    ctid: str,
    account: str,
    params: Dict[str, Any],
    config: JobConfig,
) -> Dict[str, str]:
    symbol = str(params.get("symbol") or config.symbol)
    period = str(params.get("period") or config.period)
    start_utc = str(params["start_utc"])
    end_utc = str(params["end_utc"])
    data_mode = str(params.get("data_mode") or config.data_mode)
    balance = params.get("balance", config.balance)
    commission = params.get("commission", config.commission)
    spread = params.get("spread", config.spread)

    return {
        "CTID": ctid,
        "PWD-FILE": "/mnt/pwd",
        "ACCOUNT": account,
        "SYMBOL": symbol,
        "PERIOD": period,
        "START": _format_iso_utc_for_ctrader(start_utc),
        "END": _format_iso_utc_for_ctrader(end_utc),
        "DATA-MODE": data_mode,
        "BALANCE": _format_cli_value(balance),
        "COMMISSION": _format_cli_value(commission),
        "SPREAD": _format_cli_value(spread),
    }


def _build_export_command(job_id: str, pass_id: str, params: Dict[str, Any], config: JobConfig) -> List[str]:
    data_mode = str(params.get("data_mode") or config.data_mode)
    broker_code = str(params.get("broker_code") or "unknown")
    requested_start_utc = str(params["start_utc"])
    requested_end_utc = str(params["end_utc"])
    cbot_params = {**config.fixed_params, **dict(params.get("cbot_params") or {})}

    command = [
        "backtest",
        f"/mnt/algos/{job_id}.algo",
        "--environment-variables",
        "--exit-on-stop",
        "--full-access",
        f"--RunId={pass_id}",
        f"--BrokerCode={broker_code}",
        f"--DataModeLabel={data_mode}",
        f"--RequestedStartUtc={requested_start_utc}",
        f"--RequestedEndUtc={requested_end_utc}",
        "--ExportDirectoryPath=/mnt/results",
    ]
    for param_name, param_value in cbot_params.items():
        command.append(f"--{param_name}={_format_cli_value(param_value)}")
    return command


def _export_artifacts_exist(result_dir: Path) -> bool:
    return (result_dir / "manifest.json").exists() and (result_dir / "bars.csv").exists()


def _cleanup_export_cache(job_id: str, pass_id: str) -> None:
    pass_cache_dir = settings.algos_dir / "data" / job_id / pass_id
    if pass_cache_dir.exists():
        shutil.rmtree(pass_cache_dir, ignore_errors=True)

    job_cache_dir = settings.algos_dir / "data" / job_id
    try:
        if job_cache_dir.exists() and not any(job_cache_dir.iterdir()):
            job_cache_dir.rmdir()
    except OSError:
        pass


def _cleanup_failed_export_result_dir(result_dir: Path) -> None:
    manifest_path = result_dir / "manifest.json"
    bars_path = result_dir / "bars.csv"
    if manifest_path.exists() or bars_path.exists():
        return
    shutil.rmtree(result_dir, ignore_errors=True)


def _parse_export_result(
    result_dir: Path,
    host_result_dir: Path,
    pass_id: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    manifest_path = result_dir / "manifest.json"
    bars_path = result_dir / "bars.csv"

    if not manifest_path.exists() or not bars_path.exists():
        raise FileNotFoundError(f"Expected export artifacts in {result_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row_count = int(manifest.get("row_count") or 0)

    return {
        "job_type": "export",
        "run_id": manifest.get("run_id") or pass_id,
        "broker_code": manifest.get("broker_code") or params.get("broker_code"),
        "symbol": manifest.get("symbol") or params.get("symbol"),
        "timeframe": manifest.get("timeframe") or params.get("period"),
        "data_mode_label": manifest.get("data_mode_label") or params.get("data_mode"),
        "requested_start_utc": manifest.get("requested_start_utc") or params.get("start_utc"),
        "requested_end_utc": manifest.get("requested_end_utc") or params.get("end_utc"),
        "first_bar_utc": manifest.get("first_bar_utc"),
        "last_bar_utc": manifest.get("last_bar_utc"),
        "row_count": row_count,
        "artifact_dir": str(result_dir),
        "host_artifact_dir": str(host_result_dir),
        "manifest_path": str(manifest_path),
        "bars_path": str(bars_path),
        "bars_bytes": bars_path.stat().st_size,
        "manifest_bytes": manifest_path.stat().st_size,
    }


def _fsb_job_ready() -> tuple[bool, str]:
    if not settings.fsb_data_dsn.strip():
        return False, "FSB_DATA_DSN is not configured on the server"
    if not settings.fsb_repo_root.exists():
        return False, f"FSB_REPO_ROOT does not exist: {settings.fsb_repo_root}"
    if not Path(settings.fsb_python_bin).exists():
        return False, f"FSB_PYTHON_BIN does not exist: {settings.fsb_python_bin}"
    return True, "ok"


def _fsb_pass_id(generation: int, strategy_id: str) -> str:
    return f"g{generation:03d}-{strategy_id}"


async def _handle_fsb_event(
    db,
    *,
    job_id: str,
    event: dict[str, Any],
    state: FsbJobProgressState,
) -> None:
    event_name = event.get("event")
    if event_name == "generation_started":
        snapshot = state.generation_started(
            generation=int(event.get("generation") or 0),
            total_candidates=int(event.get("total_candidates") or 0),
        )
        await update_job_progress(db, job_id, json.dumps(snapshot), utcnow_iso())
        return

    if event_name == "candidate_started":
        strategy = dict(event.get("strategy") or {})
        generation = int(event.get("generation") or 0)
        strategy_id = str(event.get("strategy_id") or strategy.get("strategy_id") or "")
        family = str(event.get("family") or strategy.get("family") or "")
        await upsert_fsb_pass(
            db,
            pass_id=_fsb_pass_id(generation, strategy_id),
            job_id=job_id,
            params_json=json.dumps(strategy),
            status="running",
            generation=generation,
            strategy_id=strategy_id,
            family=family,
            candidate_status="running",
            started_at=utcnow_iso(),
        )
        snapshot = state.candidate_started()
        await update_job_progress(db, job_id, json.dumps(snapshot), utcnow_iso())
        return

    if event_name == "candidate_finished":
        strategy = dict(event.get("strategy") or {})
        generation = int(event.get("generation") or 0)
        strategy_id = str(event.get("strategy_id") or strategy.get("strategy_id") or "")
        family = str(event.get("family") or strategy.get("family") or "")
        candidate_status = str(event.get("status") or "failed")
        result_payload = dict(event.get("metrics") or {})
        result_payload.update(
            {
                "stage": event.get("stage"),
                "rejection_code": event.get("rejection_code"),
                "rejection_detail": event.get("rejection_detail"),
            }
        )
        await upsert_fsb_pass(
            db,
            pass_id=_fsb_pass_id(generation, strategy_id),
            job_id=job_id,
            params_json=json.dumps(strategy),
            status="done" if candidate_status in {"passed", "rejected"} else "failed",
            generation=generation,
            strategy_id=strategy_id,
            family=family,
            candidate_status=candidate_status,
            finished_at=utcnow_iso(),
            result_json=json.dumps(result_payload),
        )
        await increment_completed(db, job_id, utcnow_iso())
        snapshot = state.candidate_finished(
            status=candidate_status,
            duration_seconds=float(event.get("duration_seconds") or 0.0),
        )
        await update_job_progress(db, job_id, json.dumps(snapshot), utcnow_iso())
        return

    if event_name == "search_finished":
        snapshot = state.set_state(str(event.get("state") or "done"))
        await update_job_progress(db, job_id, json.dumps(snapshot), utcnow_iso())


async def _process_fsb_job(job_row: dict, job_sem: asyncio.Semaphore) -> None:
    job_id = job_row["id"]
    payload = json.loads(job_row["config_json"])
    ready, detail = _fsb_job_ready()
    db = await get_db()
    try:
        if not ready:
            await update_job_error(db, job_id, detail, utcnow_iso())
            await update_job_status(db, job_id, "failed", utcnow_iso())
            return

        async with job_sem:
            payload_path = Path(job_row["algo_path"])
            payload["job_id"] = job_id
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            state = FsbJobProgressState(
                started_monotonic=asyncio.get_event_loop().time(),
                total_passes=int(job_row["total_passes"] or 0),
            )
            await update_job_status(db, job_id, "running", utcnow_iso())
            await update_job_progress(db, job_id, json.dumps(state.snapshot()), utcnow_iso())
            await update_job_error(db, job_id, None, utcnow_iso())

            env = os.environ.copy()
            pythonpath_parts = [str(settings.fsb_repo_root / "src")]
            if env.get("PYTHONPATH"):
                pythonpath_parts.append(env["PYTHONPATH"])
            env.update(
                {
                    "FSB_DATA_DSN": settings.fsb_data_dsn,
                    "PYTHONPATH": os.pathsep.join(pythonpath_parts),
                }
            )

            process = await asyncio.create_subprocess_exec(
                settings.fsb_python_bin,
                "-m",
                "forex_scalping_backtester.remote_worker",
                str(payload_path),
                cwd=str(settings.fsb_repo_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await update_job_worker_pid(db, job_id, process.pid, utcnow_iso())
            stderr_task = asyncio.create_task(process.stderr.read())

            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON fsb worker line for job %s: %s", job_id, text)
                    continue
                await _handle_fsb_event(db, job_id=job_id, event=event, state=state)

            return_code = await process.wait()
            stderr_output = (await stderr_task).decode("utf-8", errors="replace").strip()

            final_snapshot = state.snapshot()
            completed = int(final_snapshot.get("completed_passes") or 0)
            await db.execute(
                "UPDATE jobs SET total_passes = ?, updated_at = ? WHERE id = ?",
                (completed or int(job_row["total_passes"] or 0), utcnow_iso(), job_id),
            )
            await db.commit()

            if return_code != 0:
                error_detail = stderr_output or f"fsb worker exited with code {return_code}"
                await update_job_error(db, job_id, error_detail, utcnow_iso())
                await update_job_progress(db, job_id, json.dumps(state.set_state("failed")), utcnow_iso())
                await update_job_status(db, job_id, "failed", utcnow_iso())
                return

            if state.state != "done":
                await update_job_progress(db, job_id, json.dumps(state.set_state("done")), utcnow_iso())
            await update_job_status(db, job_id, "done", utcnow_iso())
            payload_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.error("fsb job %s failed: %s", job_id, exc, exc_info=True)
        await update_job_error(db, job_id, str(exc), utcnow_iso())
        await update_job_progress(db, job_id, json.dumps(state.set_state("failed") if "state" in locals() else {}), utcnow_iso())
        await update_job_status(db, job_id, "failed", utcnow_iso())
    finally:
        await update_job_worker_pid(db, job_id, None, utcnow_iso())
        await db.close()


# ── Single pass executor ───────────────────────────────────────────────────

async def run_single_pass(
    job_id: str,
    pass_id: str,
    params: Dict[str, Any],
    config: JobConfig,
) -> None:
    """Run a single backtest pass inside a Docker container."""
    db = await get_db()
    try:
        # Resolve effective credentials
        ctid = config.ctid or settings.ctid
        account = config.account or settings.ctrader_account

        # Prepare paths
        algo_file = settings.algos_dir / f"{job_id}.algo"
        result_dir = settings.results_dir / pass_id
        result_dir.mkdir(parents=True, exist_ok=True)

        # Docker volumes — use HOST paths for sibling container bind mounts.
        # The server container sees /data/algos, but ctrader-cli containers
        # are siblings (spawned via Docker socket) and need HOST paths.
        host_algos = str(settings.host_algos_dir.resolve())
        host_results = str((settings.host_results_dir / pass_id).resolve())
        host_pwd = str(settings.host_pwd_file_path.resolve())

        if config.strategy == Strategy.export:
            environment = _build_export_environment(
                ctid=ctid,
                account=account,
                params=params,
                config=config,
            )
            command = _build_export_command(job_id, pass_id, params, config)
        else:
            environment = _build_optimization_environment(
                ctid=ctid,
                account=account,
                config=config,
            )
            command = _build_optimization_command(job_id, params)

        logger.info(
            "Starting pass %s (job %s) with params %s | cmd=%s",
            pass_id, job_id, json.dumps(params), " ".join(command),
        )

        # Run container in executor to avoid blocking the event loop
        # NOTE: We use remove=False so we can read logs on failure,
        # then manually remove the container after.
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: _get_docker().containers.run(
                image=settings.docker_image,
                command=command,
                environment=environment,
                volumes={
                    host_algos: {"bind": "/mnt/algos", "mode": "rw"},
                    host_results: {"bind": "/mnt/results", "mode": "rw"},
                    host_pwd: {"bind": "/mnt/pwd", "mode": "ro"},
                },
                remove=False,
                detach=True,
            ),
        )

        container_id = container.id
        await update_pass_running(db, pass_id, container_id, utcnow_iso())

        # Wait for container to finish (with timeout)
        try:
            exit_result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: container.wait()),
                timeout=settings.pass_timeout_seconds,
            )
            exit_code = exit_result.get("StatusCode", -1)
        except asyncio.TimeoutError:
            logger.warning("Pass %s timed out, killing container", pass_id)
            try:
                await loop.run_in_executor(None, container.kill)
            except Exception:
                pass
            # Clean up container
            try:
                await loop.run_in_executor(None, lambda: container.remove(force=True))
            except Exception:
                pass
            await update_pass_failed(db, pass_id, "Timeout exceeded", utcnow_iso())
            await increment_completed(db, job_id, utcnow_iso())
            return

        if exit_code != 0:
            # Get logs before removing
            try:
                logs = await loop.run_in_executor(
                    None,
                    lambda: container.logs(tail=50).decode("utf-8", errors="replace"),
                )
            except Exception:
                logs = "Could not retrieve container logs"
            if not (config.strategy == Strategy.export and _export_artifacts_exist(result_dir)):
                # Remove container
                try:
                    await loop.run_in_executor(None, lambda: container.remove(force=True))
                except Exception:
                    pass
                if config.strategy == Strategy.export:
                    _cleanup_failed_export_result_dir(result_dir)
                err_msg = f"Container exited with code {exit_code}: {logs[-4000:]}"
                logger.error("Pass %s failed: %s", pass_id, err_msg)
                await update_pass_failed(db, pass_id, err_msg, utcnow_iso())
                await increment_completed(db, job_id, utcnow_iso())
                return

            logger.warning(
                "Export pass %s exited with code %s after writing artifacts; continuing",
                pass_id,
                exit_code,
            )

        # Remove container after successful run
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except Exception:
            pass

        if config.strategy == Strategy.export:
            result = _parse_export_result(
                result_dir,
                Path(host_results),
                pass_id,
                params,
            )
        else:
            result = parse_report(result_dir)
        await update_pass_done(db, pass_id, json.dumps(result), utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
        if config.strategy == Strategy.export:
            logger.info("Export pass %s done: rows=%s", pass_id, result.get("row_count", 0))
        else:
            logger.info("Pass %s done: net_profit=%.2f", pass_id, result.get("net_profit", 0))

    except DockerException as exc:
        logger.error("Docker error on pass %s: %s", pass_id, exc)
        if config.strategy == Strategy.export:
            _cleanup_failed_export_result_dir(result_dir)
        await update_pass_failed(db, pass_id, f"Docker error: {exc}", utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
    except Exception as exc:
        logger.error("Unexpected error on pass %s: %s", pass_id, exc, exc_info=True)
        if config.strategy == Strategy.export:
            _cleanup_failed_export_result_dir(result_dir)
        await update_pass_failed(db, pass_id, str(exc), utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
    finally:
        if config.strategy == Strategy.export:
            _cleanup_export_cache(job_id, pass_id)
        await db.close()


# ── Genetic generation manager ─────────────────────────────────────────────

async def _run_genetic_job(
    job_id: str,
    config: JobConfig,
    semaphore: asyncio.Semaphore,
) -> None:
    """
    Run a genetic optimization job in waves.

    Each generation is submitted, awaited, scored, and the next generation
    is produced until max_passes is exhausted.
    """
    db = await get_db()
    try:
        total_budget = config.max_passes
        used = 0
        generation = 0

        while used < total_budget:
            generation += 1
            # Fetch queued passes for this generation
            passes = await get_queued_passes(db, job_id, limit=total_budget - used)
            if not passes:
                break

            # Run this generation in parallel
            tasks = []
            for p in passes:
                params = json.loads(p["params_json"])
                tasks.append(
                    _sem_run(semaphore, job_id, p["id"], params, config)
                )
            await asyncio.gather(*tasks)
            used += len(passes)

            if used >= total_budget:
                break

            # Evaluate: rank eligible passes and score them by final rank
            done_passes = await get_done_passes_for_job(db, job_id)
            scored = build_ranked_population(done_passes, config)

            if len(scored) < 2:
                logger.warning(
                    "Genetic job %s stopped after generation %d: only %d passes satisfied ranking constraints",
                    job_id,
                    generation,
                    len(scored),
                )
                break

            # Produce next generation
            remaining = total_budget - used
            gen_size = min(20, remaining)
            if gen_size <= 0:
                break

            param_ranges = {
                k: ParamRange(**v) if isinstance(v, dict) else v
                for k, v in config.params.items()
            }
            next_gen = next_generation(
                param_ranges, scored, generation_size=gen_size
            )

            # Insert new passes
            new_rows = []
            for combo in next_gen:
                pid = str(uuid4())
                new_rows.append((pid, job_id, json.dumps(combo), "queued"))
            await insert_passes(db, new_rows)

            # Update job total_passes
            await db.execute(
                "UPDATE jobs SET total_passes = total_passes + ?, updated_at = ? WHERE id = ?",
                (len(new_rows), utcnow_iso(), job_id),
            )
            await db.commit()

            logger.info(
                "Genetic job %s: generation %d complete, %d new passes queued",
                job_id, generation, len(new_rows),
            )
    finally:
        await db.close()


async def _sem_run(
    sem: asyncio.Semaphore,
    job_id: str,
    pass_id: str,
    params: Dict[str, Any],
    config: JobConfig,
) -> None:
    """Run a single pass under a semaphore."""
    async with sem:
        await run_single_pass(job_id, pass_id, params, config)


# ── Main worker loop ───────────────────────────────────────────────────────

async def worker_loop() -> None:
    """
    Background worker that continuously processes queued jobs.

    Polls the DB every 2 seconds, finds jobs with queued passes, and
    dispatches them with proper concurrency limits.
    """
    logger.info("Worker starting up…")

    # Recovery: re-queue any passes stuck in 'running'
    db = await get_db()
    try:
        recovered = await requeue_running_passes(db)
        if recovered:
            logger.info("Recovered %d interrupted passes → re-queued", recovered)
        orphaned = await mark_orphaned_fsb_jobs_failed(db, utcnow_iso())
        if orphaned:
            logger.info("Marked %d orphaned fsb jobs as failed", orphaned)
    finally:
        await db.close()

    # Job-level semaphore (max concurrent jobs)
    job_semaphore = asyncio.Semaphore(settings.max_parallel_jobs)
    active_jobs: dict = {}  # job_id → asyncio.Task

    while True:
        try:
            db = await get_db()
            try:
                jobs = await get_all_jobs(db)
            finally:
                await db.close()

            for job in jobs:
                jid = job["id"]
                status = job["status"]

                # Skip finished / already active jobs
                if status in ("done", "failed"):
                    active_jobs.pop(jid, None)
                    continue
                if jid in active_jobs and not active_jobs[jid].done():
                    continue

                if (job.get("job_type") or JobType.opti.value) == JobType.fsb_search.value:
                    task = asyncio.create_task(_process_fsb_job(job, job_semaphore))
                else:
                    config = JobConfig(**json.loads(job["config_json"]))
                    parallel = min(
                        config.parallel_workers,
                        settings.max_parallel_workers_per_job,
                    )
                    sem = asyncio.Semaphore(parallel)

                    if config.strategy.value == "genetic":
                        task = asyncio.create_task(
                            _process_genetic_job(jid, config, sem, job_semaphore)
                        )
                    else:
                        task = asyncio.create_task(
                            _process_standard_job(jid, config, sem, job_semaphore)
                        )
                active_jobs[jid] = task

            # Clean up completed tasks
            done_ids = [jid for jid, t in active_jobs.items() if t.done()]
            for jid in done_ids:
                task = active_jobs.pop(jid)
                if task.exception():
                    logger.error("Job %s task failed: %s", jid, task.exception())

        except Exception as exc:
            logger.error("Worker loop error: %s", exc, exc_info=True)

        await asyncio.sleep(2)


async def _process_standard_job(
    job_id: str,
    config: JobConfig,
    sem: asyncio.Semaphore,
    job_sem: asyncio.Semaphore,
) -> None:
    """Process a grid or random job: run all queued passes concurrently."""
    async with job_sem:
        db = await get_db()
        try:
            await update_job_status(db, job_id, "running", utcnow_iso())
        finally:
            await db.close()

        logger.info("Processing job %s (strategy=%s)", job_id, config.strategy.value)

        while True:
            db = await get_db()
            try:
                passes = await get_queued_passes(db, job_id, limit=50)
            finally:
                await db.close()

            if not passes:
                break

            tasks = []
            for p in passes:
                params = json.loads(p["params_json"])
                tasks.append(_sem_run(sem, job_id, p["id"], params, config))
            await asyncio.gather(*tasks)

        # Mark job done
        db = await get_db()
        try:
            job = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await job.fetchone()
            # Check if any passes failed
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM passes WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            ) as cur:
                fail_row = await cur.fetchone()
                fail_count = fail_row["cnt"] if fail_row else 0

            final_status = "done"
            # If ALL passes failed, mark job as failed
            if row and fail_count == row["total_passes"]:
                final_status = "failed"

            await update_job_status(db, job_id, final_status, utcnow_iso())
            logger.info("Job %s finished with status=%s", job_id, final_status)
        finally:
            await db.close()


async def _process_genetic_job(
    job_id: str,
    config: JobConfig,
    sem: asyncio.Semaphore,
    job_sem: asyncio.Semaphore,
) -> None:
    """Process a genetic job with generational waves."""
    async with job_sem:
        db = await get_db()
        try:
            await update_job_status(db, job_id, "running", utcnow_iso())
        finally:
            await db.close()

        logger.info("Processing genetic job %s", job_id)
        await _run_genetic_job(job_id, config, sem)

        # Mark done
        db = await get_db()
        try:
            await update_job_status(db, job_id, "done", utcnow_iso())
            logger.info("Genetic job %s finished", job_id)
        finally:
            await db.close()
