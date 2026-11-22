import argparse
import runpy
import sys
from datetime import date, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.core import database
from app.jobs import portfolio_snapshots_job
from app.models.job_tracking import JobTrackingNotificationError, JobTrackingStartError
from app.models.portfolio_snapshots import (
    PortfolioSnapshotFailure,
    PortfolioSnapshotJobResult,
)
from app.repositories.job_tracking_repository import JobTrackingRepository
from app.services.job_tracking.adapters.portfolio_snapshot_adapter import (
    PortfolioSnapshotResultAdapter,
)

PORTFOLIO_A = UUID("10000000-0000-0000-0000-000000000011")
PORTFOLIO_B = UUID("10000000-0000-0000-0000-000000000012")
SNAPSHOT_DATE = date(2026, 8, 26)


def _connection(*, fetchall=None, fetchone=None, execute_error=None) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    if fetchall is not None:
        cursor.fetchall.return_value = fetchall
    if fetchone is not None:
        cursor.fetchone.return_value = fetchone
    if execute_error is not None:
        cursor.execute.side_effect = execute_error
    conn.cursor.return_value = cursor
    return conn


def test_snapshot_job_dry_run_rolls_back_and_preserves_partial_warning(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,), (PORTFOLIO_B,)])
    complete_conn = _connection(fetchone=({"valuation_status": "success"},))
    partial_conn = _connection(
        fetchone=(
            {
                "valuation_status": "partial",
                "warnings": ["USD/TRY fiyatı eski olabilir"],
            },
        )
    )
    connections = iter([list_conn, complete_conn, partial_conn])
    monkeypatch.setattr(
        portfolio_snapshots_job, "get_db_connection", lambda: next(connections)
    )

    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=False, snapshot_date=SNAPSHOT_DATE)
    )

    assert result.status == "partial"
    assert result.requested_count == 2
    assert result.completed_count == 2
    assert result.partial_count == 1
    assert result.failed_count == 0
    assert "10000000…0012: USD/TRY fiyatı eski olabilir" in result.warnings
    complete_conn.rollback.assert_called_once_with()
    partial_conn.rollback.assert_called_once_with()
    complete_conn.commit.assert_not_called()
    partial_conn.commit.assert_not_called()


def test_snapshot_job_apply_commits_successful_snapshot(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    snapshot_conn = _connection(fetchone=({"valuation_status": "success"},))
    connections = iter([list_conn, snapshot_conn])
    monkeypatch.setattr(
        portfolio_snapshots_job, "get_db_connection", lambda: next(connections)
    )

    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )

    assert result.status == "success"
    assert result.completed_count == 1
    assert result.failed_count == 0
    snapshot_conn.commit.assert_called_once_with()
    snapshot_conn.rollback.assert_not_called()
    snapshot_conn.cursor.return_value.execute.assert_called_once_with(
        "SELECT private.resolve_daily_portfolio_snapshot(%s, %s)",
        (PORTFOLIO_A, SNAPSHOT_DATE),
    )


def test_recorded_snapshot_is_reused_without_recalculation_or_write_metric(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    snapshot_conn = _connection(fetchone=({"valuation_status": "success", "reused": True},))
    connections = iter([list_conn, snapshot_conn])
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: next(connections))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )
    assert result.status == "success"
    assert result.completed_count == result.reused_count == 1
    assert result.database_write_executed is False
    outcome = PortfolioSnapshotResultAdapter().to_outcome(result)
    assert outcome.sync_action == "no_op"
    assert outcome.metrics["unchanged_count"] == 1
    assert outcome.metrics["inserted_or_updated_count"] == 0
    assert outcome.metrics["database_write_executed"] is False


def test_missing_recorded_snapshot_is_partial_not_fabricated_or_failed(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    snapshot_conn = _connection(fetchone=({"valuation_status": "missing", "reused": False},))
    connections = iter([list_conn, snapshot_conn])
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: next(connections))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )
    assert result.status == "partial"
    assert result.missing_snapshot_count == 1
    assert result.completed_count == result.failed_count == result.reused_count == 0
    assert "geçmiş kayıt oluşturulmadı" in result.warnings[0]
    outcome = PortfolioSnapshotResultAdapter().to_outcome(result)
    assert outcome.metrics["missing_count"] == 1
    assert outcome.metrics["inserted_or_updated_count"] == 0
    assert outcome.metrics["database_write_executed"] is False


def test_reused_partial_snapshot_keeps_warning_without_double_count(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    snapshot_conn = _connection(fetchone=({
        "valuation_status": "partial", "reused": True, "warnings": ["stale price"],
    },))
    connections = iter([list_conn, snapshot_conn])
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: next(connections))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=False, snapshot_date=SNAPSHOT_DATE)
    )
    assert result.status == "partial"
    assert result.completed_count == result.partial_count == result.reused_count == 1
    assert "stale price" in result.warnings[0]
    outcome = PortfolioSnapshotResultAdapter().to_outcome(result)
    assert outcome.metrics["valid_count"] == 1
    assert outcome.metrics["database_write_executed"] is False
    snapshot_conn.rollback.assert_called_once()


def test_snapshot_job_isolates_per_portfolio_failure(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,), (PORTFOLIO_B,)])
    failed_conn = _connection(execute_error=RuntimeError("database secret"))
    success_conn = _connection(fetchone=({"valuation_status": "success"},))
    connections = iter([list_conn, failed_conn, success_conn])
    monkeypatch.setattr(
        portfolio_snapshots_job, "get_db_connection", lambda: next(connections)
    )

    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )

    assert result.status == "partial"
    assert result.completed_count == 1
    assert result.failed_count == 1
    assert result.failures[0].code == "portfolio_snapshot_unexpected_error"
    assert "secret" not in result.failures[0].message
    assert "secret" not in result.errors[0]
    success_conn.commit.assert_called_once_with()


def test_snapshot_job_fails_closed_when_portfolio_list_cannot_be_read(monkeypatch):
    def fail_connection():
        raise RuntimeError("postgresql://user:password@private-host/db")

    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", fail_connection)

    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )

    assert result.status == "failed"
    assert result.requested_count == 0
    assert result.failures[0].code == "portfolio_list_read_failed"
    assert "password" not in result.errors[0]
    assert result.finished_at is not None


def test_snapshot_adapter_reports_metrics_and_sanitizes_error():
    adapter = PortfolioSnapshotResultAdapter()
    result = PortfolioSnapshotJobResult(
        status="partial",
        snapshot_date=SNAPSHOT_DATE,
        dry_run=False,
        requested_count=2,
        completed_count=1,
        partial_count=0,
        failed_count=1,
        errors=["postgresql://user:secret@private-host/db"],
        warnings=["stale price"],
        failures=[
            PortfolioSnapshotFailure(
                portfolio_id=PORTFOLIO_B,
                code="portfolio_instrument_price_missing",
                message="safe message",
            )
        ],
        finished_at="2026-08-27T01:10:01+00:00",
        duration_seconds=2.0,
    )

    outcome = adapter.to_outcome(result)

    assert outcome.sync_action == "partial_update"
    assert outcome.metrics["requested_count"] == 2
    assert outcome.metrics["missing_count"] == 1
    assert outcome.metrics["database_write_executed"] is True
    assert "secret" not in (outcome.error or "")
    assert "private-host" not in (outcome.error or "")


def test_default_date_remains_yesterday_even_when_execution_is_delayed(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 21, 14, 0, tzinfo=tz)

    monkeypatch.setattr(portfolio_snapshots_job, "datetime", Clock)
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: _connection(fetchall=[]))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=False, snapshot_date=None)
    )
    assert result.snapshot_date == date(2026, 11, 20)
    assert result.status == "success"


def test_cli_routes_through_tracking_with_explicit_date_and_no_email(monkeypatch):
    factory = MagicMock()
    runner = MagicMock()
    runner.run.side_effect = lambda business, **kwargs: business()
    tracking = MagicMock(return_value=runner)
    monkeypatch.setattr(portfolio_snapshots_job, "TrackedJobRunner", tracking)
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: _connection(fetchall=[]))
    result = portfolio_snapshots_job.run_job(
        ["--dry-run", "--no-email", "--snapshot-date", "2026-11-20"],
        email_service=MagicMock(), db_connection_factory=factory,
    )
    assert result.snapshot_date == date(2026, 11, 20)
    assert runner.run.call_args.kwargs == {"dry_run": True, "no_email": True}
    assert tracking.call_args.kwargs["db_connection_factory"] is factory


@pytest.mark.parametrize("status,exit_code", [("success", 0), ("partial", 0), ("failed", 1)])
def test_cli_does_not_fail_for_observable_missing_history(monkeypatch, capsys, status, exit_code):
    result = PortfolioSnapshotJobResult(
        status=status, snapshot_date=SNAPSHOT_DATE, dry_run=True,
        missing_snapshot_count=1 if status == "partial" else 0,
    )
    monkeypatch.setattr(portfolio_snapshots_job, "run_job", lambda: result)
    with pytest.raises(SystemExit) as exc:
        portfolio_snapshots_job.main()
    assert exc.value.code == exit_code
    assert f"Missing Snapshot: {result.missing_snapshot_count}" in capsys.readouterr().out


def test_real_price_error_still_fails_and_identifies_missing_symbols(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    failed_conn = _connection(execute_error=RuntimeError("portfolio_instrument_price_missing"))
    symbols_conn = _connection(fetchall=[("RACE",)])
    connections = iter([list_conn, failed_conn, symbols_conn])
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: next(connections))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=True, snapshot_date=SNAPSHOT_DATE)
    )
    assert result.status == "failed"
    assert result.failures[0].missing_symbols == ["RACE"]
    assert PortfolioSnapshotResultAdapter().to_outcome(result).sync_action == "failed_no_write"


def test_missing_symbol_diagnosis_does_not_leak_database_error(monkeypatch, caplog):
    def fail():
        raise RuntimeError("password=SecretSnapshotPassword")

    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", fail)
    assert portfolio_snapshots_job._find_missing_price_symbols(PORTFOLIO_A, SNAPSHOT_DATE) == []
    assert "SecretSnapshotPassword" not in caplog.text


@pytest.mark.parametrize("finished_at", [None, "2026-11-21T02:00:00", "not-a-date"])
def test_snapshot_adapter_handles_optional_finish_time(finished_at):
    outcome = PortfolioSnapshotResultAdapter().to_outcome(PortfolioSnapshotJobResult(
        status="success", snapshot_date=SNAPSHOT_DATE, dry_run=True, finished_at=finished_at,
    ))
    assert outcome.sync_action == "dry_run"
    assert outcome.metrics["database_write_executed"] is False
    if finished_at and finished_at != "not-a-date":
        assert outcome.finished_at.utcoffset().total_seconds() == 0
    else:
        assert outcome.finished_at is None


def test_tracking_start_failure_never_runs_snapshot_business_logic(monkeypatch):
    business = MagicMock()
    monkeypatch.setattr(portfolio_snapshots_job, "_execute_portfolio_snapshots_job", business)

    def unavailable():
        raise RuntimeError("tracking connection unavailable")

    with pytest.raises(JobTrackingStartError):
        portfolio_snapshots_job.run_job(
            ["--apply-db", "--no-email"], email_service=MagicMock(),
            db_connection_factory=unavailable,
        )
    business.assert_not_called()


def test_snapshot_cli_entrypoint_is_read_only_in_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(database, "get_db_connection", lambda: _connection(fetchall=[]))
    monkeypatch.setattr(sys, "argv", ["portfolio_snapshots_job", "--dry-run", "--no-email"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(portfolio_snapshots_job.__file__, run_name="__main__")
    assert exc.value.code == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_reused_partial_deduplicates_recorded_warnings(monkeypatch):
    list_conn = _connection(fetchall=[(PORTFOLIO_A,)])
    snapshot_conn = _connection(fetchone=({
        "valuation_status": "partial", "reused": True, "warnings": ["stale", "stale"],
    },))
    connections = iter([list_conn, snapshot_conn])
    monkeypatch.setattr(portfolio_snapshots_job, "get_db_connection", lambda: next(connections))
    result = portfolio_snapshots_job._execute_portfolio_snapshots_job(
        argparse.Namespace(apply_db=False, snapshot_date=SNAPSHOT_DATE)
    )
    assert result.status == "partial" and len(result.warnings) == 1


@pytest.mark.parametrize("notification_db_fails", [False, True])
def test_missing_history_notification_survives_report_rendering_failure(monkeypatch, notification_db_fails):
    result = PortfolioSnapshotJobResult(
        status="partial", snapshot_date=SNAPSHOT_DATE, dry_run=False,
        requested_count=1, missing_snapshot_count=1, warnings=["Snapshot not captured"],
    )
    business = MagicMock(return_value=result)
    monkeypatch.setattr(portfolio_snapshots_job, "_execute_portfolio_snapshots_job", business)
    monkeypatch.setattr(JobTrackingRepository, "start_run", MagicMock(return_value=PORTFOLIO_A))
    finalize = MagicMock(return_value=True)
    notification = MagicMock(return_value=True)
    monkeypatch.setattr(JobTrackingRepository, "finalize_run", finalize)
    monkeypatch.setattr(JobTrackingRepository, "update_notification_status", notification)
    monkeypatch.setattr(
        portfolio_snapshots_job.PortfolioSnapshotEmailRenderer, "render",
        MagicMock(side_effect=RuntimeError("password=SecretRenderFailure")),
    )
    email = MagicMock()
    email.is_configured.return_value = True
    email.send_email.return_value = True
    if notification_db_fails:
        notification.side_effect = RuntimeError("notification persistence unavailable")
        with pytest.raises(JobTrackingNotificationError):
            portfolio_snapshots_job.run_job(
                ["--apply-db"], email_service=email,
                db_connection_factory=lambda: _connection(),
            )
        business.assert_called_once()
        return
    actual = portfolio_snapshots_job.run_job(
        ["--apply-db"], email_service=email,
        db_connection_factory=lambda: _connection(),
    )
    assert actual.status == "partial" and actual.email_status == "sent"
    assert finalize.call_args.kwargs["outcome"].metrics["database_write_executed"] is False
    assert notification.call_args.kwargs["status"] == "sent"
    assert "SecretRenderFailure" not in str(email.send_email.call_args)
    assert "Rapor Hatası" in str(email.send_email.call_args)
    business.assert_called_once()
