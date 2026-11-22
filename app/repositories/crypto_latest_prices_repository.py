from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from psycopg import Connection

from app.models.crypto_prices import (
    VALID_CRYPTO_STALE_REASONS,
    CryptoDbInstrument,
    CryptoPriceSyncItem,
    CryptoStaleReason,
)
from app.services.crypto_prices_service import plan_crypto_price_upserts


class CryptoLatestPricesRepository:
    """Transactional, idempotent database operations for crypto instruments & latest prices."""

    @staticmethod
    def get_all_crypto_instruments(conn: Connection) -> List[CryptoDbInstrument]:
        """Fetch all CRYPTO instruments from public.instruments."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, company_name, currency, is_active
                FROM instruments
                WHERE market = 'CRYPTO'
                  AND asset_type = 'crypto'
                ORDER BY symbol;
                """
            )
            rows = cur.fetchall()
            return [
                CryptoDbInstrument(
                    id=UUID(str(row[0])),
                    symbol=row[1].strip().upper(),
                    company_name=str(row[2]),
                    currency=str(row[3]),
                    is_active=bool(row[4]),
                )
                for row in rows
            ]

    @staticmethod
    def get_stored_latest_prices(
        conn: Connection,
        instrument_ids: Optional[Sequence[UUID]] = None,
    ) -> Dict[UUID, dict]:
        """Fetch existing rows from public.instrument_latest_prices."""
        query = """
            SELECT
              instrument_id, price, currency, previous_close, change_amount,
              change_percent, price_at, provider_code, is_stale, stale_reason
            FROM instrument_latest_prices
        """
        params = []
        if instrument_ids is not None:
            query += " WHERE instrument_id = ANY(%s);"
            params.append(list(instrument_ids))
        else:
            query += ";"

        with conn.cursor() as cur:
            cur.execute(query, params if params else None)
            stored_rows = cur.fetchall()

        stored_map: Dict[UUID, dict] = {}
        for row in stored_rows:
            stored_map[UUID(str(row[0]))] = {
                "price": Decimal(str(row[1])),
                "currency": str(row[2]),
                "previous_close": Decimal(str(row[3])) if row[3] is not None else None,
                "change_amount": Decimal(str(row[4])) if row[4] is not None else None,
                "change_percent": Decimal(str(row[5])) if row[5] is not None else None,
                "price_at": row[6],
                "provider_code": str(row[7]),
                "is_stale": bool(row[8]),
                "stale_reason": str(row[9]) if row[9] is not None else None,
            }
        return stored_map

    @staticmethod
    def upsert_prices(
        conn: Connection,
        items: Sequence[CryptoPriceSyncItem],
        stored_map: Optional[Dict[UUID, dict]] = None,
    ) -> Tuple[int, int, int]:
        """Perform transactional, idempotent batch upsert of latest crypto prices.

        Returns:
            (inserted_or_updated_count, unchanged_count, stale_skipped_count)
        """
        if not items:
            return 0, 0, 0

        if stored_map is None:
            inst_ids = [item.instrument_id for item in items]
            stored_map = CryptoLatestPricesRepository.get_stored_latest_prices(conn, inst_ids)

        to_upsert, unchanged_count, stale_skipped_count = plan_crypto_price_upserts(stored_map, items)

        if not to_upsert:
            return 0, unchanged_count, stale_skipped_count

        query = """
            INSERT INTO instrument_latest_prices (
              instrument_id, price, currency, previous_close, change_amount,
              change_percent, price_at, fetched_at, provider_code, is_stale,
              stale_reason, created_at, updated_at
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, now(), %s, %s,
              %s, now(), now()
            )
            ON CONFLICT (instrument_id) DO UPDATE
            SET
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
        reason: CryptoStaleReason,
        stored_map: Optional[Dict[UUID, dict]] = None,
    ) -> int:
        """Mark existing price rows as stale for missing or inactive instruments.

        Does NOT create fake rows if no price record existed previously.
        """
        if not instrument_ids:
            return 0

        if reason not in VALID_CRYPTO_STALE_REASONS:
            raise ValueError(f"Invalid stale_reason '{reason}'. Must be one of {VALID_CRYPTO_STALE_REASONS}")

        if stored_map is None:
            stored_map = CryptoLatestPricesRepository.get_stored_latest_prices(conn, instrument_ids)

        # Only target instruments that actually have an existing row in instrument_latest_prices
        # and are not already marked with the exact same stale reason
        candidate_ids = [
            uid for uid in instrument_ids
            if uid in stored_map
            and (stored_map[uid]["is_stale"] is not True or stored_map[uid]["stale_reason"] != reason)
        ]

        if not candidate_ids:
            return 0

        query = """
            UPDATE instrument_latest_prices
            SET
              is_stale = true,
              stale_reason = %s,
              updated_at = now()
            WHERE instrument_id = ANY(%s)
              AND (is_stale IS NOT TRUE OR stale_reason IS DISTINCT FROM %s);
        """

        with conn.cursor() as cur:
            cur.execute(query, (reason, list(candidate_ids), reason))
            return cur.rowcount if cur.rowcount != -1 else len(candidate_ids)
