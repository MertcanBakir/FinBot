from datetime import date
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class PortfolioSnapshotFailure(BaseModel):
    portfolio_id: Optional[UUID] = None
    code: str
    message: str
    missing_symbols: List[str] = Field(default_factory=list)


class PortfolioSnapshotJobResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    snapshot_date: date
    dry_run: bool
    requested_count: int = 0
    completed_count: int = 0
    partial_count: int = 0
    failed_count: int = 0
    reused_count: int = 0
    missing_snapshot_count: int = 0
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    failures: List[PortfolioSnapshotFailure] = Field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    email_status: Optional[str] = None
    email_error: Optional[str] = None
    provider_request_executed: bool = False
    database_write_executed: bool = False
