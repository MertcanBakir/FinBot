import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.database import get_db_connection
from app.models.job_tracking import JobRunSpecification
from app.models.portfolio_snapshots import (
    PortfolioSnapshotFailure,
    PortfolioSnapshotJobResult,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.portfolio_snapshot_adapter import (
    PortfolioSnapshotResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    PortfolioSnapshotEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.services.portfolio_snapshots_report import (
    mask_portfolio_id,
)

logger = logging.getLogger(__name__)

SAFE_SNAPSHOT_ERRORS = {
    "portfolio_instrument_price_missing": "Açık pozisyonlardan en az birinin güncel fiyatı bulunamadı.",
    "portfolio_fx_price_missing": "Portföy değerlemesi için USD/TRY kuru bulunamadı.",
    "portfolio_invalid_snapshot_date": "Snapshot tarihi boş veya gelecekte.",
    "portfolio_snapshot_portfolio_not_found": "Portföy bulunamadı veya arşivlenmiş.",
}


def classify_snapshot_error(exc: Exception) -> tuple[str, str]:
    diag = getattr(exc, "diag", None)
    raw = getattr(diag, "message_primary", None) or str(exc)
    for code, message in SAFE_SNAPSHOT_ERRORS.items():
        if code in raw:
            return code, message
    return "portfolio_snapshot_unexpected_error", "Snapshot güvenli biçimde oluşturulamadı."


def _find_missing_price_symbols(portfolio_id: UUID, snapshot_date: date) -> List[str]:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT i.symbol
                    FROM public.portfolio_instruments pi
                    JOIN public.instruments i ON i.id = pi.instrument_id
                    CROSS JOIN LATERAL private.get_portfolio_position_summary_at(
                        %s,
                        i.id,
                        (((%s::date + 1)::timestamp AT TIME ZONE 'Europe/Istanbul')
                            - interval '1 microsecond')
                    ) s
                    LEFT JOIN public.instrument_latest_prices lp
                      ON lp.instrument_id = i.id
                    WHERE pi.portfolio_id = %s
                      AND s.current_quantity > 0
                      AND (lp.price IS NULL OR lp.price <= 0)
                    ORDER BY i.symbol
                    """,
                    (portfolio_id, snapshot_date, portfolio_id),
                )
                return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning(
            "Missing-price diagnosis failed (portfolio_id=%s, error_type=%s)",
            portfolio_id,
            type(exc).__name__,
        )
        return []


def _finish_result(result: PortfolioSnapshotJobResult, started_at: datetime) -> None:
    finished_at = datetime.now(timezone.utc)
    result.finished_at = finished_at.isoformat()
    result.duration_seconds = max(0.0, (finished_at - started_at).total_seconds())


def run_job(
    argv: Optional[List[str]] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
) -> PortfolioSnapshotJobResult:
    parser = argparse.ArgumentParser(description="Build daily portfolio snapshots")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply-db", action="store_true")
    parser.add_argument("--snapshot-date", type=date.fromisoformat)
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args(argv)
    dry_run = not args.apply_db
    email_svc = email_service or EmailService()

    runner = TrackedJobRunner(
        spec=JobRunSpecification(
            job_name="portfolio_snapshots_job",
            operation_type="portfolio_snapshot",
            scope="PORTFOLIO",
            provider_code=None,
        ),
        adapter=PortfolioSnapshotResultAdapter(),
        renderer=PortfolioSnapshotEmailRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=email_svc,
        db_connection_factory=db_connection_factory or get_db_connection,
    )

    return runner.run(
        lambda: _execute_portfolio_snapshots_job(
            args=args,
            email_service=email_svc,
        ),
        dry_run=dry_run,
        no_email=args.no_email,
    )


def _execute_portfolio_snapshots_job(
    args: argparse.Namespace,
    email_service: Optional[EmailService] = None,
) -> PortfolioSnapshotJobResult:
    started_at = datetime.now(timezone.utc)
    dry_run = not args.apply_db
    target = args.snapshot_date or (
        datetime.now(ZoneInfo("Europe/Istanbul")).date() - timedelta(days=1)
    )
    result = PortfolioSnapshotJobResult(
        status="success",
        snapshot_date=target,
        dry_run=dry_run,
        started_at=started_at.isoformat(),
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM public.portfolios WHERE archived_at IS NULL ORDER BY id"
                )
                portfolio_ids = [row[0] for row in cur.fetchall()]
        result.requested_count = len(portfolio_ids)

        for portfolio_id in portfolio_ids:
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT private.resolve_daily_portfolio_snapshot(%s, %s)",
                            (portfolio_id, target),
                        )
                        payload = cur.fetchone()[0]
                    if dry_run:
                        conn.rollback()
                    else:
                        conn.commit()
                if payload.get("valuation_status") == "missing":
                    result.missing_snapshot_count += 1
                    result.warnings.append(
                        f"{mask_portfolio_id(portfolio_id)}: İstenen gün içinde kaydedilmiş "
                        "snapshot yok; güncel değerlerle geçmiş kayıt oluşturulmadı."
                    )
                    continue
                result.completed_count += 1
                if payload.get("reused", False):
                    result.reused_count += 1
                elif not dry_run:
                    result.database_write_executed = True
                if payload.get("valuation_status") == "partial":
                    result.partial_count += 1
                    for warning in payload.get("warnings") or []:
                        safe_warning = f"{mask_portfolio_id(portfolio_id)}: {warning}"
                        if safe_warning not in result.warnings:
                            result.warnings.append(safe_warning)
            except Exception as exc:
                code, safe_message = classify_snapshot_error(exc)
                missing_symbols = (
                    _find_missing_price_symbols(portfolio_id, target)
                    if code == "portfolio_instrument_price_missing"
                    else []
                )
                logger.error(
                    "Portfolio snapshot failed (portfolio_id=%s, error_code=%s, error_type=%s)",
                    portfolio_id,
                    code,
                    type(exc).__name__,
                )
                result.failed_count += 1
                result.failures.append(
                    PortfolioSnapshotFailure(
                        portfolio_id=portfolio_id,
                        code=code,
                        message=safe_message,
                        missing_symbols=missing_symbols,
                    )
                )
                result.errors.append(
                    f"Portfolio {mask_portfolio_id(portfolio_id)}: {safe_message}"
                )

        if result.failed_count:
            result.status = "failed" if result.completed_count == 0 else "partial"
        elif result.partial_count or result.missing_snapshot_count:
            result.status = "partial"

        _finish_result(result, started_at)
        return result
    except Exception as exc:
        logger.error("Snapshot job failed before processing (error_type=%s)", type(exc).__name__)
        result.status = "failed"
        result.errors.append("Portföy listesi güvenli biçimde okunamadı")
        result.failures.append(
            PortfolioSnapshotFailure(
                code="portfolio_list_read_failed",
                message="Portföy listesi güvenli biçimde okunamadı.",
            )
        )
        _finish_result(result, started_at)
        return result


def main() -> None:
    result = run_job()
    print(f"Mode: {'DRY RUN' if result.dry_run else 'APPLY DB'}")
    print(f"Status: {result.status.upper()} | Snapshot Date: {result.snapshot_date}")
    print(
        f"Requested: {result.requested_count} | Completed: {result.completed_count} | "
        f"Partial: {result.partial_count} | Failed: {result.failed_count} | "
        f"Reused: {result.reused_count} | Missing Snapshot: {result.missing_snapshot_count}"
    )
    raise SystemExit(1 if result.status == "failed" else 0)


if __name__ == "__main__":
    main()
