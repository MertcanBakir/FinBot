"""Supabase-backed runtime instrument catalog access.

Runtime jobs and API services must use ``public.instruments`` as their source
of truth. JSON catalog snapshots are import artifacts only.
"""

from typing import Dict, List

from psycopg import Connection


class InstrumentCatalogRepository:
    @staticmethod
    def get_active_symbols(
        conn: Connection,
        *,
        market: str,
        asset_type: str = "stock",
        yahoo_suffix: bool = False,
    ) -> List[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol
                FROM public.instruments
                WHERE market = %s
                  AND asset_type = %s
                  AND is_active = true
                ORDER BY symbol;
                """,
                (market, asset_type),
            )
            rows = cur.fetchall()

        symbols = [str(row[0]).strip().upper() for row in rows if row[0]]
        if yahoo_suffix:
            return [symbol if symbol.endswith(".IS") else f"{symbol}.IS" for symbol in symbols]
        return symbols


class KapMemberMappingRepository:
    @staticmethod
    def get_active_bist_mappings(conn: Connection) -> Dict[str, Dict[str, str]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.symbol, i.company_name, km.mkk_member_oid,
                       km.kap_company_name, km.last_verified_at
                FROM public.instruments AS i
                JOIN public.kap_member_mappings AS km
                  ON km.instrument_id = i.id
                WHERE i.market = 'BIST100'
                  AND i.asset_type = 'stock'
                  AND i.is_active = true
                ORDER BY i.symbol;
                """
            )
            rows = cur.fetchall()

        return {
            str(row[0]).strip().upper(): {
                "symbol": str(row[0]).strip().upper(),
                "company_name": str(row[3] or row[1] or row[0]).strip(),
                "mkk_member_oid": str(row[2]).strip(),
                "last_verified_at": row[4].isoformat() if row[4] else None,
            }
            for row in rows
            if row[0] and row[2]
        }

    @staticmethod
    def upsert_bist_mappings(conn: Connection, mappings: Dict[str, Dict[str, str]]) -> int:
        if not mappings:
            return 0

        rows = [
            (
                symbol.strip().upper(),
                str(payload.get("mkk_member_oid") or "").strip(),
                str(payload.get("company_name") or symbol).strip(),
            )
            for symbol, payload in mappings.items()
            if symbol and payload.get("mkk_member_oid")
        ]
        if not rows:
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.kap_member_mappings (
                    instrument_id, mkk_member_oid, kap_company_name, last_verified_at
                )
                SELECT i.id, %s, %s, now()
                FROM public.instruments AS i
                WHERE i.market = 'BIST100'
                  AND i.asset_type = 'stock'
                  AND i.symbol = %s
                ON CONFLICT (instrument_id) DO UPDATE SET
                    mkk_member_oid = EXCLUDED.mkk_member_oid,
                    kap_company_name = EXCLUDED.kap_company_name,
                    last_verified_at = EXCLUDED.last_verified_at;
                """,
                [(oid, company_name, symbol) for symbol, oid, company_name in rows],
            )
        return len(rows)
