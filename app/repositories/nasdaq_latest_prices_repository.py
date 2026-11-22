import logging
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from psycopg import Connection

from app.models.nasdaq_prices import NasdaqDbInstrument, NasdaqPriceSyncItem
from app.services.nasdaq_prices_service import plan_nasdaq_price_upserts

logger = logging.getLogger(__name__)

VALID_NASDAQ_STALE_REASONS = {"price_not_published", "instrument_inactive"}


class NasdaqLatestPricesRepository:
    """Repository for reading NASDAQ instruments and atomically managing NASDAQ latest prices in Supabase."""

    @staticmethod
    def get_all_nasdaq_instruments(conn: Connection) -> List[NasdaqDbInstrument]:
        """Read all NASDAQ100 stock instruments from public.instruments ordered deterministically by symbol."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, company_name, currency, is_active
                FROM public.instruments
                WHERE market = 'NASDAQ100' AND asset_type = 'stock'
                ORDER BY symbol;
                """
            )
            rows = cur.fetchall()

        instruments: List[NasdaqDbInstrument] = []
        for r in rows:
            instruments.append(
                NasdaqDbInstrument(
                    id=r[0],
                    symbol=r[1],
                    company_name=r[2],
                    currency=r[3] or "USD",
                    is_active=bool(r[4]),
                )
            )
        return instruments

    @staticmethod
    def get_stored_latest_prices(
        conn: Connection,
        instrument_ids: Optional[Sequence[UUID]] = None,
    ) -> Dict[UUID, dict]:
        """Read existing latest prices from public.instrument_latest_prices.

        Optionally restricted to the given list of instrument IDs to prevent loading unrelated market rows.
        """
        query = """
            SELECT instrument_id, price, currency, previous_close, change_amount,
                   change_percent, price_at, provider_code, is_stale, stale_reason
            FROM public.instrument_latest_prices
        """
        params = []
        if instrument_ids:
            query += " WHERE instrument_id = ANY(%s);"
            params.append(list(instrument_ids))
        else:
            query += ";"

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        stored_map: Dict[UUID, dict] = {}
        for r in rows:
            inst_id = r[0] if isinstance(r[0], UUID) else UUID(str(r[0]))
            stored_map[inst_id] = {
                "price": Decimal(str(r[1])) if r[1] is not None else None,
                "currency": str(r[2]),
                "previous_close": Decimal(str(r[3])) if r[3] is not None else None,
                "change_amount": Decimal(str(r[4])) if r[4] is not None else None,
                "change_percent": Decimal(str(r[5])) if r[5] is not None else None,
                "price_at": r[6],
                "provider_code": str(r[7]),
                "is_stale": bool(r[8]) if r[8] is not None else False,
                "stale_reason": str(r[9]) if r[9] is not None else None,
            }
        return stored_map

    @staticmethod
    def upsert_prices(
        conn: Connection,
        items: Sequence[NasdaqPriceSyncItem],
        stored_map: Optional[Dict[UUID, dict]] = None,
    ) -> Tuple[int, int, int]:
        """Idempotently batch upsert valid NASDAQ stock prices into public.instrument_latest_prices.

        Returns:
            (inserted_or_updated_count, unchanged_count, stale_skipped_count)
        """
        if not items:
            return 0, 0, 0

        if stored_map is None:
            inst_ids = [item.instrument_id for item in items]
            stored_map = NasdaqLatestPricesRepository.get_stored_latest_prices(conn, instrument_ids=inst_ids)

        to_upsert, unchanged_count, stale_skipped_count = plan_nasdaq_price_upserts(stored_map, items)

        if not to_upsert:
            return 0, unchanged_count, stale_skipped_count

        query = """
            INSERT INTO public.instrument_latest_prices (
                instrument_id, price, currency, previous_close, change_amount,
                change_percent, price_at, fetched_at, provider_code, is_stale,
                stale_reason, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, now(), %s, %s,
                %s, now(), now()
            )
            ON CONFLICT (instrument_id) DO UPDATE SET
                price = EXCLUDED.price,
                currency = EXCLUDED.currency,
                previous_close = EXCLUDED.previous_close,
                change_amount = EXCLUDED.change_amount,
                change_percent = EXCLUDED.change_percent,
                price_at = EXCLUDED.price_at,
                fetched_at = EXCLUDED.fetched_at,
                provider_code = EXCLUDED.provider_code,
                is_stale = EXCLUDED.is_stale,
                stale_reason = EXCLUDED.stale_reason,
                updated_at = now()
            WHERE
                EXCLUDED.price_at > instrument_latest_prices.price_at
                OR (
                    EXCLUDED.price_at = instrument_latest_prices.price_at
                    AND (
                        instrument_latest_prices.price IS DISTINCT FROM EXCLUDED.price
                        OR instrument_latest_prices.previous_close IS DISTINCT FROM EXCLUDED.previous_close
                        OR instrument_latest_prices.change_amount IS DISTINCT FROM EXCLUDED.change_amount
                        OR instrument_latest_prices.change_percent IS DISTINCT FROM EXCLUDED.change_percent
                        OR instrument_latest_prices.currency IS DISTINCT FROM EXCLUDED.currency
                        OR instrument_latest_prices.provider_code IS DISTINCT FROM EXCLUDED.provider_code
                        OR instrument_latest_prices.is_stale IS DISTINCT FROM EXCLUDED.is_stale
                        OR instrument_latest_prices.stale_reason IS DISTINCT FROM EXCLUDED.stale_reason
                    )
                );
        """

        params = [
            (
                item.instrument_id,
                item.price,
                item.currency,
                item.previous_close,
                item.change_amount,
                item.change_percent,
                item.price_at,
                item.provider_code,
                item.is_stale,
                item.stale_reason,
            )
            for item in to_upsert
        ]

        with conn.cursor() as cur:
            cur.executemany(query, params)
            inserted_or_updated = cur.rowcount if cur.rowcount != -1 else len(to_upsert)

        return inserted_or_updated, unchanged_count, stale_skipped_count

    @staticmethod
    def mark_stale_prices(
        conn: Connection,
        instrument_ids: Sequence[UUID],
        reason: str,
        stored_map: Optional[Dict[UUID, dict]] = None,
    ) -> int:
        """Mark specified instruments as stale with an explicit reason.

        Enforces:
        - Only permitted reasons: 'price_not_published', 'instrument_inactive'.
        - Updates ONLY existing rows in public.instrument_latest_prices; never inserts fake empty rows.
        - Skips no-op updates if already stale with the exact same reason.
        """
        if not instrument_ids:
            return 0

        if reason not in VALID_NASDAQ_STALE_REASONS:
            raise ValueError(f"Invalid stale_reason '{reason}'. Must be one of {VALID_NASDAQ_STALE_REASONS}")

        if stored_map is not None:
            target_ids = [uid for uid in instrument_ids if uid in stored_map]
        else:
            target_ids = list(instrument_ids)

        if not target_ids:
            return 0

        query = """
            UPDATE public.instrument_latest_prices
            SET is_stale = true,
                stale_reason = %s,
                updated_at = now()
            WHERE instrument_id = ANY(%s)
              AND (is_stale IS NOT TRUE OR stale_reason IS DISTINCT FROM %s);
        """

        with conn.cursor() as cur:
            cur.execute(query, (reason, target_ids, reason))
            updated_count = cur.rowcount if cur.rowcount != -1 else len(target_ids)

        return updated_count
