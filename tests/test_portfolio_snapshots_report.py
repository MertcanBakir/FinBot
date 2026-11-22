from datetime import date
from uuid import UUID

from app.jobs.portfolio_snapshots_job import classify_snapshot_error
from app.models.portfolio_snapshots import (
    PortfolioSnapshotFailure,
    PortfolioSnapshotJobResult,
)
from app.services.job_tracking.notification.renderer import (
    PortfolioSnapshotEmailRenderer,
)
from app.services.portfolio_snapshots_report import (
    format_datetime_tr,
    format_portfolio_snapshots_email_html,
    format_portfolio_snapshots_email_text,
    format_portfolio_snapshots_subject,
)

PORTFOLIO_ID = UUID("5d2b09bd-3ba2-4b0d-a93e-732ab14c4d8d")


def _failed_result() -> PortfolioSnapshotJobResult:
    return PortfolioSnapshotJobResult(
        status="failed",
        snapshot_date=date(2026, 8, 26),
        dry_run=False,
        requested_count=1,
        completed_count=0,
        partial_count=0,
        failed_count=1,
        started_at="2026-08-27T01:09:59+00:00",
        finished_at="2026-08-27T01:10:01+00:00",
        duration_seconds=2.0,
        failures=[
            PortfolioSnapshotFailure(
                portfolio_id=PORTFOLIO_ID,
                code="portfolio_instrument_price_missing",
                message="Açık pozisyonlardan en az birinin güncel fiyatı bulunamadı.",
                missing_symbols=["NVDA"],
            )
        ],
    )


def test_snapshot_report_subject_text_and_html_are_actionable_and_safe():
    result = _failed_result()

    subject = format_portfolio_snapshots_subject(result)
    text = format_portfolio_snapshots_email_text(result)
    html = format_portfolio_snapshots_email_html(result)

    assert subject == "❌ FinBot Portföy Snapshot — Oluşturma başarısız — 26 Ağustos 2026"
    assert "NVDA" in text
    assert "NVDA" in html
    assert "Eksik fiyat" in text
    assert "Önerilen aksiyon" in html
    assert "5d2b09bd…4d8d" in text
    assert str(PORTFOLIO_ID) not in text
    assert str(PORTFOLIO_ID) not in html
    assert "Günlük Portföy Snapshot" in html


def test_snapshot_report_escapes_failure_content():
    result = _failed_result()
    result.failures[0].message = "<script>alert(1)</script>"
    result.failures[0].missing_symbols = ["NVDA<script>"]

    html = format_portfolio_snapshots_email_html(result)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_snapshot_error_classifier_exposes_only_safe_domain_messages():
    assert classify_snapshot_error(Exception("portfolio_instrument_price_missing")) == (
        "portfolio_instrument_price_missing",
        "Açık pozisyonlardan en az birinin güncel fiyatı bulunamadı.",
    )
    code, message = classify_snapshot_error(
        Exception("password=secret postgresql://private-host/internal")
    )
    assert code == "portfolio_snapshot_unexpected_error"
    assert "secret" not in message
    assert "private-host" not in message


def test_renderer_generates_multipart_html_report():
    renderer = PortfolioSnapshotEmailRenderer()
    result = _failed_result()

    subject, body_text, body_html = renderer.render(result)

    assert subject.startswith("❌ FinBot Portföy Snapshot")
    assert body_html.startswith("<!DOCTYPE html>")
    assert "NVDA" in body_html
    assert "NVDA" in body_text


def test_partial_reconciliation_report_preserves_missing_history_and_escape_rules():
    result = PortfolioSnapshotJobResult(
        status="partial", snapshot_date=date(2026, 11, 20), dry_run=False,
        requested_count=2, completed_count=1, reused_count=1, missing_snapshot_count=1,
        warnings=["snapshot <missing>"],
        failures=[PortfolioSnapshotFailure(code="example", message="example")],
    )
    text = format_portfolio_snapshots_email_text(result)
    html = format_portfolio_snapshots_email_html(result)
    assert "Mevcut kaydı kullanılan: 1" in text
    assert "Gün içinde kaydedilmemiş: 1" in text
    assert "snapshot &lt;missing&gt;" in html
    assert "Kısmi" in format_portfolio_snapshots_subject(result)
    assert "TWR" not in text and "FIFO" not in html
    assert "Eksik geçmiş kayıtlar güncel fiyatlarla doldurulmaz." in text


def test_success_reconciliation_and_optional_report_dates():
    result = PortfolioSnapshotJobResult(
        status="success", snapshot_date=date(2026, 11, 20), dry_run=True,
    )
    assert "Başarılı" in format_portfolio_snapshots_subject(result)
    assert "BAŞARILI" in format_portfolio_snapshots_email_text(result)
    assert "Başarısız Portföyler" not in format_portfolio_snapshots_email_html(result)
    assert format_datetime_tr(None) == "Belirtilmedi"
    assert format_datetime_tr("not-a-date") == "not-a-date"
    assert "12:00:00" in format_datetime_tr("2026-11-20T12:00:00")
