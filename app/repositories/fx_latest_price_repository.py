from decimal import Decimal
from typing import Optional
from uuid import UUID

from psycopg import Connection

from app.models.fx_prices import FxPrice


class FxLatestPriceRepository:
    @staticmethod
    def get_instrument(conn: Connection) -> Optional[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, currency, is_active
                FROM public.instruments
                WHERE market = 'FX' AND symbol = 'USDTRY' AND asset_type = 'fx';
                """
            )
            rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return {"id": UUID(str(rows[0][0])), "currency": rows[0][1], "is_active": bool(rows[0][2])}

    @staticmethod
    def get_stored(conn: Connection, instrument_id: UUID) -> Optional[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT price, previous_close, change_amount, change_percent,
                       price_at, provider_code, is_stale, stale_reason
                FROM public.instrument_latest_prices WHERE instrument_id = %s;
                """,
                (instrument_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "price": Decimal(str(row[0])),
            "previous_close": Decimal(str(row[1])) if row[1] is not None else None,
            "change_amount": Decimal(str(row[2])) if row[2] is not None else None,
            "change_percent": Decimal(str(row[3])) if row[3] is not None else None,
            "price_at": row[4], "provider_code": row[5],
            "is_stale": bool(row[6]), "stale_reason": row[7],
        }

    @staticmethod
    def is_unchanged(stored: Optional[dict], item: FxPrice) -> bool:
        return bool(stored) and all((
            stored["price"] == item.price,
            stored["previous_close"] == item.previous_close,
            stored["change_amount"] == item.change_amount,
            stored["change_percent"] == item.change_percent,
            stored["price_at"] == item.price_at,
            stored["provider_code"] == item.provider_code,
            stored["is_stale"] is False,
            stored["stale_reason"] is None,
        ))

    @staticmethod
    def upsert(conn: Connection, item: FxPrice) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.instrument_latest_prices (
                    instrument_id, price, currency, previous_close, change_amount,
                    change_percent, price_at, fetched_at, provider_code,
                    is_stale, stale_reason, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s,false,null,now(),now())
                ON CONFLICT (instrument_id) DO UPDATE SET
                    price=excluded.price, currency=excluded.currency,
                    previous_close=excluded.previous_close,
                    change_amount=excluded.change_amount,
                    change_percent=excluded.change_percent,
                    price_at=excluded.price_at, fetched_at=now(),
                    provider_code=excluded.provider_code,
                    is_stale=false, stale_reason=null, updated_at=now()
                WHERE excluded.price_at > instrument_latest_prices.price_at
                   OR (excluded.price_at = instrument_latest_prices.price_at AND (
                       instrument_latest_prices.price IS DISTINCT FROM excluded.price OR
                       instrument_latest_prices.previous_close IS DISTINCT FROM excluded.previous_close OR
                       instrument_latest_prices.change_amount IS DISTINCT FROM excluded.change_amount OR
                       instrument_latest_prices.change_percent IS DISTINCT FROM excluded.change_percent OR
                       instrument_latest_prices.provider_code IS DISTINCT FROM excluded.provider_code OR
                       instrument_latest_prices.is_stale IS DISTINCT FROM false OR
                       instrument_latest_prices.stale_reason IS NOT NULL
                   ));
                """,
                (
                    item.instrument_id, item.price, item.currency,
                    item.previous_close, item.change_amount, item.change_percent,
                    item.price_at, item.provider_code,
                ),
            )
            return max(cur.rowcount, 0)
