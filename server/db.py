"""SQLite helpers using aiosqlite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from server.config import settings

DB_PATH = settings.data_dir / "opti.db"

# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    algo_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    job_type TEXT NOT NULL DEFAULT 'opti',
    strategy TEXT NOT NULL DEFAULT 'grid',
    total_passes INTEGER NOT NULL DEFAULT 0,
    completed_passes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    progress_json TEXT,
    error_detail TEXT,
    worker_pid INTEGER
);

CREATE TABLE IF NOT EXISTS passes (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result_json TEXT,
    container_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    generation INTEGER,
    strategy_id TEXT,
    family TEXT,
    candidate_status TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_passes_job_id ON passes(job_id);
CREATE INDEX IF NOT EXISTS idx_passes_status ON passes(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


async def get_db() -> aiosqlite.Connection:
    """Open (or create) the database and return a connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create tables if they don't exist."""
    db = await get_db()
    try:
        await db.executescript(_SCHEMA)
        await _add_column_if_missing(db, "jobs", "job_type", "TEXT NOT NULL DEFAULT 'opti'")
        await _add_column_if_missing(db, "jobs", "progress_json", "TEXT")
        await _add_column_if_missing(db, "jobs", "error_detail", "TEXT")
        await _add_column_if_missing(db, "jobs", "worker_pid", "INTEGER")
        await _add_column_if_missing(db, "passes", "generation", "INTEGER")
        await _add_column_if_missing(db, "passes", "strategy_id", "TEXT")
        await _add_column_if_missing(db, "passes", "family", "TEXT")
        await _add_column_if_missing(db, "passes", "candidate_status", "TEXT")
        await db.commit()
    finally:
        await db.close()


# ── Job helpers ─────────────────────────────────────────────────────────────

async def insert_job(
    db: aiosqlite.Connection,
    job_id: str,
    name: str,
    algo_path: str,
    strategy: str,
    total_passes: int,
    created_at: str,
    config_json: str,
    job_type: str = "opti",
) -> None:
    await db.execute(
        """INSERT INTO jobs (id, name, algo_path, status, job_type, strategy,
           total_passes, completed_passes, created_at, updated_at, config_json)
           VALUES (?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?)""",
        (job_id, name, algo_path, job_type, strategy, total_passes, created_at, created_at, config_json),
    )
    await db.commit()


async def get_job(db: aiosqlite.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_jobs(db: aiosqlite.Connection) -> List[Dict[str, Any]]:
    async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def get_jobs_by_filter(
    db: aiosqlite.Connection,
    status: Optional[str] = None,
    before_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if before_date:
        query += " AND created_at < ?"
        params.append(before_date)
    query += " ORDER BY created_at DESC"
    
    async with db.execute(query, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_job_status(
    db: aiosqlite.Connection, job_id: str, status: str, updated_at: str
) -> None:
    await db.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at, job_id),
    )
    await db.commit()


async def update_job_progress(
    db: aiosqlite.Connection,
    job_id: str,
    progress_json: str,
    updated_at: str,
) -> None:
    await db.execute(
        "UPDATE jobs SET progress_json = ?, updated_at = ? WHERE id = ?",
        (progress_json, updated_at, job_id),
    )
    await db.commit()


async def update_job_error(
    db: aiosqlite.Connection,
    job_id: str,
    error_detail: str | None,
    updated_at: str,
) -> None:
    await db.execute(
        "UPDATE jobs SET error_detail = ?, updated_at = ? WHERE id = ?",
        (error_detail, updated_at, job_id),
    )
    await db.commit()


async def update_job_worker_pid(
    db: aiosqlite.Connection,
    job_id: str,
    worker_pid: int | None,
    updated_at: str,
) -> None:
    await db.execute(
        "UPDATE jobs SET worker_pid = ?, updated_at = ? WHERE id = ?",
        (worker_pid, updated_at, job_id),
    )
    await db.commit()


async def increment_completed(
    db: aiosqlite.Connection, job_id: str, updated_at: str
) -> None:
    await db.execute(
        "UPDATE jobs SET completed_passes = completed_passes + 1, updated_at = ? WHERE id = ?",
        (updated_at, job_id),
    )
    await db.commit()


async def delete_job(db: aiosqlite.Connection, job_id: str) -> None:
    await db.execute("DELETE FROM passes WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()


# ── Pass helpers ────────────────────────────────────────────────────────────

async def insert_passes(
    db: aiosqlite.Connection, rows: List[tuple]
) -> None:
    """Bulk insert pass rows: (id, job_id, params_json, status)."""
    await db.executemany(
        "INSERT INTO passes (id, job_id, params_json, status) VALUES (?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def upsert_fsb_pass(
    db: aiosqlite.Connection,
    *,
    pass_id: str,
    job_id: str,
    params_json: str,
    status: str,
    generation: int,
    strategy_id: str,
    family: str,
    candidate_status: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    result_json: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO passes (
            id, job_id, params_json, status, result_json, started_at, finished_at,
            generation, strategy_id, family, candidate_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            params_json = excluded.params_json,
            status = excluded.status,
            result_json = excluded.result_json,
            started_at = COALESCE(excluded.started_at, passes.started_at),
            finished_at = COALESCE(excluded.finished_at, passes.finished_at),
            generation = excluded.generation,
            strategy_id = excluded.strategy_id,
            family = excluded.family,
            candidate_status = excluded.candidate_status
        """,
        (
            pass_id,
            job_id,
            params_json,
            status,
            result_json,
            started_at,
            finished_at,
            generation,
            strategy_id,
            family,
            candidate_status,
        ),
    )
    await db.commit()


async def get_queued_passes(
    db: aiosqlite.Connection, job_id: str, limit: int = 10
) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM passes WHERE job_id = ? AND status = 'queued' LIMIT ?",
        (job_id, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_pass_running(
    db: aiosqlite.Connection, pass_id: str, container_id: str, started_at: str
) -> None:
    await db.execute(
        "UPDATE passes SET status = 'running', container_id = ?, started_at = ? WHERE id = ?",
        (container_id, started_at, pass_id),
    )
    await db.commit()


async def update_pass_done(
    db: aiosqlite.Connection,
    pass_id: str,
    result_json: str,
    finished_at: str,
) -> None:
    await db.execute(
        "UPDATE passes SET status = 'done', result_json = ?, finished_at = ? WHERE id = ?",
        (result_json, finished_at, pass_id),
    )
    await db.commit()


async def update_pass_failed(
    db: aiosqlite.Connection,
    pass_id: str,
    error_msg: str,
    finished_at: str,
) -> None:
    result = json.dumps({"error": error_msg})
    await db.execute(
        "UPDATE passes SET status = 'failed', result_json = ?, finished_at = ? WHERE id = ?",
        (result, finished_at, pass_id),
    )
    await db.commit()


async def get_passes(
    db: aiosqlite.Connection,
    job_id: str,
    status: Optional[str] = None,
    sort_by: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Fetch passes for a job with optional filters."""
    query = "SELECT * FROM passes WHERE job_id = ?"
    params: list = [job_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    # Sort by a result metric (requires json_extract)
    if sort_by:
        query += f" ORDER BY CAST(json_extract(result_json, '$.{sort_by}') AS REAL) DESC"
    else:
        query += " ORDER BY COALESCE(finished_at, started_at) DESC, generation DESC, id ASC"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with db.execute(query, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def get_all_passes_for_job(
    db: aiosqlite.Connection,
    job_id: str,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch all passes for a job so ranking can happen in Python."""
    query = "SELECT * FROM passes WHERE job_id = ?"
    params: list = [job_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY finished_at DESC, started_at DESC, id ASC"
    async with db.execute(query, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def get_best_pass(
    db: aiosqlite.Connection, job_id: str, fitness: str
) -> Optional[Dict[str, Any]]:
    query = f"""
        SELECT * FROM passes
        WHERE job_id = ? AND status = 'done'
        ORDER BY CAST(json_extract(result_json, '$.{fitness}') AS REAL) DESC
        LIMIT 1
    """
    async with db.execute(query, (job_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_running_passes(db: aiosqlite.Connection) -> List[Dict[str, Any]]:
    async with db.execute("SELECT * FROM passes WHERE status = 'running'") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def requeue_running_passes(db: aiosqlite.Connection) -> int:
    """Re-queue any passes left in 'running' state (server restart recovery)."""
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM passes WHERE status = 'running'"
    ) as cur:
        row = await cur.fetchone()
        count = row["cnt"] if row else 0
    await db.execute(
        "UPDATE passes SET status = 'queued', container_id = NULL, started_at = NULL WHERE status = 'running'"
    )
    await db.commit()
    return count


async def count_queued_jobs(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('queued', 'running')"
    ) as cur:
        row = await cur.fetchone()
        return row["cnt"] if row else 0


async def count_queued_passes(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM passes WHERE status = 'queued'"
    ) as cur:
        row = await cur.fetchone()
        return row["cnt"] if row else 0


async def get_done_passes_for_job(
    db: aiosqlite.Connection, job_id: str
) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM passes WHERE job_id = ? AND status = 'done'", (job_id,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def mark_orphaned_fsb_jobs_failed(db: aiosqlite.Connection, updated_at: str) -> int:
    await db.execute(
        """
        UPDATE jobs
        SET status = 'failed',
            error_detail = COALESCE(error_detail, 'fsb worker no longer running'),
            worker_pid = NULL,
            updated_at = ?
        WHERE job_type = 'fsb_search'
          AND status = 'running'
        """,
        (updated_at,),
    )
    await db.commit()
    return db.total_changes


async def _add_column_if_missing(
    db: aiosqlite.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    async with db.execute(f"PRAGMA table_info({table_name})") as cur:
        columns = {row["name"] async for row in cur}
    if column_name in columns:
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
