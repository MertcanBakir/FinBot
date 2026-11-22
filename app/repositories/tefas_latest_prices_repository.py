from decimal import Decimal
from typing import Dict, Optional, Sequence, Tuple
from uuid import UUID

from psycopg import Connection

from app.models.tefas_prices import (
    VALID_STALE_REASONS,
    TefasCatalogReconciliationPlan,
    TefasDbInstrument,
    TefasPriceSyncItem,
)
from app.services.tefas_prices_service import plan_price_upserts


class TefasLatestPricesRepository:
    """Transactional, idempotent database operations for TEFAS catalog & latest prices."""

    @staticmethod
    def get_all_tefas_instruments(conn: Connection) -> Dict[str, TefasDbInstrument]:
        """Fetch all TEFAS investment funds (active and inactive) from public.instruments."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, company_name, is_active
                FROM instruments
                WHERE market = 'TEFAS'
                  AND asset_type = 'fund';
                """
            )
            rows = cur.fetchall()
            return {
                row[1].strip().upper(): TefasDbInstrument(
                    id=UUID(str(row[0])),
                    symbol=row[1].strip().upper(),
                    company_name=str(row[2]),
                    is_active=bool(row[3]),
                )
                for row in rows
            }

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
    def apply_catalog_reconciliation(
        conn: Connection,
        plan: TefasCatalogReconciliationPlan,
    ) -> Dict[str, UUID]:
        """Apply catalog mutations (add active/inactive, reactivate, rename, deactivate) within current transaction.

        Returns:
            Dict[str, UUID] mapping for newly added funds (symbol -> UUID).
        """
        newly_added_ids: Dict[str, UUID] = {}

        with conn.cursor() as cur:
            # 1. Insert new active instruments
            if plan.to_add_active:
                query_active = """
                    INSERT INTO instruments (
                      market, asset_type, symbol, company_name, currency, is_active
                    )
                    VALUES ('TEFAS', 'fund', %s, %s, 'TRY', true)
                    ON CONFLICT (market, symbol) DO UPDATE
                    SET
                      company_name = EXCLUDED.company_name,
                      is_active = true,
                      updated_at = now()
                    RETURNING symbol, id;
                """
                for item in plan.to_add_active:
                    cur.execute(query_active, (item.fund_code, item.fund_name))
                    res = cur.fetchone()
                    if res:
                        newly_added_ids[res[0].strip().upper()] = UUID(str(res[1]))

            # 2. Insert new inactive instruments
            if plan.to_add_inactive:
                query_inactive = """
                    INSERT INTO instruments (
                      market, asset_type, symbol, company_name, currency, is_active
                    )
                    VALUES ('TEFAS', 'fund', %s, %s, 'TRY', false)
                    ON CONFLICT (market, symbol) DO UPDATE
                    SET
                      company_name = EXCLUDED.company_name,
                      is_active = false,
                      updated_at = now()
                    RETURNING symbol, id;
                """
                for item in plan.to_add_inactive:
                    cur.execute(query_inactive, (item.fund_code, item.fund_name))
                    res = cur.fetchone()
                    if res:
                        newly_added_ids[res[0].strip().upper()] = UUID(str(res[1]))

            # 3. Reactivate instruments
            if plan.to_reactivate_ids:
                cur.execute(
                    """
                    UPDATE instruments
                    SET is_active = true, updated_at = now()
                    WHERE id = ANY(%s);
                    """,
                    (plan.to_reactivate_ids,),
                )

            # 4. Update company names
            if plan.to_update_names:
                for inst_id, _symbol, new_name in plan.to_update_names:
                    cur.execute(
                        """
                        UPDATE instruments
                        SET company_name = %s, updated_at = now()
                        WHERE id = %s;
                        """,
                        (new_name, inst_id),
                    )

            # 5. Deactivate missing / verified unpriced instruments (soft delete)
            if plan.to_deactivate_ids:
                cur.execute(
                    """
                    UPDATE instruments
                    SET is_active = false, updated_at = now()
                    WHERE id = ANY(%s);
                    """,
                    (plan.to_deactivate_ids,),
                )

        return newly_added_ids

    @staticmethod
    def upsert_prices(
        conn: Connection,
        items: Sequence[TefasPriceSyncItem],
    ) -> Tuple[int, int, int]:
        """Perform transactional, idempotent batch upsert of latest fund prices into public.instrument_latest_prices.

        Returns:
            (inserted_or_updated_count, unchanged_count, stale_skipped_count)
        """
        if not items:
            return 0, 0, 0

        inst_ids = [item.instrument_id for item in items]
        stored_map = TefasLatestPricesRepository.get_stored_latest_prices(conn, inst_ids)

        to_upsert, unchanged_count, stale_skipped_count = plan_price_upserts(stored_map, items)

        if to_upsert:
            query = """
                INSERT INTO instrument_latest_prices (
                  instrument_id, price, currency, previous_close, change_amount, change_percent,
                  price_at, fetched_at, provider_code, is_stale, stale_reason, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, false, NULL, now(), now())
                ON CONFLICT (instrument_id) DO UPDATE
                SET
                  price = EXCLUDED.price,
                  currency = EXCLUDED.currency,
                  previous_close = EXCLUDED.previous_close,
                  change_amount = EXCLUDED.change_amount,
                  change_percent = EXCLUDED.change_percent,
                  price_at = EXCLUDED.price_at,
                  fetched_at = now(),
                  provider_code = EXCLUDED.provider_code,
                  is_stale = false,
                  stale_reason = NULL,
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
                )
                for item in to_upsert
            ]
            with conn.cursor() as cur:
                cur.executemany(query, params)

        return len(to_upsert), unchanged_count, stale_skipped_count

    @staticmethod
    def mark_stale_prices(
        conn: Connection,
        instrument_ids: Sequence[UUID],
        reason: str,
    ) -> int:
        """Mark existing price rows as stale with a structured reason.

        Does not modify price, price_at, previous_close, change fields, or fetched_at.
        Does not insert fake price rows if none existed previously.
        Does not update rows that are already in the exact same stale state.
        """
        if not instrument_ids:
            return 0

        if reason not in VALID_STALE_REASONS:
            raise ValueError(
                f"Invalid stale reason '{reason}'. Must be one of {sorted(list(VALID_STALE_REASONS))}"
            )

        query = """
            UPDATE instrument_latest_prices
            SET
              is_stale = true,
              stale_reason = %s,
              updated_at = now()
            WHERE
              instrument_id = ANY(%s)
              AND (
                is_stale IS NOT TRUE
                OR stale_reason IS DISTINCT FROM %s
              );
        """
        with conn.cursor() as cur:
            cur.execute(query, (reason, list(instrument_ids), reason))
            return cur.rowcount if cur.rowcount != -1 else 0
