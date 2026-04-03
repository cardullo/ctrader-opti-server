"""Pydantic models for request / response schemas and internal types."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class PassStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Strategy(str, Enum):
    grid = "grid"
    random = "random"
    genetic = "genetic"


class FitnessMetric(str, Enum):
    net_profit = "net_profit"
    sharpe = "sharpe_ratio"
    profit_factor = "profit_factor"
    win_rate = "win_rate"


# ── Parameter range ─────────────────────────────────────────────────────────

class ParamRange(BaseModel):
    min: float
    max: float
    step: float


# ── Job config (submitted by client) ───────────────────────────────────────

class JobConfig(BaseModel):
    name: str = "unnamed"
    ctid: Optional[str] = None
    account: Optional[str] = None
    symbol: str = "EURUSD"
    period: str = "H1"
    start: str = "01/01/2023"
    end: str = "01/01/2025"
    data_mode: str = "m1"
    balance: float = 10000
    commission: float = 15
    spread: float = 1
    strategy: Strategy = Strategy.grid
    max_passes: int = 500
    parallel_workers: int = 4
    params: Dict[str, ParamRange] = Field(default_factory=dict)
    fitness: FitnessMetric = FitnessMetric.net_profit


# ── API response models ────────────────────────────────────────────────────

class JobCreateResponse(BaseModel):
    job_id: str
    total_passes: int


class PassResult(BaseModel):
    id: str
    job_id: str
    params: Dict[str, Any]
    status: str
    result: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    name: str
    status: str
    strategy: str
    total_passes: int
    completed_passes: int
    created_at: str
    best_pass_summary: Optional[Dict[str, Any]] = None


class JobDetail(BaseModel):
    id: str
    name: str
    status: str
    strategy: str
    total_passes: int
    completed_passes: int
    created_at: str
    updated_at: str
    config: JobConfig
    top_passes: List[PassResult] = []


class BestPassResponse(BaseModel):
    pass_result: PassResult
    cbotset_params: Dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    docker_available: bool
    queued_jobs: int
    queued_passes: int
    disk_usage_mb: float


def utcnow_iso() -> str:
    """Return current UTC time as ISO‑8601 string."""
    return datetime.now(timezone.utc).isoformat()
