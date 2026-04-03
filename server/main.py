"""
FastAPI application entrypoint — REST API for cTrader cBot optimization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware

from server.config import settings
from server.db import (
    count_queued_jobs,
    count_queued_passes,
    delete_job as db_delete_job,
    get_all_jobs,
    get_best_pass,
    get_db,
    get_job,
    get_passes,
    init_db,
    insert_job,
    insert_passes,
    update_job_status,
)
from server.models import (
    BestPassResponse,
    FitnessMetric,
    HealthResponse,
    JobConfig,
    JobCreateResponse,
    JobDetail,
    JobSummary,
    PassResult,
    utcnow_iso,
)
from server.optimizer import generate_combinations
from server.worker import check_docker, worker_loop

# ── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
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


# ── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="cTrader Optimization Server",
    description="Remote cBot backtest optimization orchestration",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth dependency ─────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(...)) -> str:
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key





# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db = await get_db()
    try:
        qj = await count_queued_jobs(db)
        qp = await count_queued_passes(db)
    finally:
        await db.close()

    # Disk usage of data dir
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
    )


# ── POST /jobs ──────────────────────────────────────────────────────────────

@app.post("/jobs", response_model=JobCreateResponse, dependencies=[Depends(verify_api_key)])
async def create_job(
    file: UploadFile = File(...),
    config: str = Form(...),
) -> JobCreateResponse:
    # Parse config
    try:
        cfg = JobConfig(**json.loads(config))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid config JSON: {exc}")

    job_id = str(uuid4())
    now = utcnow_iso()

    # Save algo file
    algo_path = settings.algos_dir / f"{job_id}.algo"
    algo_path.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    algo_path.write_bytes(content)

    # Generate parameter combinations
    param_ranges = {k: v for k, v in cfg.params.items()}
    combos = generate_combinations(param_ranges, cfg.strategy.value, cfg.max_passes)
    total_passes = len(combos)

    if total_passes == 0:
        raise HTTPException(
            status_code=422,
            detail="No parameter combinations generated — check param ranges",
        )

    # Insert job
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
            config_json=json.dumps(cfg.model_dump()),
        )

        # Insert passes
        rows = []
        for combo in combos:
            pid = str(uuid4())
            rows.append((pid, job_id, json.dumps(combo), "queued"))
        await insert_passes(db, rows)
    finally:
        await db.close()

    logger.info("Created job %s (%s) with %d passes", job_id, cfg.name, total_passes)
    return JobCreateResponse(job_id=job_id, total_passes=total_passes)


# ── GET /jobs ───────────────────────────────────────────────────────────────

@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs() -> list:
    db = await get_db()
    try:
        jobs = await get_all_jobs(db)
        result = []
        for j in jobs:
            cfg = JobConfig(**json.loads(j["config_json"]))
            best = await get_best_pass(db, j["id"], cfg.fitness.value)
            best_summary = None
            if best and best.get("result_json"):
                best_result = json.loads(best["result_json"])
                best_summary = {
                    "params": json.loads(best["params_json"]),
                    **{k: v for k, v in best_result.items() if k != "error"},
                }
            result.append(
                JobSummary(
                    id=j["id"],
                    name=j["name"],
                    status=j["status"],
                    strategy=j["strategy"],
                    total_passes=j["total_passes"],
                    completed_passes=j["completed_passes"],
                    created_at=j["created_at"],
                    best_pass_summary=best_summary,
                )
            )
        return result
    finally:
        await db.close()


# ── GET /jobs/{job_id} ──────────────────────────────────────────────────────

@app.get("/jobs/{job_id}", response_model=JobDetail, dependencies=[Depends(verify_api_key)])
async def get_job_detail(job_id: str) -> JobDetail:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        cfg = JobConfig(**json.loads(job["config_json"]))
        top = await get_passes(
            db, job_id, status="done", sort_by=cfg.fitness.value, limit=20
        )

        top_passes = []
        for p in top:
            top_passes.append(
                PassResult(
                    id=p["id"],
                    job_id=p["job_id"],
                    params=json.loads(p["params_json"]),
                    status=p["status"],
                    result=json.loads(p["result_json"]) if p["result_json"] else None,
                    started_at=p["started_at"],
                    finished_at=p["finished_at"],
                )
            )

        return JobDetail(
            id=job["id"],
            name=job["name"],
            status=job["status"],
            strategy=job["strategy"],
            total_passes=job["total_passes"],
            completed_passes=job["completed_passes"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            config=cfg,
            top_passes=top_passes,
        )
    finally:
        await db.close()


# ── GET /jobs/{job_id}/passes ───────────────────────────────────────────────

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

        passes = await get_passes(db, job_id, status=status, sort_by=sort_by, limit=limit, offset=offset)
        return [
            PassResult(
                id=p["id"],
                job_id=p["job_id"],
                params=json.loads(p["params_json"]),
                status=p["status"],
                result=json.loads(p["result_json"]) if p["result_json"] else None,
                started_at=p["started_at"],
                finished_at=p["finished_at"],
            )
            for p in passes
        ]
    finally:
        await db.close()


# ── GET /jobs/{job_id}/best ─────────────────────────────────────────────────

@app.get("/jobs/{job_id}/best", response_model=BestPassResponse, dependencies=[Depends(verify_api_key)])
async def best_pass(job_id: str) -> BestPassResponse:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        cfg = JobConfig(**json.loads(job["config_json"]))
        best = await get_best_pass(db, job_id, cfg.fitness.value)
        if not best:
            raise HTTPException(status_code=404, detail="No completed passes yet")

        params = json.loads(best["params_json"])
        result = json.loads(best["result_json"]) if best["result_json"] else None

        return BestPassResponse(
            pass_result=PassResult(
                id=best["id"],
                job_id=best["job_id"],
                params=params,
                status=best["status"],
                result=result,
                started_at=best["started_at"],
                finished_at=best["finished_at"],
            ),
            cbotset_params=params,
        )
    finally:
        await db.close()


# ── DELETE /jobs/{job_id} ───────────────────────────────────────────────────

@app.delete("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str) -> dict:
    db = await get_db()
    try:
        job = await get_job(db, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Cancel any running containers for this job
        try:
            docker_client = __import__("docker").from_env()
            passes_running = await get_passes(db, job_id, status="running", limit=500)
            for p in passes_running:
                cid = p.get("container_id") if isinstance(p, dict) else None
                if not cid:
                    # PassResult object
                    continue
                try:
                    container = docker_client.containers.get(cid)
                    container.kill()
                    logger.info("Killed container %s for pass %s", cid, p.get("id", "?"))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Could not cancel containers: %s", exc)

        await update_job_status(db, job_id, "failed", utcnow_iso())

        # Clean up files
        algo_file = settings.algos_dir / f"{job_id}.algo"
        if algo_file.exists():
            algo_file.unlink()
        results_prefix = settings.results_dir
        # Remove result dirs for this job's passes
        async with db.execute(
            "SELECT id FROM passes WHERE job_id = ?", (job_id,)
        ) as cur:
            pass_ids = [row["id"] async for row in cur]
        for pid in pass_ids:
            rdir = results_prefix / pid
            if rdir.exists():
                shutil.rmtree(rdir, ignore_errors=True)

        await db_delete_job(db, job_id)
    finally:
        await db.close()

    return {"status": "cancelled", "job_id": job_id}


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, reload=True)
