import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.earnings import KapEarningsFetchResult, OfficialIrProviderResult
from app.providers.earnings.kap_provider import KapEarningsProvider
from app.providers.earnings.official_ir.registry import (
    OfficialIrRegistry,
    official_ir_registry,
)
from app.services.bist_fiscal_period_archive import (
    BistFiscalPeriodArchive,
    bist_fiscal_period_archive,
)
from app.services.bist_reconciliation import (
    reconcile_kap_and_official_ir,
    scope_official_ir_results,
)

logger = logging.getLogger(__name__)


async def run_bist_earnings_backfill(
    year: int = 2026,
    provider: Optional[KapEarningsProvider] = None,
    ir_registry: Optional[OfficialIrRegistry] = None,
    fiscal_archive: Optional[BistFiscalPeriodArchive] = None
) -> int:
    """Execute BIST earnings backfill for specified target calendar year."""
    if provider is None:
        provider = KapEarningsProvider()
    if ir_registry is None:
        ir_registry = official_ir_registry
    if fiscal_archive is None:
        fiscal_archive = bist_fiscal_period_archive

    tz = ZoneInfo(settings.TIMEZONE)
    as_of_date = datetime.now(tz).date()

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    if year_start > as_of_date:
        print(f"ERROR: Target year {year} is in the future (today is {as_of_date.isoformat()}). Cannot backfill future years.")
        return 1

    coverage_start = year_start
    coverage_end = year_end
    published_from = year_start
    published_to = min(year_end, as_of_date)

    print(f"=== Starting BIST100 Earnings Backfill for Year {year} ===")
    print(f"Coverage Window (earnings_date): {coverage_start.isoformat()} to {coverage_end.isoformat()}")
    print(f"Filing Search Window (publish_date): {published_from.isoformat()} to {published_to.isoformat()}")

    # 1. Fetch KAP
    try:
        fetch_res = await provider.fetch_calendar(
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            published_from=published_from,
            published_to=published_to
        )
    except Exception as e:
        logger.error(f"Error during BIST earnings backfill KAP fetch: {e}", exc_info=True)
        fetch_res = KapEarningsFetchResult(
            status="failed",
            events=[],
            normalized_events=[],
            warnings=[f"Exception occurred during KAP backfill fetch: {e}"]
        )

    print(f"KAP Provider Fetch Status: {fetch_res.status.upper()}")
    print(f"Disclosures Found: {fetch_res.metrics.financial_calendar_disclosure_count}")
    print(f"KAP Events in Year Coverage: {len(fetch_res.events)}")

    # 2. Fetch Official IR independently
    ir_results: List[OfficialIrProviderResult] = []
    if ir_registry:
        try:
            ir_results = await ir_registry.fetch_all()
        except Exception as e:
            logger.warning(f"Official IR fetch failed during backfill: {e}")
            for prov in ir_registry.get_all_providers():
                ir_results.append(OfficialIrProviderResult(
                    symbol=prov.symbol,
                    source_provider=prov.source_provider,
                    status="failed",
                    parser_failed=True,
                    warnings=[f"Official IR fetch exception: {str(e)}"]
                ))

    # 3. Scope Official IR results for target backfill year and reconcile
    scoped_ir_results = scope_official_ir_results(
        ir_results=ir_results,
        earnings_date_from=coverage_start,
        earnings_date_to=coverage_end,
    )
    combined_res = reconcile_kap_and_official_ir(
        kap_result=fetch_res,
        ir_results=scoped_ir_results,
        relevance_start=coverage_start,
        relevance_end=coverage_end,
        emit_out_of_scope_warnings=False
    )

    print(f"Combined Reconciled Status: {combined_res.status.upper()}")
    print(f"Total Normalized Events: {len(combined_res.normalized_events)}")

    # Strict target calendar year filter: 2026-01-01 <= earnings_date <= 2026-12-31
    cov_start_str = coverage_start.isoformat()
    cov_end_str = coverage_end.isoformat()
    year_events = [
        ev for ev in combined_res.normalized_events
        if cov_start_str <= ev.earnings_date <= cov_end_str
    ]

    # If status is failed or 0 usable events produced across both sources, fail with exit code 1
    if combined_res.status == "failed" or (not year_events and fetch_res.status == "failed"):
        print("ERROR: Backfill failed. No valid events available from KAP or Official IR.")
        if combined_res.warnings:
            print("Warnings / Errors:")
            for w in combined_res.warnings:
                print(f"  - {w}")
        return 1

    # Upsert events into fiscal period partition files
    upsert_res = fiscal_archive.upsert_events(
        events=year_events,
        status=combined_res.status,
        generated_at=datetime.now(tz).isoformat(),
        warnings=combined_res.warnings
    )

    print("\n--- Partition Upsert Results ---")
    print(f"Upsert Status: {upsert_res.status.upper()}")
    print(f"Files Created: {upsert_res.files_created}")
    print(f"Files Updated: {upsert_res.files_updated}")
    print(f"Events Added: {upsert_res.events_added}")
    print(f"Events Changed: {upsert_res.events_changed}")
    print(f"Events Unchanged: {upsert_res.events_unchanged}")
    if upsert_res.affected_files:
        print(f"Affected Files: {', '.join(upsert_res.affected_files)}")
    if upsert_res.warnings:
        print("Upsert Warnings:")
        for w in upsert_res.warnings:
            print(f"  - {w}")

    if upsert_res.status == "failed" or upsert_res.error:
        print(f"ERROR: Partition upsert failed: {upsert_res.error}")
        return 1

    print("\nBackfill successfully completed!")
    return 0


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Run one-time BIST100 fiscal period earnings archive backfill.")
    parser.add_argument("--year", type=int, default=2026, help="Target calendar year for backfill (default: 2026)")
    args = parser.parse_args()

    exit_code = asyncio.run(run_bist_earnings_backfill(year=args.year))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
