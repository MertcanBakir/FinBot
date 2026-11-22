from typing import List, Literal, Optional

from pydantic import BaseModel, Field

EconomicCalendarRefreshStatus = Literal["success", "partial", "failed"]
EconomicSyncAction = Literal[
    "replaced",
    "merged_and_updated",
    "preserved_previous",
    "initial_partial_written",
    "write_failed",
    "not_written",
    "dry_run"
]

class EconomicEventChangeDetail(BaseModel):
    name: str
    country: str
    change_type: Literal["added", "changed", "removed", "carried_forward"]
    provider_id: Optional[str] = None
    event_code: Optional[str] = None
    reference_period: Optional[str] = None
    source_event_id: Optional[str] = None
    old_scheduled_at_istanbul: Optional[str] = None
    new_scheduled_at_istanbul: Optional[str] = None


class EconomicEventDiff(BaseModel):
    previous_event_count: int = 0
    fetched_event_count: int = 0
    final_event_count: int = 0

    added_count: int = 0
    changed_count: int = 0
    removed_count: int = 0
    unchanged_count: int = 0
    aged_out_count: int = 0
    carried_forward_count: int = 0

    details: List[EconomicEventChangeDetail] = Field(default_factory=list)


class EconomicProviderRefreshOutcome(BaseModel):
    provider_id: str
    provider_name: str
    status: Literal["success", "partial", "failed"]
    structure_validated: bool = False
    event_count: int = 0
    carried_forward_count: int = 0
    observed_candidate_count: int = 0
    candidate_count: int = 0
    malformed_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class EconomicCalendarRefreshJobResult(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    generated_at: str
    coverage_start: str
    coverage_end: str
    status: EconomicCalendarRefreshStatus
    sync_action: EconomicSyncAction
    state_source: str

    diff: EconomicEventDiff
    provider_outcomes: List[EconomicProviderRefreshOutcome] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    email_sent: bool = False
    email_status: Literal[
        "sent",
        "failed",
        "skipped_not_configured",
        "skipped_dry_run",
        "skipped_no_email"
    ] = "skipped_not_configured"
    email_error: Optional[str] = None

    database_status: Literal[
        "success",
        "failed",
        "skipped_not_configured",
        "skipped_dry_run",
    ] = "skipped_not_configured"
    database_error: Optional[str] = None
