"""Export idempotent Supabase reference-data seed SQL.

Only the three non-user reference tables are read. The database transaction is
explicitly read-only, and connection details are never printed or written.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings

DEFAULT_OUTPUT = PROJECT_ROOT / "supabase" / "seed.sql"
CHUNK_SIZE = 100


def _literal(conn: psycopg.Connection[Any], value: Any) -> str:
    return sql.Literal(value).as_string(conn)


def _values_block(
    conn: psycopg.Connection[Any], rows: Sequence[Sequence[Any]]
) -> str:
    return ",\n".join(
        "    (" + ", ".join(_literal(conn, value) for value in row) + ")"
        for row in rows
    )


def _chunks(rows: Sequence[Sequence[Any]]) -> Iterable[Sequence[Sequence[Any]]]:
    for start in range(0, len(rows), CHUNK_SIZE):
        yield rows[start : start + CHUNK_SIZE]


def _provider_sql(conn: psycopg.Connection[Any], rows: Sequence[Sequence[Any]]) -> str:
    statements = []
    for chunk in _chunks(rows):
        statements.append(
            f"""INSERT INTO public.data_providers (
    code, name, provider_type, official_source, base_url, is_active
) VALUES
{_values_block(conn, chunk)}
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    provider_type = EXCLUDED.provider_type,
    official_source = EXCLUDED.official_source,
    base_url = EXCLUDED.base_url,
    is_active = EXCLUDED.is_active
WHERE (data_providers.name, data_providers.provider_type,
       data_providers.official_source, data_providers.base_url,
       data_providers.is_active)
      IS DISTINCT FROM
      (EXCLUDED.name, EXCLUDED.provider_type, EXCLUDED.official_source,
       EXCLUDED.base_url, EXCLUDED.is_active);"""
        )
    return "\n\n".join(statements)


def _instrument_sql(conn: psycopg.Connection[Any], rows: Sequence[Sequence[Any]]) -> str:
    statements = []
    for chunk in _chunks(rows):
        statements.append(
            f"""INSERT INTO public.instruments (
    market, symbol, company_name, is_active, asset_type, currency
) VALUES
{_values_block(conn, chunk)}
ON CONFLICT (market, symbol) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    is_active = EXCLUDED.is_active,
    asset_type = EXCLUDED.asset_type,
    currency = EXCLUDED.currency
WHERE (instruments.company_name, instruments.is_active,
       instruments.asset_type, instruments.currency)
      IS DISTINCT FROM
      (EXCLUDED.company_name, EXCLUDED.is_active,
       EXCLUDED.asset_type, EXCLUDED.currency);"""
        )
    return "\n\n".join(statements)


def _kap_mapping_sql(conn: psycopg.Connection[Any], rows: Sequence[Sequence[Any]]) -> str:
    statements = []
    for chunk in _chunks(rows):
        statements.append(
            f"""INSERT INTO public.kap_member_mappings (
    instrument_id, mkk_member_oid, kap_company_name, last_verified_at
)
SELECT i.id, seed.mkk_member_oid, seed.kap_company_name,
       seed.last_verified_at::timestamptz
FROM (VALUES
{_values_block(conn, chunk)}
) AS seed(market, symbol, mkk_member_oid, kap_company_name, last_verified_at)
JOIN public.instruments AS i
  ON i.market = seed.market
 AND i.symbol = seed.symbol
ON CONFLICT (instrument_id) DO UPDATE SET
    mkk_member_oid = EXCLUDED.mkk_member_oid,
    kap_company_name = EXCLUDED.kap_company_name,
    last_verified_at = EXCLUDED.last_verified_at
WHERE (kap_member_mappings.mkk_member_oid,
       kap_member_mappings.kap_company_name,
       kap_member_mappings.last_verified_at)
      IS DISTINCT FROM
      (EXCLUDED.mkk_member_oid, EXCLUDED.kap_company_name,
       EXCLUDED.last_verified_at);"""
        )
    return "\n\n".join(statements)


def export_seed(output_path: Path) -> tuple[int, int, int]:
    dsn = settings.SUPABASE_DATABASE_URL
    if not dsn:
        raise RuntimeError("SUPABASE_DATABASE_URL is not configured.")

    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")

        provider_rows = conn.execute(
            """
            SELECT code, name, provider_type, official_source, base_url, is_active
            FROM public.data_providers
            ORDER BY code
            """
        ).fetchall()
        instrument_rows = conn.execute(
            """
            SELECT market, symbol, company_name, is_active, asset_type, currency
            FROM public.instruments
            ORDER BY market, symbol
            """
        ).fetchall()
        kap_rows = conn.execute(
            """
            SELECT i.market, i.symbol, km.mkk_member_oid, km.kap_company_name,
                   km.last_verified_at
            FROM public.kap_member_mappings AS km
            JOIN public.instruments AS i ON i.id = km.instrument_id
            ORDER BY i.market, i.symbol
            """
        ).fetchall()

        content = "\n\n".join(
            (
                "-- Generated reference-data seed. Do not add user or operational data.\n"
                "-- Source tables: data_providers, instruments, kap_member_mappings.\n"
                "BEGIN;",
                _provider_sql(conn, provider_rows),
                _instrument_sql(conn, instrument_rows),
                _kap_mapping_sql(conn, kap_rows),
                "COMMIT;\n",
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    return len(provider_rows), len(instrument_rows), len(kap_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Seed SQL output path (default: supabase/seed.sql)",
    )
    args = parser.parse_args()

    provider_count, instrument_count, kap_count = export_seed(args.output.resolve())
    print(f"Seed written: {args.output}")
    print(
        "Reference rows: "
        f"providers={provider_count}, instruments={instrument_count}, "
        f"kap_mappings={kap_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
