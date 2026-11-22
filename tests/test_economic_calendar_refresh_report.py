from app.models.economic_calendar_refresh import (
    EconomicCalendarRefreshJobResult,
    EconomicEventChangeDetail,
    EconomicEventDiff,
    EconomicProviderRefreshOutcome,
)
from app.services.economic_calendar_refresh_report import (
    format_economic_calendar_refresh_email_html,
    format_economic_calendar_refresh_email_text,
    format_economic_calendar_refresh_subject,
)


def _build_job_result(
    status="success",
    added_count=0,
    changed_count=0,
    removed_count=0,
    carried_forward_count=0,
    details=None,
    warnings=None,
    provider_outcomes=None
) -> EconomicCalendarRefreshJobResult:
    diff = EconomicEventDiff(
        previous_event_count=10,
        fetched_event_count=10 + added_count,
        final_event_count=10 + added_count - removed_count,
        added_count=added_count,
        changed_count=changed_count,
        removed_count=removed_count,
        carried_forward_count=carried_forward_count,
        details=details or []
    )
    return EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status=status,
        sync_action="replaced" if status == "success" else "merged_and_updated",
        state_source="dummy_path.json",
        diff=diff,
        provider_outcomes=provider_outcomes or [
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=3),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", event_count=2),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="FRED / BLS", status="success", event_count=4),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", event_count=2),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="Federal Reserve", status="success", event_count=2)
        ],
        warnings=warnings or []
    )

def test_subject_line_variations():
    # Success no changes
    r1 = _build_job_result(status="success", added_count=0, changed_count=0, removed_count=0)
    assert format_economic_calendar_refresh_subject(r1) == "✅ FinBot Ekonomik Takvim — Güncel — 17 Ağustos 2026"

    # Success 1 change
    r2 = _build_job_result(status="success", added_count=1)
    assert format_economic_calendar_refresh_subject(r2) == "✅ FinBot Ekonomik Takvim — 1 değişiklik — 17 Ağustos 2026"

    # Success 3 changes
    r3 = _build_job_result(status="success", added_count=2, changed_count=1)
    assert format_economic_calendar_refresh_subject(r3) == "✅ FinBot Ekonomik Takvim — 3 değişiklik — 17 Ağustos 2026"

    # Partial
    r4 = _build_job_result(status="partial", added_count=0)
    assert "⚠️ FinBot Ekonomik Takvim — Kısmi başarı" in format_economic_calendar_refresh_subject(r4)

    # Failed
    r5 = _build_job_result(status="failed")
    assert format_economic_calendar_refresh_subject(r5) == "❌ FinBot Ekonomik Takvim — Güncelleme başarısız — 17 Ağustos 2026"

def test_report_exact_warning_deduplication():
    # Same warning placed in result.warnings and provider warnings and provider error
    shared_warn = "CRITICAL_SHARED_CALENDAR_NETWORK_TIMEOUT"
    r = _build_job_result(
        status="partial",
        warnings=[shared_warn, "Another warning"],
        provider_outcomes=[
            EconomicProviderRefreshOutcome(
                provider_id="BLS", provider_name="FRED / BLS", status="failed",
                event_count=0, warnings=[shared_warn], error=shared_warn
            )
        ]
    )

    text_body = format_economic_calendar_refresh_email_text(r)
    html_body = format_economic_calendar_refresh_email_html(r)

    assert text_body.count(shared_warn) == 1
    assert html_body.count(shared_warn) == 1

def test_report_html_escaping_and_truncation():
    # 25 change items with dangerous HTML characters
    details = [
        EconomicEventChangeDetail(
            name=f"Test Event <script>alert({i})</script>",
            country="TR",
            change_type="added",
            new_scheduled_at_istanbul="2026-09-01T10:00:00+03:00"
        )
        for i in range(25)
    ]
    r = _build_job_result(status="success", added_count=25, details=details)

    html_body = format_economic_calendar_refresh_email_html(r)
    text_body = format_economic_calendar_refresh_email_text(r)

    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "... ve 5 kayıt daha" in html_body
    assert "... ve 5 kayıt daha" in text_body


def test_report_state_write_failed_warning_formatting():
    from app.jobs.economic_calendar_refresh_job import STATE_WRITE_FAILED_WARNING
    r = _build_job_result(
        status="failed",
        warnings=[STATE_WRITE_FAILED_WARNING]
    )
    r.sync_action = "write_failed"

    text_body = format_economic_calendar_refresh_email_text(r)
    html_body = format_economic_calendar_refresh_email_html(r)

    assert text_body.count(STATE_WRITE_FAILED_WARNING) == 1
    assert html_body.count(STATE_WRITE_FAILED_WARNING) == 1
    assert "Rolling economic calendar state save failed to write to disk" in text_body
    assert "Rolling economic calendar state save failed to write to disk" in html_body
