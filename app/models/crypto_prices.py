from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional, Set
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CryptoStaleReason = Literal["price_not_published", "instrument_inactive"]
VALID_CRYPTO_STALE_REASONS: Set[str] = {"price_not_published", "instrument_inactive"}


class CryptoDbInstrument(BaseModel):
    """Represents a cryptocurrency record in public.instruments.

    Note on schema semantics:
    The public.instruments database table contains columns:
    (id, market, symbol, company_name, currency, is_active, created_at, updated_at, asset_type).
    It does NOT contain a separate source_pair column.
    The Binance trading pair is derived dynamically at runtime as symbol + 'USDT' (e.g. BTC -> BTCUSDT).
    """

    id: UUID
    symbol: str
    company_name: str
    currency: str = "USD"
    is_active: bool

    @property
    def source_pair(self) -> str:
        """Derive standard Binance Spot trading pair dynamically at runtime (e.g. BTC -> BTCUSDT)."""
        return f"{self.symbol.strip().upper()}USDT"


class CryptoNormalizedPrice(BaseModel):
    """Normalized single cryptocurrency 24hr price ticker from Binance."""

    symbol: str
    source_pair: str
    price: Decimal
    previous_close: Decimal  # Rolling 24hr openPrice reference from Binance
    change_amount: Decimal   # lastPrice - openPrice
    change_percent: Decimal  # ((lastPrice - openPrice) / openPrice) * 100
    price_at: datetime       # UTC timestamptz from Binance closeTime
    currency: Literal["USD"] = "USD"
    provider_code: Literal["BINANCE"] = "BINANCE"

    @field_validator("symbol", "source_pair")
    @classmethod
    def non_empty_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be empty")
        return normalized

    @field_validator("price", "previous_close")
    @classmethod
    def validate_positive_prices(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("price and previous_close must be finite and positive")
        return value


class CryptoPriceSyncItem(BaseModel):
    """Item ready to be upserted into public.instrument_latest_prices."""

    instrument_id: UUID
    symbol: str
    source_pair: str
    price: Decimal
    currency: str = "USD"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "BINANCE"
    is_stale: bool = False
    stale_reason: Optional[CryptoStaleReason] = None


class CryptoPricesJobResult(BaseModel):
    """Structured execution outcome of the Binance crypto latest prices job."""

    status: Literal["success", "partial", "failed"]
    started_at: str
    finished_at: str
    duration_seconds: float
    requested_active_count: int = 0
    response_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    matched_count: int = 0
    unmatched_count: int = 0
    duplicate_count: int = 0
    missing_count: int = 0
    inserted_or_updated_count: int = 0
    unchanged_count: int = 0
    stale_skipped_count: int = 0
    price_not_published_stale_count: int = 0
    instrument_inactive_stale_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    email_status: Optional[str] = None
    email_error: Optional[str] = None
    dry_run: bool = False
    provider_request_executed: bool = False
    database_write_executed: bool = False


BinanceCryptoPricesJobResult = CryptoPricesJobResult
