import json
import os
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.models.market import EconomicEvent
from app.providers.calendar import (
    BeaCalendarProvider,
    BlsCalendarProvider,
    CalendarProviderFetchResult,
    FedCalendarProvider,
    TcmbCalendarProvider,
    TuikCalendarProvider,
)
from app.services.economic_calendar import _validate_and_scope_provider_events

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "economic_calendar")


def _read_fixture(filename: str) -> str:
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_fixture_bytes(filename: str) -> bytes:
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "rb") as f:
        return f.read()


# ====================================================
# 1. BLS ICS PROVIDER TESTS
# ====================================================

@pytest.mark.asyncio
async def test_bls_ics_fixture_parsing_and_events():
    provider = BlsCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    ics_content = _read_fixture_bytes("bls_schedule.ics")

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.content = ics_content
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert len(fetch_res.warnings) == 0
        assert len(fetch_res.events) == 12

        for ev in fetch_res.events:
            assert ev.provider_id == "BLS"
            assert ev.source == "BLS"
            assert ev.source_event_id is not None
            assert ev.source_url == "https://www.bls.gov/schedule/news_release/bls.ics"

        # Verify exact required dates:
        # JOLTS: 1 Sep, 29 Sep, 3 Nov
        jolts = [e for e in fetch_res.events if e.event_code == "US_JOLTS"]
        assert len(jolts) == 3
        jolts_dates = [datetime.fromisoformat(e.scheduled_at_istanbul).date().isoformat() for e in jolts]
        assert "2026-09-01" in jolts_dates
        assert "2026-09-29" in jolts_dates
        assert "2026-11-03" in jolts_dates

        # NFP: 4 Sep, 2 Oct, 6 Nov
        nfp = [e for e in fetch_res.events if e.event_code == "US_NFP"]
        assert len(nfp) == 3
        nfp_dates = [datetime.fromisoformat(e.scheduled_at_istanbul).date().isoformat() for e in nfp]
        assert "2026-09-04" in nfp_dates
        assert "2026-10-02" in nfp_dates
        assert "2026-11-06" in nfp_dates

        # PPI: 10 Sep, 15 Oct, 13 Nov
        ppi = [e for e in fetch_res.events if e.event_code == "US_PPI"]
        assert len(ppi) == 3
        ppi_dates = [datetime.fromisoformat(e.scheduled_at_istanbul).date().isoformat() for e in ppi]
        assert "2026-09-10" in ppi_dates
        assert "2026-10-15" in ppi_dates
        assert "2026-11-13" in ppi_dates

        # CPI: 11 Sep, 14 Oct, 10 Nov
        cpi = [e for e in fetch_res.events if e.event_code == "US_CPI"]
        assert len(cpi) == 3
        cpi_dates = [datetime.fromisoformat(e.scheduled_at_istanbul).date().isoformat() for e in cpi]
        assert "2026-09-11" in cpi_dates
        assert "2026-10-14" in cpi_dates
        assert "2026-11-10" in cpi_dates


@pytest.mark.asyncio
async def test_bls_exact_allowlist_negative_cases_rejected():
    provider = BlsCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    # ICS containing similar titles that must NOT match
    custom_ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//BLS Test//EN
BEGIN:VEVENT
UID:VET-001
SUMMARY:Employment Situation of Veterans
DTSTART:20260904T123000Z
END:VEVENT
BEGIN:VEVENT
UID:STATE-001
SUMMARY:State Job Openings and Labor Turnover
DTSTART:20260901T140000Z
END:VEVENT
BEGIN:VEVENT
UID:NFP-001
SUMMARY:Employment Situation
DTSTART:20260904T123000Z
END:VEVENT
END:VCALENDAR"""

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.content = custom_ics
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        # Only the exact "Employment Situation" must match
        assert len(fetch_res.events) == 1
        assert fetch_res.events[0].source_event_id == "NFP-001"
        assert fetch_res.events[0].event_code == "US_NFP"
        assert not any(e.source_event_id in ("VET-001", "STATE-001") for e in fetch_res.events)


@pytest.mark.asyncio
async def test_bls_dst_transition_america_new_york_to_istanbul():
    provider = BlsCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    ics_content = _read_fixture_bytes("bls_schedule.ics")

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.content = ics_content
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        events = fetch_res.events

        # September NFP (EDT UTC-4): 08:30 EDT -> 15:30 TRT (+3)
        sep_nfp = next(e for e in events if e.event_code == "US_NFP" and "2026-09-04" in e.scheduled_at_istanbul)
        assert "2026-09-04T15:30:00+03:00" in sep_nfp.scheduled_at_istanbul

        # November NFP (EST UTC-5, after Nov 1 DST end): 08:30 EST -> 16:30 TRT (+3)
        nov_nfp = next(e for e in events if e.event_code == "US_NFP" and "2026-11-06" in e.scheduled_at_istanbul)
        assert "2026-11-06T16:30:00+03:00" in nov_nfp.scheduled_at_istanbul


@pytest.mark.asyncio
async def test_bls_in_memory_only_no_disk_files_created(tmp_path):
    provider = BlsCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    ics_content = _read_fixture_bytes("bls_schedule.ics")
    files_before = set(os.listdir("."))

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.content = ics_content
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert len(fetch_res.events) == 12

    files_after = set(os.listdir("."))
    assert files_after == files_before, "BLS provider wrote unexpected files to disk!"


@pytest.mark.asyncio
async def test_bls_invalid_ics_content():
    provider = BlsCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.content = b"<html><body>Not an ICS calendar</body></html>"
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert len(fetch_res.events) == 0
        assert fetch_res.structure_validated is False
        assert len(fetch_res.warnings) > 0


# ====================================================
# 2. TÜİK JSON PROVIDER TESTS
# ====================================================

@pytest.mark.asyncio
async def test_tuik_json_fixture_preserves_both_gdp_events_at_same_timestamp():
    provider = TuikCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    tuik_raw = _read_fixture("tuik_annual_2026.json")
    tuik_json = json.loads(tuik_raw)

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.json = MagicMock(return_value=tuik_json)
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert len(fetch_res.warnings) == 0
        # Exactly 12 events in coverage
        assert len(fetch_res.events) == 12

        # Verify BOTH GDP events exist at 2026-08-31T10:00:00+03:00
        gdp_events = [e for e in fetch_res.events if e.event_code == "TR_GDP"]
        assert len(gdp_events) == 2

        gdp_tuples = [(e.event_code, e.reference_period, e.scheduled_at_istanbul) for e in gdp_events]
        assert ("TR_GDP", "2025", "2026-08-31T10:00:00+03:00") in gdp_tuples
        assert ("TR_GDP", "2026-Q2", "2026-08-31T10:00:00+03:00") in gdp_tuples

        # Verify other indicator counts:
        # 3 TR_CPI, 4 TR_LABOR, 3 TR_INDUSTRIAL_PRODUCTION
        cpi_events = [e for e in fetch_res.events if e.event_code == "TR_CPI"]
        assert len(cpi_events) == 3
        labor_events = [e for e in fetch_res.events if e.event_code == "TR_LABOR"]
        assert len(labor_events) == 4
        ip_events = [e for e in fetch_res.events if e.event_code == "TR_INDUSTRIAL_PRODUCTION"]
        assert len(ip_events) == 3


@pytest.mark.asyncio
async def test_tuik_deduplication_prefers_valid_id_and_link():
    provider = TuikCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    # Same logical event in both lists: one with id=0 (upcoming), one with id=58000 (published)
    data = {
        "yayindaOlanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 58000,
                "link": "https://data.tuik.gov.tr/Bulten/Index?p=58000"
            }
        ],
        "yayindaOlmayanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 0,
                "link": ""
            }
        ]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.json = MagicMock(return_value=data)
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert len(fetch_res.events) == 1
        assert fetch_res.events[0].source_event_id == "TUIK:TR_CPI:2026-08"
        assert fetch_res.events[0].source_url == "https://data.tuik.gov.tr/Bulten/Index?p=58000"


@pytest.mark.asyncio
async def test_tuik_multi_year_window_queries_each_year_once():
    provider = TuikCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 12, 1, 0, 0, tzinfo=tz_tr)
    end = datetime(2027, 3, 2, 0, 0, tzinfo=tz_tr)

    tuik_json = {"yayindaOlanlarList": [], "yayindaOlmayanlarList": []}
    called_urls = []
    async def mock_get(url, **kwargs):
        called_urls.append(url)
        res = MagicMock()
        res.json = MagicMock(return_value=tuik_json)
        res.raise_for_status = MagicMock()
        return res

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        fetch_res = await provider.fetch_events(start, end)
        assert len(called_urls) == 2
@pytest.mark.asyncio
async def test_tuik_retry_recovers_from_initial_timeout():
    provider = TuikCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    fixture_path = os.path.join(FIXTURES_DIR, "tuik_annual_2026.json")
    with open(fixture_path, encoding="utf-8") as f:
        tuik_data = json.load(f)

    req = MagicMock()
    timeout_exc = httpx.ReadTimeout("TÜİK initial timeout", request=req)
    res_ok = MagicMock()
    res_ok.status_code = 200
    res_ok.json = MagicMock(return_value=tuik_data)
    res_ok.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", side_effect=[timeout_exc, res_ok]) as mock_get:
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            fetch_res = await provider.fetch_events(start, end)
            assert fetch_res.structure_validated is True
            assert len(fetch_res.events) == 12
            assert len(fetch_res.warnings) == 0
            assert mock_get.call_count == 2
            assert mock_sleep.call_count == 1


@pytest.mark.asyncio
async def test_tuik_lifecycle_with_real_provider_output():
    from app.jobs.economic_calendar_refresh_job import calculate_economic_diff
    from app.models.economic_calendar import (
        EconomicProviderResult,
        RollingEconomicCalendar,
    )

    provider = TuikCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    # 1. First fetch: Unpublished (id=0, empty link)
    data_unpublished = {
        "yayindaOlanlarList": [],
        "yayindaOlmayanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 0,
                "link": ""
            }
        ]
    }
    with patch("httpx.AsyncClient.get") as mock_get:
        res1 = MagicMock()
        res1.status_code = 200
        res1.json = MagicMock(return_value=data_unpublished)
        res1.raise_for_status = MagicMock()
        mock_get.return_value = res1

        res_unpub = await provider.fetch_events(start, end)
        assert len(res_unpub.events) == 1
        ev_unpub = res_unpub.events[0]
        assert ev_unpub.source_event_id == "TUIK:TR_CPI:2026-08"

    # 2. Second fetch: Published (id=58000, direct link, same date)
    data_published = {
        "yayindaOlanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 58000,
                "link": "https://data.tuik.gov.tr/Bulten/Index?p=58000"
            }
        ],
        "yayindaOlmayanlarList": []
    }
    with patch("httpx.AsyncClient.get") as mock_get:
        res2 = MagicMock()
        res2.status_code = 200
        res2.json = MagicMock(return_value=data_published)
        res2.raise_for_status = MagicMock()
        mock_get.return_value = res2

        res_pub = await provider.fetch_events(start, end)
        assert len(res_pub.events) == 1
        ev_pub = res_pub.events[0]
        assert ev_pub.source_event_id == "TUIK:TR_CPI:2026-08"
        assert ev_pub.source_url == "https://data.tuik.gov.tr/Bulten/Index?p=58000"

    # Verify diff when date is identical -> UNCHANGED
    cal_prev = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17", coverage_end="2026-11-15",
        timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)],
        events=[ev_unpub]
    )
    cal_curr = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17", coverage_end="2026-11-15",
        timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)],
        events=[ev_pub]
    )
    diff_same = calculate_economic_diff(cal_prev, cal_curr, cal_curr)
    assert diff_same.unchanged_count == 1
    assert diff_same.changed_count == 0
    assert diff_same.added_count == 0
    assert diff_same.removed_count == 0

    # 3. Third fetch: Rescheduled date (2026-09-04T10:00:00)
    data_rescheduled = {
        "yayindaOlanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-04T10:00:00",
                "id": 58000,
                "link": "https://data.tuik.gov.tr/Bulten/Index?p=58000"
            }
        ],
        "yayindaOlmayanlarList": []
    }
    with patch("httpx.AsyncClient.get") as mock_get:
        res3 = MagicMock()
        res3.status_code = 200
        res3.json = MagicMock(return_value=data_rescheduled)
        res3.raise_for_status = MagicMock()
        mock_get.return_value = res3

        res_resched = await provider.fetch_events(start, end)
        assert len(res_resched.events) == 1
        ev_resched = res_resched.events[0]
        assert ev_resched.source_event_id == "TUIK:TR_CPI:2026-08"

    cal_resched = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17", coverage_end="2026-11-15",
        timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)],
        events=[ev_resched]
    )
    diff_resched = calculate_economic_diff(cal_prev, cal_resched, cal_resched)
    assert diff_resched.changed_count == 1
    assert diff_resched.unchanged_count == 0
    assert diff_resched.added_count == 0
    assert diff_resched.removed_count == 0

    # 4. Response with both unpublished and published duplicate -> exactly 1 event with direct link
    data_both = {
        "yayindaOlanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 58000,
                "link": "https://data.tuik.gov.tr/Bulten/Index?p=58000"
            }
        ],
        "yayindaOlmayanlarList": [
            {
                "adi": "Tüketici Fiyat Endeksi",
                "donemi": "Ağustos 2026",
                "gTarih": "2026-09-03T10:00:00",
                "id": 0,
                "link": ""
            }
        ]
    }
    with patch("httpx.AsyncClient.get") as mock_get:
        res4 = MagicMock()
        res4.status_code = 200
        res4.json = MagicMock(return_value=data_both)
        res4.raise_for_status = MagicMock()
        mock_get.return_value = res4

        res_both = await provider.fetch_events(start, end)
        assert len(res_both.events) == 1
        assert res_both.events[0].source_event_id == "TUIK:TR_CPI:2026-08"
        assert res_both.events[0].source_url == "https://data.tuik.gov.tr/Bulten/Index?p=58000"


# ====================================================
# 3. TCMB MIDTABLE PROVIDER TESTS
# ====================================================

@pytest.mark.asyncio
async def test_tcmb_midtable_fixture_parsing_and_events():
    provider = TcmbCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    html = _read_fixture("tcmb_calendar.html")

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.text = html
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert len(fetch_res.warnings) == 0
        assert len(fetch_res.events) == 3

        # 10 Sep PPK
        ppk_sep = next(e for e in fetch_res.events if e.event_code == "TR_POLICY_RATE" and "2026-09-10" in e.scheduled_at_istanbul)
        assert "2026-09-10T14:00:00+03:00" in ppk_sep.scheduled_at_istanbul

        # 22 Oct PPK
        ppk_oct = next(e for e in fetch_res.events if e.event_code == "TR_POLICY_RATE" and "2026-10-22" in e.scheduled_at_istanbul)
        assert "2026-10-22T14:00:00+03:00" in ppk_oct.scheduled_at_istanbul

        # 12 Nov Enflasyon Raporu
        inf_nov = next(e for e in fetch_res.events if e.event_code == "TR_INFLATION_REPORT" and "2026-11-12" in e.scheduled_at_istanbul)
        assert "2026-11-12T10:30:00+03:00" in inf_nov.scheduled_at_istanbul


@pytest.mark.asyncio
async def test_tcmb_missing_midtable_yields_structural_warning():
    provider = TcmbCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    bad_html = "<html><body><table id='otherTable'><tr><td>Generic</td></tr></table></body></html>"

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.text = bad_html
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert len(fetch_res.events) == 0
        assert fetch_res.structure_validated is False
        assert len(fetch_res.warnings) > 0


# ====================================================
# 4. BEA RELEASE SCHEDULE PROVIDER TESTS
# ====================================================

@pytest.mark.asyncio
async def test_bea_schedule_fixture_parsing_and_events():
    provider = BeaCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    html = _read_fixture("bea_calendar.html")

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.text = html
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert len(fetch_res.warnings) == 0
        assert len(fetch_res.events) == 6

        # Check distinct GDP versions
        gdp_events = [e for e in fetch_res.events if e.event_code == "US_GDP"]
        assert len(gdp_events) == 3
        gdp_ids = [e.source_event_id for e in gdp_events]
        assert "BEA:US_GDP:2026-Q2:second" in gdp_ids
        assert "BEA:US_GDP:2026-Q2:third" in gdp_ids
        assert "BEA:US_GDP:2026-Q3:advance" in gdp_ids


# ====================================================
# 5. FED FOMC CALENDAR PROVIDER TESTS
# ====================================================

@pytest.mark.asyncio
async def test_fed_fomc_fixture_exact_selector_and_ordinals():
    provider = FedCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    html = _read_fixture("fed_fomc_calendar.html")

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.text = html
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert len(fetch_res.warnings) == 0
        assert len(fetch_res.events) == 2

        # 16 Sep 2026 (6th meeting of 2026) & 28 Oct 2026 (7th meeting of 2026)
        dates = [datetime.fromisoformat(e.scheduled_at_istanbul).date().isoformat() for e in fetch_res.events]
        assert "2026-09-16" in dates
        assert "2026-10-28" in dates

        # Verify deterministic ordinals
        e1 = next(e for e in fetch_res.events if "2026-09-16" in e.scheduled_at_istanbul)
        e2 = next(e for e in fetch_res.events if "2026-10-28" in e.scheduled_at_istanbul)
        assert e1.source_event_id == "FED:US_FOMC:2026-N06"
        assert e2.source_event_id == "FED:US_FOMC:2026-N07"


@pytest.mark.asyncio
async def test_fed_fomc_malformed_meeting_row_increments_malformed_count():
    provider = FedCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 17, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 11, 15, 23, 59, tzinfo=tz_tr)

    # Panel with 1 valid meeting and 1 malformed meeting
    bad_html = """
    <div class="panel">
      <div class="panel-heading">2026 FOMC Meetings</div>
      <div class="fomc-meeting">
        <div class="fomc-meeting__month">September</div>
        <div class="fomc-meeting__date">15-16*</div>
      </div>
      <div class="fomc-meeting">
        <!-- Missing month and date -->
      </div>
    </div>
    """

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.text = bad_html
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        assert fetch_res.structure_validated is True
        assert fetch_res.observed_candidate_count == 2
        assert fetch_res.candidate_count == 1
        assert fetch_res.malformed_count == 1
        assert len(fetch_res.events) == 1
        assert len(fetch_res.warnings) == 1


# ====================================================
# 6. AUTHORITATIVE-EMPTY VS STRUCTURAL FAILURE TESTS
# ====================================================

def test_structural_failure_vs_healthy_empty():
    tz_tr = ZoneInfo("Europe/Istanbul")
    cov_start = date(2026, 8, 17)
    cov_end = date(2026, 11, 15)

    # 1. Structural failure: structure_validated=False
    fetch_failed = CalendarProviderFetchResult(
        events=[],
        warnings=["TÜİK Veri Takvimi sayfa yapısı doğrulanamadı"],
        structure_validated=False,
        candidate_count=0,
        malformed_count=0
    )
    scoped, pr_res, _ = _validate_and_scope_provider_events(
        "TUIK", "TÜİK Veri Takvimi", fetch_failed, cov_start, cov_end, tz_tr
    )
    assert len(scoped) == 0
    assert pr_res.status == "failed"
    assert pr_res.structure_validated is False

    # 2. Healthy empty: structure_validated=True, 0 candidates, 0 malformed
    fetch_empty = CalendarProviderFetchResult(
        events=[],
        warnings=[],
        structure_validated=True,
        candidate_count=0,
        malformed_count=0
    )
    scoped, pr_res, _ = _validate_and_scope_provider_events(
        "FED", "Federal Reserve FOMC Calendar", fetch_empty, cov_start, cov_end, tz_tr
    )
    assert len(scoped) == 0
    assert pr_res.status == "success"
    assert pr_res.structure_validated is True

    # 3. Partial: valid events present with malformed_count > 0
    valid_ev = EconomicEvent(
        name="TCMB Faiz Kararı", country="TR",
        scheduled_at="2026-09-10T14:00:00+03:00", scheduled_at_istanbul="2026-09-10T14:00:00+03:00",
        source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE"
    )
    fetch_partial = CalendarProviderFetchResult(
        events=[valid_ev],
        warnings=["TCMB: Tarih ayrıştırılamadı"],
        structure_validated=True,
        candidate_count=2,
        malformed_count=1
    )
    scoped, pr_res, _ = _validate_and_scope_provider_events(
        "TCMB", "TCMB Takvimi", fetch_partial, cov_start, cov_end, tz_tr
    )
    assert len(scoped) == 1
    assert pr_res.status == "partial"
    assert pr_res.structure_validated is True
