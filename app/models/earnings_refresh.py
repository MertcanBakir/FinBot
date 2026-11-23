from typing import List, Literal, Optional

from pydantic import BaseModel, Field

RefreshStatus = Literal["success", "partial", "failed"]

SyncAction = Literal[
    "replaced",
    "merged_and_updated",
    "initial_partial_written",
    "preserved_previous",
    "write_failed",
    "not_written",
]

EmailDeliveryStatus = Literal[
    "pending",
    "sent",
    "skipped_not_configured",
    "skipped_no_email",
    "skipped_dry_run",
    "failed",
]


class NasdaqScheduleSnapshot(BaseModel):
    earnings_date: str
    session: Optional[str] = None


class EarningsEventChangeDetail(BaseModel):
    market: Literal["BIST100", "NASDAQ100"]
    change_type: Literal["added", "changed", "removed", "aged_out", "carried_forward"]
    symbol: str

    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None

    old_date: Optional[str] = None
    new_date: Optional[str] = None

    old_session: Optional[str] = None
    new_session: Optional[str] = None
    session: Optional[str] = None
    session_label: Optional[str] = None
    source: Optional[str] = None

    old_schedules: List[NasdaqScheduleSnapshot] = Field(default_factory=list)
    new_schedules: List[NasdaqScheduleSnapshot] = Field(default_factory=list)


class EarningsEventDiff(BaseModel):
    previous_event_count: int = 0
    fetched_event_count: int = 0
    final_event_count: int = 0

    added_count: int = 0
    removed_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    aged_out_count: int = 0
    carried_forward_count: int = 0

    details: List[EarningsEventChangeDetail] = Field(default_factory=list)
    added_details: List[str] = Field(default_factory=list)
    removed_details: List[str] = Field(default_factory=list)
    changed_details: List[str] = Field(default_factory=list)
    aged_out_details: List[str] = Field(default_factory=list)
    carried_forward_symbols: List[str] = Field(default_factory=list)


class OfficialIrProviderSummary(BaseModel):
    symbol: str
    source_provider: str
    status: RefreshStatus = "success"
    rows_seen: int = 0
    accepted_row_count: int = 0
    rejected_row_count: int = 0
    parser_failed: bool = False
    retry_count: int = 0
    events_count: int = 0
    warnings: List[str] = Field(default_factory=list)


class OfficialIrRefreshSummary(BaseModel):
    providers: List[OfficialIrProviderSummary] = Field(default_factory=list)
    events_added_from_ir_count: int = 0
    events_overridden_by_kap_count: int = 0
    kap_ir_conflicts_count: int = 0
    ir_dates_changed_count: int = 0
    kap_fetched_event_count: int = 0
    ir_fetched_event_count: int = 0

    # Structured scoping fields
    kap_fetch_status: Optional[RefreshStatus] = None
    kap_normalized_event_count: int = 0
    kap_rolling_event_count: int = 0
    ir_provider_raw_event_count: int = 0
    ir_normalized_event_count: int = 0
    ir_rolling_event_count: int = 0
    combined_rolling_event_count: int = 0
    ir_carried_forward_count: int = 0
    kap_warnings: List[str] = Field(default_factory=list)


class MarketRefreshOutcome(BaseModel):
    market: Literal["BIST100", "NASDAQ100"]
    outcome_status: RefreshStatus
    fetch_status: RefreshStatus

    sync_action: SyncAction
    state_updated: bool = False
    state_preserved: bool = False

    coverage_start: str
    coverage_end: str

    total_symbol_count: Optional[int] = None
    completed_symbol_count: Optional[int] = None
    unavailable_symbol_count: Optional[int] = None
    scheduled_symbol_count: Optional[int] = None
    healthy_negative_symbol_count: Optional[int] = None

    diff: EarningsEventDiff = Field(default_factory=EarningsEventDiff)
    official_ir_summary: Optional[OfficialIrRefreshSummary] = None
    unavailable_symbols: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class EarningsRefreshJobResult(BaseModel):
    started_at: str
    finished_at: str
    duration_seconds: float

    coverage_start: str
    coverage_end: str
    overall_status: RefreshStatus

    bist: MarketRefreshOutcome
    nasdaq: MarketRefreshOutcome

    email_status: EmailDeliveryStatus = "pending"
    email_error: Optional[str] = None

    database_status: Literal[
        "success",
        "failed",
        "skipped_not_configured",
        "skipped_dry_run",
    ] = "skipped_not_configured"
    database_error: Optional[str] = None
