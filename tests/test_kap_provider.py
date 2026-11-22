import json
import os
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.earnings import (
    BistEarningsEvent,
    KapEarningsFetchMetrics,
    KapEarningsFetchResult,
    KapSymbolEarningsStatus,
)
from app.providers.earnings.kap_provider import (
    KapDisclosureEnvelope,
    KapEarningsProvider,
    KapPartitionResult,
    normalize_date,
    normalize_publish_date,
    normalize_subject,
    parse_kap_financial_calendar_html,
)
from app.services.bist100_earnings import Bist100EarningsService

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "kap")


def load_fixture_json(filename: str) -> Any:
    filepath = os.path.join(FIXTURE_DIR, filename)
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


# --- 1. Real Calendar Date Validation Tests ---

def test_normalize_date_real_calendar_validation():
    # Valid dates
    assert normalize_date("10/08/2026") == "2026-08-10"
    assert normalize_date("10.08.2026") == "2026-08-10"
    assert normalize_date("2026-08-10") == "2026-08-10"
    assert normalize_date("29/02/2024") == "2024-02-29"  # Leap year valid

    # Invalid dates return None or raise ValueError
    with pytest.raises(ValueError):
        from app.providers.earnings.kap_provider import normalize_date_strict
        normalize_date_strict("31/02/2026")
    with pytest.raises(ValueError):
        from app.providers.earnings.kap_provider import normalize_date_strict
        normalize_date_strict("31/04/2026")

    assert normalize_date(None) is None
    assert normalize_date("-") is None


def test_normalize_publish_date_date_only():
    assert normalize_publish_date("2026-08-10T14:22:15Z") == "2026-08-10"
    assert normalize_publish_date("2026-08-10") == "2026-08-10"
    assert normalize_publish_date("not-a-date") is None
    assert normalize_publish_date(None) is None


def test_normalize_subject():
    assert normalize_subject("  Finansal   Takvim  ") == "finansal takvim"
    assert normalize_subject("FINANSAL TAKVIM") == "finansal takvim"
    assert normalize_subject("Özel Durum Açıklaması") != "finansal takvim"
    assert normalize_subject(None) == ""


# --- 2. Member Resolver & Duplicate OID & Extra Mapping Tests ---

def test_resolve_best_member_candidate_strict():
    provider = KapEarningsProvider()
    candidates = [{"companyCode": "CCOLA", "title": "COCA-COLA İÇECEK A.Ş.", "mkkMemberOid": "OID_CCOLA"}]
    res = provider.resolve_best_member_candidate("CCOLA", candidates)
    assert res is not None and res["mkkMemberOid"] == "OID_CCOLA"

    garan_candidates = [
        {"companyCode": "2113", "title": "GARANTİ OPERASYONEL KİRALAMA A.Ş."},
        {"companyCode": "2422", "title": "TÜRKİYE GARANTİ BANKASI A.Ş."}
    ]
    assert provider.resolve_best_member_candidate("GARAN", garan_candidates) is None


@pytest.mark.asyncio
async def test_duplicate_oid_marks_symbols_ambiguous_and_unavailable():
    provider = KapEarningsProvider()
    duplicate_cache = {
        "GARAN": {"symbol": "GARAN", "mkk_member_oid": "DUPLICATE_OID"},
        "TSKB": {"symbol": "TSKB", "mkk_member_oid": "DUPLICATE_OID"}
    }
    with patch.object(provider, "load_member_mappings", return_value=duplicate_cache):
        with patch.object(provider, "_http_request", new_callable=AsyncMock):
            async with httpx.AsyncClient() as client:
                res_cache, warnings, resolved, unresolved, res_map = await provider.get_member_mappings(client, ["GARAN", "TSKB"])
                assert res_map["GARAN"] == "ambiguous"
                assert res_map["TSKB"] == "ambiguous"
                assert any("Duplicate OID" in w for w in warnings)


@pytest.mark.asyncio
async def test_extra_stale_mapping_detected_and_warned():
    provider = KapEarningsProvider()
    cache_with_extra = {
        "CCOLA": {"symbol": "CCOLA", "mkk_member_oid": "OID_CCOLA"},
        "EXTRA_STALE_SYM": {"symbol": "EXTRA_STALE_SYM", "mkk_member_oid": "OID_EXTRA"}
    }
    with patch.object(provider, "load_member_mappings", return_value=cache_with_extra):
        with patch.object(provider, "_http_request", new_callable=AsyncMock):
            async with httpx.AsyncClient() as client:
                res_cache, warnings, resolved, unresolved, res_map = await provider.get_member_mappings(client, ["CCOLA"])
                assert "CCOLA" in resolved
                assert any("Extra/stale mappings" in w for w in warnings)


# --- 3. Token-Safe Stock Code Mapping Tests ---

def test_extract_matched_symbol_token_safe():
    provider = KapEarningsProvider()
    metrics = KapEarningsFetchMetrics()
    oid_map = {"OID_CCOLA": "CCOLA"}
    target_symbols = {"CCOLA", "AEFES"}

    disc1 = {"mkkMemberOid": None, "stockCodes": " CCOLA.IS, AEFES.IS "}
    sym1, err1 = provider._extract_matched_symbol(disc1, oid_map, target_symbols, metrics)
    assert sym1 is None and "Ambiguous" in err1

    disc2 = {"mkkMemberOid": None, "stockCodes": ["CCOLA.IS"]}
    sym2, err2 = provider._extract_matched_symbol(disc2, oid_map, target_symbols, metrics)
    assert sym2 == "CCOLA" and err2 is None

    disc3 = {"mkkMemberOid": None, "stockCodes": "UNKNOWN_STOCK"}
    sym3, err3 = provider._extract_matched_symbol(disc3, oid_map, target_symbols, metrics)
    assert sym3 is None and "Unmappable" in err3
    assert metrics.unmappable_disclosure_count == 1


# --- 4. Unmappable Provenance & Missing disclosureIndex Tests ---

@pytest.mark.asyncio
async def test_unmappable_provenance_marks_symbols_unavailable():
    provider = KapEarningsProvider()
    mock_cache = {
        "CCOLA": {"symbol": "CCOLA", "company_name": "COCA COLA", "mkk_member_oid": "OID_CCOLA"},
        "AEFES": {"symbol": "AEFES", "company_name": "ANADOLU EFES", "mkk_member_oid": "OID_AEFES"}
    }
    disclosures = [
        {"disclosureIndex": 1, "mkkMemberOid": None, "stockCodes": "UNKNOWN", "subject": "Finansal Takvim", "publishDate": "2026-08-01"}
    ]

    def mock_http_side_effect(client, method, url, is_search=False, **kwargs):
        if is_search:
            return disclosures
        return []

    with patch.object(provider, "load_member_mappings", return_value=mock_cache):
        with patch.object(provider, "_load_bist100_symbols", return_value=["CCOLA", "AEFES"]):
            with patch.object(provider, "_http_request", side_effect=mock_http_side_effect):
                res = await provider.fetch_calendar(
                    coverage_start=date(2026, 8, 1),
                    coverage_end=date(2026, 8, 31),
                    published_from=date(2025, 8, 1),
                    published_to=date(2026, 8, 10)
                )

                assert res.status in ("partial", "failed")
                assert len(res.events) == 0
                assert res.metrics.unmappable_disclosure_count >= 1

                sym_map = {s.symbol: s for s in res.symbol_results}
                assert sym_map["CCOLA"].calendar_status == "unavailable"
                assert not sym_map["CCOLA"].is_complete


@pytest.mark.asyncio
async def test_missing_disclosure_index_marks_provenance_incomplete():
    provider = KapEarningsProvider()
    mock_cache = {"CCOLA": {"symbol": "CCOLA", "company_name": "COCA COLA", "mkk_member_oid": "OID_CCOLA"}}
    disclosures = [{"disclosureIndex": None, "mkkMemberOid": "OID_CCOLA", "subject": "Finansal Takvim", "publishDate": "2026-08-01"}]

    with patch.object(provider, "load_member_mappings", return_value=mock_cache):
        with patch.object(provider, "_load_bist100_symbols", return_value=["CCOLA"]):
            with patch.object(provider, "_http_request", return_value=disclosures):
                res = await provider.fetch_calendar(
                    coverage_start=date(2026, 8, 1),
                    coverage_end=date(2026, 8, 31),
                    published_from=date(2025, 8, 1),
                    published_to=date(2026, 8, 10)
                )

                assert res.status == "failed"
                assert res.symbol_results[0].calendar_status == "unavailable"
                assert not res.symbol_results[0].is_complete


# --- 5. Adaptive Partitioning & Saturated Leaf Tests ---

@pytest.mark.asyncio
async def test_adaptive_partitioning_under_2000():
    provider = KapEarningsProvider()
    metrics = KapEarningsFetchMetrics()
    mock_rows = [{"disclosureIndex": 1}, {"disclosureIndex": 2}]

    resp = MagicMock(status_code=200)
    resp.json.return_value = mock_rows
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=resp)

    part_res = await provider._fetch_by_criteria_partitioned(mock_client, ["OID1"], "2026-01-01", "2026-06-30", metrics)
    assert len(part_res.envelopes) == 2
    assert metrics.search_request_count == 1
    assert metrics.adaptive_leaf_count == 1
    assert metrics.saturated_leaf_count == 0


@pytest.mark.asyncio
async def test_saturated_leaf_rows_excluded_completely():
    provider = KapEarningsProvider()
    metrics = KapEarningsFetchMetrics()
    mock_2000 = [{"disclosureIndex": i} for i in range(2000)]

    resp = MagicMock(status_code=200)
    resp.json.return_value = mock_2000
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=resp)

    part_res = await provider._fetch_by_criteria_partitioned(mock_client, ["OID1"], "2026-08-10", "2026-08-10", metrics)
    assert len(part_res.envelopes) == 0
    assert "OID1" in part_res.saturated_oids
    assert "OID1" in part_res.incomplete_oids
    assert metrics.saturated_leaf_count == 1


# --- 6. Golden Fixtures Tests (CCOLA, DOHOL, DOAS) ---

def test_parse_kap_financial_calendar_html_golden_ccola():
    fixture = load_fixture_json("ccola_1627747.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 1
    assert parse_res.rows[0]["period_start"] == "2026-04-01"
    assert parse_res.rows[0]["period_end"] == "2026-06-30"
    assert parse_res.rows[0]["planned_date"] == "2026-08-10"


def test_parse_kap_financial_calendar_html_golden_dohol():
    """A. DOHOL Golden Fixture Test: Verifies loading tests/fixtures/kap/dohol_1642837.json and parsing 2 rows."""
    fixture = load_fixture_json("dohol_1642837.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 2
    r1 = parse_res.rows[0]
    r2 = parse_res.rows[1]

    assert r1["period_start"] == "2026-01-01"
    assert r1["period_end"] == "2026-03-31"
    assert r1["planned_date"] == "2026-05-07"

    assert r2["period_start"] == "2026-01-01"
    assert r2["period_end"] == "2026-06-30"
    assert r2["planned_date"] == "2026-08-11"


def test_parse_kap_financial_calendar_html_golden_doas_regression():
    """B. DOAS Golden Fixture Test: Verifies loading tests/fixtures/kap/doas_1643124.json and critical planned_date != period_start regression assertion."""
    fixture = load_fixture_json("doas_1643124.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 1
    assert parse_res.rows[0]["period_start"] == "2026-01-01"
    assert parse_res.rows[0]["period_end"] == "2026-06-30"
    assert parse_res.rows[0]["planned_date"] == "2026-08-19"
    assert parse_res.rows[0]["planned_date"] != "2026-01-01"


# --- 7. Header Recognition & Malformed / Blank Date Tests ---

def test_incomplete_header_table_rejected():
    """E. Generic table with 1 or 2 headers (missing full 3 semantic headers) without taxonomy class MUST BE REJECTED."""
    html = """
    <table>
        <tr>
            <th>Dönem Başlangıç Tarihi</th>
        </tr>
        <tr>
            <td>01.01.2026</td>
            <td>30.06.2026</td>
            <td>20.08.2026</td>
        </tr>
    </table>
    """
    parse_res = parse_kap_financial_calendar_html(html)
    assert parse_res.parser_failed
    assert len(parse_res.rows) == 0


def test_generic_table_without_recognized_header_or_taxonomy_rejected():
    html = """
    <table>
        <tr>
            <td>01.01.2026</td>
            <td>30.06.2026</td>
            <td>20.08.2026</td>
        </tr>
    </table>
    """
    parse_res = parse_kap_financial_calendar_html(html)
    assert parse_res.parser_failed
    assert len(parse_res.rows) == 0


def test_malformed_planned_date_semantic_header():
    """D. Malformed planned_date='invalid-date' in semantic header table triggers parser_failed = True."""
    html = """
    <table>
        <tr>
            <th>Dönem Başlangıç Tarihi</th>
            <th>Dönem Bitiş Tarihi</th>
            <th>Planlanan KAP'ta İlan Tarihi</th>
        </tr>
        <tr>
            <td>01.01.2026</td>
            <td>30.06.2026</td>
            <td>invalid-date</td>
        </tr>
    </table>
    """
    parse_res = parse_kap_financial_calendar_html(html)
    assert parse_res.parser_failed
    assert len(parse_res.rows) == 0


def test_malformed_planned_date_taxonomy_fallback():
    """D. Malformed planned_date='invalid-date' in taxonomy table triggers parser_failed = True."""
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda_FirstQuarter|">
            <td>01/01/2026</td>
            <td>31/03/2026</td>
            <td>invalid-date</td>
        </tr>
    </table>
    """
    parse_res = parse_kap_financial_calendar_html(html)
    assert parse_res.parser_failed
    assert len(parse_res.rows) == 0


def test_valid_blank_planned_date():
    """D. Valid blank planned_date='-' produces planned_date = None without parser failure."""
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda_ThirdQuarter|">
            <td>01/01/2026</td>
            <td>30/09/2026</td>
            <td>-</td>
        </tr>
    </table>
    """
    parse_res = parse_kap_financial_calendar_html(html)
    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 1
    assert parse_res.rows[0]["planned_date"] is None


# --- 8. Status Propagation Tests in Bist100EarningsService ---

@pytest.mark.asyncio
async def test_bist100_earnings_service_status_propagation():
    """C. Partial & Failed status propagation test in Bist100EarningsService."""
    provider = KapEarningsProvider()
    service = Bist100EarningsService(provider=provider)

    completed_event = BistEarningsEvent(
        symbol="CCOLA",
        company_name="COCA-COLA İÇECEK A.Ş.",
        earnings_date="2026-08-10"
    )

    # Test Partial Status Propagation
    partial_result = KapEarningsFetchResult(
        status="partial",
        events=[completed_event],
        warnings=["KAP byCriteria query failed for another OID chunk"],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="CCOLA", refresh_status="success", calendar_status="scheduled", is_complete=True),
            KapSymbolEarningsStatus(symbol="AEFES", refresh_status="failed", calendar_status="unavailable", is_complete=False)
        ]
    )

    with patch.object(provider, "get_earnings_result", new_callable=AsyncMock) as mock_get_res:
        mock_get_res.return_value = partial_result
        with patch.object(provider, "_load_bist100_symbols", return_value=["CCOLA", "AEFES"]):
            cal_partial = await service.get_week_events(offset=0, ref_date=datetime(2026, 8, 10))
            assert cal_partial.status == "partial"
            assert cal_partial.provider_status == "partial"
            assert len(cal_partial.events) == 1
            assert cal_partial.events[0].symbol == "CCOLA"

    # Test Failed Status Propagation
    failed_result = KapEarningsFetchResult(
        status="failed",
        events=[],
        warnings=["Provider failed"],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="CCOLA", refresh_status="failed", calendar_status="unavailable", is_complete=False)
        ]
    )

    with patch.object(provider, "get_earnings_result", new_callable=AsyncMock) as mock_get_res:
        mock_get_res.return_value = failed_result
        with patch.object(provider, "_load_bist100_symbols", return_value=["CCOLA"]):
            cal_failed = await service.get_week_events(offset=0, ref_date=datetime(2026, 8, 10))
            assert cal_failed.status == "failed"
            assert cal_failed.provider_status == "failed"
            assert len(cal_failed.events) == 0


@pytest.mark.asyncio
async def test_normalized_events_outside_rolling_coverage_retained_via_provider():
    provider = KapEarningsProvider()
    pgsus_fixture = load_fixture_json("pgsus_1637651.json")

    env_pgsus = KapDisclosureEnvelope(
        disclosure={
            "disclosureIndex": 1637651,
            "publishDate": "28.07.2026 15:03:14",
            "subject": "Finansal Takvim",
            "title": "PEGASUS HAVA TAŞIMACILIĞI A.Ş.",
            "stockCodes": "PGSUS",
            "disclosureClass": "ODA",
            "disclosureType": "Finansal Bilanço Tarihi"
        },
        source_oids={"123"},
        query_from="2026-01-01",
        query_to="2026-08-14"
    )
    env_incomplete = KapDisclosureEnvelope(
        disclosure={
            "disclosureIndex": 9999999,
            "publishDate": "28.07.2026 15:03:14",
            "subject": "Finansal Takvim",
            "title": "INCOMPLETE A.Ş.",
            "stockCodes": "INCOMPLETE",
            "disclosureClass": "ODA",
            "disclosureType": "Finansal Bilanço Tarihi"
        },
        source_oids={"456"},
        query_from="2026-01-01",
        query_to="2026-08-14"
    )

    part_res = KapPartitionResult(
        envelopes=[env_pgsus, env_incomplete],
        completed_oids={"123", "456"}
    )

    async def mock_http(client, method, url, **kwargs):
        if "1637651" in url:
            return pgsus_fixture
        return None

    mock_mappings = (
        {"PGSUS": {"mkk_member_oid": "123", "company_name": "PEGASUS HAVA TAŞIMACILIĞI A.Ş."}, "INCOMPLETE": {"mkk_member_oid": "456", "company_name": "INCOMPLETE A.Ş."}},
        [],
        ["PGSUS", "INCOMPLETE"],
        [],
        {"PGSUS": "resolved", "INCOMPLETE": "resolved"}
    )

    with patch.object(provider, "_load_bist100_symbols", return_value=["PGSUS", "INCOMPLETE"]), \
         patch.object(provider, "get_member_mappings", new_callable=AsyncMock, return_value=mock_mappings), \
         patch.object(provider, "_fetch_by_criteria_partitioned", new_callable=AsyncMock) as mock_criteria, \
         patch.object(provider, "_http_request", side_effect=mock_http):

        mock_criteria.return_value = part_res

        result = await provider.fetch_calendar(
            coverage_start=date(2026, 8, 14),
            coverage_end=date(2026, 11, 12),
            published_from=date(2026, 1, 1),
            published_to=date(2026, 8, 14)
        )

        assert not any(ev.symbol == "PGSUS" for ev in result.events)

        pgsus = next(ev for ev in result.normalized_events if ev.symbol == "PGSUS")
        assert pgsus.earnings_date == "2026-08-12"
        assert pgsus.fiscal_period == "Q2"
        assert pgsus.fiscal_year == 2026
        assert pgsus.period_start == "2026-01-01"
        assert pgsus.period_end == "2026-06-30"
        assert pgsus.disclosure_index == 1637651
        assert pgsus.source_url == "https://www.kap.org.tr/tr/Bildirim/1637651"

        assert result.metrics.normalized_event_count >= 1
        assert result.metrics.final_event_count == len(result.events)

        assert not any(ev.symbol == "INCOMPLETE" for ev in result.normalized_events)


@pytest.mark.asyncio
async def test_kap_provider_doas_and_dohol_integration():
    provider = KapEarningsProvider()

    doas_fixture = load_fixture_json("doas_1643124.json")
    dohol_fixture = load_fixture_json("dohol_1642837.json")

    env_doas = KapDisclosureEnvelope(
        disclosure={
            "disclosureIndex": 1643124,
            "publishDate": "2026-07-28T10:00:00Z",
            "subject": "Finansal Takvim",
            "title": "DOĞUŞ OTOMOTİV SERVİS VE TİCARET A.Ş.",
            "stockCodes": "DOAS",
            "disclosureClass": "ODA",
            "disclosureType": "Finansal Bilanço Tarihi"
        },
        source_oids={"111"},
        query_from="2026-01-01",
        query_to="2026-08-14"
    )

    env_dohol = KapDisclosureEnvelope(
        disclosure={
            "disclosureIndex": 1642837,
            "publishDate": "2026-07-28T11:00:00Z",
            "subject": "Finansal Takvim",
            "title": "DOĞAN ŞİRKETLER GRUBU HOLDİNG A.Ş.",
            "stockCodes": "DOHOL",
            "disclosureClass": "ODA",
            "disclosureType": "Finansal Bilanço Tarihi"
        },
        source_oids={"222"},
        query_from="2026-01-01",
        query_to="2026-08-14"
    )

    part_res = KapPartitionResult(
        envelopes=[env_doas, env_dohol],
        completed_oids={"111", "222"}
    )

    async def mock_http(client, method, url, **kwargs):
        if "1643124" in url:
            return doas_fixture
        if "1642837" in url:
            return dohol_fixture
        return None

    mock_mappings = (
        {
            "DOAS": {"mkk_member_oid": "111", "company_name": "DOĞUŞ OTOMOTİV SERVİS VE TİCARET A.Ş."},
            "DOHOL": {"mkk_member_oid": "222", "company_name": "DOĞAN ŞİRKETLER GRUBU HOLDİNG A.Ş."}
        },
        [],
        ["DOAS", "DOHOL"],
        [],
        {"DOAS": "resolved", "DOHOL": "resolved"}
    )

    with patch.object(provider, "_load_bist100_symbols", return_value=["DOAS", "DOHOL"]), \
         patch.object(provider, "get_member_mappings", new_callable=AsyncMock, return_value=mock_mappings), \
         patch.object(provider, "_fetch_by_criteria_partitioned", new_callable=AsyncMock) as mock_criteria, \
         patch.object(provider, "_http_request", side_effect=mock_http):

        mock_criteria.return_value = part_res

        result = await provider.fetch_calendar(
            coverage_start=date(2026, 1, 1),
            coverage_end=date(2026, 12, 31),
            published_from=date(2026, 1, 1),
            published_to=date(2026, 8, 14)
        )

        assert result.status != "failed"

        # Check DOAS
        doas_events = [ev for ev in result.normalized_events if ev.symbol == "DOAS"]
        assert len(doas_events) == 1
        doas_ev = doas_events[0]
        assert doas_ev.fiscal_period == "Q2"
        assert doas_ev.fiscal_year == 2026
        assert doas_ev.period_start == "2026-01-01"
        assert doas_ev.period_end == "2026-06-30"
        assert doas_ev.earnings_date == "2026-08-19"
        assert doas_ev.disclosure_index == 1643124
        assert doas_ev.source_url == "https://www.kap.org.tr/tr/Bildirim/1643124"

        # Check DOHOL
        dohol_events = [ev for ev in result.normalized_events if ev.symbol == "DOHOL"]
        assert len(dohol_events) == 2
        dohol_periods = {ev.fiscal_period: ev for ev in dohol_events}
        assert "Q1" in dohol_periods
        assert "Q2" in dohol_periods
        assert dohol_periods["Q1"].earnings_date == "2026-05-07"
        assert dohol_periods["Q2"].earnings_date == "2026-08-11"
        assert dohol_periods["Q1"].disclosure_index == 1642837
        assert dohol_periods["Q2"].disclosure_index == 1642837

        # Neither DOAS nor DOHOL should be in unresolved_symbols
        assert "DOAS" not in result.unresolved_symbols
        assert "DOHOL" not in result.unresolved_symbols
