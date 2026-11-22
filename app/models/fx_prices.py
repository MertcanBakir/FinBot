from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel


class FxPrice(BaseModel):
    instrument_id: UUID
    price: Decimal
    previous_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_percent: Optional[Decimal] = None
    price_at: datetime
    currency: Literal["TRY"] = "TRY"
    provider_code: Literal["YAHOO_FINANCE"] = "YAHOO_FINANCE"


class FxPriceJobResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    dry_run: bool
    instrument_id: Optional[UUID] = None
    price: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    price_at: Optional[datetime] = None
    database_writes: int = 0
    was_unchanged: bool = False
    error: Optional[str] = None
    email_status: Optional[str] = None
    email_error: Optional[str] = None
    provider_request_executed: bool = False
    database_write_executed: bool = False
