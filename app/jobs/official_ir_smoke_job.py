import asyncio
import logging
import sys
from collections import Counter
from typing import List, Optional

from app.models.earnings import OfficialIrProviderResult
from app.providers.earnings.official_ir.registry import (
    OfficialIrRegistry,
    official_ir_registry,
)

logger = logging.getLogger(__name__)


async def run_official_ir_smoke_test(registry: Optional[OfficialIrRegistry] = None) -> int:
    """Read-only smoke test and diagnostic quality gate for Official IR providers."""
    registry = registry or official_ir_registry
    providers = registry.get_all_providers()
    total_providers = len(providers)

    print("=== Official Investor Relations (IR) Providers Smoke Test ===")
    print(f"Running read-only diagnostic on {total_providers} official IR pages...\n")

    results: List[OfficialIrProviderResult] = await registry.fetch_all()

    any_failed = False
    any_anomalies = False

    expected_symbols = Counter(p.symbol for p in providers)
    actual_symbols = Counter(r.symbol for r in results)

    if total_providers == 0:
        print("❌ CRITICAL: No providers registered in OfficialIrRegistry.\n")
        any_failed = True

    if expected_symbols != actual_symbols:
        missing = sorted(list((expected_symbols - actual_symbols).elements()))
        extra = sorted(list((actual_symbols - expected_symbols).elements()))
        duplicates = sorted([sym for sym, count in actual_symbols.items() if count > 1])

        err_parts = []
        if missing:
            err_parts.append(f"Missing: {', '.join(missing)}")
        if extra:
            err_parts.append(f"Unexpected/Extra: {', '.join(extra)}")
        if duplicates:
            err_parts.append(f"Duplicate results: {', '.join(duplicates)}")

        print(f"❌ CRITICAL: Provider result mismatch. {'; '.join(err_parts)}\n")
        any_failed = True

    print("--- Provider Results ---\n")
    for res in results:
        is_ok = (res.status == "success") and (not res.parser_failed) and (len(res.events) > 0)
        icon = "✅" if is_ok else "❌"

        if not is_ok:
            any_failed = True

        print(f"{icon} [{res.symbol}] - {res.source_provider}")
        print(f"   Status: {res.status.upper()} (Parser Failed: {res.parser_failed})")
        print(f"   Rows Seen: {res.rows_seen}, Accepted: {res.accepted_row_count}, Rejected: {res.rejected_row_count}, Retries: {res.retry_count}")

        if res.events:
            print("   Parsed Events:")
            for ev in res.events:
                print(f"     - {ev.fiscal_period} {ev.fiscal_year}: {ev.earnings_date} ({ev.date_type}) | {ev.period_start} -> {ev.period_end}")
        else:
            print("   Parsed Events: None")
            if res.status == "success":
                print("   ⚠️ ANOMALY: Provider returned success but produced 0 events on recognized table!")
                any_anomalies = True

        if res.warnings:
            print("   Warnings:")
            for w in res.warnings:
                print(f"     * {w}")
        print()

    print("=== Smoke Test Summary ===")
    if not any_failed and not any_anomalies:
        print(f"All {len(results)} Official IR providers succeeded with >=1 valid event.\n")
        return 0
    else:
        print("Smoke test FAILED. One or more providers failed, had parser errors, or produced 0 events.\n")
        return 1


def main():
    logging.basicConfig(level=logging.INFO)
    exit_code = asyncio.run(run_official_ir_smoke_test())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
