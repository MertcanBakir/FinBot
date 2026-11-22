from datetime import date

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.models.earnings_refresh import (
    BistArchiveRefreshOutcome,
    EarningsEventChangeDetail,
    EarningsEventDiff,
    EarningsRefreshJobResult,
    MarketRefreshOutcome,
    OfficialIrProviderSummary,
    OfficialIrRefreshSummary,
)
from app.services.earnings_refresh_report import (
    build_earnings_refresh_email_view,
    compare_bist_calendars,
    compare_nasdaq_calendars,
    format_earnings_refresh_email_html,
    format_earnings_refresh_email_text,
)


def test_compare_bist_calendars_diff_categories_and_structured_details():
    cov_start = date(2026, 8, 13)
    cov_end = date(2026, 11, 11)

    prev_events = [
        BistEarningsEvent(symbol="AGED", company_name="AGED", earnings_date="2026-08-01", period_start="2026-01-01", period_end="2026-03-31", fiscal_year=2026, fiscal_period="Q1"), # Aged out
        BistEarningsEvent(symbol="UNCHANGED", company_name="UNCH", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"), # Unchanged
        BistEarningsEvent(symbol="CHANGED", company_name="CHG", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"),  # Changed
        BistEarningsEvent(symbol="REMOVED", company_name="REM", earnings_date="2026-08-25", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"),  # Removed
        BistEarningsEvent(symbol="CARRIED", company_name="CARR", earnings_date="2026-09-01", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"), # Carried forward
    ]
    previous = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=prev_events
    )

    fetched_events = [
        BistEarningsEvent(symbol="UNCHANGED", company_name="UNCH", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"),
        BistEarningsEvent(symbol="CHANGED", company_name="CHG", earnings_date="2026-08-22", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"),
        BistEarningsEvent(symbol="ADDED", company_name="ADD", earnings_date="2026-09-10", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2"),
    ]
    fetched_sym_results = [
        KapSymbolEarningsStatus(symbol="UNCHANGED", is_complete=True),
        KapSymbolEarningsStatus(symbol="CHANGED", is_complete=True),
        KapSymbolEarningsStatus(symbol="ADDED", is_complete=True),
        KapSymbolEarningsStatus(symbol="CARRIED", is_complete=False),
    ]
    fetched = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=fetched_events,
        symbol_results=fetched_sym_results
    )

    final_events = fetched_events + [
        BistEarningsEvent(symbol="CARRIED", company_name="CARR", earnings_date="2026-09-01", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2")
    ]
    final = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=final_events,
        symbol_results=fetched_sym_results
    )

    diff = compare_bist_calendars(previous, fetched, final, cov_start, cov_end)

    # Counts integrity
    assert diff.previous_event_count == 5
    assert diff.fetched_event_count == 3
    assert diff.final_event_count == 4
    assert diff.aged_out_count == 1
    assert diff.unchanged_count == 2
    assert diff.changed_count == 1
    assert diff.removed_count == 1
    assert diff.added_count == 1
    assert diff.carried_forward_count == 1
    assert diff.carried_forward_symbols == ["CARRIED"]

    # Structured details check
    assert len(diff.details) == 5
    detail_types = [d.change_type for d in diff.details]
    assert "aged_out" in detail_types
    assert "changed" in detail_types
    assert "removed" in detail_types
    assert "added" in detail_types
    assert "carried_forward" in detail_types

    chg_d = next(d for d in diff.details if d.change_type == "changed")
    assert chg_d.symbol == "CHANGED"
    assert chg_d.old_date == "2026-08-20"
    assert chg_d.new_date == "2026-08-22"
    assert chg_d.fiscal_year == 2026
    assert chg_d.fiscal_period == "Q2"


def test_compare_nasdaq_calendars_single_and_multi_schedules():
    cov_start = date(2026, 8, 13)
    cov_end = date(2026, 11, 11)

    # Multi-schedule test for AAPL: 2 previous schedules -> 2 final schedules
    prev_events = [
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-10-28", session="after_hours", session_label="After Hours"),
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-10-29", session="pre_market", session_label="Pre-Market"),
        NasdaqEarningsEvent(symbol="NVDA", company_name="Nvidia", earnings_date="2026-08-25", session="pre_market", session_label="Pre-Market"), # Unchanged
    ]
    previous = Nasdaq100EarningsCalendar(generated_at="2026-08-12T12:00:00", coverage_start="2026-08-12", coverage_end="2026-11-10", events=prev_events)

    final_events = [
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-10-30", session="after_hours", session_label="After Hours"),
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-10-31", session="pre_market", session_label="Pre-Market"),
        NasdaqEarningsEvent(symbol="NVDA", company_name="Nvidia", earnings_date="2026-08-25", session="pre_market", session_label="Pre-Market"),
    ]
    final = Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11", events=final_events)

    diff = compare_nasdaq_calendars(previous, final, final, cov_start, cov_end)

    # 1. Changed count is exactly 1 (per symbol)
    assert diff.changed_count == 1
    assert diff.unchanged_count == 1

    # 2. Structured detail check
    aapl_d = next(d for d in diff.details if d.symbol == "AAPL" and d.change_type == "changed")
    assert len(aapl_d.old_schedules) == 2
    assert len(aapl_d.new_schedules) == 2
    assert aapl_d.old_date is None  # Must NOT be a comma-separated string!
    assert aapl_d.new_date is None

    result = EarningsRefreshJobResult(
        started_at="2026-08-17T05:04:00+00:00",
        finished_at="2026-08-17T05:04:18+00:00",
        duration_seconds=18.2,
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            diff=diff
        )
    )

    text_body = format_earnings_refresh_email_text(result)
    html_body = format_earnings_refresh_email_html(result)

    # 3. Friendly multi-schedule formatting in plain text and HTML
    assert "AAPL" in text_body
    assert "28 Ekim 2026 • Kapanış sonrası" in text_body
    assert "29 Ekim 2026 • Açılış öncesi" in text_body
    assert "30 Ekim 2026 • Kapanış sonrası" in text_body
    assert "31 Ekim 2026 • Açılış öncesi" in text_body

    assert "28 Ekim 2026 • Kapanış sonrası" in html_body
    assert "30 Ekim 2026 • Kapanış sonrası" in html_body

    # 4. Must NOT leak raw ISO or English session codes
    assert "after_hours" not in text_body
    assert "pre_market" not in text_body
    assert "2026-10-28" not in text_body
    assert "2026-10-29" not in text_body

    assert "after_hours" not in html_body
    assert "pre_market" not in html_body
    assert "2026-10-28" not in html_body
    assert "2026-10-29" not in html_body


# --- 1. Official IR Aggregate Status Decision Table Test ---

def test_official_ir_aggregate_status_decision_table():
    def make_res(providers):
        return EarningsRefreshJobResult(
            started_at="2026-08-17T05:04:00+00:00",
            finished_at="2026-08-17T05:04:18+00:00",
            duration_seconds=18.2,
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            overall_status="partial",
            bist=MarketRefreshOutcome(
                market="BIST100",
                outcome_status="partial",
                fetch_status="success",
                sync_action="preserved_previous",
                coverage_start="2026-08-17",
                coverage_end="2026-11-15",
                official_ir_summary=OfficialIrRefreshSummary(providers=providers)
            ),
            nasdaq=MarketRefreshOutcome(
                market="NASDAQ100",
                outcome_status="success",
                fetch_status="success",
                sync_action="replaced",
                coverage_start="2026-08-17",
                coverage_end="2026-11-15"
            )
        )

    # 1. 7 partial -> partial (orange)
    providers_7_partial = [
        OfficialIrProviderSummary(symbol=f"S{i}", source_provider=f"P{i}", status="partial", parser_failed=False)
        for i in range(7)
    ]
    v1 = build_earnings_refresh_email_view(make_res(providers_7_partial))
    assert v1["sources"]["ir"]["status"] == "partial"
    assert v1["sources"]["ir"]["color"] == "#B45309"
    assert v1["sources"]["ir"]["background"] == "#FEF3C7"
    assert v1["sources"]["ir"]["status_text"] == "7 / 7 kısmi"

    # HTML check for 7 partial
    html_1 = format_earnings_refresh_email_html(make_res(providers_7_partial))
    assert 'color: #B45309; background-color: #FEF3C7' in html_1
    assert '7 / 7 kısmi</span>' in html_1

    # 2. 7 failed -> failed (red)
    providers_7_failed = [
        OfficialIrProviderSummary(symbol=f"S{i}", source_provider=f"P{i}", status="failed", parser_failed=False)
        for i in range(7)
    ]
    v2 = build_earnings_refresh_email_view(make_res(providers_7_failed))
    assert v2["sources"]["ir"]["status"] == "failed"
    assert v2["sources"]["ir"]["color"] == "#B91C1C"
    assert v2["sources"]["ir"]["background"] == "#FEE2E2"
    assert v2["sources"]["ir"]["status_text"] == "0 / 7 başarılı"

    # HTML check for 7 failed
    html_2 = format_earnings_refresh_email_html(make_res(providers_7_failed))
    assert 'color: #B91C1C; background-color: #FEE2E2' in html_2
    assert '0 / 7 başarılı</span>' in html_2

    # 3. 6 success + 1 partial -> partial (orange)
    providers_6_succ_1_part = [
        OfficialIrProviderSummary(symbol=f"S{i}", source_provider=f"P{i}", status="success", parser_failed=False)
        for i in range(6)
    ] + [OfficialIrProviderSummary(symbol="SPart", source_provider="PPart", status="partial", parser_failed=False)]
    v3 = build_earnings_refresh_email_view(make_res(providers_6_succ_1_part))
    assert v3["sources"]["ir"]["status"] == "partial"
    assert v3["sources"]["ir"]["color"] == "#B45309"
    assert v3["sources"]["ir"]["status_text"] == "6 başarılı • 1 kısmi"

    # 4. 2 partial + 5 failed -> partial (orange)
    providers_2_part_5_fail = [
        OfficialIrProviderSummary(symbol=f"S{i}", source_provider=f"P{i}", status="partial", parser_failed=False)
        for i in range(2)
    ] + [
        OfficialIrProviderSummary(symbol=f"F{i}", source_provider=f"PF{i}", status="failed", parser_failed=False)
        for i in range(5)
    ]
    v4 = build_earnings_refresh_email_view(make_res(providers_2_part_5_fail))
    assert v4["sources"]["ir"]["status"] == "partial"
    assert v4["sources"]["ir"]["color"] == "#B45309"
    assert v4["sources"]["ir"]["status_text"] == "2 kısmi • 5 hatalı"

    # 5. 7 success -> success (green)
    providers_7_success = [
        OfficialIrProviderSummary(symbol=f"S{i}", source_provider=f"P{i}", status="success", parser_failed=False)
        for i in range(7)
    ]
    v5 = build_earnings_refresh_email_view(make_res(providers_7_success))
    assert v5["sources"]["ir"]["status"] == "success"
    assert v5["sources"]["ir"]["color"] == "#166534"
    assert v5["sources"]["ir"]["status_text"] == "7 / 7 başarılı"

    # 6. 0 providers -> unknown (gray)
    v6 = build_earnings_refresh_email_view(make_res([]))
    assert v6["sources"]["ir"]["status"] == "unknown"
    assert v6["sources"]["ir"]["color"] == "#6B7280"
    assert v6["sources"]["ir"]["status_text"] == "Yapılandırılmadı"


# --- 2. Exact Warning Deduplication Across All 5 Locations Test ---

def test_format_earnings_refresh_email_exact_error_deduplication_all_five_locations():
    """Verify that identical warning string in 5 different result locations is deduplicated to count=1."""
    exact_msg = "CRITICAL_SHARED_DATABASE_TIMEOUT"

    ir_provider = OfficialIrProviderSummary(
        symbol="MAVI",
        source_provider="MAVI IR",
        status="failed",
        parser_failed=False,
        retry_count=2,
        warnings=[exact_msg]
    )

    archive_outcome = BistArchiveRefreshOutcome(
        archive_status="failed",
        error=exact_msg,
        warnings=[exact_msg, "Different Archive Warning"]
    )

    result = EarningsRefreshJobResult(
        started_at="2026-08-13T12:00:00Z",
        finished_at="2026-08-13T12:00:05Z",
        duration_seconds=5.2,
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        overall_status="failed",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="failed",
            fetch_status="failed",
            sync_action="preserved_previous",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11",
            error=exact_msg,
            warnings=[exact_msg, "Different BIST Warning"],
            official_ir_summary=OfficialIrRefreshSummary(
                kap_fetch_status="failed",
                providers=[ir_provider]
            ),
            archive_outcome=archive_outcome
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11"
        )
    )

    text_body = format_earnings_refresh_email_text(result)
    html_body = format_earnings_refresh_email_html(result)

    # 1. Exact error message must appear exactly ONCE in both plain-text and HTML
    assert text_body.count(exact_msg) == 1
    assert html_body.count(exact_msg) == 1

    # 2. Distinct warnings must still be preserved
    assert "Different BIST Warning" in text_body
    assert "Different Archive Warning" in text_body

    # 3. Provider clean reason in Data Sources must be clean and not raw warning
    assert "MAVI: Sağlayıcı hatası (2 tekrar denemesi)" in text_body
    assert "MAVI</strong>: Sağlayıcı hatası (2 tekrar denemesi)" in html_body


# --- 3. Provider Warning Deduplication & Clean Reasons Test ---

def test_provider_warning_deduplication_and_clean_reasons():
    warning = "MAVI table structure changed unexpectedly"

    ir_summary = OfficialIrRefreshSummary(
        providers=[
            OfficialIrProviderSummary(
                symbol="MAVI",
                source_provider="MAVI IR",
                status="partial",
                parser_failed=False,
                retry_count=0,
                warnings=[warning],
            ),
            OfficialIrProviderSummary(
                symbol="GARAN",
                source_provider="GARAN IR",
                status="failed",
                parser_failed=False,
                retry_count=2,
                warnings=["GARAN HTTP 500 server error"],
            ),
            OfficialIrProviderSummary(
                symbol="THYAO",
                source_provider="THYAO IR",
                status="failed",
                parser_failed=True,
                warnings=["THYAO report link missing"],
            ),
            OfficialIrProviderSummary(
                symbol="AKSA",
                source_provider="AKSA IR",
                status="failed",
                parser_failed=False,
                retry_count=0,
                warnings=["AKSA network drop"],
            )
        ]
    )

    result = EarningsRefreshJobResult(
        started_at="2026-08-17T05:04:00+00:00",
        finished_at="2026-08-17T05:04:18+00:00",
        duration_seconds=18.2,
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        overall_status="partial",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="partial",
            fetch_status="partial",
            sync_action="preserved_previous",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            official_ir_summary=ir_summary,
            warnings=[warning, "General BIST Warning"]  # Same warning placed in bist.warnings as well
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15"
        )
    )

    text_body = format_earnings_refresh_email_text(result)
    html_body = format_earnings_refresh_email_html(result)

    # 1. Exact warning count must be exactly 1 across the whole email!
    assert text_body.count(warning) == 1
    assert html_body.count(warning) == 1

    # 2. Problematic IR provider reasons in Data Sources section must be clean and not raw warnings
    assert "MAVI: Kısmi veri" in text_body
    assert "GARAN: Sağlayıcı hatası (2 tekrar denemesi)" in text_body
    assert "THYAO: Parser hatası" in text_body
    assert "AKSA: Sağlayıcı hatası" in text_body

    assert "MAVI</strong>: Kısmi veri" in html_body
    assert "GARAN</strong>: Sağlayıcı hatası (2 tekrar denemesi)" in html_body
    assert "THYAO</strong>: Parser hatası" in html_body
    assert "AKSA</strong>: Sağlayıcı hatası" in html_body


# --- 4. User-Friendly Change Details Formatting Test ---

def test_user_friendly_change_details_bist_and_nasdaq():
    bist_diff = EarningsEventDiff(
        added_count=1,
        changed_count=1,
        removed_count=1,
        details=[
            EarningsEventChangeDetail(
                market="BIST100",
                change_type="added",
                symbol="BIMAS",
                fiscal_year=2026,
                fiscal_period="Q3",
                new_date="2026-11-09",
                source="Official IR"
            ),
            EarningsEventChangeDetail(
                market="BIST100",
                change_type="changed",
                symbol="CCOLA",
                fiscal_year=2026,
                fiscal_period="Q2",
                old_date="2026-08-20",
                new_date="2026-08-22",
                source="KAP"
            ),
            EarningsEventChangeDetail(
                market="BIST100",
                change_type="removed",
                symbol="GARAN",
                fiscal_year=2026,
                fiscal_period="Q3",
                old_date="2026-10-28",
                source="Official IR"
            ),
        ]
    )

    nasdaq_diff = EarningsEventDiff(
        added_count=1,
        changed_count=1,
        details=[
            EarningsEventChangeDetail(
                market="NASDAQ100",
                change_type="added",
                symbol="AAPL",
                new_date="2026-10-29",
                session="after_hours",
                source="Yahoo Finance"
            ),
            EarningsEventChangeDetail(
                market="NASDAQ100",
                change_type="changed",
                symbol="MSFT",
                old_date="2026-10-28",
                old_session="after_hours",
                new_date="2026-10-29",
                new_session="after_hours",
                source="Yahoo Finance"
            ),
        ]
    )

    result = EarningsRefreshJobResult(
        started_at="2026-08-17T05:04:00+00:00",
        finished_at="2026-08-17T05:04:18+00:00",
        duration_seconds=18.2,
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            total_symbol_count=100,
            completed_symbol_count=100,
            scheduled_symbol_count=32,
            healthy_negative_symbol_count=68,
            diff=bist_diff
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            diff=nasdaq_diff
        )
    )

    text_body = format_earnings_refresh_email_text(result)
    html_body = format_earnings_refresh_email_html(result)

    # 1. BIST Added
    assert "BIMAS • Q3 2026 • 9 Kasım 2026" in text_body
    assert "BIMAS" in html_body
    assert "Q3 2026" in html_body
    assert "9 Kasım 2026" in html_body

    # Verify no raw ISO or period range in normal output
    assert "[2026-01-01" not in text_body
    assert "[2026-01-01" not in html_body
    assert "(added)" not in text_body
    assert "(added)" not in html_body

    # 2. BIST Changed
    assert "CCOLA • Q2 2026" in text_body
    assert "20 Ağustos 2026 → 22 Ağustos 2026" in text_body
    assert "20 Ağustos 2026" in html_body
    assert "22 Ağustos 2026" in html_body

    # 3. BIST Removed
    assert "GARAN • Q3 2026 • 28 Ekim 2026" in text_body
    assert "GARAN" in html_body
    assert "28 Ekim 2026" in html_body

    # 4. Nasdaq Added with Turkish session
    assert "AAPL • 29 Ekim 2026 • Kapanış sonrası" in text_body
    assert "AAPL" in html_body
    assert "29 Ekim 2026" in html_body
    assert "Kapanış sonrası" in html_body

    # 5. Nasdaq Changed
    assert "28 Ekim 2026 • Kapanış sonrası → 29 Ekim 2026 • Kapanış sonrası" in text_body


# --- 5. HTML Escaping Safety Test ---

def test_format_earnings_refresh_email_html_escaping():
    malicious_script = '<script>alert("x")</script>'
    diff = EarningsEventDiff(
        added_count=1,
        details=[
            EarningsEventChangeDetail(
                market="BIST100",
                change_type="added",
                symbol=malicious_script,
                fiscal_year=2026,
                fiscal_period="Q3",
                new_date="2026-11-09"
            )
        ]
    )

    result = EarningsRefreshJobResult(
        started_at="2026-08-13T12:00:00Z",
        finished_at="2026-08-13T12:00:05Z",
        duration_seconds=5.2,
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        overall_status="failed",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="failed",
            fetch_status="failed",
            sync_action="preserved_previous",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11",
            error=malicious_script,
            warnings=[malicious_script],
            diff=diff
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11"
        )
    )

    html_out = format_earnings_refresh_email_html(result)
    assert "<script>" not in html_out
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html_out


# --- 6. Detail Limits (20 items max) Test ---

def test_format_earnings_refresh_email_detail_limits():
    details_25 = [
        EarningsEventChangeDetail(
            market="BIST100",
            change_type="added",
            symbol=f"SYM{i}",
            fiscal_year=2026,
            fiscal_period="Q3",
            new_date=f"2026-09-{i:02d}"
        ) for i in range(1, 26)
    ]
    result = EarningsRefreshJobResult(
        started_at="2026-08-17T05:04:00+00:00",
        finished_at="2026-08-17T05:04:18+00:00",
        duration_seconds=18.2,
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            total_symbol_count=100,
            completed_symbol_count=100,
            scheduled_symbol_count=50,
            healthy_negative_symbol_count=50,
            diff=EarningsEventDiff(added_count=25, details=details_25)
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15"
        )
    )

    text = format_earnings_refresh_email_text(result)
    html_out = format_earnings_refresh_email_html(result)

    assert "SYM20" in text
    assert "SYM21" not in text
    assert "... ve 5 kayıt daha" in text

    assert "SYM20" in html_out
    assert "SYM21" not in html_out
    assert "... ve 5 kayıt daha" in html_out


# --- 7. None BIST Metrics Notice Test ---

def test_format_earnings_refresh_email_none_bist_metrics_notice():
    result = EarningsRefreshJobResult(
        started_at="2026-08-13T12:00:00Z",
        finished_at="2026-08-13T12:00:05Z",
        duration_seconds=5.2,
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        overall_status="partial",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="failed",
            fetch_status="failed",
            sync_action="preserved_previous",
            state_preserved=True,
            coverage_start="2026-08-13",
            coverage_end="2026-11-11",
            total_symbol_count=None,
            completed_symbol_count=None,
            unavailable_symbol_count=None,
            scheduled_symbol_count=None,
            healthy_negative_symbol_count=None,
            error="KAP down"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11"
        )
    )

    text = format_earnings_refresh_email_text(result)
    html_out = format_earnings_refresh_email_html(result)

    assert "Güncel sembol kontrol sayıları üretilemedi." in text
    assert "Önceki güvenilir durum korundu." in text

    assert "Güncel sembol kontrol sayıları üretilemedi." in html_out
    assert "Önceki güvenilir durum korundu." in html_out


def test_compare_bist_calendars_ir_carried_forward_included():
    """Verify Official IR carried forward events are correctly identified in BIST diff."""
    from app.models.earnings import KapSymbolEarningsStatus

    cov_start = date(2026, 8, 18)
    cov_end = date(2026, 11, 17)

    # Previous cache has MAVI Official IR event
    mavi_prev = BistEarningsEvent(
        symbol="MAVI",
        company_name="MAVİ GİYİM SANAYİ VE TİCARET A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-09-16",
        period_start="2026-02-01",
        period_end="2026-07-31",
        source="Official IR",
        source_provider="MAVI Investor Relations",
        source_type="official_financial_calendar",
        date_type="company_announced",
    )
    previous_cal = Bist100EarningsCalendar(
        generated_at="2026-08-17T05:00:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-16",
        status="success",
        provider_status="success",
        events=[mavi_prev],
    )

    # Current fetched calendar failed MAVI (ir_incomplete_symbols=["MAVI"]) and has no MAVI event
    # KAP symbol results are complete
    fetched_cal = Bist100EarningsCalendar(
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="partial",
        provider_status="partial",
        events=[],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="MAVI", is_complete=True, status="healthy_negative")
        ],
        ir_incomplete_symbols=["MAVI"],
    )

    # Final combined calendar preserved MAVI from previous cache
    final_cal = Bist100EarningsCalendar(
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="partial",
        provider_status="partial",
        events=[mavi_prev],
    )

    diff = compare_bist_calendars(previous_cal, fetched_cal, final_cal, cov_start, cov_end)

    assert diff.carried_forward_count == 1
    assert diff.carried_forward_symbols == ["MAVI"]
    assert any(
        d.change_type == "carried_forward" and d.symbol == "MAVI"
        for d in diff.details
    )


def test_compare_bist_calendars_mixed_fetched_and_carried_forward():
    """Verify that when a symbol is in incomplete_symbols but some of its events are fetched today,
    only the truly unfetched previous events are counted as carried_forward."""
    from app.models.earnings import KapSymbolEarningsStatus

    cov_start = date(2026, 8, 18)
    cov_end = date(2026, 11, 17)

    mavi_q2 = BistEarningsEvent(
        symbol="MAVI",
        company_name="MAVİ GİYİM SANAYİ VE TİCARET A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-09-16",
        period_start="2026-02-01",
        period_end="2026-07-31",
        source="Official IR",
        source_provider="MAVI Investor Relations",
        source_type="official_financial_calendar",
        date_type="company_announced",
    )
    mavi_q3 = BistEarningsEvent(
        symbol="MAVI",
        company_name="MAVİ GİYİM SANAYİ VE TİCARET A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q3",
        earnings_date="2026-10-20",
        period_start="2026-02-01",
        period_end="2026-10-31",
        source="Official IR",
        source_provider="MAVI Investor Relations",
        source_type="official_financial_calendar",
        date_type="company_announced",
    )

    # Previous has both Q2 and Q3
    previous_cal = Bist100EarningsCalendar(
        generated_at="2026-08-17T05:00:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-16",
        status="success",
        provider_status="success",
        events=[mavi_q2, mavi_q3],
    )

    # Fetched today: only Q2 arrived successfully, Q3 failed / not present
    fetched_cal = Bist100EarningsCalendar(
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="partial",
        provider_status="partial",
        events=[mavi_q2],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="MAVI", is_complete=True, status="healthy_negative")
        ],
        ir_incomplete_symbols=["MAVI"],
    )

    # Final merged calendar has both Q2 (fetched) and Q3 (carried forward)
    final_cal = Bist100EarningsCalendar(
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="partial",
        provider_status="partial",
        events=[mavi_q2, mavi_q3],
    )

    diff = compare_bist_calendars(previous_cal, fetched_cal, final_cal, cov_start, cov_end)

    # 1. Carried forward count is exactly 1 (only Q3 was carried forward)
    assert diff.carried_forward_count == 1
    assert diff.carried_forward_symbols == ["MAVI"]

    # 2. Carried forward detail belongs to Q3, not Q2
    carried_details = [d for d in diff.details if d.change_type == "carried_forward"]
    assert len(carried_details) == 1
    assert carried_details[0].symbol == "MAVI"
    assert carried_details[0].fiscal_period == "Q3"
    assert carried_details[0].new_date == "2026-10-20"

    # 3. No carried forward detail exists for Q2
    assert not any(d.change_type == "carried_forward" and d.fiscal_period == "Q2" for d in diff.details)
