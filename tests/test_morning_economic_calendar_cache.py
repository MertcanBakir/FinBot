from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.market_session import MarketSession
from app.jobs.morning_job import run_morning_job
from app.models.earnings import WeeklyEarningsEvent
from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import (
    Bist100Data,
    BitcoinData,
    CurrencyData,
    EconomicEvent,
    GoldData,
    MarketData,
    MarketSnapshot,
)
from app.repositories.refresh_repository import RefreshRepository
from app.services.market_email_report import build_morning_email_content
from app.services.morning_calendar_service import MorningCalendarService
from tests.conftest import create_mock_db_connection


def _build_test_calendar(coverage_start="2026-08-17") -> RollingEconomicCalendar:
    d_start = date.fromisoformat(coverage_start)
    d_end = d_start + timedelta(days=90)
    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start=d_start.isoformat(),
        coverage_end=d_end.isoformat(),
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1)
        ],
        events=[
            EconomicEvent(
                name="TÜİK TÜFE",
                country="TR",
                scheduled_at="",
                scheduled_at_istanbul="2026-08-17T10:00:00+03:00",
                source="TÜİK",
                provider_id="TUIK",
            )
        ],
    )


def _build_test_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        success=True,
        generated_at="2026-08-17T08:30:00+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.PRE_MARKET,
        market=MarketData(
            bist100=Bist100Data(price=14000.0, change_percent=0.5),
            usdtry=CurrencyData(price=47.0),
            eurtry=CurrencyData(price=54.0),
            gold=GoldData(xauusd=4000.0, gram_try=6000.0),
            bitcoin=BitcoinData(usd=60000.0),
        ),
        text="Sample text",
        warnings=[],
    )


# ---------------------------------------------------------------------------
# 1. Supabase Weekly Calendar Database Queries Tests
# ---------------------------------------------------------------------------


def test_repository_get_weekly_economic_events():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "TÜİK Tüketici Fiyat Endeksi",
            "TR",
            datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc),
            "TÜİK",
            "TUIK",
            "CPI_TR",
            "2026-07",
            "EVT_1",
            "https://tuik.gov.tr",
            False,
            False,
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    start_dt = datetime(2026, 8, 24, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    end_dt = datetime(2026, 8, 30, 23, 59, tzinfo=ZoneInfo("Europe/Istanbul"))

    events = RefreshRepository.get_weekly_economic_events(conn, start_dt, end_dt)

    assert len(events) == 1
    assert events[0].name == "TÜİK Tüketici Fiyat Endeksi"
    assert events[0].country == "TR"
    assert events[0].provider_id == "TUIK"
    assert "2026-08-24T10:00:00+03:00" in events[0].scheduled_at_istanbul


def test_repository_get_weekly_earnings_events():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("THYAO", "Türk Hava Yolları", "BIST100", 2026, "Q2", date(2026, 8, 25), None, "company_announced"),
        ("NVDA", "NVIDIA Corp", "NASDAQ100", 2026, "Q2", date(2026, 8, 27), "after_hours", "estimated"),
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    start_date = date(2026, 8, 24)
    end_date = date(2026, 8, 30)

    events = RefreshRepository.get_weekly_earnings_events(conn, start_date, end_date)

    assert len(events) == 2
    assert events[0].symbol == "THYAO"
    assert events[0].market == "BIST100"
    assert events[0].earnings_date == "2026-08-25"
    assert events[1].symbol == "NVDA"
    assert events[1].session == "after_hours"


# ---------------------------------------------------------------------------
# 2. MorningCalendarService Supabase-only Resolution Tests
# ---------------------------------------------------------------------------


def test_morning_calendar_service_fetches_from_supabase_when_available():
    service = MorningCalendarService()
    ref_dt = datetime(2026, 8, 24, 8, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    mock_econ = [
        EconomicEvent(name="TCMB PPK Kararı", country="TR", scheduled_at="", scheduled_at_istanbul="", source="TCMB")
    ]
    mock_earn = [
        WeeklyEarningsEvent(symbol="THYAO", company_name="THY", market="BIST100", earnings_date="2026-08-25")
    ]

    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection") as mock_conn_ctx, \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=mock_econ), \
         patch.object(RefreshRepository, "get_weekly_earnings_events", return_value=mock_earn):

        econ_res, earn_res, warns, src = service.get_weekly_calendar(ref_dt)

        assert src == "database"
        assert len(econ_res) == 1
        assert econ_res[0].name == "TCMB PPK Kararı"
        assert len(earn_res) == 1
        assert earn_res[0].symbol == "THYAO"
        assert len(warns) == 0


def test_morning_calendar_service_fails_closed_on_db_error():
    service = MorningCalendarService()
    ref_dt = datetime(2026, 8, 17, 8, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection", side_effect=RuntimeError("DB timeout")):
        with pytest.raises(RuntimeError, match="Supabase calendar query failed"):
            service.get_weekly_calendar(ref_dt)


# ---------------------------------------------------------------------------
# 3. Email Content Rendering with Earnings Events Tests
# ---------------------------------------------------------------------------


def test_build_morning_email_content_renders_earnings_section():
    snapshot = _build_test_snapshot()
    as_of = datetime(2026, 8, 24, 8, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    econ_events = [
        EconomicEvent(
            name="TCMB PPK Faiz Kararı",
            country="TR",
            scheduled_at="2026-08-27T11:00:00Z",
            scheduled_at_istanbul="2026-08-27T14:00:00+03:00",
            source="TCMB",
        )
    ]
    earnings_events = [
        WeeklyEarningsEvent(
            symbol="THYAO",
            company_name="Türk Hava Yolları",
            market="BIST100",
            fiscal_year=2026,
            fiscal_period="Q2",
            earnings_date="2026-08-25",
        ),
        WeeklyEarningsEvent(
            symbol="NVDA",
            company_name="NVIDIA Corporation",
            market="NASDAQ100",
            fiscal_year=2026,
            fiscal_period="Q2",
            earnings_date="2026-08-27",
            session="after_hours",
        ),
    ]

    content = build_morning_email_content(
        snapshot=snapshot,
        events=econ_events,
        as_of=as_of,
        earnings_events=earnings_events,
    )

    # Text Body Checks
    assert "BU HAFTA BİLANÇO AÇIKLAYACAK ŞİRKETLER:" in content.body_text
    assert "THYAO (Türk Hava Yolları) • 2026/Q2" in content.body_text
    assert "[NASDAQ] 27 Ağustos 2026 (Piyasa Sonrası) — NVDA" in content.body_text

    # HTML Body Checks
    assert "Bu Hafta Bilanço Açıklayacak Şirketler" in content.body_html
    assert "THYAO" in content.body_html
    assert "NVDA" in content.body_html
    assert "Piyasa Sonrası" in content.body_html
    assert "display: flex" not in content.body_html


# ---------------------------------------------------------------------------
# 4. Morning Job End-to-End Orchestration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("app.services.morning_calendar_service.morning_calendar_service.get_weekly_calendar")
@patch("app.services.market_summary.market_summary_service.generate")
async def test_morning_job_executes_with_supabase_calendar(mock_generate, mock_get_calendar):
    tz = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 24, 8, 30, tzinfo=tz)

    mock_get_calendar.return_value = (
        [EconomicEvent(name="TCMB PPK", country="TR", scheduled_at="", scheduled_at_istanbul="2026-08-27T14:00:00+03:00", source="TCMB")],
        [WeeklyEarningsEvent(symbol="THYAO", company_name="THY", market="BIST100", earnings_date="2026-08-25")],
        [],
        "database",
    )
    mock_generate.return_value = _build_test_snapshot()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    with patch("app.jobs.morning_job.datetime") as mock_now_dt:
        mock_now_dt.now.return_value = ref_dt
        result = await run_morning_job(
            as_of=ref_dt,
            email_service=mock_email,
            db_connection_factory=create_mock_db_connection,
        )
        assert result.status == "success"
        mock_email.send_email.assert_called_once()
        kwargs = mock_email.send_email.call_args.kwargs
        assert "TCMB PPK" in kwargs["body_text"]
        assert "THYAO" in kwargs["body_text"]
        assert "THYAO" in kwargs["body_html"]


def test_morning_calendar_service_option_a_smart_week_window():
    service = MorningCalendarService()
    tz = ZoneInfo("Europe/Istanbul")

    # 1. Thursday 2026-08-20 (weekday=3) -> Current week: Mon 2026-08-17 to Sun 2026-08-23
    thursday = datetime(2026, 8, 20, 8, 30, tzinfo=tz)
    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection") as mock_conn, \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[]) as mock_econ, \
         patch.object(RefreshRepository, "get_weekly_earnings_events", return_value=[]) as mock_earn:

        service.get_weekly_calendar(thursday)
        mock_econ.assert_called_once()
        args = mock_econ.call_args[0]
        assert args[1].date() == date(2026, 8, 17)
        assert args[2].date() == date(2026, 8, 23)

    # 2. Friday 2026-08-21 (weekday=4) -> Next week: Mon 2026-08-24 to Sun 2026-08-30
    friday = datetime(2026, 8, 21, 8, 30, tzinfo=tz)
    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection") as mock_conn, \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[]) as mock_econ, \
         patch.object(RefreshRepository, "get_weekly_earnings_events", return_value=[]) as mock_earn:

        service.get_weekly_calendar(friday)
        mock_econ.assert_called_once()
        args = mock_econ.call_args[0]
        assert args[1].date() == date(2026, 8, 24)
        assert args[2].date() == date(2026, 8, 30)

    # 3. Sunday 2026-08-23 (weekday=6) -> Next week: Mon 2026-08-24 to Sun 2026-08-30
    sunday = datetime(2026, 8, 23, 10, 0, tzinfo=tz)
    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection") as mock_conn, \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[]) as mock_econ, \
         patch.object(RefreshRepository, "get_weekly_earnings_events", return_value=[]) as mock_earn:

        service.get_weekly_calendar(sunday)
        mock_econ.assert_called_once()
        args = mock_econ.call_args[0]
        assert args[1].date() == date(2026, 8, 24)
        assert args[2].date() == date(2026, 8, 30)

    # 4. Monday 2026-08-24 (weekday=0) -> Current week: Mon 2026-08-24 to Sun 2026-08-30
    monday = datetime(2026, 8, 24, 8, 30, tzinfo=tz)
    with patch("app.services.morning_calendar_service.settings.SUPABASE_DATABASE_URL", "postgresql://fake:5432/db"), \
         patch("app.services.morning_calendar_service.get_db_connection") as mock_conn, \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[]) as mock_econ, \
         patch.object(RefreshRepository, "get_weekly_earnings_events", return_value=[]) as mock_earn:

        service.get_weekly_calendar(monday)
        mock_econ.assert_called_once()
        args = mock_econ.call_args[0]
        assert args[1].date() == date(2026, 8, 24)
        assert args[2].date() == date(2026, 8, 30)


def test_build_morning_email_content_renders_next_week_titles_on_friday():
    snapshot = _build_test_snapshot()
    # Friday as_of
    friday_as_of = datetime(2026, 8, 21, 8, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    econ_events = [
        EconomicEvent(
            name="TCMB PPK Faiz Kararı",
            country="TR",
            scheduled_at="2026-08-27T11:00:00Z",
            scheduled_at_istanbul="2026-08-27T14:00:00+03:00",
            source="TCMB",
        )
    ]
    earnings_events = [
        WeeklyEarningsEvent(
            symbol="THYAO",
            company_name="Türk Hava Yolları",
            market="BIST100",
            fiscal_year=2026,
            fiscal_period="Q2",
            earnings_date="2026-08-25",
        ),
    ]

    content = build_morning_email_content(
        snapshot=snapshot,
        events=econ_events,
        as_of=friday_as_of,
        earnings_events=earnings_events,
    )

    # Verify dynamic "GELECEK HAFTA" titles for Friday
    assert "GELECEK HAFTA TAKİP EDİLECEK EKONOMİK VERİLER:" in content.body_text
    assert "GELECEK HAFTA BİLANÇO AÇIKLAYACAK ŞİRKETLER:" in content.body_text
    assert "🗓 Gelecek Hafta Takip Edilecek Ekonomik Veriler" in content.body_html
    assert "📊 Gelecek Hafta Bilanço Açıklayacak Şirketler" in content.body_html
