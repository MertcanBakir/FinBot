from datetime import date

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    BistFiscalPeriodEarningsCalendar,
    KapEarningsFetchResult,
    OfficialIrIssue,
    OfficialIrProviderResult,
)
from app.services.bist_fiscal_period_archive import is_incoming_event_superior_or_newer
from app.services.bist_reconciliation import (
    BIST_DAILY_MIN_EARNINGS_DATE,
    reconcile_kap_and_official_ir,
    scope_official_ir_results,
)
from app.services.calendar_state_store import merge_bist_partial_calendar


def test_reconciliation_ir_fills_missing_kap_period():
    kap_res = KapEarningsFetchResult(
        status="success",
        events=[],
        normalized_events=[],
        warnings=[]
    )
    bimas_ev = BistEarningsEvent(
        symbol="BIMAS",
        company_name="BİM BİRLEŞİK MAĞAZALAR A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-08-17",
        period_start="2026-01-01",
        period_end="2026-06-30",
        publish_date=None,
        date_type="company_announced",
        source="Official IR",
        source_provider="BIMAS Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="bimas:2026:Q2",
        disclosure_index=None,
        source_url="https://www.bim.com.tr/Categories/640/finansal-takvim.aspx"
    )
    ir_res = OfficialIrProviderResult(
        symbol="BIMAS",
        source_provider="BIMAS Investor Relations",
        status="success",
        events=[bimas_ev]
    )

    combined = reconcile_kap_and_official_ir(kap_res, [ir_res])
    assert combined.status == "success"
    assert len(combined.normalized_events) == 1
    ev = combined.normalized_events[0]
    assert ev.symbol == "BIMAS"
    assert ev.source == "Official IR"
    assert ev.source_provider == "BIMAS Investor Relations"


def test_reconciliation_same_date_kap_and_ir_prefers_kap():
    kap_ev = BistEarningsEvent(
        symbol="GARAN",
        company_name="TÜRKİYE GARANTİ BANKASI A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-07-30",
        period_start="2026-01-01",
        period_end="2026-06-30",
        publish_date="2026-01-15",
        date_type="company_announced",
        source="KAP",
        source_provider="KAP Financial Calendar",
        source_type="financial_calendar",
        disclosure_index=123456,
        source_url="https://www.kap.org.tr/tr/Bildirim/123456"
    )
    ir_ev = BistEarningsEvent(
        symbol="GARAN",
        company_name="TÜRKİYE GARANTİ BANKASI A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-07-30",
        period_start="2026-01-01",
        period_end="2026-06-30",
        publish_date=None,
        date_type="company_announced",
        source="Official IR",
        source_provider="GARAN Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="garan:2026:Q2",
        disclosure_index=None,
        source_url="https://www.garantibbvainvestorrelations.com"
    )
    kap_res = KapEarningsFetchResult(
        status="success",
        events=[kap_ev],
        normalized_events=[kap_ev],
        warnings=[]
    )
    ir_res = OfficialIrProviderResult(
        symbol="GARAN",
        source_provider="GARAN Investor Relations",
        status="success",
        events=[ir_ev]
    )

    combined = reconcile_kap_and_official_ir(kap_res, [ir_res])
    assert combined.status == "success"
    assert len(combined.normalized_events) == 1
    ev = combined.normalized_events[0]
    assert ev.source == "KAP"
    assert ev.disclosure_index == 123456


def test_reconciliation_different_date_conflict_prefers_kap_and_marks_partial():
    kap_ev = BistEarningsEvent(
        symbol="GARAN",
        company_name="TÜRKİYE GARANTİ BANKASI A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-07-28",  # KAP date
        period_start="2026-01-01",
        period_end="2026-06-30",
        publish_date="2026-01-15",
        date_type="company_announced",
        source="KAP",
        source_provider="KAP Financial Calendar",
        source_type="financial_calendar",
        disclosure_index=123456,
        source_url="https://www.kap.org.tr/tr/Bildirim/123456"
    )
    ir_ev = BistEarningsEvent(
        symbol="GARAN",
        company_name="TÜRKİYE GARANTİ BANKASI A.Ş.",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-07-30",  # IR date
        period_start="2026-01-01",
        period_end="2026-06-30",
        publish_date=None,
        date_type="company_announced",
        source="Official IR",
        source_provider="GARAN Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="garan:2026:Q2",
        disclosure_index=None,
        source_url="https://www.garantibbvainvestorrelations.com"
    )
    kap_res = KapEarningsFetchResult(
        status="success",
        events=[kap_ev],
        normalized_events=[kap_ev],
        warnings=[]
    )
    ir_res = OfficialIrProviderResult(
        symbol="GARAN",
        source_provider="GARAN Investor Relations",
        status="success",
        events=[ir_ev]
    )

    combined = reconcile_kap_and_official_ir(kap_res, [ir_res])
    assert combined.status == "partial"
    assert len(combined.normalized_events) == 1
    ev = combined.normalized_events[0]
    assert ev.source == "KAP"
    assert ev.earnings_date == "2026-07-28"  # KAP retained
    assert any("Conflict" in w for w in combined.warnings)


def test_reconciliation_decision_table_comprehensive():
    # 1. KAP success + IR partial -> partial
    ir_partial = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="partial", parser_failed=False, events=[], warnings=["Warning"]
    )
    res1 = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="success"), [ir_partial])
    assert res1.status == "partial"
    assert "BIMAS" in res1.ir_incomplete_symbols

    # 2. KAP failed + usable IR event -> partial
    ir_ev = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-17", period_start="2026-01-01", period_end="2026-06-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q2", source_url="https://bim"
    )
    ir_success = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[ir_ev]
    )
    res2 = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="failed"), [ir_success])
    assert res2.status == "partial"
    assert len(res2.normalized_events) == 1

    # 3. KAP failed + 0 usable IR events (even if IR success) -> failed
    ir_empty_success = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[]
    )
    res3 = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="failed"), [ir_empty_success])
    assert res3.status == "failed"

    # 4. KAP failed + empty IR results list -> failed
    res4 = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="failed"), [])
    assert res4.status == "failed"

    # 5. KAP failed + all IR failed -> failed
    ir_failed = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="failed", parser_failed=True, events=[]
    )
    res5 = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="failed"), [ir_failed])
    assert res5.status == "failed"


def test_reconciliation_mixed_valid_and_malformed_marks_partial_and_tracks_symbol():
    # Provider with status="partial" (valid event + malformed warning)
    valid_ev = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-17", period_start="2026-01-01", period_end="2026-06-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q2", source_url="https://bim"
    )
    ir_mixed = OfficialIrProviderResult(
        symbol="BIMAS",
        source_provider="BIMAS IR",
        status="partial",
        parser_failed=False,
        events=[valid_ev],
        warnings=["Could not parse valid date from 'invalid-date'"]
    )
    combined = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="success"), [ir_mixed])
    assert combined.status == "partial"
    assert "BIMAS" in combined.ir_incomplete_symbols
    assert len(combined.normalized_events) == 1


def test_reconciliation_duplicate_ir_events_same_date_keeps_one():
    ev1 = BistEarningsEvent(
        symbol="TEST", company_name="TEST", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-05-01", period_start="2026-01-01", period_end="2026-03-31",
        date_type="company_announced", source="Official IR", source_provider="TEST IR",
        source_type="official_financial_calendar", source_event_id="test:2026:Q1:1", source_url="https://test"
    )
    ev2 = BistEarningsEvent(
        symbol="TEST", company_name="TEST", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-05-01", period_start="2026-01-01", period_end="2026-03-31",
        date_type="company_announced", source="Official IR", source_provider="TEST IR",
        source_type="official_financial_calendar", source_event_id="test:2026:Q1:2", source_url="https://test"
    )
    ir_res = OfficialIrProviderResult(
        symbol="TEST", source_provider="TEST IR", status="success", events=[ev1, ev2]
    )
    res = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="success"), [ir_res])
    assert len(res.normalized_events) == 1
    assert res.status == "success"


def test_reconciliation_conflicting_ir_dates_excludes_identity_and_marks_incomplete_without_previous_cache():
    ev1 = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q3",
        earnings_date="2026-11-09", period_start="2026-01-01", period_end="2026-09-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q3:1", source_url="https://bim"
    )
    ev2 = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q3",
        earnings_date="2026-11-10", period_start="2026-01-01", period_end="2026-09-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q3:2", source_url="https://bim"
    )
    ir_res = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[ev1, ev2]
    )
    res = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="success"), [ir_res])

    # Must be partial
    assert res.status == "partial"
    # Identity excluded from current output!
    assert len(res.normalized_events) == 0
    # Tracked in incomplete symbols
    assert "BIMAS" in res.ir_incomplete_symbols
    # Warning contains both dates
    assert any("2026-11-09" in w and "2026-11-10" in w for w in res.warnings)


def test_reconciliation_conflicting_ir_dates_carries_forward_previous_reliable_cache():
    # Previous cache has BIMAS Q3 = 2026-11-08
    prev_bimas = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q3",
        earnings_date="2026-11-08", period_start="2026-01-01", period_end="2026-09-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q3", source_url="https://bim"
    )
    previous_bist = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-13T12:00:00Z",
        coverage_start="2026-08-14",
        coverage_end="2026-11-13",
        status="success",
        events=[prev_bimas],
        symbol_results=[]
    )

    # Current fetch has IR conflict: 2026-11-09 vs 2026-11-10
    ev1 = prev_bimas.model_copy(update={"earnings_date": "2026-11-09"})
    ev2 = prev_bimas.model_copy(update={"earnings_date": "2026-11-10"})
    ir_res = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[ev1, ev2]
    )
    comb = reconcile_kap_and_official_ir(KapEarningsFetchResult(status="success"), [ir_res])

    # Current rolling calendar has 0 events for BIMAS and ir_incomplete_symbols=["BIMAS"]
    current_bist = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-14T12:00:00Z",
        coverage_start="2026-08-14",
        coverage_end="2026-11-13",
        status=comb.status,
        events=comb.normalized_events,
        ir_incomplete_symbols=comb.ir_incomplete_symbols,
        warnings=comb.warnings
    )

    merged = merge_bist_partial_calendar(previous=previous_bist, current=current_bist)
    assert len(merged.events) == 1
    # Final is the previous reliable 2026-11-08, NEVER arbitrarily 2026-11-10
    assert merged.events[0].earnings_date == "2026-11-08"
    assert merged.events[0].source == "Official IR"
    assert any("carried forward" in w.lower() for w in merged.warnings)


def test_is_incoming_event_superior_or_newer_rules():
    kap_ev = BistEarningsEvent(
        symbol="TEST",
        company_name="TEST",
        fiscal_year=2026,
        fiscal_period="Q1",
        earnings_date="2026-05-01",
        period_start="2026-01-01",
        period_end="2026-03-31",
        publish_date="2026-01-10",
        disclosure_index=100,
        source="KAP",
        source_type="financial_calendar",
        source_url="https://kap"
    )
    ir_ev = BistEarningsEvent(
        symbol="TEST",
        company_name="TEST",
        fiscal_year=2026,
        fiscal_period="Q1",
        earnings_date="2026-05-02",
        period_start="2026-01-01",
        period_end="2026-03-31",
        date_type="company_announced",
        source="Official IR",
        source_provider="TEST IR",
        source_type="official_financial_calendar",
        source_event_id="test:2026:Q1",
        source_url="https://ir"
    )
    # KAP vs IR -> KAP always wins
    assert is_incoming_event_superior_or_newer(inc_ev=kap_ev, cur_ev=ir_ev) is True
    assert is_incoming_event_superior_or_newer(inc_ev=ir_ev, cur_ev=kap_ev) is False

    # Newer KAP replaces older KAP
    newer_kap = kap_ev.model_copy(update={"publish_date": "2026-01-20"})
    assert is_incoming_event_superior_or_newer(inc_ev=newer_kap, cur_ev=kap_ev) is True
    assert is_incoming_event_superior_or_newer(inc_ev=kap_ev, cur_ev=newer_kap) is False

    # Incoming IR replaces same IR provider
    newer_ir = ir_ev.model_copy(update={"earnings_date": "2026-05-05"})
    assert is_incoming_event_superior_or_newer(inc_ev=newer_ir, cur_ev=ir_ev) is True


def test_bist_fiscal_period_calendar_validation_aksa_company_announced():
    data_aksa = {
        "schema_version": "2.1",
        "dataset_type": "fiscal_period_partition",
        "generated_at": "2026-08-14T00:00:00Z",
        "market": "BIST100",
        "fiscal_year": 2026,
        "fiscal_period": "Q1",
        "status": "success",
        "provider_status": "success",
        "events": [
            {
                "symbol": "AKSA",
                "company_name": "AKSA",
                "fiscal_year": 2026,
                "fiscal_period": "Q1",
                "earnings_date": "2026-05-07",
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "publish_date": None,
                "date_type": "company_announced",
                "source": "Official IR",
                "source_provider": "AKSA Investor Relations",
                "source_type": "official_financial_calendar",
                "source_event_id": "aksa:2026:Q1",
                "disclosure_index": None,
                "source_url": "https://aksa"
            }
        ],
        "warnings": []
    }
    cal_aksa = BistFiscalPeriodEarningsCalendar.model_validate(data_aksa)
    assert cal_aksa.events[0].date_type == "company_announced"


def test_reconciliation_historical_ir_conflict_excluded_outside_scope_does_not_make_status_partial():
    # MAVI Q1 2023 conflict: 2023-06-08 vs 2023-06-09
    ev1 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-08", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-387", source_url="https://mavi"
    )
    ev2 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-09", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-388", source_url="https://mavi"
    )
    ir_res = OfficialIrProviderResult(
        symbol="MAVI", source_provider="MAVI IR", status="success", events=[ev1, ev2]
    )

    # 2026 relevance scope (e.g. 2026-08-14 -> 2026-11-13)
    res_2026 = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success"),
        [ir_res],
        relevance_start=date(2026, 8, 14),
        relevance_end=date(2026, 11, 13),
        emit_out_of_scope_warnings=False
    )

    # Status must be SUCCESS (not made partial by historical 2023 conflict)
    assert res_2026.status == "success"
    # Identity is still excluded from current normalized output
    assert len(res_2026.normalized_events) == 0
    # MAVI is NOT marked as incomplete in current refresh
    assert "MAVI" not in res_2026.ir_incomplete_symbols
    # When emit_out_of_scope_warnings=False, no warning is emitted
    assert len(res_2026.warnings) == 0

    # When emit_out_of_scope_warnings=True, non-blocking historical notice is emitted
    res_2026_with_warn = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success"),
        [ir_res],
        relevance_start=date(2026, 8, 14),
        relevance_end=date(2026, 11, 13),
        emit_out_of_scope_warnings=True
    )
    assert any("Historical Official IR conflict" in w and "outside current relevance scope" in w for w in res_2026_with_warn.warnings)


def test_reconciliation_conflict_within_relevance_scope_makes_status_partial():
    # Same MAVI Q1 2023 conflict: 2023-06-08 vs 2023-06-09
    ev1 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-08", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-387", source_url="https://mavi"
    )
    ev2 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-09", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-388", source_url="https://mavi"
    )
    ir_res = OfficialIrProviderResult(
        symbol="MAVI", source_provider="MAVI IR", status="success", events=[ev1, ev2]
    )

    # 2023 relevance scope (e.g. 2023-01-01 -> 2023-12-31)
    res_2023 = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success"),
        [ir_res],
        relevance_start=date(2023, 1, 1),
        relevance_end=date(2023, 12, 31)
    )

    # Status must be PARTIAL in 2023 scope
    assert res_2023.status == "partial"
    assert len(res_2023.normalized_events) == 0
    assert "MAVI" in res_2023.ir_incomplete_symbols
    assert any("Conflicting Official IR dates" in w for w in res_2023.warnings)


def test_reconciliation_unscoped_conflict_preserves_strict_partial():
    ev1 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-08", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-387", source_url="https://mavi"
    )
    ev2 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-09", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-388", source_url="https://mavi"
    )
    ir_res = OfficialIrProviderResult(
        symbol="MAVI", source_provider="MAVI IR", status="success", events=[ev1, ev2]
    )

    # No relevance scope provided -> strict behavior preserved
    res_unscoped = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success"),
        [ir_res]
    )
    assert res_unscoped.status == "partial"
    assert "MAVI" in res_unscoped.ir_incomplete_symbols


def test_reconciliation_historical_kap_ir_conflict_outside_scope():
    kap_ev = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-08", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="KAP", source_provider="KAP",
        source_type="financial_calendar", disclosure_index=1157375, source_url="https://kap/1157375"
    )
    ir_ev = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
        earnings_date="2023-06-09", period_start="2023-02-01", period_end="2023-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-388", source_url="https://mavi"
    )

    # 2026 scope: KAP event is in 2023 (outside scope), emit_out_of_scope_warnings=False (clean)
    res = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success", events=[kap_ev], normalized_events=[kap_ev]),
        [OfficialIrProviderResult(symbol="MAVI", source_provider="MAVI IR", status="success", events=[ir_ev])],
        relevance_start=date(2026, 1, 1),
        relevance_end=date(2026, 12, 31),
        emit_out_of_scope_warnings=False
    )
    # KAP event retained, status is success
    assert res.status == "success"
    assert len(res.normalized_events) == 1
    assert res.normalized_events[0].earnings_date == "2023-06-08"
    assert len(res.warnings) == 0

    # With emit_out_of_scope_warnings=True
    res_warn = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success", events=[kap_ev], normalized_events=[kap_ev]),
        [OfficialIrProviderResult(symbol="MAVI", source_provider="MAVI IR", status="success", events=[ir_ev])],
        relevance_start=date(2026, 1, 1),
        relevance_end=date(2026, 12, 31),
        emit_out_of_scope_warnings=True
    )
    assert any("Historical KAP/IR conflict" in w for w in res_warn.warnings)


def test_reconciliation_kap_ir_conflict_in_scope():
    kap_ev = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-06-11", period_start="2026-02-01", period_end="2026-04-30",
        date_type="company_announced", source="KAP", source_provider="KAP",
        source_type="financial_calendar", disclosure_index=2000, source_url="https://kap/2000"
    )
    ir_ev = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-06-12", period_start="2026-02-01", period_end="2026-04-30",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="eid-2000", source_url="https://mavi"
    )

    # 2026 scope: in scope
    res = reconcile_kap_and_official_ir(
        KapEarningsFetchResult(status="success", events=[kap_ev], normalized_events=[kap_ev]),
        [OfficialIrProviderResult(symbol="MAVI", source_provider="MAVI IR", status="success", events=[ir_ev])],
        relevance_start=date(2026, 1, 1),
        relevance_end=date(2026, 12, 31)
    )
    assert res.status == "partial"
    assert len(res.normalized_events) == 1
    assert res.normalized_events[0].earnings_date == "2026-06-11"  # KAP retained
    assert any("Conflict for MAVI" in w for w in res.warnings)


# ==========================================
# Unit Tests for scope_official_ir_results
# ==========================================

def test_scope_official_ir_results_filters_by_earnings_date_not_fiscal_year():
    # FY 2025 announcement on 2026-03-17 -> IN SCOPE for daily 2026-01-01
    ev_fy25 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2025, fiscal_period="FY",
        earnings_date="2026-03-17", period_start="2025-02-01", period_end="2026-01-31",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="mavi:2025:FY", source_url="https://mavi"
    )
    # Q3 2025 announcement on 2025-11-07 -> OUT OF SCOPE for daily 2026-01-01
    ev_q3_25 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2025, fiscal_period="Q3",
        earnings_date="2025-11-07", period_start="2025-02-01", period_end="2025-10-31",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="mavi:2025:Q3", source_url="https://mavi"
    )
    # Q2 2026 announcement on 2026-09-16 -> IN SCOPE
    ev_q2_26 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-09-16", period_start="2026-02-01", period_end="2026-07-31",
        date_type="company_announced", source="Official IR", source_provider="MAVI IR",
        source_type="official_financial_calendar", source_event_id="mavi:2026:Q2", source_url="https://mavi"
    )

    issue_hist = OfficialIrIssue(
        message="Typo corrected in 2025",
        kind="informational",
        earnings_date=date(2025, 3, 18),
        fiscal_year=2024,
        fiscal_period="FY",
        is_global=False
    )
    issue_global = OfficialIrIssue(
        message="Global notice",
        kind="informational",
        is_global=True
    )

    raw_result = OfficialIrProviderResult(
        symbol="MAVI",
        source_provider="MAVI Investor Relations",
        status="partial",
        events=[ev_fy25, ev_q3_25, ev_q2_26],
        rows_seen=3,
        accepted_row_count=3,
        rejected_row_count=0,
        warnings=["Typo corrected in 2025", "Global notice"],
        issues=[issue_hist, issue_global]
    )

    scoped = scope_official_ir_results([raw_result], earnings_date_from=BIST_DAILY_MIN_EARNINGS_DATE)
    assert len(scoped) == 1
    s_res = scoped[0]

    # Preserved FY 2025 (2026-03-17) and Q2 2026 (2026-09-16), excluded Q3 2025 (2025-11-07)
    assert len(s_res.events) == 2
    assert [e.earnings_date for e in s_res.events] == ["2026-03-17", "2026-09-16"]
    assert s_res.accepted_row_count == 2

    # Global issue kept, historical issue dropped
    assert len(s_res.issues) == 1
    assert s_res.issues[0].message == "Global notice"
    assert s_res.warnings == ["Global notice"]
    assert s_res.status == "success"  # No in-scope failure/validation issue


def test_scope_official_ir_results_preserves_global_and_provider_failures():
    fail_res = OfficialIrProviderResult(
        symbol="THYAO",
        source_provider="THYAO Investor Relations",
        status="failed",
        events=[],
        parser_failed=True,
        warnings=["HTTP 500 error fetching from https://thyao"],
        issues=[OfficialIrIssue(
            message="HTTP 500 error fetching from https://thyao",
            kind="provider_failure",
            is_global=True
        )]
    )

    scoped = scope_official_ir_results([fail_res], earnings_date_from=BIST_DAILY_MIN_EARNINGS_DATE)
    assert len(scoped) == 1
    assert scoped[0].status == "failed"
    assert scoped[0].parser_failed is True
    assert scoped[0].warnings == ["HTTP 500 error fetching from https://thyao"]
    assert len(scoped[0].issues) == 1


def test_scope_official_ir_results_preserves_in_scope_row_validation_errors():
    in_scope_issue = OfficialIrIssue(
        message="Earnings date 2026-01-10 before period_end 2026-03-31 for AKSA Q1 2026",
        kind="row_validation",
        earnings_date=date(2026, 1, 10),
        fiscal_year=2026,
        fiscal_period="Q1",
        is_global=False
    )
    aksa_ev = BistEarningsEvent(
        symbol="AKSA", company_name="AKSA", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-13", period_start="2026-01-01", period_end="2026-06-30",
        date_type="company_announced", source="Official IR", source_provider="AKSA IR",
        source_type="official_financial_calendar", source_event_id="aksa:2026:Q2", source_url="https://aksa"
    )

    res = OfficialIrProviderResult(
        symbol="AKSA",
        source_provider="AKSA Investor Relations",
        status="partial",
        events=[aksa_ev],
        warnings=[in_scope_issue.message],
        issues=[in_scope_issue]
    )

    scoped = scope_official_ir_results([res], earnings_date_from=BIST_DAILY_MIN_EARNINGS_DATE)
    assert len(scoped) == 1
    assert scoped[0].status == "partial"
    assert len(scoped[0].events) == 1
    assert scoped[0].warnings == [in_scope_issue.message]


def test_scope_official_ir_results_is_pure_function():
    ev = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-05-18", period_start="2026-01-01", period_end="2026-03-31",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q1", source_url="https://bimas"
    )
    original_res = OfficialIrProviderResult(
        symbol="BIMAS",
        source_provider="BIMAS Investor Relations",
        status="success",
        events=[ev],
        warnings=[]
    )

    scoped = scope_official_ir_results([original_res], earnings_date_from=date(2027, 1, 1))
    # Original is not mutated
    assert len(original_res.events) == 1
    # Scoped has empty events for 2027
    assert len(scoped[0].events) == 0


def test_scope_official_ir_results_fy2025_unknown_date_not_hidden():
    """Test 1: FY 2025 with unknown earnings_date must NOT be dropped by fiscal_year filter."""
    issue = OfficialIrIssue(
        message="Could not parse FY 2025 earnings date",
        kind="row_validation",
        earnings_date=None,
        fiscal_year=2025,
        fiscal_period="FY",
        is_global=False,
    )

    raw = OfficialIrProviderResult(
        symbol="MAVI",
        source_provider="MAVI Investor Relations",
        status="partial",
        events=[],
        warnings=[issue.message],
        issues=[issue],
    )

    scoped = scope_official_ir_results(
        [raw],
        earnings_date_from=date(2026, 1, 1),
    )[0]

    assert scoped.status == "partial"
    assert scoped.warnings == ["Could not parse FY 2025 earnings date"]
    assert len(scoped.issues) == 1


def test_scope_official_ir_results_unparseable_issue_date_preserved():
    """Test 2: Row validation issue with unparseable earnings_date string must be preserved."""
    issue = OfficialIrIssue(
        message="Invalid date format in item",
        kind="row_validation",
        earnings_date="invalid-date",
        fiscal_year=2025,
        fiscal_period="FY",
        is_global=False,
    )
    raw = OfficialIrProviderResult(
        symbol="MAVI",
        source_provider="MAVI Investor Relations",
        status="partial",
        events=[],
        warnings=[issue.message],
        issues=[issue],
    )
    scoped = scope_official_ir_results(
        [raw],
        earnings_date_from=date(2026, 1, 1),
    )[0]
    assert scoped.status == "partial"
    assert scoped.warnings == ["Invalid date format in item"]
    assert len(scoped.issues) == 1


def test_scope_official_ir_results_known_historical_issue_dropped():
    """Test 3: Known historical issue with valid earnings_date < cutoff must be dropped."""
    issue = OfficialIrIssue(
        message="Corrected MAVI FY 2024 visible date 2024-03-18 to 2025-03-18 using validated row metadata",
        kind="informational",
        earnings_date="2025-03-18",
        fiscal_year=2024,
        fiscal_period="FY",
        is_global=False,
    )
    ev_2026 = BistEarningsEvent(
        symbol="MAVI",
        company_name="MAVI",
        fiscal_year=2025,
        fiscal_period="FY",
        earnings_date="2026-03-17",
        period_start="2025-02-01",
        period_end="2026-01-31",
        date_type="company_announced",
        source="Official IR",
        source_provider="MAVI IR",
        source_type="official_financial_calendar",
        source_event_id="mavi:2025:FY",
        source_url="https://mavi"
    )
    raw = OfficialIrProviderResult(
        symbol="MAVI",
        source_provider="MAVI Investor Relations",
        status="success",
        events=[ev_2026],
        warnings=[issue.message],
        issues=[issue],
    )
    scoped = scope_official_ir_results(
        [raw],
        earnings_date_from=date(2026, 1, 1),
    )[0]
    assert scoped.status == "success"
    assert scoped.warnings == []
    assert len(scoped.issues) == 0
    assert len(scoped.events) == 1
    assert scoped.events[0].earnings_date == "2026-03-17"
