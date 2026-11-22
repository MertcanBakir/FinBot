from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

BistJobStatus = Literal["success", "partial", "failed", "skipped"]
BistStaleReason = Literal["price_not_published", "instrument_inactive"]


class XistSessionInfo(BaseModel):
    """Borsa Istanbul (XIST) session information for a specific calendar date."""

    trading_date: date
    is_trading_day: bool
    market_open: Optional[datetime] = None   # Timezone-aware UTC or Istanbul datetime
    market_close: Optional[datetime] = None  # Timezone-aware UTC or Istanbul datetime


class BistDbInstrument(BaseModel):
    """Represents a BIST100 stock record in public.instruments.

    Note on schema semantics:
    The public.instruments database table contains columns:
    (id, market, symbol, company_name, currency, is_active, created_at, updated_at, asset_type).
    It does NOT contain a separate yahoo_ticker column.
    The Yahoo Finance ticker is derived dynamically at runtime as symbol + '.IS' (e.g. THYAO -> THYAO.IS).
    """

    id: UUID
    symbol: str
    company_name: str
    currency: str = "TRY"
    is_active: bool

    @property
    def yahoo_ticker(self) -> str:
        """Derive standard Yahoo Finance ticker dynamically at runtime (e.g. THYAO -> THYAO.IS)."""
        return f"{self.symbol.strip().upper()}.IS"


class BistNormalizedPrice(BaseModel):
    """Normalized BIST stock price record parsed from Yahoo Finance."""

    symbol: str
    yahoo_ticker: str
    price: Decimal
    currency: str = "TRY"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "YAHOO_FINANCE"


class BistPriceSyncItem(BaseModel):
    """Item ready for upsert into public.instrument_latest_prices."""

    instrument_id: UUID
    symbol: str
    yahoo_ticker: str
    price: Decimal
    currency: str = "TRY"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "YAHOO_FINANCE"
    is_stale: bool = False
    stale_reason: Optional[BistStaleReason] = None


class BistPricesJobResult(BaseModel):
    """Complete execution metrics and outcome summary for BIST latest prices sync job."""

    status: BistJobStatus
    skip_reason: Optional[str] = None  # "market_closed"
    started_at: str
    finished_at: str
    duration_seconds: float = 0.0
    trading_date: Optional[str] = None

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
