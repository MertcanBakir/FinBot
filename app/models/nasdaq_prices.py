from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

NasdaqJobStatus = Literal["success", "partial", "failed", "skipped"]
NasdaqStaleReason = Literal["price_not_published", "instrument_inactive"]
NasdaqExecutionWindow = Literal["regular_session", "post_close"]


class NasdaqSessionInfo(BaseModel):
    """NASDAQ market session information for a specific calendar date and execution window."""

    trading_date: date
    is_trading_day: bool
    market_open: Optional[datetime] = None   # Timezone-aware UTC datetime
    market_close: Optional[datetime] = None  # Timezone-aware UTC datetime
    early_close: bool = False
    market_window_start: Optional[datetime] = None  # market_open + 30m (UTC)
    market_window_end: Optional[datetime] = None    # market_close + 6h (UTC)


class NasdaqDbInstrument(BaseModel):
    """Represents a NASDAQ100 stock record in public.instruments.

    Note on schema semantics:
    The public.instruments database table contains columns:
    (id, market, symbol, company_name, currency, is_active, created_at, updated_at, asset_type).
    The Yahoo Finance ticker is derived dynamically at runtime as symbol.strip().upper() (e.g. AAPL -> AAPL).
    """

    id: UUID
    symbol: str
    company_name: str
    currency: str = "USD"
    is_active: bool

    @property
    def yahoo_ticker(self) -> str:
        """Derive standard Yahoo Finance ticker dynamically at runtime (e.g. AAPL -> AAPL)."""
        return self.symbol.strip().upper()


class NasdaqNormalizedPrice(BaseModel):
    """Normalized NASDAQ stock price record parsed from Yahoo Finance."""

    symbol: str
    yahoo_ticker: str
    price: Decimal
    currency: str = "USD"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "YAHOO_FINANCE"


class NasdaqPriceSyncItem(BaseModel):
    """Item ready for upsert into public.instrument_latest_prices."""

    instrument_id: UUID
    symbol: str
    yahoo_ticker: str
    price: Decimal
    currency: str = "USD"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "YAHOO_FINANCE"
    is_stale: bool = False
    stale_reason: Optional[NasdaqStaleReason] = None


class NasdaqPricesJobResult(BaseModel):
    """Complete execution metrics and outcome summary for NASDAQ latest prices sync job."""

    status: NasdaqJobStatus
    skip_reason: Optional[str] = None  # "market_closed", "market_not_ready", "outside_market_window"
    started_at: str
    finished_at: str
    duration_seconds: float = 0.0
    trading_date: Optional[str] = None
    session_open: Optional[str] = None
    session_close: Optional[str] = None
    execution_window: Optional[NasdaqExecutionWindow] = None
    market_window_start: Optional[str] = None
    market_window_end: Optional[str] = None

    # Catalog & Fetch metrics
    requested_active_count: int = 0
    observed_ticker_count: int = 0
    response_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    duplicate_count: int = 0

    # Matching metrics
    matched_count: int = 0
    missing_count: int = 0
    unmatched_count: int = 0
    previous_close_matched_count: int = 0
    previous_close_missing_count: int = 0

    # DB Write metrics
    inserted_or_updated_count: int = 0
    unchanged_count: int = 0
    stale_skipped_count: int = 0
    price_not_published_stale_count: int = 0
    instrument_inactive_stale_count: int = 0

    # Diagnostics & Alerts
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    email_status: Optional[str] = None  # "sent", "failed", or None
    email_error: Optional[str] = None
    dry_run: bool = True
    provider_request_executed: bool = False
    database_write_executed: bool = False
