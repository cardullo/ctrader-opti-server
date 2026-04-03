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
    strategy TEXT NOT NULL DEFAULT 'grid',
    total_passes INTEGER NOT NULL DEFAULT 0,
    completed_passes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    config_json TEXT NOT NULL
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
) -> None:
    await db.execute(
        """INSERT INTO jobs (id, name, algo_path, status, strategy,
           total_passes, completed_passes, created_at, updated_at, config_json)
           VALUES (?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?)""",
        (job_id, name, algo_path, strategy, total_passes, created_at, created_at, config_json),
    )
    await db.commit()


async def get_job(db: aiosqlite.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_jobs(db: aiosqlite.Connection) -> List[Dict[str, Any]]:
    async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_job_status(
    db: aiosqlite.Connection, job_id: str, status: str, updated_at: str
) -> None:
    await db.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at, job_id),
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
        query += " ORDER BY finished_at DESC"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
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
