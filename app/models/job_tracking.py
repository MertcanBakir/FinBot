from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["running", "success", "partial", "failed", "skipped"]
TerminalJobStatus = Literal["success", "partial", "failed", "skipped"]
ProviderTerminalStatus = Literal["success", "partial", "failed"]

ProviderScope = Literal[
    "BIST100",
    "NASDAQ100",
    "ECONOMIC",
    "EARNINGS",
    "CRYPTO",
    "TEFAS",
    "FX",
    "PORTFOLIO",
]

NotificationStatus = Literal[
    "not_required",
    "pending",
    "sent",
    "failed",
    "disabled",
    "not_configured",
    "skipped_dry_run",
]

SyncAction = Literal[
    "updated",
    "partial_update",
    "no_op",
    "skipped",
    "failed_no_write",
    "dry_run",
    "replaced",
    "merged_and_updated",
    "initial_partial_written",
    "preserved_previous",
    "write_failed",
    "not_written",
]

TriggerType = Literal["scheduled", "manual"]

OperationType = Literal[
    "prices_sync",
    "calendar_refresh",
    "portfolio_snapshot",
    "notification_dispatch",
]


class JobTrackingError(Exception):
    """Base exception for job tracking failures."""


class DuplicateJobRunError(JobTrackingError):
    """Raised when a job with identical (job_name, github_run_id, github_run_attempt) already exists."""


class JobTrackingStartError(JobTrackingError):
    """Raised when starting a tracked run fails in Tx A."""


class JobTrackingFinalizeError(JobTrackingError):
    """Raised when finalizing a tracked run fails in Tx C."""


class JobTrackingNotificationError(JobTrackingError):
    """Raised when persisting notification status fails in Tx D."""


class JobRunSpecification(BaseModel):
    """Static metadata identifying a specific job type and operational scope."""

    model_config = ConfigDict(frozen=True)

    job_name: str
    operation_type: OperationType
    scope: Optional[str] = None
    provider_code: Optional[str] = None


class JobRunContext(BaseModel):
    """Runtime execution environment and triggering context for a job run."""

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    trigger_type: TriggerType = "scheduled"
    github_run_id: Optional[str] = None
    github_run_attempt: Optional[int] = Field(default=None, gt=0)
    github_workflow: Optional[str] = None
    github_sha: Optional[str] = None
    github_ref: Optional[str] = None
    dry_run: bool = False
    no_email: bool = False


class JobExecutionContext(BaseModel):
    """Typed execution context passed to business logic containing run_id and parameters."""

    model_config = ConfigDict(frozen=True)

    run_id: Optional[UUID] = None
    dry_run: bool = False
    no_email: bool = False
    started_at: datetime


class ProviderRunOutcome(BaseModel):
    """Execution telemetry for an individual provider in multi-provider jobs."""

    model_config = ConfigDict(frozen=True)

    provider_code: str
    scope: ProviderScope
    status: ProviderTerminalStatus
    structure_validated: Optional[bool] = None
    observed_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    fresh_event_count: int = Field(default=0, ge=0)
    carried_forward_count: int = Field(default=0, ge=0)
    malformed_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)


class JobRunOutcome(BaseModel):
    """Standardized outcome data produced by a job execution."""

    model_config = ConfigDict(frozen=True)

    status: TerminalJobStatus
    sync_action: Optional[SyncAction] = None
    skip_reason: Optional[str] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = Field(default=None, ge=0.0)
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    fresh_event_count: Optional[int] = Field(default=None, ge=0)
    final_event_count: Optional[int] = Field(default=None, ge=0)
    warnings: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    provider_outcomes: List[ProviderRunOutcome] = Field(default_factory=list)
