"""Pydantic models for request / response schemas and internal types."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

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


class JobType(str, Enum):
    opti = "opti"
    fsb_search = "fsb_search"


class Strategy(str, Enum):
    grid = "grid"
    random = "random"
    genetic = "genetic"
    export = "export"


class FitnessMetric(str, Enum):
    net_profit = "net_profit"
    sharpe = "sharpe_ratio"
    profit_factor = "profit_factor"
    win_rate = "win_rate"
    average_trade = "average_trade"


class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"


class ConstraintOperator(str, Enum):
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    eq = "eq"


# ── Parameter range ─────────────────────────────────────────────────────────

class ParamRange(BaseModel):
    min: float
    max: float
    step: float


class RankingRule(BaseModel):
    metric: str
    direction: SortDirection = SortDirection.desc


class MetricConstraint(BaseModel):
    metric: str
    operator: ConstraintOperator
    value: float


class ExportChunk(BaseModel):
    symbol: str
    period: str
    start_utc: str
    end_utc: str
    data_mode: str = "m1"
    broker_code: str = "unknown"
    balance: float = 10000
    commission: float = 0
    spread: float = 0
    cbot_params: Dict[str, Any] = Field(default_factory=dict)


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
    fixed_params: Dict[str, Any] = Field(default_factory=dict)
    params: Dict[str, ParamRange] = Field(default_factory=dict)
    fitness: FitnessMetric = FitnessMetric.net_profit
    ranking: List[RankingRule] = Field(default_factory=list)
    constraints: List[MetricConstraint] = Field(default_factory=list)
    chunks: List[ExportChunk] = Field(default_factory=list)


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
    ranking_eligible: Optional[bool] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    generation: Optional[int] = None
    strategy_id: Optional[str] = None
    family: Optional[str] = None
    candidate_status: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    name: str
    status: str
    strategy: str
    job_type: str = JobType.opti.value
    total_passes: int
    completed_passes: int
    created_at: str
    best_pass_summary: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None
    error_detail: Optional[str] = None


class JobDetail(BaseModel):
    id: str
    name: str
    status: str
    strategy: str
    job_type: str = JobType.opti.value
    total_passes: int
    completed_passes: int
    created_at: str
    updated_at: str
    config: Dict[str, Any]
    top_passes: List[PassResult] = Field(default_factory=list)
    progress: Optional[Dict[str, Any]] = None
    error_detail: Optional[str] = None


class BestPassResponse(BaseModel):
    pass_result: PassResult
    cbotset_params: Dict[str, Any]


class ImportCompletedExportsRequest(BaseModel):
    delete_artifacts: bool = False
    pass_ids: Optional[List[str]] = None


class ImportCompletedExportsResult(BaseModel):
    pass_id: str
    status: str
    detail: str


class ImportCompletedExportsResponse(BaseModel):
    job_id: str
    discovered: int
    imported: int
    skipped: int
    quarantined: int
    failed: int
    results: List[ImportCompletedExportsResult] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    docker_available: bool
    queued_jobs: int
    queued_passes: int
    disk_usage_mb: float
    fsb_ready: bool = False


def utcnow_iso() -> str:
    """Return current UTC time as ISO‑8601 string."""
    return datetime.now(timezone.utc).isoformat()
