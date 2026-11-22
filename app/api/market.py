from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.models.market import BistMovers, MarketSnapshot
from app.services.bist_movers import bist_movers_service
from app.services.economic_calendar_state_store import economic_calendar_state_store
from app.services.market_summary import market_summary_service

router = APIRouter(prefix="/api", tags=["Market"])

def _dedupe_warnings(warnings_list: List[str]) -> List[str]:
    return list(dict.fromkeys(w.strip() for w in warnings_list if w and w.strip()))

@router.get(
    "/market-summary",
    response_model=MarketSnapshot,
    summary="Get Market Summary",
    description="Returns current market snapshot. Use ?format=text for plain text output. Does NOT send emails."
)
async def get_market_summary(format: str = Query("json", description="Response format: 'json' or 'text'")):
    snapshot = await market_summary_service.generate()
    if format.lower() == "text":
        return PlainTextResponse(content=snapshot.text, media_type="text/plain; charset=utf-8")
    return snapshot

@router.get(
    "/bist-movers",
    response_model=BistMovers,
    summary="Get Top BIST 100 Movers",
    description="Returns top 3 gainers and top 3 losers among BIST 100 components. Does NOT send emails."
)
async def get_bist_movers():
    movers, _ = await bist_movers_service.get_movers()
    return movers

@router.get(
    "/economic-calendar/today",
    summary="Get Today's Tracked Economic Events",
    description="Returns today's tracked economic events from Supabase. Does NOT send emails or call live providers."
)
async def get_today_economic_events() -> Dict[str, Any]:
    cached_cal = economic_calendar_state_store.load()
    if not cached_cal:
        raise HTTPException(status_code=503, detail="Supabase economic calendar is unavailable.")

    tz_tr = ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz_tr)
    today_date = now.date()

    cov_start = date.fromisoformat(cached_cal.coverage_start)
    cov_end = date.fromisoformat(cached_cal.coverage_end)
    if today_date < cov_start or today_date > cov_end:
        raise HTTPException(status_code=503, detail="Supabase economic calendar has no current coverage for today.")

    events, warnings, _ = economic_calendar_state_store.events_between(today_date, today_date, as_of=today_date)
    return {
        "date": today_date.strftime("%Y-%m-%d"),
        "timezone": settings.TIMEZONE,
        "status": cached_cal.status,
        "stale": economic_calendar_state_store.is_stale(now),
        "events": [ev.model_dump() for ev in events],
        "warnings": _dedupe_warnings(warnings + list(cached_cal.warnings))
    }

@router.get(
    "/economic-calendar/week",
    summary="Get Tracked Economic Events for Selected Week",
    description="Returns tracked economic events for the selected week from Supabase. Does NOT send emails or call live providers."
)
async def get_week_economic_events(
    offset: int = Query(0, description="Week offset from current week: 0 for current week, 1 for next week, etc.")
) -> Dict[str, Any]:
    cached_cal = economic_calendar_state_store.load()
    if not cached_cal:
        raise HTTPException(status_code=503, detail="Supabase economic calendar is unavailable.")

    tz_tr = ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz_tr)
    ref_date = now + timedelta(weeks=offset)
    monday = ref_date.date() - timedelta(days=ref_date.weekday())
    sunday = monday + timedelta(days=6)

    cov_start = date.fromisoformat(cached_cal.coverage_start)
    cov_end = date.fromisoformat(cached_cal.coverage_end)

    if sunday < cov_start or monday > cov_end:
        raise HTTPException(status_code=422, detail="Requested week range has no overlap with calendar coverage.")

    eff_start = max(monday, cov_start)
    eff_end = min(sunday, cov_end)
    is_complete = (monday >= cov_start and sunday <= cov_end)

    events, warnings, _ = economic_calendar_state_store.events_between(eff_start, eff_end, as_of=now.date())

    coverage_warnings = []
    if not is_complete:
        coverage_warnings.append(f"İstenen hafta ({monday} — {sunday}) kısmen mevcut veri kapsamı ({cov_start} — {cov_end}) dışında kalıyor.")

    return {
        "schema_version": "2.0",
        "week_start": monday.strftime("%Y-%m-%d"),
        "week_end": sunday.strftime("%Y-%m-%d"),
        "requested_start": monday.strftime("%Y-%m-%d"),
        "requested_end": sunday.strftime("%Y-%m-%d"),
        "effective_start": eff_start.strftime("%Y-%m-%d"),
        "effective_end": eff_end.strftime("%Y-%m-%d"),
        "is_complete": is_complete,
        "timezone": settings.TIMEZONE,
        "status": cached_cal.status,
        "stale": economic_calendar_state_store.is_stale(now),
        "events": [ev.model_dump() for ev in events],
        "warnings": _dedupe_warnings(warnings + coverage_warnings + list(cached_cal.warnings))
    }

@router.get(
    "/economic-calendar/month",
    summary="Get Tracked Economic Events for Selected Month",
    description="Returns tracked economic events for the selected month from Supabase. Does NOT send emails or call live providers."
)
async def get_month_economic_events(
    offset: int = Query(0, description="Month offset from current month: 0 for current month, 1 for next month, etc.")
) -> Dict[str, Any]:
    cached_cal = economic_calendar_state_store.load()
    if not cached_cal:
        raise HTTPException(status_code=503, detail="Supabase economic calendar is unavailable.")

    tz_tr = ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz_tr)

    year = now.year
    month = now.month + offset
    while month > 12:
        year += 1
        month -= 12
    while month < 1:
        year -= 1
        month += 12

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year, 12, 31)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    cov_start = date.fromisoformat(cached_cal.coverage_start)
    cov_end = date.fromisoformat(cached_cal.coverage_end)

    if month_end < cov_start or month_start > cov_end:
        raise HTTPException(status_code=422, detail="Requested month range has no overlap with calendar coverage.")

    eff_start = max(month_start, cov_start)
    eff_end = min(month_end, cov_end)
    is_complete = (month_start >= cov_start and month_end <= cov_end)

    events, warnings, _ = economic_calendar_state_store.events_between(eff_start, eff_end, as_of=now.date())

    coverage_warnings = []
    if not is_complete:
        coverage_warnings.append(f"İstenen ay ({month_start} — {month_end}) kısmen mevcut veri kapsamı ({cov_start} — {cov_end}) dışında kalıyor.")

    return {
        "schema_version": "2.0",
        "month_start": month_start.strftime("%Y-%m-%d"),
        "month_end": month_end.strftime("%Y-%m-%d"),
        "requested_start": month_start.strftime("%Y-%m-%d"),
        "requested_end": month_end.strftime("%Y-%m-%d"),
        "effective_start": eff_start.strftime("%Y-%m-%d"),
        "effective_end": eff_end.strftime("%Y-%m-%d"),
        "is_complete": is_complete,
        "timezone": settings.TIMEZONE,
        "status": cached_cal.status,
        "stale": economic_calendar_state_store.is_stale(now),
        "events": [ev.model_dump() for ev in events],
        "warnings": _dedupe_warnings(warnings + coverage_warnings + list(cached_cal.warnings))
    }

@router.get(
    "/economic-calendar",
    summary="Get Tracked Economic Events for Custom Date Range",
    description="Returns tracked economic events for a custom date range from Supabase. Does NOT send emails or call live providers."
)
async def get_range_economic_events(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format")
) -> Dict[str, Any]:
    try:
        d_start = date.fromisoformat(start_date)
        d_end = date.fromisoformat(end_date)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format (expected YYYY-MM-DD): {e}",
        ) from e

    if d_start > d_end:
        raise HTTPException(status_code=400, detail=f"start_date ({start_date}) cannot be after end_date ({end_date})")

    if (d_end - d_start).days > 90:
        raise HTTPException(status_code=400, detail="Requested date range cannot exceed 91 calendar days inclusive (start to start + 90 days).")

    cached_cal = economic_calendar_state_store.load()
    if not cached_cal:
        raise HTTPException(status_code=503, detail="Supabase economic calendar is unavailable.")

    cov_start = date.fromisoformat(cached_cal.coverage_start)
    cov_end = date.fromisoformat(cached_cal.coverage_end)
    if d_start < cov_start or d_end > cov_end:
        raise HTTPException(
            status_code=422,
            detail=f"Requested range [{start_date}, {end_date}] is outside available data coverage [{cov_start}, {cov_end}]."
        )

    tz_tr = ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz_tr)

    events, warnings, _ = economic_calendar_state_store.events_between(d_start, d_end, as_of=now.date())

    return {
        "schema_version": "2.0",
        "generated_at": cached_cal.generated_at,
        "coverage_start": cached_cal.coverage_start,
        "coverage_end": cached_cal.coverage_end,
        "requested_start": start_date,
        "requested_end": end_date,
        "timezone": settings.TIMEZONE,
        "status": cached_cal.status,
        "stale": economic_calendar_state_store.is_stale(now),
        "provider_results": [pr.model_dump() for pr in cached_cal.provider_results],
        "events": [ev.model_dump() for ev in events],
        "warnings": _dedupe_warnings(warnings + list(cached_cal.warnings))
    }
