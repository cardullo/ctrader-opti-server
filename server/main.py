"""
FastAPI application entrypoint — REST API for cTrader and fsb job orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import tarfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from server.config import settings
from server.db import (
    count_queued_jobs,
    count_queued_passes,
    delete_job as db_delete_job,
    get_all_passes_for_job,
    get_all_jobs,
    get_db,
    get_done_passes_for_job,
    get_job,
    get_passes,
    init_db,
    insert_job,
    insert_passes,
    update_job_status,
    get_jobs_by_filter,
)
from server.export_importer import ExportArtifactImporter
from server.models import (
    BestPassResponse,
    HealthResponse,
    ImportCompletedExportsRequest,
    ImportCompletedExportsResponse,
    ImportCompletedExportsResult,
    JobConfig,
    JobCreateResponse,
    JobDetail,
    JobSummary,
    JobType,
    PassResult,
    Strategy,
    utcnow_iso,
)
from server.optimizer import generate_combinations
from server.ranking import (
    best_ranked_pass,
    effective_ranking_rules,
    format_ranking_summary,
    rank_pass_rows,
)
from server.worker import check_docker, worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _row_to_pass_result(
    row: dict,
    *,
    params: Optional[dict] = None,
    result: Optional[dict] = None,
    ranking_eligible: Optional[bool] = None,
) -> PassResult:
    return PassResult(
        id=row["id"],
        job_id=row["job_id"],
        params=params if params is not None else json.loads(row["params_json"]),
        status=row["status"],
        result=result if result is not None else (json.loads(row["result_json"]) if row["result_json"] else None),
        ranking_eligible=ranking_eligible,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        generation=row.get("generation"),
        strategy_id=row.get("strategy_id"),
        family=row.get("family"),
        candidate_status=row.get("candidate_status"),
    )


def _build_best_summary(best_result: dict, params: dict, cfg: JobConfig, ranking_eligible: bool) -> dict:
    ranking_rules = effective_ranking_rules(cfg)
    return {
        "params": params,
        "ranking_eligible": ranking_eligible,
        "ranking_summary": format_ranking_summary(best_result, ranking_rules),
        **{k: v for k, v in best_result.items() if k != "error"},
    }


def _is_export_job(cfg: JobConfig) -> bool:
    return cfg.strategy == Strategy.export


def _job_type(row: dict) -> str:
    return row.get("job_type") or JobType.opti.value


def _is_fsb_job(row: dict) -> bool:
    return _job_type(row) == JobType.fsb_search.value


def _job_progress(row: dict) -> dict[str, Any] | None:
    raw = row.get("progress_json")
    return json.loads(raw) if raw else None


def _cleanup_temp_file(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        logger.warning("Could not remove temporary archive %s", path, exc_info=True)


def _fsb_artifact_dir(job_id: str) -> Path:
    return settings.fsb_repo_root / "artifacts" / job_id


def _fsb_payload_path(job_id: str) -> Path:
    return settings.algos_dir / f"{job_id}.fsb.json"


def _fsb_job_ready() -> tuple[bool, str]:
    if not settings.fsb_data_dsn.strip():
        return False, "FSB_DATA_DSN is not configured on the server"
    if not settings.fsb_repo_root.exists():
        return False, f"FSB_REPO_ROOT does not exist: {settings.fsb_repo_root}"
    if not Path(settings.fsb_python_bin).exists():
        return False, f"FSB_PYTHON_BIN does not exist: {settings.fsb_python_bin}"
    return True, "ok"


def _fsb_top_passes(rows: list[dict]) -> list[PassResult]:
    def score(row: dict) -> tuple[float, float]:
        result = json.loads(row["result_json"]) if row.get("result_json") else {}
        return (
            float(result.get("fitness") or float("-inf")),
            float(result.get("profit_factor") or float("-inf")),
        )

    ranked = sorted([row for row in rows if row.get("result_json")], key=score, reverse=True)
    return [_row_to_pass_result(row) for row in ranked[:20]]


async def _get_pass_row(db, job_id: str, pass_id: str) -> Optional[dict]:
    async with db.execute(
        "SELECT * FROM passes WHERE id = ? AND job_id = ?",
        (pass_id, job_id),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


def _parse_pass_result_json(pass_row: dict) -> dict[str, Any]:
    raw = pass_row.get("result_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def _import_completed_export_passes(
    *,
    job_id: str,
    pass_rows: list[dict],
    delete_artifacts: bool,
) -> ImportCompletedExportsResponse:
    importer = ExportArtifactImporter(settings.fsb_data_dsn, quarantine_root=settings.quarantine_dir)
    results: list[ImportCompletedExportsResult] = []
    counts = {
        "imported": 0,
        "skipped": 0,
        "quarantined": 0,
        "failed": 0,
    }

    for pass_row in pass_rows:
        pass_id = str(pass_row["id"])
        try:
            result_payload = _parse_pass_result_json(pass_row)
        except Exception as exc:  # noqa: BLE001
            result = ImportCompletedExportsResult(
                pass_id=pass_id,
                status="failed",
                detail=f"invalid result_json: {exc}",
            )
        else:
            if result_payload.get("job_type") != "export":
                result = ImportCompletedExportsResult(
                    pass_id=pass_id,
                    status="failed",
                    detail="pass does not contain export artifacts",
                )
            else:
                imported = importer.import_pass(
                    job_id=job_id,
                    pass_id=pass_id,
                    run_id=str(result_payload.get("run_id") or pass_id),
                    delete_artifacts=delete_artifacts,
                )
                result = ImportCompletedExportsResult(
                    pass_id=imported.pass_id,
                    status=imported.status,
                    detail=imported.detail,
                )

        counts[result.status] += 1
        results.append(result)

    return ImportCompletedExportsResponse(
        job_id=job_id,
        discovered=len(pass_rows),
        imported=counts["imported"],
        skipped=counts["skipped"],
        quarantined=counts["quarantined"],
        failed=counts["failed"],
        results=results,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    logger.info("Database initialized")
    worker_task = asyncio.create_task(worker_loop())
    logger.info("Background worker started")
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="cTrader Optimization Server",
    description="Remote cBot and fsb orchestration",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def verify_api_key(x_api_key: str = Header(...)) -> str:
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db = await get_db()
    try:
        qj = await count_queued_jobs(db)
        qp = await count_queued_passes(db)
    finally:
        await db.close()

    try:
        usage = shutil.disk_usage(str(settings.data_dir))
        used_mb = round((usage.total - usage.free) / (1024 * 1024), 1)
    except Exception:
        used_mb = 0.0

    return HealthResponse(
        status="ok",
        docker_available=check_docker(),
        queued_jobs=qj,
        queued_passes=qp,
        disk_usage_mb=used_mb,
        fsb_ready=_fsb_job_ready()[0],
    )


@app.post("/jobs", response_model=JobCreateResponse, dependencies=[Depends(verify_api_key)])
async def create_job(request: Request) -> JobCreateResponse:
    content_type = request.headers.get("content-type", "")
    job_id = str(uuid4())
    now = utcnow_iso()

    if content_type.startswith("application/json"):
        payload = await request.json()
        if payload.get("job_type") != JobType.fsb_search.value:
            raise HTTPException(status_code=422, detail="JSON job creation only supports job_type=fsb_search")
        ready, detail = _fsb_job_ready()
        if not ready:
            raise HTTPException(status_code=503, detail=detail)
        payload_path = _fsb_payload_path(job_id)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        planned_total = int(payload.get("planned_total_candidates") or 0)
        if planned_total <= 0:
            raise HTTPException(status_code=422, detail="planned_total_candidates must be greater than zero")

        db = await get_db()
        try:
            await insert_job(
                db,
                job_id=job_id,
                name=str(payload.get("name") or f"fsb-search-{job_id[:8]}"),
                algo_path=str(payload_path),
                strategy="fsb_search",
                total_passes=planned_total,
                created_at=now,
                config_json=json.dumps(payload),
                job_type=JobType.fsb_search.value,
            )
        finally:
            await db.close()
        logger.info("Created fsb_search job %s", job_id)
        return JobCreateResponse(job_id=job_id, total_passes=planned_total)

    form = await request.form()
    file = form.get("file")
    
    config_raw = form.get("config")
    config_file = form.get("config_file")

    if config_file is not None:
        config_raw = (await config_file.read()).decode("utf-8")

    if file is None or config_raw is None:
        raise HTTPException(status_code=422, detail="Expected multipart form with file and config")
    try:
        cfg = JobConfig(**json.loads(str(config_raw)))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid config JSON: {exc}")

    algo_path = settings.algos_dir / f"{job_id}.algo"
    algo_path.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    algo_path.write_bytes(content)

    if _is_export_job(cfg):
        if not cfg.chunks:
            raise HTTPException(status_code=422, detail="Export jobs require at least one chunk in config.chunks")
        total_passes = len(cfg.chunks)
        pass_payloads = [chunk.model_dump(mode="json") for chunk in cfg.chunks]
    else:
        param_ranges = {k: v for k, v in cfg.params.items()}
        combos = generate_combinations(param_ranges, cfg.strategy.value, cfg.max_passes)
        total_passes = len(combos)
        if total_passes == 0:
            raise HTTPException(status_code=422, detail="No parameter combinations generated — check param ranges")
        pass_payloads = [{**cfg.fixed_params, **combo} for combo in combos]

    db = await get_db()
    try:
        await insert_job(
            db,
            job_id=job_id,
            name=cfg.name,
            algo_path=str(algo_path),
            strategy=cfg.strategy.value,
            total_passes=total_passes,
            created_at=now,
            config_json=json.dumps(cfg.model_dump(mode="json")),
            job_type=JobType.opti.value,
        )
        rows = []
        for payload in pass_payloads:
            pid = str(uuid4())
            rows.append((pid, job_id, json.dumps(payload), "queued"))
        await insert_passes(db, rows)
    finally:
        await db.close()

    logger.info("Created opti job %s (%s) with %d passes", job_id, cfg.name, total_passes)
    return JobCreateResponse(job_id=job_id, total_passes=total_passes)


@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs() -> list:
    db = await get_db()
    try:
        jobs = await get_all_jobs(db)
        result = []
        for job in jobs:
            job_type = _job_type(job)
            best_summary = None
            summary_error_detail = job.get("error_detail")
            try:
                if job_type == JobType.opti.value:
                    if job.get("strategy") != Strategy.export.value:
                        cfg = JobConfig(**json.loads(job["config_json"]))
                        if not _is_export_job(cfg):
                            done_rows = await get_done_passes_for_job(db, job["id"])
                            best = best_ranked_pass(done_rows, cfg, respect_constraints=True)
                            if best:
                                best_summary = _build_best_summary(best.result, best.params, cfg, best.eligible)
                else:
                    done_rows = await get_done_passes_for_job(db, job["id"])
                    top_passes = _fsb_top_passes(done_rows)
                    if top_passes:
                        result_payload = top_passes[0].result or {}
                        best_summary = {
                            "strategy_id": top_passes[0].strategy_id,
                            "family": top_passes[0].family,
                            "fitness": result_payload.get("fitness"),
                            "profit_factor": result_payload.get("profit_factor"),
                            "trade_count": result_payload.get("trade_count"),
                        }
            except Exception as exc:
                logger.exception("Could not build /jobs summary for job %s", job["id"])
                if not summary_error_detail:
                    summary_error_detail = f"Status summary unavailable: {type(exc).__name__}"
            result.append(
                JobSummary(
                    id=job["id"],
                    name=job["name"],
                    status=job["status"],
                    strategy=job["strategy"],
                    job_type=job_type,
                    total_passes=job["total_passes"],
                    completed_passes=job["completed_passes"],
                    created_at=job["created_at"],
                    best_pass_summary=best_summary,
                    progress=_job_progress(job),
                    error_detail=summary_error_detail,
                )
            )
        return result
    finally:
        await db.close()


@app.get("/jobs/{job_id}", response_model=JobDetail, dependencies=[Depends(verify_api_key)])
async def get_job_detail(job_id: str) -> JobDetail:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        job_type = _job_type(job)
        config_payload = json.loads(job["config_json"])
        if job_type == JobType.opti.value:
            cfg = JobConfig(**config_payload)
            if _is_export_job(cfg):
                top_passes = [_row_to_pass_result(row) for row in await get_passes(db, job_id, limit=20, offset=0)]
            else:
                done_rows = await get_done_passes_for_job(db, job_id)
                ranked = rank_pass_rows(done_rows, cfg, respect_constraints=True)[:20]
                top_passes = [
                    _row_to_pass_result(record.row, params=record.params, result=record.result, ranking_eligible=record.eligible)
                    for record in ranked
                ]
        else:
            done_rows = await get_done_passes_for_job(db, job_id)
            top_passes = _fsb_top_passes(done_rows)

        return JobDetail(
            id=job["id"],
            name=job["name"],
            status=job["status"],
            strategy=job["strategy"],
            job_type=job_type,
            total_passes=job["total_passes"],
            completed_passes=job["completed_passes"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            config=config_payload,
            top_passes=top_passes,
            progress=_job_progress(job),
            error_detail=job.get("error_detail"),
        )
    finally:
        await db.close()


@app.get("/jobs/{job_id}/passes", dependencies=[Depends(verify_api_key)])
async def list_passes(
    job_id: str,
    status: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if _is_fsb_job(job):
            passes = await get_passes(db, job_id, status=status, limit=limit, offset=offset)
            return [_row_to_pass_result(row) for row in passes]

        cfg = JobConfig(**json.loads(job["config_json"]))
        if _is_export_job(cfg):
            passes = await get_passes(db, job_id, status=status, sort_by=sort_by, limit=limit, offset=offset)
            return [_row_to_pass_result(row) for row in passes]

        if status == "done" or sort_by:
            all_rows = await get_all_passes_for_job(db, job_id, status=status)
            ranked = rank_pass_rows(
                all_rows,
                cfg,
                sort_by=sort_by,
                respect_constraints=sort_by is None,
                include_ineligible=True,
            )
            return [
                _row_to_pass_result(record.row, params=record.params, result=record.result, ranking_eligible=record.eligible)
                for record in ranked[offset : offset + limit]
            ]

        passes = await get_passes(db, job_id, status=status, limit=limit, offset=offset)
        return [_row_to_pass_result(row) for row in passes]
    finally:
        await db.close()


@app.get("/jobs/{job_id}/best", response_model=BestPassResponse, dependencies=[Depends(verify_api_key)])
async def best_pass(job_id: str) -> BestPassResponse:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if _is_fsb_job(job):
            raise HTTPException(status_code=400, detail="fsb_search jobs do not support /best")

        cfg = JobConfig(**json.loads(job["config_json"]))
        if _is_export_job(cfg):
            raise HTTPException(status_code=400, detail="Export jobs do not have a single best pass")

        done_rows = await get_done_passes_for_job(db, job_id)
        best = best_ranked_pass(done_rows, cfg, respect_constraints=True)
        if not best:
            raise HTTPException(status_code=404, detail="No completed passes yet")

        return BestPassResponse(
            pass_result=_row_to_pass_result(
                best.row,
                params=best.params,
                result=best.result,
                ranking_eligible=best.eligible,
            ),
            cbotset_params=best.params,
        )
    finally:
        await db.close()


@app.get("/jobs/{job_id}/passes/{pass_id}/artifact", dependencies=[Depends(verify_api_key)])
async def download_pass_artifact(job_id: str, pass_id: str) -> FileResponse:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if _is_fsb_job(job):
            raise HTTPException(status_code=400, detail="Artifacts are only available for export jobs")

        cfg = JobConfig(**json.loads(job["config_json"]))
        if not _is_export_job(cfg):
            raise HTTPException(status_code=400, detail="Artifacts are only available for export jobs")

        pass_row = await _get_pass_row(db, job_id, pass_id)
        if not pass_row:
            raise HTTPException(status_code=404, detail="Pass not found")
        if pass_row["status"] != "done":
            raise HTTPException(status_code=409, detail="Pass is not completed yet")

        result = json.loads(pass_row["result_json"]) if pass_row.get("result_json") else {}
        if result.get("job_type") != "export":
            raise HTTPException(status_code=400, detail="Pass does not contain export artifacts")

        artifact_dir = settings.results_dir / pass_id
        if not artifact_dir.exists():
            raise HTTPException(status_code=404, detail="Artifact directory not found on server")

        with tempfile.NamedTemporaryFile(prefix=f"{pass_id}-", suffix=".tar.gz", delete=False) as archive_file:
            archive_path = Path(archive_file.name)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(artifact_dir, arcname=pass_id)
        return FileResponse(
            archive_path,
            media_type="application/gzip",
            filename=f"{pass_id}.tar.gz",
            background=BackgroundTask(_cleanup_temp_file, str(archive_path)),
        )
    finally:
        await db.close()


@app.get("/jobs/{job_id}/result-bundle", dependencies=[Depends(verify_api_key)])
async def download_fsb_result_bundle(job_id: str) -> FileResponse:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if not _is_fsb_job(job):
            raise HTTPException(status_code=400, detail="Result bundles are only available for fsb_search jobs")
        if job["status"] != "done":
            raise HTTPException(status_code=409, detail="Job is not completed yet")
        artifact_dir = _fsb_artifact_dir(job_id)
        if not artifact_dir.exists():
            raise HTTPException(status_code=404, detail="fsb artifact directory not found on server")

        with tempfile.NamedTemporaryFile(prefix=f"{job_id}-", suffix=".tar.gz", delete=False) as archive_file:
            archive_path = Path(archive_file.name)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(artifact_dir, arcname=f"artifacts/{job_id}")
        return FileResponse(
            archive_path,
            media_type="application/gzip",
            filename=f"{job_id}.tar.gz",
            background=BackgroundTask(_cleanup_temp_file, str(archive_path)),
        )
    finally:
        await db.close()


@app.delete("/jobs/{job_id}/passes/{pass_id}/artifact", dependencies=[Depends(verify_api_key)])
async def delete_pass_artifact(job_id: str, pass_id: str) -> dict:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if _is_fsb_job(job):
            raise HTTPException(status_code=400, detail="Artifacts are only available for export jobs")

        cfg = JobConfig(**json.loads(job["config_json"]))
        if not _is_export_job(cfg):
            raise HTTPException(status_code=400, detail="Artifacts are only available for export jobs")

        pass_row = await _get_pass_row(db, job_id, pass_id)
        if not pass_row:
            raise HTTPException(status_code=404, detail="Pass not found")

        artifact_dir = settings.results_dir / pass_id
        if not artifact_dir.exists():
            return {"status": "already_deleted", "job_id": job_id, "pass_id": pass_id}

        shutil.rmtree(artifact_dir, ignore_errors=True)
        return {"status": "deleted", "job_id": job_id, "pass_id": pass_id}
    finally:
        await db.close()


@app.post(
    "/jobs/{job_id}/import-completed-exports",
    response_model=ImportCompletedExportsResponse,
    dependencies=[Depends(verify_api_key)],
)
async def import_completed_exports_endpoint(
    job_id: str,
    request: ImportCompletedExportsRequest,
) -> ImportCompletedExportsResponse:
    if not settings.fsb_data_dsn.strip():
        raise HTTPException(status_code=503, detail="FSB_DATA_DSN is not configured on the server")

    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if _is_fsb_job(job):
            raise HTTPException(status_code=400, detail="Completed export imports are only available for export jobs")

        cfg = JobConfig(**json.loads(job["config_json"]))
        if not _is_export_job(cfg):
            raise HTTPException(status_code=400, detail="Completed export imports are only available for export jobs")

        done_rows = await get_done_passes_for_job(db, job_id)
        if request.pass_ids is None:
            selected_rows = done_rows
        else:
            requested_ids = set(request.pass_ids)
            selected_rows = [row for row in done_rows if row["id"] in requested_ids]
    finally:
        await db.close()

    return await asyncio.to_thread(
        _import_completed_export_passes,
        job_id=job_id,
        pass_rows=selected_rows,
        delete_artifacts=request.delete_artifacts,
    )


async def _cleanup_job(db, job_id: str, job: dict) -> None:
    if _is_fsb_job(job):
        worker_pid = job.get("worker_pid")
        if worker_pid:
            try:
                os.kill(int(worker_pid), signal.SIGTERM)
            except OSError:
                pass
        artifact_dir = _fsb_artifact_dir(job_id)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        payload_path = _fsb_payload_path(job_id)
        payload_path.unlink(missing_ok=True)
    else:
        try:
            docker_client = __import__("docker").from_env()
            passes_running = await get_passes(db, job_id, status="running", limit=500)
            for row in passes_running:
                cid = row.get("container_id") if isinstance(row, dict) else None
                if not cid:
                    continue
                try:
                    docker_client.containers.get(cid).kill()
                    logger.info("Killed container %s for pass %s", cid, row.get("id", "?"))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Could not cancel containers: %s", exc)

        algo_file = settings.algos_dir / f"{job_id}.algo"
        algo_file.unlink(missing_ok=True)
        async with db.execute("SELECT id FROM passes WHERE job_id = ?", (job_id,)) as cur:
            pass_ids = [row["id"] async for row in cur]
        for pass_id in pass_ids:
            rdir = settings.results_dir / pass_id
            if rdir.exists():
                shutil.rmtree(rdir, ignore_errors=True)

    await update_job_status(db, job_id, "failed", utcnow_iso())
    await db_delete_job(db, job_id)

@app.delete("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str) -> dict:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        await _cleanup_job(db, job_id, job)
    finally:
        await db.close()

    return {"status": "cancelled", "job_id": job_id}

@app.delete("/jobs", dependencies=[Depends(verify_api_key)])
async def bulk_cancel_jobs(
    status: Optional[str] = Query(None),
    before: Optional[str] = Query(None)
) -> dict:
    if not status and not before:
        raise HTTPException(status_code=400, detail="Must provide at least one filter criterion (status or before)")
    
    db = await get_db()
    deleted_count = 0
    try:
        jobs = await get_jobs_by_filter(db, status=status, before_date=before)
        for job in jobs:
            await _cleanup_job(db, job["id"], job)
            deleted_count += 1
    finally:
        await db.close()

    return {"status": "success", "deleted_count": deleted_count}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, reload=True)
