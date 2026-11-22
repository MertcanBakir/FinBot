from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsApiResponse,
    BistEarningsEvent,
    CombinedEarningsApiResponse,
    EarningsCalendarStatus,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsApiResponse,
    NasdaqEarningsEvent,
)
from app.services.calendar_state_store import calendar_state_store

router = APIRouter(prefix="/api/earnings", tags=["earnings"])

SESSION_ORDER = {
    "pre_market": 0,
    "time_not_supplied": 1,
    "after_hours": 2,
}


def _now_istanbul() -> datetime:
    """Return current timezone-aware datetime in Istanbul."""
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def _dedupe_warnings(warnings_list: List[str]) -> List[str]:
    """Deduplicate warnings preserving first-seen order, stripping whitespace and omitting empty."""
    return list(dict.fromkeys(w.strip() for w in warnings_list if w and w.strip()))


def _validate_cache_metadata(
    cached_cal: Any,
    market_name: str
) -> Tuple[Optional[date], Optional[date], Optional[str]]:
    """Validate canonical cache metadata integrity: generated_at, timezone, coverage_start, coverage_end, and event dates.

    Returns:
        (cov_start, cov_end, error_message)
        If valid, error_message is None. If invalid, cov_start/cov_end are None and error_message describes the failure.
    """
    if not hasattr(cached_cal, "generated_at") or not cached_cal.generated_at:
        return None, None, f"{market_name} cache generated_at is missing."

    try:
        dt = datetime.fromisoformat(cached_cal.generated_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None, None, f"{market_name} cache generated_at must include timezone information."
    except Exception as e:
        return None, None, f"Malformed generated_at '{cached_cal.generated_at}' in {market_name} cache: {e}"

    if not hasattr(cached_cal, "coverage_start") or not cached_cal.coverage_start:
        return None, None, f"{market_name} cache coverage_start is missing."

    if not hasattr(cached_cal, "coverage_end") or not cached_cal.coverage_end:
        return None, None, f"{market_name} cache coverage_end is missing."

    try:
        cov_start = date.fromisoformat(cached_cal.coverage_start)
    except Exception as e:
        return None, None, f"Malformed coverage_start '{cached_cal.coverage_start}' in {market_name} cache: {e}"

    try:
        cov_end = date.fromisoformat(cached_cal.coverage_end)
    except Exception as e:
        return None, None, f"Malformed coverage_end '{cached_cal.coverage_end}' in {market_name} cache: {e}"

    if cov_start > cov_end:
        return None, None, f"Reversed coverage window [{cov_start} > {cov_end}] in {market_name} cache."

    # Validate each event's earnings_date format and coverage window inclusion
    events = getattr(cached_cal, "events", [])
    for ev in events:
        if not hasattr(ev, "earnings_date") or not ev.earnings_date:
            return None, None, f"Missing earnings_date in {market_name} event '{getattr(ev, 'symbol', 'UNKNOWN')}'."
        try:
            ev_d = date.fromisoformat(ev.earnings_date)
        except Exception as e:
            return None, None, f"Malformed earnings_date '{ev.earnings_date}' in {market_name} event '{ev.symbol}': {e}"
        if ev_d < cov_start or ev_d > cov_end:
            return None, None, f"Event {ev.symbol} date '{ev_d}' is outside cache coverage [{cov_start}, {cov_end}]."

    return cov_start, cov_end, None


def _month_bounds(now_dt: datetime, offset: int) -> Tuple[date, date]:
    """Calculate start and end date for a month given an offset in O(1) time without unbounded loops."""
    try:
        total_months = (now_dt.year - 1) * 12 + (now_dt.month - 1) + offset
        target_year, month_idx = divmod(total_months, 12)
        target_year += 1
        target_month = month_idx + 1

        if target_year > 9999 or target_year < 1:
            raise HTTPException(
                status_code=400,
                detail=f"Requested month offset ({offset}) produces year ({target_year}) outside valid calendar range [1, 9999]."
            )

        month_start = date(target_year, target_month, 1)
        if target_month == 12:
            month_end = date(target_year, 12, 31)
        else:
            month_end = date(target_year, target_month + 1, 1) - timedelta(days=1)
        return month_start, month_end
    except (OverflowError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Requested month offset ({offset}) causes date overflow: {e}"
        ) from e


def _week_bounds(now_dt: datetime, offset: int) -> Tuple[date, date]:
    """Calculate Monday and Sunday dates for a week given an offset, safely handling overflow."""
    try:
        ref_date = now_dt + timedelta(weeks=offset)
        monday = ref_date.date() - timedelta(days=ref_date.weekday())
        sunday = monday + timedelta(days=6)
        if monday.year > 9999 or sunday.year > 9999 or monday.year < 1:
            raise HTTPException(
                status_code=400,
                detail=f"Requested week offset ({offset}) exceeds supported calendar range [1, 9999]."
            )
        return monday, sunday
    except (OverflowError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Requested week offset ({offset}) causes date overflow: {e}"
        ) from e


def _validate_range_params(start_date: str, end_date: str) -> Tuple[date, date]:
    """Validate custom date range parameters: YYYY-MM-DD, start <= end, and max 91 calendar days inclusive."""
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
        raise HTTPException(
            status_code=400,
            detail="Requested date range cannot exceed 91 calendar days inclusive (start to start + 90 days)."
        )

    return d_start, d_end


def _is_cache_stale(generated_at_str: str, now_dt: datetime) -> bool:
    """Check if cache generated_at is older than 36 hours (assumes generated_at already validated)."""
    try:
        dt = datetime.fromisoformat(generated_at_str.replace("Z", "+00:00"))
        tz_tr = ZoneInfo(settings.TIMEZONE)
        dt = dt.astimezone(tz_tr)
        age_seconds = (now_dt - dt).total_seconds()
        return age_seconds > (36 * 3600)
    except Exception:
        return True


def _filter_and_sort_bist_events(
    events: List[BistEarningsEvent],
    eff_start: date,
    eff_end: date
) -> List[BistEarningsEvent]:
    """Filter BIST events within effective date range and sort deterministically."""
    filtered = [
        ev for ev in events
        if eff_start <= date.fromisoformat(ev.earnings_date) <= eff_end
    ]
    filtered.sort(key=lambda ev: (
        ev.earnings_date,
        ev.symbol,
        ev.fiscal_year or 0,
        ev.fiscal_period or "",
    ))
    return filtered


def _filter_and_sort_nasdaq_events(
    events: List[NasdaqEarningsEvent],
    eff_start: date,
    eff_end: date
) -> List[NasdaqEarningsEvent]:
    """Filter NASDAQ events within effective date range and sort deterministically."""
    filtered = [
        ev for ev in events
        if eff_start <= date.fromisoformat(ev.earnings_date) <= eff_end
    ]
    filtered.sort(key=lambda ev: (
        ev.earnings_date,
        SESSION_ORDER.get(ev.session, 1),
        ev.symbol,
    ))
    return filtered


_CACHE_NOT_PROVIDED = object()


def _evaluate_bist_cache(
    view: Literal["today", "week", "month", "range"],
    req_start: date,
    req_end: date,
    now_dt: datetime,
    cached_cal: Any = _CACHE_NOT_PROVIDED,
    load_error: Optional[str] = None,
    schema_version: str = "2.0",
    date_field: Optional[str] = None,
    week_bounds: Optional[Tuple[str, str]] = None,
    month_bounds: Optional[Tuple[str, str]] = None,
) -> Tuple[Optional[BistEarningsApiResponse], Optional[str], Optional[Literal["missing_or_failed", "invalid_cache", "outside_coverage"]]]:
    """Pure evaluation of BIST100 cache for requested date window without raising HTTP exceptions.

    Returns:
        (response_obj, error_message, error_kind)
        where error_kind is "missing_or_failed", "invalid_cache", or "outside_coverage".
    """
    if load_error is not None:
        return None, load_error, "invalid_cache"

    if cached_cal is _CACHE_NOT_PROVIDED:
        try:
            cached_cal = calendar_state_store.load_bist()
        except Exception as e:
            return None, f"BIST100 Supabase earnings data failed validation: {e}", "invalid_cache"

    if cached_cal is None:
        return None, "BIST100 Supabase earnings data is unavailable.", "missing_or_failed"

    if cached_cal.status == "failed":
        return None, "BIST100 rolling earnings cache status is failed.", "missing_or_failed"

    cov_start, cov_end, validation_err = _validate_cache_metadata(cached_cal, "BIST100")
    if validation_err is not None:
        return None, validation_err, "invalid_cache"

    assert cov_start is not None and cov_end is not None

    cov_warning: Optional[str] = None

    if view == "today":
        if req_start < cov_start or req_start > cov_end:
            return None, f"Today ({req_start}) is outside BIST cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = req_start
        eff_end = req_end
        is_complete = True
    elif view in ["week", "month"]:
        if req_end < cov_start or req_start > cov_end:
            return None, f"Requested {view} range [{req_start}, {req_end}] has no overlap with BIST cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = max(req_start, cov_start)
        eff_end = min(req_end, cov_end)
        is_complete = (req_start >= cov_start and req_end <= cov_end)
        if not is_complete:
            view_label = "hafta" if view == "week" else "ay"
            cov_warning = f"İstenen {view_label} ({req_start} — {req_end}) kısmen BIST cache kapsamı ({cov_start} — {cov_end}) dışında kalıyor."
    elif view == "range":
        if req_start < cov_start or req_end > cov_end:
            return None, f"Requested range [{req_start}, {req_end}] is outside BIST cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = req_start
        eff_end = req_end
        is_complete = True
    else:
        return None, f"Unknown view: {view}", "invalid_cache"

    try:
        filtered_events = _filter_and_sort_bist_events(cached_cal.events, eff_start, eff_end)
    except Exception as e:
        return None, f"Failed filtering BIST events: {e}", "invalid_cache"

    stale = _is_cache_stale(cached_cal.generated_at, now_dt)

    raw_warnings = list(cached_cal.warnings)
    if cov_warning:
        raw_warnings.append(cov_warning)
    final_warnings = _dedupe_warnings(raw_warnings)

    resp = BistEarningsApiResponse(
        schema_version=schema_version,
        generated_at=cached_cal.generated_at,
        coverage_start=cached_cal.coverage_start,
        coverage_end=cached_cal.coverage_end,
        requested_start=req_start.strftime("%Y-%m-%d"),
        requested_end=req_end.strftime("%Y-%m-%d"),
        effective_start=eff_start.strftime("%Y-%m-%d"),
        effective_end=eff_end.strftime("%Y-%m-%d"),
        is_complete=is_complete,
        timezone=settings.TIMEZONE,
        market="BIST100",
        status=cached_cal.status,
        provider_status=cached_cal.provider_status,
        stale=stale,
        events=filtered_events,
        warnings=final_warnings,
        date=date_field,
        week_start=week_bounds[0] if week_bounds else None,
        week_end=week_bounds[1] if week_bounds else None,
        month_start=month_bounds[0] if month_bounds else None,
        month_end=month_bounds[1] if month_bounds else None,
    )
    return resp, None, None


def _evaluate_nasdaq_cache(
    view: Literal["today", "week", "month", "range"],
    req_start: date,
    req_end: date,
    now_dt: datetime,
    cached_cal: Any = _CACHE_NOT_PROVIDED,
    load_error: Optional[str] = None,
    schema_version: str = "2.0",
    date_field: Optional[str] = None,
    week_bounds: Optional[Tuple[str, str]] = None,
    month_bounds: Optional[Tuple[str, str]] = None,
) -> Tuple[Optional[NasdaqEarningsApiResponse], Optional[str], Optional[Literal["missing_or_failed", "invalid_cache", "outside_coverage"]]]:
    """Pure evaluation of NASDAQ100 cache for requested date window without raising HTTP exceptions.

    Returns:
        (response_obj, error_message, error_kind)
        where error_kind is "missing_or_failed", "invalid_cache", or "outside_coverage".
    """
    if load_error is not None:
        return None, load_error, "invalid_cache"

    if cached_cal is _CACHE_NOT_PROVIDED:
        try:
            cached_cal = calendar_state_store.load_nasdaq()
        except Exception as e:
            return None, f"NASDAQ100 Supabase earnings data failed validation: {e}", "invalid_cache"

    if cached_cal is None:
        return None, "NASDAQ100 Supabase earnings data is unavailable.", "missing_or_failed"

    if cached_cal.status == "failed":
        return None, "NASDAQ100 rolling earnings cache status is failed.", "missing_or_failed"

    cov_start, cov_end, validation_err = _validate_cache_metadata(cached_cal, "NASDAQ100")
    if validation_err is not None:
        return None, validation_err, "invalid_cache"

    assert cov_start is not None and cov_end is not None

    cov_warning: Optional[str] = None

    if view == "today":
        if req_start < cov_start or req_start > cov_end:
            return None, f"Today ({req_start}) is outside NASDAQ cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = req_start
        eff_end = req_end
        is_complete = True
    elif view in ["week", "month"]:
        if req_end < cov_start or req_start > cov_end:
            return None, f"Requested {view} range [{req_start}, {req_end}] has no overlap with NASDAQ cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = max(req_start, cov_start)
        eff_end = min(req_end, cov_end)
        is_complete = (req_start >= cov_start and req_end <= cov_end)
        if not is_complete:
            view_label = "hafta" if view == "week" else "ay"
            cov_warning = f"İstenen {view_label} ({req_start} — {req_end}) kısmen NASDAQ cache kapsamı ({cov_start} — {cov_end}) dışında kalıyor."
    elif view == "range":
        if req_start < cov_start or req_end > cov_end:
            return None, f"Requested range [{req_start}, {req_end}] is outside NASDAQ cache coverage [{cov_start}, {cov_end}].", "outside_coverage"
        eff_start = req_start
        eff_end = req_end
        is_complete = True
    else:
        return None, f"Unknown view: {view}", "invalid_cache"

    try:
        filtered_events = _filter_and_sort_nasdaq_events(cached_cal.events, eff_start, eff_end)
    except Exception as e:
        return None, f"Failed filtering NASDAQ events: {e}", "invalid_cache"

    stale = _is_cache_stale(cached_cal.generated_at, now_dt)

    raw_warnings = list(cached_cal.warnings)
    if cov_warning:
        raw_warnings.append(cov_warning)
    final_warnings = _dedupe_warnings(raw_warnings)

    resp = NasdaqEarningsApiResponse(
        schema_version=schema_version,
        generated_at=cached_cal.generated_at,
        coverage_start=cached_cal.coverage_start,
        coverage_end=cached_cal.coverage_end,
        requested_start=req_start.strftime("%Y-%m-%d"),
        requested_end=req_end.strftime("%Y-%m-%d"),
        effective_start=eff_start.strftime("%Y-%m-%d"),
        effective_end=eff_end.strftime("%Y-%m-%d"),
        is_complete=is_complete,
        timezone=settings.TIMEZONE,
        market="NASDAQ100",
        status=cached_cal.status,
        provider_status=cached_cal.provider_status,
        stale=stale,
        events=filtered_events,
        warnings=final_warnings,
        date=date_field,
        week_start=week_bounds[0] if week_bounds else None,
        week_end=week_bounds[1] if week_bounds else None,
        month_start=month_bounds[0] if month_bounds else None,
        month_end=month_bounds[1] if month_bounds else None,
    )
    return resp, None, None


def _build_combined_response(
    view: Literal["today", "week", "month", "range"],
    req_start: date,
    req_end: date,
    now_dt: datetime,
    date_field: Optional[str] = None,
    week_bounds: Optional[Tuple[str, str]] = None,
    month_bounds: Optional[Tuple[str, str]] = None,
) -> CombinedEarningsApiResponse:
    """Build unified combined response for BIST100 + NASDAQ100 earnings calendars with complete market isolation."""
    # Ensure load_bist and load_nasdaq are called exactly once
    cached_bist: Optional[Bist100EarningsCalendar] = None
    bist_load_err: Optional[str] = None
    try:
        cached_bist = calendar_state_store.load_bist()
    except Exception as e:
        cached_bist = None
        bist_load_err = f"BIST100 rolling earnings cache load error: {e}"

    cached_nasdaq: Optional[Nasdaq100EarningsCalendar] = None
    nasdaq_load_err: Optional[str] = None
    try:
        cached_nasdaq = calendar_state_store.load_nasdaq()
    except Exception as e:
        cached_nasdaq = None
        nasdaq_load_err = f"NASDAQ100 rolling earnings cache load error: {e}"

    bist_resp, bist_err, bist_err_kind = _evaluate_bist_cache(
        view=view,
        req_start=req_start,
        req_end=req_end,
        now_dt=now_dt,
        cached_cal=cached_bist,
        load_error=bist_load_err,
        schema_version="2.0",
        date_field=date_field,
        week_bounds=week_bounds,
        month_bounds=month_bounds,
    )

    nasdaq_resp, nasdaq_err, nasdaq_err_kind = _evaluate_nasdaq_cache(
        view=view,
        req_start=req_start,
        req_end=req_end,
        now_dt=now_dt,
        cached_cal=cached_nasdaq,
        load_error=nasdaq_load_err,
        schema_version="2.0",
        date_field=date_field,
        week_bounds=week_bounds,
        month_bounds=month_bounds,
    )

    # 1. Zero usable markets case
    if bist_resp is None and nasdaq_resp is None:
        # If both are purely outside_coverage:
        if bist_err_kind == "outside_coverage" and nasdaq_err_kind == "outside_coverage":
            if view == "today":
                raise HTTPException(
                    status_code=503,
                    detail=f"Both markets failed for today: BIST: {bist_err}; NASDAQ: {nasdaq_err}"
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"Requested {view} range has no available coverage in both markets: BIST: {bist_err}; NASDAQ: {nasdaq_err}"
                )
        # If at least one market is missing_or_failed or invalid_cache -> 503
        raise HTTPException(
            status_code=503,
            detail=f"Both BIST100 and NASDAQ100 rolling earnings caches are unavailable. BIST: {bist_err}; NASDAQ: {nasdaq_err}"
        )

    # 2. Record market errors for any unavailable market
    market_errors: Dict[str, str] = {}
    if bist_resp is None and bist_err:
        market_errors["BIST100"] = bist_err
    if nasdaq_resp is None and nasdaq_err:
        market_errors["NASDAQ100"] = nasdaq_err

    # 3. Determine top-level status
    if bist_resp is not None and nasdaq_resp is not None:
        if bist_resp.status == "success" and nasdaq_resp.status == "success":
            top_status: EarningsCalendarStatus = "success"
        else:
            top_status = "partial"
    else:
        top_status = "partial"

    # 4. Determine top-level stale
    active_stales: List[bool] = []
    if bist_resp is not None:
        active_stales.append(bist_resp.stale)
    if nasdaq_resp is not None:
        active_stales.append(nasdaq_resp.stale)
    top_stale = any(active_stales) if active_stales else False

    # 5. Determine top-level is_complete
    top_is_complete = (
        bist_resp is not None
        and nasdaq_resp is not None
        and bist_resp.is_complete
        and nasdaq_resp.is_complete
    )

    # 6. Determine top-level warnings
    top_warnings_raw: List[str] = []
    if bist_resp is not None:
        top_warnings_raw.extend(bist_resp.warnings)
    if nasdaq_resp is not None:
        top_warnings_raw.extend(nasdaq_resp.warnings)
    top_warnings = _dedupe_warnings(top_warnings_raw)

    return CombinedEarningsApiResponse(
        schema_version="2.0",
        view=view,
        timezone=settings.TIMEZONE,
        requested_start=req_start.strftime("%Y-%m-%d"),
        requested_end=req_end.strftime("%Y-%m-%d"),
        status=top_status,
        stale=top_stale,
        is_complete=top_is_complete,
        bist100=bist_resp,
        nasdaq100=nasdaq_resp,
        market_errors=market_errors,
        warnings=top_warnings,
    )


# -----------------------------------------------------------------------------
# BIST 100 Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/bist100/today",
    response_model=BistEarningsApiResponse,
    summary="Get Today's BIST 100 Earnings Calendar",
    description="Returns today's BIST 100 earnings announcement dates from Supabase. Does NOT send emails or call live providers."
)
async def get_bist100_today_earnings() -> BistEarningsApiResponse:
    now_dt = _now_istanbul()
    today_d = now_dt.date()
    resp, err_msg, err_kind = _evaluate_bist_cache(
        view="today",
        req_start=today_d,
        req_end=today_d,
        now_dt=now_dt,
        schema_version="2.0",
        date_field=today_d.strftime("%Y-%m-%d"),
    )
    if err_kind in ("missing_or_failed", "invalid_cache", "outside_coverage"):
        raise HTTPException(status_code=503, detail=err_msg or "BIST100 rolling earnings cache is unavailable.")
    assert resp is not None
    return resp


@router.get(
    "/bist100/week",
    response_model=BistEarningsApiResponse,
    summary="Get BIST 100 Earnings Calendar for Selected Week",
    description="Returns BIST 100 earnings announcement dates from Supabase for the selected week. Backward-compatible schema_version 1.0. Does NOT send emails or call live providers."
)
async def get_bist100_week_earnings(
    offset: int = Query(0, ge=0, description="Week offset: 0 for current week, 1 for next week, etc.")
) -> BistEarningsApiResponse:
    now_dt = _now_istanbul()
    monday, sunday = _week_bounds(now_dt, offset)
    resp, err_msg, err_kind = _evaluate_bist_cache(
        view="week",
        req_start=monday,
        req_end=sunday,
        now_dt=now_dt,
        schema_version="1.0",
        week_bounds=(monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")),
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "BIST100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


@router.get(
    "/bist100/month",
    response_model=BistEarningsApiResponse,
    summary="Get BIST 100 Earnings Calendar for Selected Month",
    description="Returns BIST 100 earnings announcement dates from Supabase for the selected month. Does NOT send emails or call live providers."
)
async def get_bist100_month_earnings(
    offset: int = Query(0, ge=0, description="Month offset: 0 for current month, 1 for next month, etc.")
) -> BistEarningsApiResponse:
    now_dt = _now_istanbul()
    m_start, m_end = _month_bounds(now_dt, offset)
    resp, err_msg, err_kind = _evaluate_bist_cache(
        view="month",
        req_start=m_start,
        req_end=m_end,
        now_dt=now_dt,
        schema_version="2.0",
        month_bounds=(m_start.strftime("%Y-%m-%d"), m_end.strftime("%Y-%m-%d")),
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "BIST100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


@router.get(
    "/bist100",
    response_model=BistEarningsApiResponse,
    summary="Get BIST 100 Earnings Calendar for Custom Date Range",
    description="Returns BIST 100 earnings announcement dates from Supabase for a custom date range (up to 91 days inclusive). Does NOT send emails or call live providers."
)
async def get_bist100_range_earnings(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
) -> BistEarningsApiResponse:
    d_start, d_end = _validate_range_params(start_date, end_date)
    now_dt = _now_istanbul()
    resp, err_msg, err_kind = _evaluate_bist_cache(
        view="range",
        req_start=d_start,
        req_end=d_end,
        now_dt=now_dt,
        schema_version="2.0",
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "BIST100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


# -----------------------------------------------------------------------------
# NASDAQ 100 Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/nasdaq100/today",
    response_model=NasdaqEarningsApiResponse,
    summary="Get Today's NASDAQ 100 Earnings Calendar",
    description="Returns today's NASDAQ 100 earnings announcement dates from Supabase. Does NOT send emails or call live providers."
)
async def get_nasdaq100_today_earnings() -> NasdaqEarningsApiResponse:
    now_dt = _now_istanbul()
    today_d = now_dt.date()
    resp, err_msg, err_kind = _evaluate_nasdaq_cache(
        view="today",
        req_start=today_d,
        req_end=today_d,
        now_dt=now_dt,
        schema_version="2.0",
        date_field=today_d.strftime("%Y-%m-%d"),
    )
    if err_kind in ("missing_or_failed", "invalid_cache", "outside_coverage"):
        raise HTTPException(status_code=503, detail=err_msg or "NASDAQ100 rolling earnings cache is unavailable.")
    assert resp is not None
    return resp


@router.get(
    "/nasdaq100/week",
    response_model=NasdaqEarningsApiResponse,
    summary="Get NASDAQ 100 Earnings Calendar for Selected Week",
    description="Returns NASDAQ 100 earnings announcement dates from Supabase for the selected week. Backward-compatible schema_version 1.0. Does NOT send emails or call live providers."
)
async def get_nasdaq100_week_earnings(
    offset: int = Query(0, ge=0, description="Week offset: 0 for current week, 1 for next week, etc.")
) -> NasdaqEarningsApiResponse:
    now_dt = _now_istanbul()
    monday, sunday = _week_bounds(now_dt, offset)
    resp, err_msg, err_kind = _evaluate_nasdaq_cache(
        view="week",
        req_start=monday,
        req_end=sunday,
        now_dt=now_dt,
        schema_version="1.0",
        week_bounds=(monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")),
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "NASDAQ100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


@router.get(
    "/nasdaq100/month",
    response_model=NasdaqEarningsApiResponse,
    summary="Get NASDAQ 100 Earnings Calendar for Selected Month",
    description="Returns NASDAQ 100 earnings announcement dates from Supabase for the selected month. Does NOT send emails or call live providers."
)
async def get_nasdaq100_month_earnings(
    offset: int = Query(0, ge=0, description="Month offset: 0 for current month, 1 for next month, etc.")
) -> NasdaqEarningsApiResponse:
    now_dt = _now_istanbul()
    m_start, m_end = _month_bounds(now_dt, offset)
    resp, err_msg, err_kind = _evaluate_nasdaq_cache(
        view="month",
        req_start=m_start,
        req_end=m_end,
        now_dt=now_dt,
        schema_version="2.0",
        month_bounds=(m_start.strftime("%Y-%m-%d"), m_end.strftime("%Y-%m-%d")),
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "NASDAQ100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


@router.get(
    "/nasdaq100",
    response_model=NasdaqEarningsApiResponse,
    summary="Get NASDAQ 100 Earnings Calendar for Custom Date Range",
    description="Returns NASDAQ 100 earnings announcement dates from Supabase for a custom date range (up to 91 days inclusive). Does NOT send emails or call live providers."
)
async def get_nasdaq100_range_earnings(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
) -> NasdaqEarningsApiResponse:
    d_start, d_end = _validate_range_params(start_date, end_date)
    now_dt = _now_istanbul()
    resp, err_msg, err_kind = _evaluate_nasdaq_cache(
        view="range",
        req_start=d_start,
        req_end=d_end,
        now_dt=now_dt,
        schema_version="2.0",
    )
    if err_kind in ("missing_or_failed", "invalid_cache"):
        raise HTTPException(status_code=503, detail=err_msg or "NASDAQ100 rolling earnings cache is unavailable.")
    if err_kind == "outside_coverage":
        raise HTTPException(status_code=422, detail=err_msg)
    assert resp is not None
    return resp


# -----------------------------------------------------------------------------
# Combined BIST 100 + NASDAQ 100 Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/today",
    response_model=CombinedEarningsApiResponse,
    summary="Get Today's Combined Earnings Calendar",
    description="Returns today's earnings announcement dates for both BIST 100 and NASDAQ 100 from Supabase. Does NOT send emails or call live providers."
)
async def get_combined_today_earnings() -> CombinedEarningsApiResponse:
    now_dt = _now_istanbul()
    today_d = now_dt.date()
    return _build_combined_response(
        view="today",
        req_start=today_d,
        req_end=today_d,
        now_dt=now_dt,
        date_field=today_d.strftime("%Y-%m-%d"),
    )


@router.get(
    "/week",
    response_model=CombinedEarningsApiResponse,
    summary="Get Combined Earnings Calendar for Selected Week",
    description="Returns earnings announcement dates for both BIST 100 and NASDAQ 100 from Supabase for the selected week. Does NOT send emails or call live providers."
)
async def get_combined_week_earnings(
    offset: int = Query(0, ge=0, description="Week offset: 0 for current week, 1 for next week, etc.")
) -> CombinedEarningsApiResponse:
    now_dt = _now_istanbul()
    monday, sunday = _week_bounds(now_dt, offset)
    return _build_combined_response(
        view="week",
        req_start=monday,
        req_end=sunday,
        now_dt=now_dt,
        week_bounds=(monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")),
    )


@router.get(
    "/month",
    response_model=CombinedEarningsApiResponse,
    summary="Get Combined Earnings Calendar for Selected Month",
    description="Returns earnings announcement dates for both BIST 100 and NASDAQ 100 from Supabase for the selected month. Does NOT send emails or call live providers."
)
async def get_combined_month_earnings(
    offset: int = Query(0, ge=0, description="Month offset: 0 for current month, 1 for next month, etc.")
) -> CombinedEarningsApiResponse:
    now_dt = _now_istanbul()
    m_start, m_end = _month_bounds(now_dt, offset)
    return _build_combined_response(
        view="month",
        req_start=m_start,
        req_end=m_end,
        now_dt=now_dt,
        month_bounds=(m_start.strftime("%Y-%m-%d"), m_end.strftime("%Y-%m-%d")),
    )


@router.get(
    "",
    response_model=CombinedEarningsApiResponse,
    summary="Get Combined Earnings Calendar for Custom Date Range",
    description="Returns earnings announcement dates for both BIST 100 and NASDAQ 100 from Supabase for a custom range (up to 91 days inclusive). Does NOT send emails or call live providers."
)
async def get_combined_range_earnings(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
) -> CombinedEarningsApiResponse:
    d_start, d_end = _validate_range_params(start_date, end_date)
    now_dt = _now_istanbul()
    return _build_combined_response(
        view="range",
        req_start=d_start,
        req_end=d_end,
        now_dt=now_dt,
    )
