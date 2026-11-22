"""Manual integration check for concurrent portfolio SELL serialization.

Run against a local PostgreSQL database whose schema already has all Supabase
migrations applied. The fixed integration fixtures are removed before and
after the test, including when an assertion fails:

    python tests/integration/portfolio_transaction_rpc_concurrency.py <dsn>
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlparse

import psycopg

USER_ID = "00000000-0000-0000-0000-000000000011"
PORTFOLIO_ID = "10000000-0000-0000-0000-000000000011"
INSTRUMENT_ID = "20000000-0000-0000-0000-000000000011"
BUY_REQUEST_ID = "30000000-0000-0000-0000-000000000011"
SELL_REQUEST_IDS = (
    "30000000-0000-0000-0000-000000000012",
    "30000000-0000-0000-0000-000000000013",
)


def _require_local_dsn(dsn: str) -> None:
    parsed = urlparse(dsn)
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("Refusing database URL with a non-PostgreSQL scheme")
    if parsed.hostname not in allowed_hosts:
        raise SystemExit(
            f"Refusing non-local database host: {parsed.hostname or '<missing>'}"
        )


def _set_user(conn: psycopg.Connection[object]) -> None:
    conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (USER_ID,))


def _sell(dsn: str, request_id: str, barrier: Barrier) -> tuple[str, str]:
    with psycopg.connect(dsn) as conn:
        _set_user(conn)
        barrier.wait()
        try:
            result = conn.execute(
                """
                select public.record_portfolio_transaction(
                    %s, %s, 'SELL', 7, 150, 'TRY',
                    '2026-08-20 10:00:00+03', %s, null
                )
                """,
                (PORTFOLIO_ID, INSTRUMENT_ID, request_id),
            ).fetchone()[0]
            return "success", result["current_quantity"]
        except psycopg.Error as exc:
            conn.rollback()
            return "error", exc.diag.message_primary or ""


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("delete from public.portfolios where id = %s", (PORTFOLIO_ID,))
        conn.execute("delete from public.instruments where id = %s", (INSTRUMENT_ID,))
        conn.execute("delete from auth.users where id = %s", (USER_ID,))


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: portfolio_transaction_rpc_concurrency.py <dsn>")
    dsn = sys.argv[1]
    _require_local_dsn(dsn)
    _cleanup(dsn)

    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("insert into auth.users (id) values (%s)", (USER_ID,))
            conn.execute(
                """
                insert into public.portfolios (id, user_id, name, base_currency)
                values (%s, %s, 'Concurrency Portfolio', 'TRY')
                """,
                (PORTFOLIO_ID, USER_ID),
            )
            conn.execute(
                """
                insert into public.instruments (
                    id, market, symbol, company_name, is_active, asset_type, currency
                ) values (%s, 'BIST100', 'CONC', 'Concurrency Instrument',
                          true, 'stock', 'TRY')
                """,
                (INSTRUMENT_ID,),
            )
            _set_user(conn)
            conn.execute(
                """
                select public.record_portfolio_transaction(
                    %s, %s, 'BUY', 10, 100, 'TRY',
                    '2026-08-01 10:00:00+03', %s, null
                )
                """,
                (PORTFOLIO_ID, INSTRUMENT_ID, BUY_REQUEST_ID),
            )

        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda request_id: _sell(dsn, request_id, barrier),
                    SELL_REQUEST_IDS,
                )
            )

        successes = [value for status, value in results if status == "success"]
        errors = [value for status, value in results if status == "error"]
        if successes != ["3.000000000000"]:
            raise AssertionError(
                f"Expected one successful SELL with quantity 3: {results}"
            )
        if errors != ["portfolio_insufficient_position"]:
            raise AssertionError(f"Expected one atomic oversell rejection: {results}")

        with psycopg.connect(dsn) as conn:
            transaction_count, sold_quantity = conn.execute(
                """
                select
                    (select count(*) from public.portfolio_transactions
                     where portfolio_id = %s),
                    (select sum(quantity) from public.portfolio_transactions
                     where portfolio_id = %s and transaction_type = 'SELL')
                """,
                (PORTFOLIO_ID, PORTFOLIO_ID),
            ).fetchone()
        if (transaction_count, sold_quantity) != (2, 7):
            raise AssertionError(
                "Concurrent rollback left inconsistent state: "
                f"{transaction_count=}, {sold_quantity=}"
            )
    finally:
        # Exact, reserved fixture UUIDs plus local-only validation make cleanup
        # safe and keep reruns deterministic after either success or failure.
        _cleanup(dsn)

    print("portfolio transaction concurrency: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
