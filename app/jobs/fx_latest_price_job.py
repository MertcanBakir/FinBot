import argparse
import logging
from typing import Any, List, Optional

from app.core.database import get_db_connection
from app.models.fx_prices import FxPriceJobResult
from app.models.job_tracking import JobRunContext, JobRunSpecification
from app.providers.fx import YahooFxProvider
from app.repositories.fx_latest_price_repository import FxLatestPriceRepository
from app.services.email_service import EmailService
from app.services.fx_prices_service import FxPricesService
from app.services.job_tracking.adapters.fx_adapter import FxPriceResultAdapter
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import FxPriceEmailRenderer
from app.services.job_tracking.runner import TrackedJobRunner

logger = logging.getLogger(__name__)


def run_job(
    argv: Optional[List[str]] = None,
    provider: Optional[YahooFxProvider] = None,
    service: Optional[FxPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
    run_context: Optional[JobRunContext] = None,
) -> FxPriceJobResult:
    parser = argparse.ArgumentParser(description="Synchronize USDTRY latest price")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply-db", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args(argv)
    return execute_tracked_job(
        dry_run=not args.apply_db, no_email=args.no_email,
        provider=provider, service=service, email_service=email_service, db_connection_factory=db_connection_factory, run_context=run_context,
    )


def execute_tracked_job(
    *,
    dry_run: bool = True,
    no_email: bool = False,
    provider: Optional[YahooFxProvider] = None,
    service: Optional[FxPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
    run_context: Optional[JobRunContext] = None,
) -> FxPriceJobResult:

    email_svc = email_service or EmailService()

    runner = TrackedJobRunner(
        spec=JobRunSpecification(
            job_name="fx_latest_price_job",
            operation_type="prices_sync",
            scope="FX",
            provider_code="YAHOO_FINANCE",
        ),
        adapter=FxPriceResultAdapter(),
        renderer=FxPriceEmailRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=email_svc,
        db_connection_factory=db_connection_factory or get_db_connection,
    )

    return runner.run(
        lambda _ctx: _execute_fx_price_job(
            dry_run=dry_run,
            provider=provider,
            service=service,
            email_service=email_svc,
            db_connection_factory=db_connection_factory,
        ),
        dry_run=dry_run,
        no_email=no_email,
        context=run_context,
    )


def _execute_fx_price_job(
    dry_run: bool,
    provider: Optional[YahooFxProvider] = None,
    service: Optional[FxPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
) -> FxPriceJobResult:
    provider = provider or YahooFxProvider()
    service = service or FxPricesService()
    connection_factory = db_connection_factory or get_db_connection
    provider_called = False

    try:
        with connection_factory() as conn:
            instrument = FxLatestPriceRepository.get_instrument(conn)
            if not instrument or not instrument["is_active"] or instrument["currency"] != "TRY":
                raise ValueError("USDTRY reference instrument is missing or invalid")
            stored = FxLatestPriceRepository.get_stored(conn, instrument["id"])
        provider_called = True
        intraday, daily = provider.fetch()
        item = service.normalize(instrument["id"], intraday, daily)
        unchanged = FxLatestPriceRepository.is_unchanged(stored, item)
        status = "success" if item.previous_close is not None else "partial"
        result = FxPriceJobResult(
            status=status,
            dry_run=dry_run,
            instrument_id=item.instrument_id,
            price=item.price,
            previous_close=item.previous_close,
            price_at=item.price_at,
            was_unchanged=unchanged,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

        if not dry_run and not unchanged:
            with connection_factory() as conn:
                with conn.transaction():
                    current = FxLatestPriceRepository.get_instrument(conn)
                    if current != instrument:
                        raise RuntimeError("USDTRY catalog changed during synchronization")
                    result.database_writes = FxLatestPriceRepository.upsert(conn, item)
                    result.database_write_executed = result.database_writes > 0
        return result
    except Exception as exc:
        logger.error("USDTRY price job failed (error_type=%s)", type(exc).__name__)
        return FxPriceJobResult(
            status="failed",
            dry_run=dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
            error="USDTRY fiyatı güvenli biçimde güncellenemedi.",
        )


def main() -> None:
    result = run_job()
    print(f"Mode: {'DRY RUN' if result.dry_run else 'APPLY DB'}")
    print(f"Status: {result.status.upper()}")
    print(f"Price: {result.price} | Previous Close: {result.previous_close}")
    print(f"Database Writes: {result.database_writes}")
    raise SystemExit(1 if result.status == "failed" else 0)


if __name__ == "__main__":
    main()
