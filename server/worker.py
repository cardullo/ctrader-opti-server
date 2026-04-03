"""
Background worker — polls the DB for queued passes, spawns Docker
containers for ctrader-cli backtests, collects results.

Runs as an asyncio background task launched at FastAPI startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
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
    update_job_status,
    increment_completed,
    requeue_running_passes,
    get_done_passes_for_job,
    insert_passes,
)
from server.models import JobConfig, ParamRange, utcnow_iso
from server.optimizer import next_generation
from server.parser import parse_report

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

        # Write cbotset
        cbotset_path = settings.algos_dir / f"{pass_id}.cbotset"
        write_cbotset(params, cbotset_path)

        # Docker volumes
        algos_mount = str(settings.algos_dir.resolve())
        results_mount = str(result_dir.resolve())
        pwd_file_host = str(Path(settings.pwd_file_path).resolve())

        # ctrader-cli uses hyphenated env var names and reads the
        # .cbotset as the second positional argument after the .algo path.
        environment = {
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

        # The cbotset file is the second positional arg after the .algo
        command = (
            f"backtest /mnt/algos/{job_id}.algo "
            f"/mnt/algos/{pass_id}.cbotset "
            f"--environment-variables --exit-on-stop"
        )

        logger.info(
            "Starting pass %s (job %s) with params %s",
            pass_id, job_id, json.dumps(params),
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
                    algos_mount: {"bind": "/mnt/algos", "mode": "rw"},
                    results_mount: {"bind": "/mnt/results", "mode": "rw"},
                    pwd_file_host: {"bind": "/mnt/pwd", "mode": "ro"},
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
            # Remove container
            try:
                await loop.run_in_executor(None, lambda: container.remove(force=True))
            except Exception:
                pass
            err_msg = f"Container exited with code {exit_code}: {logs[-500:]}"
            logger.error("Pass %s failed: %s", pass_id, err_msg)
            await update_pass_failed(db, pass_id, err_msg, utcnow_iso())
            await increment_completed(db, job_id, utcnow_iso())
            return

        # Remove container after successful run
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except Exception:
            pass

        # Parse results
        result = parse_report(result_dir)
        await update_pass_done(db, pass_id, json.dumps(result), utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
        logger.info("Pass %s done: net_profit=%.2f", pass_id, result.get("net_profit", 0))

    except DockerException as exc:
        logger.error("Docker error on pass %s: %s", pass_id, exc)
        await update_pass_failed(db, pass_id, f"Docker error: {exc}", utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
    except Exception as exc:
        logger.error("Unexpected error on pass %s: %s", pass_id, exc, exc_info=True)
        await update_pass_failed(db, pass_id, str(exc), utcnow_iso())
        await increment_completed(db, job_id, utcnow_iso())
    finally:
        await db.close()
        # Cleanup temp cbotset
        cbotset_file = settings.algos_dir / f"{pass_id}.cbotset"
        if cbotset_file.exists():
            cbotset_file.unlink(missing_ok=True)


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

            # Evaluate: get all done passes, score by fitness
            done_passes = await get_done_passes_for_job(db, job_id)
            fitness_key = config.fitness.value if hasattr(config.fitness, 'value') else config.fitness
            scored = []
            for dp in done_passes:
                if dp["result_json"]:
                    result = json.loads(dp["result_json"])
                    score = float(result.get(fitness_key, 0))
                    params = json.loads(dp["params_json"])
                    scored.append((params, score))

            if len(scored) < 2:
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
