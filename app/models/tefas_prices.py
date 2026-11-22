from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Literal, Optional, Set, Tuple
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

TefasStaleReason = Literal["price_not_published", "instrument_inactive"]
VALID_STALE_REASONS: Set[str] = {"price_not_published", "instrument_inactive"}


class TefasDbInstrument(BaseModel):
    """Represents an existing TEFAS instrument in public.instruments."""

    id: UUID
    symbol: str
    company_name: str
    is_active: bool


class TefasCatalogObservation(BaseModel):
    """Minimal observation of a TEFAS YAT fund presence in crawler output."""

    fund_code: str
    fund_name: str
    fund_kind: Literal["YAT"] = "YAT"


class TefasCatalogObservationResult(BaseModel):
    """Structured outcome of raw TEFAS catalog observations extraction."""

    observed_funds: Dict[str, str] = Field(default_factory=dict)
    conflicting_codes: Set[str] = Field(default_factory=set)
    warnings: List[str] = Field(default_factory=list)
    identity_invalid_count: int = 0
    identity_complete: bool = True

    def __iter__(self) -> Iterator[Any]:
        """Support legacy unpacking: observed_funds, conflicting_codes, warnings."""
        return iter((self.observed_funds, self.conflicting_codes, self.warnings))


class TefasCatalogReconciliationPlan(BaseModel):
    """Calculated reconciliation plan between TEFAS observations and instruments table."""

    observed_count: int = 0
    to_add_active: List[TefasCatalogObservation] = Field(default_factory=list)
    to_add_inactive: List[TefasCatalogObservation] = Field(default_factory=list)
    to_reactivate_ids: List[UUID] = Field(default_factory=list)
    to_reactivate_symbols: List[str] = Field(default_factory=list)
    to_update_names: List[Tuple[UUID, str, str]] = Field(default_factory=list)  # (id, symbol, new_name)
    to_deactivate_ids: List[UUID] = Field(default_factory=list)
    to_deactivate_symbols: List[str] = Field(default_factory=list)
    unpriced_deactivated_ids: List[UUID] = Field(default_factory=list)
    unpriced_deactivated_symbols: List[str] = Field(default_factory=list)
    disappeared_deactivated_ids: List[UUID] = Field(default_factory=list)
    disappeared_deactivated_symbols: List[str] = Field(default_factory=list)
    ambiguous_codes: List[str] = Field(default_factory=list)
    conflicting_codes: List[str] = Field(default_factory=list)
    disappearance_reconciliation_skipped: bool = False
    disappearance_skip_reason: Optional[str] = None

    @property
    def to_add(self) -> List[TefasCatalogObservation]:
        """All funds to add (active and inactive combined)."""
        return self.to_add_active + self.to_add_inactive

    @model_validator(mode="after")
    def validate_no_overlap_in_add(self) -> "TefasCatalogReconciliationPlan":
        active_codes = {item.fund_code for item in self.to_add_active}
        for item in self.to_add_inactive:
            if item.fund_code in active_codes:
                raise ValueError(
                    f"Fund code '{item.fund_code}' cannot be present in both to_add_active and to_add_inactive."
                )
        return self


class TefasNormalizedPrice(BaseModel):
    """Normalized single investment fund price entry from TEFAS."""

    fund_code: str
    fund_name: str
    fund_kind: Literal["YAT"] = "YAT"
    price: Decimal
    price_date: date
    currency: Literal["TRY"] = "TRY"
    source: Literal["TEFAS"] = "TEFAS"

    @field_validator("fund_code")
    @classmethod
    def validate_fund_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("fund_code must not be empty")
        return normalized

    @field_validator("fund_name")
    @classmethod
    def validate_fund_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("fund_name must not be empty")
        return normalized

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("price must be finite and positive")
        return value


class TefasNormalizationResult(BaseModel):
    """Structured outcome of DataFrame normalization and 3-way price classification."""

    valid_prices: List[TefasNormalizedPrice] = Field(default_factory=list)
    positive_price_codes: Set[str] = Field(default_factory=set)
    clean_nonpositive_price_codes: Set[str] = Field(default_factory=set)
    ambiguous_or_invalid_price_codes: Set[str] = Field(default_factory=set)
    warnings: List[str] = Field(default_factory=list)
    invalid_count: int = 0
    duplicate_count: int = 0

    def __iter__(self) -> Iterator[Any]:
        """Support unpacking: valid_prices, warnings, invalid_count, duplicate_count."""
        return iter((self.valid_prices, self.warnings, self.invalid_count, self.duplicate_count))


class TefasPriceSyncItem(BaseModel):
    """Item ready to be upserted into public.instrument_latest_prices."""

    instrument_id: UUID
    symbol: str
    price: Decimal
    currency: str = "TRY"
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    provider_code: str = "TEFAS"
    is_stale: bool = False
    stale_reason: Optional[TefasStaleReason] = None


class TefasPricesJobResult(BaseModel):
    """Structured execution outcome of the daily TEFAS latest prices job."""

    status: Literal["success", "partial", "failed"]
    started_at: str
    finished_at: str
    duration_seconds: float
    tefas_data_date: Optional[str] = None
    previous_data_date: Optional[str] = None
    fetched_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    duplicate_count: int = 0
    matched_count: int = 0
    unmatched_count: int = 0
    missing_price_count: int = 0
    previous_price_matched_count: int = 0
    previous_price_missing_count: int = 0
    inserted_or_updated_count: int = 0
    unchanged_count: int = 0
    stale_skipped_count: int = 0
    price_not_published_stale_count: int = 0
    instrument_inactive_stale_count: int = 0
    catalog_observed_count: int = 0
    catalog_added_count: int = 0
    catalog_active_added_count: int = 0
    catalog_unpriced_added_count: int = 0
    catalog_name_updated_count: int = 0
    catalog_reactivated_count: int = 0
    catalog_deactivated_count: int = 0
    catalog_unpriced_deactivated_count: int = 0
    catalog_disappeared_deactivated_count: int = 0
    catalog_identity_invalid_count: int = 0
    catalog_disappearance_reconciliation_skipped: bool = False
    catalog_disappearance_skip_reason: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    email_status: Optional[str] = None
    email_error: Optional[str] = None
    dry_run: bool = False
    provider_request_executed: bool = False
    database_write_executed: bool = False
