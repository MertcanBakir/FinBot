from datetime import date
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

EarningsCalendarStatus = Literal["success", "partial", "failed"]
FiscalPeriod = Literal["Q1", "Q2", "Q3", "FY"]

class BistEarningsEvent(BaseModel):
    symbol: str
    company_name: str
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[FiscalPeriod] = None
    earnings_date: str  # YYYY-MM-DD (date only)
    period_start: Optional[str] = None  # YYYY-MM-DD
    period_end: Optional[str] = None    # YYYY-MM-DD
    publish_date: Optional[str] = None  # YYYY-MM-DD
    date_type: str = "company_announced"
    source: str = "KAP"
    source_provider: Optional[str] = None
    source_type: str = "financial_calendar"
    source_event_id: Optional[str] = None
    disclosure_index: Optional[int] = None
    source_url: Optional[str] = None

    @model_validator(mode="after")
    def normalize_source_provider(self) -> "BistEarningsEvent":
        if self.source == "KAP" and not self.source_provider:
            self.source_provider = "KAP Financial Calendar"
        return self


class KapSymbolEarningsStatus(BaseModel):
    symbol: str
    member_resolution_status: str = "resolved"  # resolved / unresolved / ambiguous
    refresh_status: str = "success"            # success / partial / failed
    calendar_status: str = "no_announced_date_in_coverage"  # scheduled / no_announced_date_in_coverage / unavailable
    scheduled_event_count: int = 0
    is_complete: bool = True
    warnings: List[str] = Field(default_factory=list)

class KapCalendarParseResult(BaseModel):
    rows: List[Dict[str, Optional[str]]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    parser_failed: bool = False
    positional_fallback_count: int = 0

class KapEarningsFetchMetrics(BaseModel):
    input_symbol_count: int = 0
    resolved_symbol_count: int = 0
    unresolved_symbol_count: int = 0

    search_request_count: int = 0
    search_retry_count: int = 0
    search_failure_count: int = 0

    adaptive_leaf_count: int = 0
    saturated_leaf_count: int = 0
    max_leaf_row_count: int = 0

    raw_disclosure_count: int = 0
    unique_disclosure_count: int = 0
    duplicate_disclosure_count: int = 0
    financial_calendar_disclosure_count: int = 0
    fallback_subject_count: int = 0

    detail_request_count: int = 0
    detail_retry_count: int = 0
    detail_failure_count: int = 0

    parser_failure_count: int = 0
    mapping_failure_count: int = 0
    unmappable_disclosure_count: int = 0
    positional_fallback_count: int = 0

    calendar_row_count: int = 0
    valid_planned_date_count: int = 0
    invalid_planned_date_count: int = 0
    superseded_event_count: int = 0
    normalized_event_count: int = 0
    final_event_count: int = 0

class KapEarningsFetchResult(BaseModel):
    status: str = "success"  # success, partial, failed
    events: List[BistEarningsEvent] = Field(default_factory=list)
    normalized_events: List[BistEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    resolved_symbols: List[str] = Field(default_factory=list)
    unresolved_symbols: List[str] = Field(default_factory=list)
    symbol_results: List[KapSymbolEarningsStatus] = Field(default_factory=list)
    metrics: KapEarningsFetchMetrics = Field(default_factory=KapEarningsFetchMetrics)

OfficialIrIssueKind = Literal[
    "provider_failure",
    "schema_failure",
    "row_validation",
    "informational",
    "conflict",
]

class OfficialIrIssue(BaseModel):
    message: str
    kind: OfficialIrIssueKind
    earnings_date: Optional[Union[str, date]] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[FiscalPeriod] = None
    is_global: bool = False

class OfficialIrProviderResult(BaseModel):
    symbol: str
    source_provider: str
    status: Literal["success", "partial", "failed"] = "success"
    events: List[BistEarningsEvent] = Field(default_factory=list)
    rows_seen: int = 0
    accepted_row_count: int = 0
    rejected_row_count: int = 0
    parser_failed: bool = False
    retry_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    issues: List[OfficialIrIssue] = Field(default_factory=list)

class BistCombinedEarningsResult(BaseModel):
    status: EarningsCalendarStatus = "success"
    rolling_events: List[BistEarningsEvent] = Field(default_factory=list)
    normalized_events: List[BistEarningsEvent] = Field(default_factory=list)
    kap_fetch_result: KapEarningsFetchResult
    official_ir_results: List[OfficialIrProviderResult] = Field(default_factory=list)
    ir_incomplete_symbols: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class BistEarningsCalendar(BaseModel):
    schema_version: str = "1.0"
    generated_at: str
    week_start: str  # YYYY-MM-DD
    week_end: str    # YYYY-MM-DD
    market: str = "BIST100"
    status: str = "success"  # success, partial, failed
    provider_status: str = "success"
    events: List[BistEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

NasdaqSession = Literal["pre_market", "time_not_supplied", "after_hours"]

class NasdaqEarningsEvent(BaseModel):
    symbol: str
    company_name: str
    earnings_date: str  # YYYY-MM-DD
    session: NasdaqSession  # pre_market, after_hours, time_not_supplied
    session_label: str  # Pre-Market, After Hours, Time Not Supplied
    source: str = "Yahoo Finance"

class NasdaqEarningsCalendar(BaseModel):
    schema_version: str = "1.0"
    generated_at: str
    week_start: str  # YYYY-MM-DD
    week_end: str    # YYYY-MM-DD
    market: str = "NASDAQ100"
    status: str = "success"  # success, partial, failed
    provider_status: str = "success"
    events: List[NasdaqEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

class Bist100EarningsCalendar(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    generated_at: str
    coverage_start: str  # YYYY-MM-DD
    coverage_end: str    # YYYY-MM-DD
    market: Literal["BIST100"] = "BIST100"
    status: EarningsCalendarStatus = "success"
    provider_status: EarningsCalendarStatus = "success"
    events: List[BistEarningsEvent] = Field(default_factory=list)
    symbol_results: List[KapSymbolEarningsStatus] = Field(default_factory=list)
    ir_incomplete_symbols: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    metrics: KapEarningsFetchMetrics = Field(default_factory=KapEarningsFetchMetrics)

class Nasdaq100EarningsCalendar(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    generated_at: str
    coverage_start: str  # YYYY-MM-DD
    coverage_end: str    # YYYY-MM-DD
    market: Literal["NASDAQ100"] = "NASDAQ100"
    status: EarningsCalendarStatus = "success"
    provider_status: EarningsCalendarStatus = "success"
    events: List[NasdaqEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

class BistEarningsApiResponse(BaseModel):
    schema_version: str = "2.0"
    generated_at: str
    coverage_start: str
    coverage_end: str
    requested_start: str
    requested_end: str
    effective_start: str
    effective_end: str
    is_complete: bool
    timezone: str
    market: Literal["BIST100"] = "BIST100"
    status: EarningsCalendarStatus
    provider_status: EarningsCalendarStatus
    stale: bool
    events: List[BistEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    date: Optional[str] = None
    week_start: Optional[str] = None
    week_end: Optional[str] = None
    month_start: Optional[str] = None
    month_end: Optional[str] = None


class NasdaqEarningsApiResponse(BaseModel):
    schema_version: str = "2.0"
    generated_at: str
    coverage_start: str
    coverage_end: str
    requested_start: str
    requested_end: str
    effective_start: str
    effective_end: str
    is_complete: bool
    timezone: str
    market: Literal["NASDAQ100"] = "NASDAQ100"
    status: EarningsCalendarStatus
    provider_status: EarningsCalendarStatus
    stale: bool
    events: List[NasdaqEarningsEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    date: Optional[str] = None
    week_start: Optional[str] = None
    week_end: Optional[str] = None
    month_start: Optional[str] = None
    month_end: Optional[str] = None


class CombinedEarningsApiResponse(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    view: Literal["today", "week", "month", "range"]
    timezone: str
    requested_start: str
    requested_end: str
    status: EarningsCalendarStatus
    stale: bool
    is_complete: bool
    bist100: Optional[BistEarningsApiResponse] = None
    nasdaq100: Optional[NasdaqEarningsApiResponse] = None
    market_errors: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
