import html
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
)
from app.models.earnings_refresh import (
    EarningsEventChangeDetail,
    EarningsEventDiff,
    EarningsRefreshJobResult,
    NasdaqScheduleSnapshot,
)

TURKISH_MONTHS = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
}

SESSION_LABELS_TR = {
    "pre_market": "Açılış öncesi",
    "after_hours": "Kapanış sonrası",
    "time_not_supplied": "Saat belirtilmedi",
    "unspecified": "Saat belirtilmedi",
}

CACHE_ACTION_MAP = {
    "replaced": "Durum güncellendi",
    "merged_and_updated": "Kısmi veri birleştirildi ve durum güncellendi",
    "initial_partial_written": "İlk kısmi durum oluşturuldu",
    "preserved_previous": "Önceki güvenilir durum korundu",
    "write_failed": "Durum yazılamadı",
    "not_written": "Durum oluşturulmadı",
}

STATUS_METRICS: Dict[str, Dict[str, str]] = {
    "success": {
        "primary": "#166534",
        "bg": "#DCFCE7",
        "badge_bg": "#22C55E",
        "border": "#BBF7D0",
        "title": "Bilanço Takvimi Güncellendi",
        "text_title": "BİLANÇO TAKVİMİ GÜNCELLENDİ",
        "icon": "✓",
        "subject_icon": "✅",
        "label": "Başarılı",
    },
    "partial": {
        "primary": "#B45309",
        "bg": "#FEF3C7",
        "badge_bg": "#F59E0B",
        "border": "#FDE68A",
        "title": "Güncelleme Kısmen Tamamlandı",
        "text_title": "GÜNCELLEME KISMEN TAMAMLANDI",
        "icon": "!",
        "subject_icon": "⚠️",
        "label": "Kısmi Başarı",
    },
    "failed": {
        "primary": "#B91C1C",
        "bg": "#FEE2E2",
        "badge_bg": "#EF4444",
        "border": "#FECACA",
        "title": "Güncelleme Başarısız",
        "text_title": "GÜNCELLEME BAŞARISIZ",
        "icon": "✕",
        "subject_icon": "❌",
        "label": "Başarısız",
    }
}


def format_turkish_date(dt_or_str: Optional[Union[date, datetime, str]]) -> str:
    """Format date to Turkish deterministic date: e.g. 17 Ağustos 2026."""
    if not dt_or_str:
        return ""
    if isinstance(dt_or_str, str):
        try:
            d = date.fromisoformat(dt_or_str.split("T")[0])
        except Exception:
            return dt_or_str
    elif isinstance(dt_or_str, datetime):
        d = dt_or_str.date()
    else:
        d = dt_or_str
    month_name = TURKISH_MONTHS.get(d.month, str(d.month))
    return f"{d.day} {month_name} {d.year}"


def format_turkish_date_short(dt_or_str: Optional[Union[date, datetime, str]]) -> str:
    """Format date to Turkish short date: e.g. 17 Ağustos."""
    if not dt_or_str:
        return ""
    if isinstance(dt_or_str, str):
        try:
            d = date.fromisoformat(dt_or_str.split("T")[0])
        except Exception:
            return dt_or_str
    elif isinstance(dt_or_str, datetime):
        d = dt_or_str.date()
    else:
        d = dt_or_str
    month_name = TURKISH_MONTHS.get(d.month, str(d.month))
    return f"{d.day} {month_name}"


def format_turkish_time(iso_str: str) -> str:
    """Extract and format time in Istanbul timezone: e.g. 08:04."""
    try:
        dt = datetime.fromisoformat(iso_str)
        tz = ZoneInfo(settings.TIMEZONE)
        dt_local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
        return dt_local.strftime("%H:%M")
    except Exception:
        return ""


def humanize_session(session: Optional[str]) -> str:
    """Translate market session to Turkish friendly label."""
    if not session:
        return ""
    return SESSION_LABELS_TR.get(session.lower(), session)


def humanize_sync_action(action: str) -> str:
    """Map internal synchronization action enum to readable Turkish text."""
    return CACHE_ACTION_MAP.get(action, action)


def source_status_presentation(status: Optional[str]) -> Dict[str, str]:
    """Return status-aware colors, badges, and Turkish labels for data sources."""
    if status == "success":
        return {
            "status": "success",
            "status_text": "Başarılı",
            "color": "#166534",
            "background": "#DCFCE7",
            "border": "#BBF7D0",
        }
    elif status == "partial":
        return {
            "status": "partial",
            "status_text": "Kısmi",
            "color": "#B45309",
            "background": "#FEF3C7",
            "border": "#FDE68A",
        }
    elif status == "failed":
        return {
            "status": "failed",
            "status_text": "Hata",
            "color": "#B91C1C",
            "background": "#FEE2E2",
            "border": "#FECACA",
        }
    else:
        return {
            "status": "unknown",
            "status_text": "Yapılandırılmadı",
            "color": "#6B7280",
            "background": "#F3F4F6",
            "border": "#E5E7EB",
        }


def calculate_meaningful_changes(result: EarningsRefreshJobResult) -> int:
    """Calculate total additions, changes, and removals excluding aged_out and unchanged."""
    bist = result.bist.diff
    nasdaq = result.nasdaq.diff
    return (
        bist.added_count
        + bist.changed_count
        + bist.removed_count
        + nasdaq.added_count
        + nasdaq.changed_count
        + nasdaq.removed_count
    )


def format_earnings_refresh_subject(result: EarningsRefreshJobResult) -> str:
    """Generate dynamic subject line according to job outcome and changes."""
    st = "failed" if result.database_status == "failed" else result.overall_status
    conf = STATUS_METRICS.get(st, STATUS_METRICS["failed"])
    date_str = format_turkish_date(result.coverage_start)
    total_changes = calculate_meaningful_changes(result)

    if st == "success":
        if total_changes == 0:
            status_text = "Güncel"
        elif total_changes == 1:
            status_text = "1 değişiklik"
        else:
            status_text = f"{total_changes} değişiklik"
    elif st == "partial":
        status_text = "Kısmi başarı"
    else:
        status_text = "Güncelleme başarısız"

    return f"{conf['subject_icon']} FinBot Bilanço Takvimi — {status_text} — {date_str}"


def collect_earnings_refresh_alerts(result: EarningsRefreshJobResult) -> List[str]:
    """Collect deduplicated warnings and errors across all 5 job sources (up to 20 items)."""
    alerts: List[str] = []
    seen: Set[str] = set()

    def add(msg: Optional[str]):
        if msg and msg.strip():
            clean_msg = msg.strip()
            if clean_msg not in seen:
                seen.add(clean_msg)
                alerts.append(clean_msg)

    # 1. BIST errors & warnings
    add(result.bist.error)
    for w in result.bist.warnings:
        add(w)

    # 2. Official IR provider warnings
    if result.bist.official_ir_summary:
        for p in result.bist.official_ir_summary.providers:
            for w in p.warnings:
                add(w)

    # 3. NASDAQ errors & warnings
    add(result.nasdaq.error)
    for w in result.nasdaq.warnings:
        add(w)

    # 4. Database errors
    if result.database_status == "failed":
        add(f"Supabase veritabanı senkronizasyonu başarısız: {result.database_error}" if result.database_error else "Supabase veritabanı senkronizasyonu başarısız.")

    return alerts[:20]


def compare_bist_calendars(
    previous: Optional[Bist100EarningsCalendar],
    fetched: Optional[Bist100EarningsCalendar],
    final: Optional[Bist100EarningsCalendar],
    coverage_start: date,
    coverage_end: date,
) -> EarningsEventDiff:
    """Compare previous BIST rolling cache against fetched and final rolling cache."""
    cov_start_str = coverage_start.isoformat()
    cov_end_str = coverage_end.isoformat()

    prev_events = previous.events if previous else []
    fetched_events = fetched.events if fetched else []
    final_events = final.events if final else []

    diff = EarningsEventDiff(
        previous_event_count=len(prev_events),
        fetched_event_count=len(fetched_events),
        final_event_count=len(final_events)
    )

    # Aged out events: in previous cache but outside current coverage window
    prev_aged_out = [
        ev for ev in prev_events
        if ev.earnings_date < cov_start_str or ev.earnings_date > cov_end_str
    ]
    diff.aged_out_count = len(prev_aged_out)
    diff.aged_out_details = [
        f"{ev.symbol} [{ev.period_start or '?'} -> {ev.period_end or '?'}]: {ev.earnings_date} (aged out)"
        for ev in prev_aged_out
    ]
    for ev in prev_aged_out:
        diff.details.append(EarningsEventChangeDetail(
            market="BIST100",
            change_type="aged_out",
            symbol=ev.symbol,
            fiscal_year=ev.fiscal_year,
            fiscal_period=ev.fiscal_period,
            old_date=ev.earnings_date,
            source=ev.source
        ))

    # Events in previous cache within current coverage window
    prev_in_cov = [
        ev for ev in prev_events
        if cov_start_str <= ev.earnings_date <= cov_end_str
    ]
    prev_in_cov_map: Dict[Tuple[str, Optional[str], Optional[str]], BistEarningsEvent] = {
        (ev.symbol, ev.period_start, ev.period_end): ev for ev in prev_in_cov
    }

    final_map: Dict[Tuple[str, Optional[str], Optional[str]], BistEarningsEvent] = {
        (ev.symbol, ev.period_start, ev.period_end): ev for ev in final_events
    }

    # Compare prev vs final
    for key, prev_ev in prev_in_cov_map.items():
        if key in final_map:
            final_ev = final_map[key]
            if prev_ev.earnings_date != final_ev.earnings_date:
                diff.changed_count += 1
                diff.changed_details.append(
                    f"{final_ev.symbol} [{final_ev.period_start or '?'} -> {final_ev.period_end or '?'}]: {prev_ev.earnings_date} -> {final_ev.earnings_date}"
                )
                diff.details.append(EarningsEventChangeDetail(
                    market="BIST100",
                    change_type="changed",
                    symbol=final_ev.symbol,
                    fiscal_year=final_ev.fiscal_year,
                    fiscal_period=final_ev.fiscal_period,
                    old_date=prev_ev.earnings_date,
                    new_date=final_ev.earnings_date,
                    source=final_ev.source
                ))
            else:
                diff.unchanged_count += 1
        else:
            diff.removed_count += 1
            diff.removed_details.append(
                f"{prev_ev.symbol} [{prev_ev.period_start or '?'} -> {prev_ev.period_end or '?'}]: {prev_ev.earnings_date} (removed)"
            )
            diff.details.append(EarningsEventChangeDetail(
                market="BIST100",
                change_type="removed",
                symbol=prev_ev.symbol,
                fiscal_year=prev_ev.fiscal_year,
                fiscal_period=prev_ev.fiscal_period,
                old_date=prev_ev.earnings_date,
                source=prev_ev.source
            ))

    for key, final_ev in final_map.items():
        if key not in prev_in_cov_map:
            diff.added_count += 1
            diff.added_details.append(
                f"{final_ev.symbol} [{final_ev.period_start or '?'} -> {final_ev.period_end or '?'}]: {final_ev.earnings_date} (added)"
            )
            diff.details.append(EarningsEventChangeDetail(
                market="BIST100",
                change_type="added",
                symbol=final_ev.symbol,
                fiscal_year=final_ev.fiscal_year,
                fiscal_period=final_ev.fiscal_period,
                new_date=final_ev.earnings_date,
                source=final_ev.source
            ))

    # Carried forward symbols detection
    if fetched and final and previous:
        kap_incomplete_symbols = {
            s.symbol for s in fetched.symbol_results if not s.is_complete
        }
        ir_incomplete_symbols = set(fetched.ir_incomplete_symbols or [])
        incomplete_syms = kap_incomplete_symbols | ir_incomplete_symbols
        carried_symbols: Set[str] = set()

        fetched_keys = {
            (ev.symbol, ev.period_start, ev.period_end) for ev in fetched_events
        }
        seen_carried_keys: Set[Tuple[str, Optional[str], Optional[str]]] = set()

        for ev in final_events:
            if ev.symbol in incomplete_syms:
                key = (ev.symbol, ev.period_start, ev.period_end)
                if key in prev_in_cov_map and key not in fetched_keys and key not in seen_carried_keys:
                    seen_carried_keys.add(key)
                    diff.carried_forward_count += 1
                    carried_symbols.add(ev.symbol)
                    diff.details.append(EarningsEventChangeDetail(
                        market="BIST100",
                        change_type="carried_forward",
                        symbol=ev.symbol,
                        fiscal_year=ev.fiscal_year,
                        fiscal_period=ev.fiscal_period,
                        new_date=ev.earnings_date,
                        source=ev.source
                    ))

        diff.carried_forward_symbols = sorted(list(carried_symbols))

    return diff


def compare_nasdaq_calendars(
    previous: Optional[Nasdaq100EarningsCalendar],
    fetched: Optional[Nasdaq100EarningsCalendar],
    final: Optional[Nasdaq100EarningsCalendar],
    coverage_start: date,
    coverage_end: date,
) -> EarningsEventDiff:
    """Compare previous NASDAQ rolling cache against fetched and final rolling cache."""
    cov_start_str = coverage_start.isoformat()
    cov_end_str = coverage_end.isoformat()

    prev_events = previous.events if previous else []
    fetched_events = fetched.events if fetched else []
    final_events = final.events if final else []

    diff = EarningsEventDiff(
        previous_event_count=len(prev_events),
        fetched_event_count=len(fetched_events),
        final_event_count=len(final_events)
    )

    prev_aged_out = [
        ev for ev in prev_events
        if ev.earnings_date < cov_start_str or ev.earnings_date > cov_end_str
    ]
    diff.aged_out_count = len(prev_aged_out)
    diff.aged_out_details = [
        f"{ev.symbol} ({ev.session_label}): {ev.earnings_date} (aged out)"
        for ev in prev_aged_out
    ]
    for ev in prev_aged_out:
        diff.details.append(EarningsEventChangeDetail(
            market="NASDAQ100",
            change_type="aged_out",
            symbol=ev.symbol,
            old_date=ev.earnings_date,
            session=ev.session,
            session_label=ev.session_label,
            source="Yahoo Finance"
        ))

    prev_in_cov = [
        ev for ev in prev_events
        if cov_start_str <= ev.earnings_date <= cov_end_str
    ]

    prev_sym_events: Dict[str, Set[Tuple[str, str]]] = {}
    for ev in prev_in_cov:
        prev_sym_events.setdefault(ev.symbol, set()).add((ev.earnings_date, ev.session))

    final_sym_events: Dict[str, Set[Tuple[str, str]]] = {}
    for ev in final_events:
        final_sym_events.setdefault(ev.symbol, set()).add((ev.earnings_date, ev.session))

    # Evaluate symbol level schedule diffs
    all_prev_symbols = set(prev_sym_events.keys())
    all_final_symbols = set(final_sym_events.keys())

    # Symbols in prev but not in final -> Removed
    for sym in sorted(all_prev_symbols - all_final_symbols):
        diff.removed_count += len(prev_sym_events[sym])
        for d, s in sorted(prev_sym_events[sym]):
            diff.removed_details.append(f"{sym} ({s}): {d} (removed)")
            diff.details.append(EarningsEventChangeDetail(
                market="NASDAQ100",
                change_type="removed",
                symbol=sym,
                old_date=d,
                session=s,
                source="Yahoo Finance"
            ))

    # Symbols in final but not in prev -> Added
    for sym in sorted(all_final_symbols - all_prev_symbols):
        diff.added_count += len(final_sym_events[sym])
        for d, s in sorted(final_sym_events[sym]):
            diff.added_details.append(f"{sym} ({s}): {d} (added)")
            diff.details.append(EarningsEventChangeDetail(
                market="NASDAQ100",
                change_type="added",
                symbol=sym,
                new_date=d,
                session=s,
                source="Yahoo Finance"
            ))

    # Symbols in both -> Check unchanged vs changed schedule
    for sym in sorted(all_prev_symbols & all_final_symbols):
        prev_set = prev_sym_events[sym]
        final_set = final_sym_events[sym]

        unchanged_set = prev_set & final_set
        diff.unchanged_count += len(unchanged_set)

        if prev_set != final_set:
            diff.changed_count += 1
            prev_str = ", ".join(f"{d} ({s})" for d, s in sorted(prev_set))
            final_str = ", ".join(f"{d} ({s})" for d, s in sorted(final_set))
            diff.changed_details.append(f"{sym}: [{prev_str}] -> [{final_str}]")

            prev_list = sorted(list(prev_set))
            final_list = sorted(list(final_set))

            old_schedules = [
                NasdaqScheduleSnapshot(earnings_date=d, session=s)
                for d, s in prev_list
            ]
            new_schedules = [
                NasdaqScheduleSnapshot(earnings_date=d, session=s)
                for d, s in final_list
            ]

            old_d = prev_list[0][0] if len(prev_list) == 1 else None
            old_s = prev_list[0][1] if len(prev_list) == 1 else None
            new_d = final_list[0][0] if len(final_list) == 1 else None
            new_s = final_list[0][1] if len(final_list) == 1 else None

            diff.details.append(EarningsEventChangeDetail(
                market="NASDAQ100",
                change_type="changed",
                symbol=sym,
                old_date=old_d,
                new_date=new_d,
                old_session=old_s,
                new_session=new_s,
                old_schedules=old_schedules,
                new_schedules=new_schedules,
                source="Yahoo Finance"
            ))

    return diff


def _format_friendly_added(item: Union[EarningsEventChangeDetail, str]) -> Dict[str, str]:
    if isinstance(item, EarningsEventChangeDetail):
        sym = item.symbol
        if item.market == "BIST100":
            period = f"{item.fiscal_period} {item.fiscal_year}" if item.fiscal_period and item.fiscal_year else ""
            date_tr = format_turkish_date(item.new_date)
            title = f"{sym} • {period}" if period else sym
            text_line = f"{title} • {date_tr}" if date_tr else title
            return {
                "symbol": sym,
                "badge": period,
                "date_line": date_tr,
                "text_line": text_line,
                "market": item.market
            }
        else:
            date_tr = format_turkish_date(item.new_date)
            sess_tr = humanize_session(item.session)
            sub = f"{date_tr} • {sess_tr}" if sess_tr else date_tr
            text_line = f"{sym} • {sub}" if sub else sym
            return {
                "symbol": sym,
                "badge": "",
                "date_line": sub,
                "text_line": text_line,
                "market": item.market
            }
    # Fallback string cleaner
    s = str(item)
    for suffix in [" (added)", " (removed)", " (aged out)"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return {"symbol": s, "badge": "", "date_line": "", "text_line": s, "market": "UNKNOWN"}


def _format_friendly_changed(item: Union[EarningsEventChangeDetail, str]) -> Dict[str, Any]:
    if isinstance(item, EarningsEventChangeDetail):
        sym = item.symbol
        if item.market == "BIST100":
            period = f"{item.fiscal_period} {item.fiscal_year}" if item.fiscal_period and item.fiscal_year else ""
            old_tr = format_turkish_date(item.old_date)
            new_tr = format_turkish_date(item.new_date)
            title = f"{sym} • {period}" if period else sym
            text_line = f"{title}\n  {old_tr} → {new_tr}"
            return {
                "symbol": sym,
                "badge": period,
                "is_multiple_schedule": False,
                "old_date": old_tr,
                "new_date": new_tr,
                "text_line": text_line,
                "market": item.market
            }
        else:
            has_multi = (len(item.old_schedules) > 1 or len(item.new_schedules) > 1)
            if has_multi:
                old_lines = [
                    f"{format_turkish_date(s.earnings_date)}" + (f" • {humanize_session(s.session)}" if humanize_session(s.session) else "")
                    for s in item.old_schedules
                ]
                new_lines = [
                    f"{format_turkish_date(s.earnings_date)}" + (f" • {humanize_session(s.session)}" if humanize_session(s.session) else "")
                    for s in item.new_schedules
                ]

                txt_parts = [f"{sym}", "  Önce:"]
                for ol in old_lines:
                    txt_parts.append(f"  • {ol}")
                txt_parts.append("  Sonra:")
                for nl in new_lines:
                    txt_parts.append(f"  • {nl}")

                return {
                    "symbol": sym,
                    "badge": "",
                    "is_multiple_schedule": True,
                    "old_schedule_lines": old_lines,
                    "new_schedule_lines": new_lines,
                    "old_date": "",
                    "new_date": "",
                    "text_line": "\n".join(txt_parts),
                    "market": item.market
                }
            else:
                old_d = item.old_date or (item.old_schedules[0].earnings_date if item.old_schedules else None)
                old_s = item.old_session or (item.old_schedules[0].session if item.old_schedules else None)
                new_d = item.new_date or (item.new_schedules[0].earnings_date if item.new_schedules else None)
                new_s = item.new_session or (item.new_schedules[0].session if item.new_schedules else None)

                old_tr = format_turkish_date(old_d)
                old_sess = humanize_session(old_s)
                old_full = f"{old_tr} • {old_sess}" if old_sess else old_tr

                new_tr = format_turkish_date(new_d)
                new_sess = humanize_session(new_s)
                new_full = f"{new_tr} • {new_sess}" if new_sess else new_tr

                text_line = f"{sym}\n  {old_full} → {new_full}"
                return {
                    "symbol": sym,
                    "badge": "",
                    "is_multiple_schedule": False,
                    "old_date": old_full,
                    "new_date": new_full,
                    "text_line": text_line,
                    "market": item.market
                }
    # Fallback string cleaner
    s = str(item).replace(" -> ", " → ")
    for suffix in [" (added)", " (removed)", " (aged out)"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return {"symbol": s, "badge": "", "is_multiple_schedule": False, "old_date": "", "new_date": "", "text_line": s, "market": "UNKNOWN"}


def _format_friendly_removed(item: Union[EarningsEventChangeDetail, str]) -> Dict[str, str]:
    if isinstance(item, EarningsEventChangeDetail):
        sym = item.symbol
        if item.market == "BIST100":
            period = f"{item.fiscal_period} {item.fiscal_year}" if item.fiscal_period and item.fiscal_year else ""
            old_tr = format_turkish_date(item.old_date)
            title = f"{sym} • {period}" if period else sym
            text_line = f"{title} • {old_tr}" if old_tr else title
            return {
                "symbol": sym,
                "badge": period,
                "date_line": old_tr,
                "text_line": text_line,
                "market": item.market
            }
        else:
            old_tr = format_turkish_date(item.old_date)
            sess_tr = humanize_session(item.session)
            sub = f"{old_tr} • {sess_tr}" if sess_tr else old_tr
            text_line = f"{sym} • {sub}" if sub else sym
            return {
                "symbol": sym,
                "badge": "",
                "date_line": sub,
                "text_line": text_line,
                "market": item.market
            }
    # Fallback string cleaner
    s = str(item)
    for suffix in [" (added)", " (removed)", " (aged out)"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return {"symbol": s, "badge": "", "date_line": "", "text_line": s, "market": "UNKNOWN"}


def build_earnings_refresh_email_view(result: EarningsRefreshJobResult) -> Dict[str, Any]:
    """Pure view-model builder containing all operational presentation decisions."""
    st = "failed" if result.database_status == "failed" else result.overall_status
    conf = STATUS_METRICS.get(st, STATUS_METRICS["failed"])

    date_formatted = format_turkish_date(result.coverage_start)
    time_formatted = format_turkish_time(result.started_at)
    cov_start_short = format_turkish_date_short(result.coverage_start)
    cov_end_full = format_turkish_date(result.coverage_end)
    coverage_formatted = f"{cov_start_short} – {cov_end_full}"
    duration_formatted = f"{result.duration_seconds:.1f} saniye"

    total_changes = calculate_meaningful_changes(result)
    has_changes = total_changes > 0 or len(result.bist.diff.carried_forward_symbols) > 0

    # Header description
    bist = result.bist
    nasdaq = result.nasdaq

    if st == "success":
        if total_changes == 0:
            header_desc = "BIST100 ve Nasdaq100 takvimleri başarıyla kontrol edildi. Bugün yeni, değişen veya kaldırılan bilanço tarihi bulunamadı."
        else:
            header_desc = f"BIST100 ve Nasdaq100 takvimleri başarıyla güncellendi ({total_changes} değişiklik)."
    elif st == "partial":
        bist_issues = []
        if bist.unavailable_symbol_count and bist.unavailable_symbol_count > 0:
            bist_issues.append(f"BIST100 tarafında {bist.unavailable_symbol_count} sembol kontrol edilemedi")
        elif bist.outcome_status != "success":
            bist_issues.append("BIST100 tarafında kısmi veri alındı")

        if nasdaq.outcome_status != "success":
            bist_issues.append("Nasdaq100 tarafında sorun yaşandı")
        else:
            bist_issues.append("Nasdaq100 başarıyla güncellendi")

        header_desc = ". ".join(bist_issues) + "."
    else:
        if bist.state_preserved or nasdaq.state_preserved:
            header_desc = "Güncelleme sırasında hata oluştu. Önceki güvenilir durum korundu."
        else:
            header_desc = "Güncelleme sırasında hata oluştu. Lütfen sistem kayıtlarını kontrol ediniz."

    # BIST100 Card Data
    if bist.total_symbol_count is not None:
        bist_completed_str = f"{bist.completed_symbol_count} / {bist.total_symbol_count} şirket kontrol edildi"
        bist_scheduled_str = f"{bist.scheduled_symbol_count or 0} şirketin açıklanmış bilanço tarihi bulunuyor"
        bist_healthy_str = f"{bist.healthy_negative_symbol_count or 0} şirketin henüz tarihi yok"
        bist_metrics_available = True
    else:
        bist_completed_str = "Güncel sembol kontrol sayıları üretilemedi."
        bist_scheduled_str = "Önceki güvenilir durum korundu."
        bist_healthy_str = ""
        bist_metrics_available = False

    bist_diff = bist.diff
    bist_changes_str = f"{bist_diff.added_count} yeni • {bist_diff.changed_count} değişen • {bist_diff.removed_count} kaldırılan"
    bist_sync_action_str = humanize_sync_action(bist.sync_action)

    # NASDAQ100 Card Data
    nasdaq_diff = nasdaq.diff
    nasdaq_events_str = f"{nasdaq_diff.final_event_count} bilanço etkinliği"
    nasdaq_changes_str = f"{nasdaq_diff.added_count} yeni • {nasdaq_diff.changed_count} değişen • {nasdaq_diff.removed_count} kaldırılan"
    nasdaq_sync_action_str = humanize_sync_action(nasdaq.sync_action)

    # Collect and format structured details
    raw_added: List[Union[EarningsEventChangeDetail, str]] = []
    raw_changed: List[Union[EarningsEventChangeDetail, str]] = []
    raw_removed: List[Union[EarningsEventChangeDetail, str]] = []

    # Priority 1: Structured details from diff
    for d in bist_diff.details + nasdaq_diff.details:
        if d.change_type == "added":
            raw_added.append(d)
        elif d.change_type == "changed":
            raw_changed.append(d)
        elif d.change_type == "removed":
            raw_removed.append(d)

    # Priority 2: Fallback to string lists if details were not populated
    if not raw_added and (bist_diff.added_details or nasdaq_diff.added_details):
        raw_added = [f"[BIST] {x}" for x in bist_diff.added_details] + [f"[NASDAQ] {x}" for x in nasdaq_diff.added_details]
    if not raw_changed and (bist_diff.changed_details or nasdaq_diff.changed_details):
        raw_changed = [f"[BIST] {x}" for x in bist_diff.changed_details] + [f"[NASDAQ] {x}" for x in nasdaq_diff.changed_details]
    if not raw_removed and (bist_diff.removed_details or nasdaq_diff.removed_details):
        raw_removed = [f"[BIST] {x}" for x in bist_diff.removed_details] + [f"[NASDAQ] {x}" for x in nasdaq_diff.removed_details]

    formatted_added = [_format_friendly_added(x) for x in raw_added]
    formatted_changed = [_format_friendly_changed(x) for x in raw_changed]
    formatted_removed = [_format_friendly_removed(x) for x in raw_removed]

    added_items = formatted_added[:20]
    added_remaining = max(0, len(formatted_added) - 20)

    changed_items = formatted_changed[:20]
    changed_remaining = max(0, len(formatted_changed) - 20)

    removed_items = formatted_removed[:20]
    removed_remaining = max(0, len(formatted_removed) - 20)

    carried_items = bist_diff.carried_forward_symbols[:20]
    carried_remaining = max(0, len(bist_diff.carried_forward_symbols) - 20)

    # Data sources evaluation with status-aware presentations
    kap_st = bist.official_ir_summary.kap_fetch_status if (bist.official_ir_summary and bist.official_ir_summary.kap_fetch_status) else bist.fetch_status
    kap_pres = source_status_presentation(kap_st)

    # Official IR Aggregate Status Classification
    ir_providers = bist.official_ir_summary.providers if bist.official_ir_summary else []
    total_ir = len(ir_providers)
    fully_success_ir = sum(1 for p in ir_providers if p.status == "success" and not p.parser_failed)
    partial_ir = sum(1 for p in ir_providers if p.status == "partial" and not p.parser_failed)
    failed_ir = sum(1 for p in ir_providers if p.status == "failed" or p.parser_failed)

    if total_ir == 0:
        ir_st = "unknown"
        ir_text = "Yapılandırılmadı"
    elif fully_success_ir == total_ir:
        ir_st = "success"
        ir_text = f"{total_ir} / {total_ir} başarılı"
    elif failed_ir == total_ir:
        ir_st = "failed"
        ir_text = f"0 / {total_ir} başarılı"
    else:
        ir_st = "partial"
        if partial_ir == total_ir:
            ir_text = f"{total_ir} / {total_ir} kısmi"
        elif fully_success_ir > 0 and partial_ir > 0 and failed_ir == 0:
            ir_text = f"{fully_success_ir} başarılı • {partial_ir} kısmi"
        elif fully_success_ir > 0 and failed_ir > 0 and partial_ir == 0:
            ir_text = f"{fully_success_ir} başarılı • {failed_ir} hatalı"
        elif partial_ir > 0 and failed_ir > 0 and fully_success_ir == 0:
            ir_text = f"{partial_ir} kısmi • {failed_ir} hatalı"
        else:
            ir_text = f"{fully_success_ir} başarılı • {partial_ir} kısmi • {failed_ir} hatalı"

    ir_pres = source_status_presentation(ir_st)
    ir_pres["status_text"] = ir_text

    nasdaq_st = nasdaq.fetch_status
    nasdaq_pres = source_status_presentation(nasdaq_st)

    # Problematic Official IR provider reasons (clean, no raw warnings to prevent duplication)
    problematic_ir: List[Tuple[str, str]] = []
    for p in ir_providers:
        if p.status != "success" or p.parser_failed:
            if p.parser_failed:
                reason = "Parser hatası"
            elif p.status == "failed":
                if p.retry_count > 0:
                    reason = f"Sağlayıcı hatası ({p.retry_count} tekrar denemesi)"
                else:
                    reason = "Sağlayıcı hatası"
            elif p.status == "partial":
                reason = "Kısmi veri"
            else:
                reason = "Veri eksik"
            problematic_ir.append((p.symbol, reason))

    # Deduplicated alerts
    alerts = collect_earnings_refresh_alerts(result)

    return {
        "status": st,
        "status_conf": conf,
        "date_formatted": date_formatted,
        "time_formatted": time_formatted,
        "coverage_formatted": coverage_formatted,
        "duration_formatted": duration_formatted,
        "header_desc": header_desc,
        "total_changes": total_changes,
        "has_changes": has_changes,
        "bist_card": {
            "completed_str": bist_completed_str,
            "scheduled_str": bist_scheduled_str,
            "healthy_str": bist_healthy_str,
            "metrics_available": bist_metrics_available,
            "changes_str": bist_changes_str,
            "sync_action_str": bist_sync_action_str,
            "unavailable_symbols": bist.unavailable_symbols,
            "state_preserved": bist.state_preserved,
            "status": bist.outcome_status,
        },
        "nasdaq_card": {
            "events_str": nasdaq_events_str,
            "changes_str": nasdaq_changes_str,
            "sync_action_str": nasdaq_sync_action_str,
            "status": nasdaq.outcome_status,
        },
        "diffs": {
            "added_items": added_items,
            "added_remaining": added_remaining,
            "changed_items": changed_items,
            "changed_remaining": changed_remaining,
            "removed_items": removed_items,
            "removed_remaining": removed_remaining,
            "carried_items": carried_items,
            "carried_remaining": carried_remaining,
        },
        "sources": {
            "kap": kap_pres,
            "ir": ir_pres,
            "nasdaq": nasdaq_pres,
            "problematic_ir": problematic_ir,
        },
        "alerts": alerts,
    }


def format_earnings_refresh_email_html(result: EarningsRefreshJobResult) -> str:
    """Format structured EarningsRefreshJobResult into a responsive, mobile-friendly HTML email."""
    view = build_earnings_refresh_email_view(result)
    conf = view["status_conf"]

    def esc(val: Any) -> str:
        return html.escape(str(val) if val is not None else "", quote=True)

    meta_parts = [esc(view["date_formatted"])]
    if view["time_formatted"]:
        meta_parts[0] += f" • {esc(view['time_formatted'])}"
    meta_parts.append(f"Kapsam: {esc(view['coverage_formatted'])}")
    meta_parts.append(f"Süre: {esc(view['duration_formatted'])}")
    meta_line = " &nbsp;|&nbsp; ".join(meta_parts)

    html_lines: List[str] = []
    html_lines.append('<!DOCTYPE html>')
    html_lines.append('<html lang="tr">')
    html_lines.append('<head>')
    html_lines.append('<meta charset="UTF-8">')
    html_lines.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html_lines.append(f'<title>{esc(conf["title"])}</title>')
    html_lines.append('</head>')
    html_lines.append('<body style="margin: 0; padding: 0; background-color: #F3F4F6; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">')
    html_lines.append('<table width="100%" bgcolor="#F3F4F6" cellpadding="0" cellspacing="0" border="0" style="background-color: #F3F4F6; width: 100%; margin: 0; padding: 24px 8px;">')
    html_lines.append('  <tr>')
    html_lines.append('    <td align="center">')
    html_lines.append('      <!-- MAIN CARD WRAPPER (MAX 640px) -->')
    html_lines.append('      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 640px; width: 100%; background-color: #FFFFFF; border-radius: 8px; overflow: hidden; border: 1px solid #E5E7EB; border-collapse: separate;">')

    # 1. STATUS HEADER
    html_lines.append('        <!-- STATUS HEADER -->')
    html_lines.append('        <tr>')
    html_lines.append(f'          <td bgcolor="{conf["bg"]}" style="background-color: {conf["bg"]}; padding: 24px 20px; border-bottom: 2px solid {conf["border"]};">')
    html_lines.append('            <table width="100%" cellpadding="0" cellspacing="0" border="0">')
    html_lines.append('              <tr>')
    html_lines.append('                <td>')
    html_lines.append(f'                  <span style="display: inline-block; font-size: 20px; font-weight: 700; color: {conf["primary"]}; line-height: 1.2;">{conf["icon"]} {esc(conf["title"])}</span>')
    html_lines.append(f'                  <div style="font-size: 13px; color: #4B5563; margin-top: 6px; line-height: 1.4;">{meta_line}</div>')
    if view["header_desc"]:
        html_lines.append(f'                  <div style="font-size: 14px; font-weight: 500; color: {conf["primary"]}; margin-top: 10px; line-height: 1.4;">{esc(view["header_desc"])}</div>')
    html_lines.append('                </td>')
    html_lines.append('              </tr>')
    html_lines.append('            </table>')
    html_lines.append('          </td>')
    html_lines.append('        </tr>')

    # 2. BODY CONTENT
    html_lines.append('        <!-- BODY CONTENT -->')
    html_lines.append('        <tr>')
    html_lines.append('          <td style="padding: 24px 20px; background-color: #FFFFFF;">')

    # SUMMARY CARDS ROW
    bist_c = view["bist_card"]
    nasdaq_c = view["nasdaq_card"]

    html_lines.append('            <!-- SUMMARY CARDS TABLE -->')
    html_lines.append('            <table width="100%" cellpadding="0" cellspacing="0" border="0">')
    html_lines.append('              <tr>')
    html_lines.append('                <!-- BIST100 CARD -->')
    html_lines.append('                <td width="49%" valign="top" style="background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 6px; padding: 14px 16px;">')
    html_lines.append('                  <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 8px; border-bottom: 1px solid #E5E7EB; padding-bottom: 6px;">BIST100</div>')
    html_lines.append(f'                  <div style="font-size: 13px; color: #374151; margin-bottom: 4px;">{esc(bist_c["completed_str"])}</div>')
    if bist_c["scheduled_str"]:
        html_lines.append(f'                  <div style="font-size: 13px; color: #374151; margin-bottom: 4px;">{esc(bist_c["scheduled_str"])}</div>')
    if bist_c["healthy_str"]:
        html_lines.append(f'                  <div style="font-size: 12px; color: #6B7280; margin-bottom: 6px;">({esc(bist_c["healthy_str"])})</div>')
    html_lines.append(f'                  <div style="font-size: 12px; font-weight: 600; color: #1F2937; margin-top: 6px;">{esc(bist_c["changes_str"])}</div>')
    html_lines.append(f'                  <div style="font-size: 12px; color: #4B5563; margin-top: 8px; background-color: #FFFFFF; padding: 4px 8px; border-radius: 4px; border: 1px solid #E5E7EB; display: inline-block;">{esc(bist_c["sync_action_str"])}</div>')
    if bist_c["unavailable_symbols"]:
        unavail_str = ", ".join(bist_c["unavailable_symbols"])
        html_lines.append(f'                  <div style="font-size: 12px; color: #DC2626; margin-top: 6px;">⚠️ Kontrol edilemeyen: {esc(unavail_str)}</div>')
    html_lines.append('                </td>')

    html_lines.append('                <td width="2%"></td>')

    html_lines.append('                <!-- NASDAQ100 CARD -->')
    html_lines.append('                <td width="49%" valign="top" style="background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 6px; padding: 14px 16px;">')
    html_lines.append('                  <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 8px; border-bottom: 1px solid #E5E7EB; padding-bottom: 6px;">NASDAQ100</div>')
    html_lines.append(f'                  <div style="font-size: 13px; color: #374151; margin-bottom: 4px;">{esc(nasdaq_c["events_str"])}</div>')
    html_lines.append(f'                  <div style="font-size: 12px; font-weight: 600; color: #1F2937; margin-top: 6px;">{esc(nasdaq_c["changes_str"])}</div>')
    html_lines.append(f'                  <div style="font-size: 12px; color: #4B5563; margin-top: 8px; background-color: #FFFFFF; padding: 4px 8px; border-radius: 4px; border: 1px solid #E5E7EB; display: inline-block;">{esc(nasdaq_c["sync_action_str"])}</div>')
    html_lines.append('                </td>')
    html_lines.append('              </tr>')
    html_lines.append('            </table>')

    # 3. CHANGES DETAILS SECTION (Only if has_changes)
    diffs = view["diffs"]
    if view["has_changes"]:
        html_lines.append('            <!-- CHANGES DETAILS -->')
        html_lines.append('            <div style="margin-top: 24px;">')
        html_lines.append('              <div style="font-size: 13px; font-weight: 700; color: #4B5563; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px;">DEĞİŞİKLİK DETAYLARI</div>')

        # Added
        if diffs["added_items"]:
            html_lines.append('              <div style="margin-bottom: 12px; background-color: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 6px; padding: 10px 14px;">')
            html_lines.append('                <div style="font-size: 12px; font-weight: 700; color: #166534; margin-bottom: 6px;">+ YENİ EKLENENLER</div>')
            html_lines.append('                <table width="100%" cellpadding="0" cellspacing="0" border="0">')
            for item in diffs["added_items"]:
                badge_html = f' <span style="color: #6B7280; font-weight: normal;">• {esc(item["badge"])}</span>' if item.get("badge") else ""
                date_html = f'<div style="font-size: 12px; color: #374151; margin-top: 2px;">{esc(item["date_line"])}</div>' if item.get("date_line") else ""
                html_lines.append('                  <tr><td style="padding: 4px 0; border-bottom: 1px dashed #DCFCE7;">')
                html_lines.append(f'                    <div style="font-size: 13px; font-weight: 600; color: #111827;">{esc(item["symbol"])}{badge_html}</div>')
                if date_html:
                    html_lines.append(f'                    {date_html}')
                html_lines.append('                  </td></tr>')
            if diffs["added_remaining"] > 0:
                html_lines.append(f'                  <tr><td style="font-size: 12px; color: #4B5563; padding-top: 6px; font-style: italic;">... ve {diffs["added_remaining"]} kayıt daha</td></tr>')
            html_lines.append('                </table>')
            html_lines.append('              </div>')

        # Changed
        if diffs["changed_items"]:
            html_lines.append('              <div style="margin-bottom: 12px; background-color: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 6px; padding: 10px 14px;">')
            html_lines.append('                <div style="font-size: 12px; font-weight: 700; color: #1E40AF; margin-bottom: 6px;">↻ TARİHİ DEĞİŞENLER</div>')
            html_lines.append('                <table width="100%" cellpadding="0" cellspacing="0" border="0">')
            for item in diffs["changed_items"]:
                badge_html = f' <span style="color: #6B7280; font-weight: normal;">• {esc(item["badge"])}</span>' if item.get("badge") else ""
                html_lines.append('                  <tr><td style="padding: 6px 0; border-bottom: 1px dashed #DBEAFE;">')
                html_lines.append(f'                    <div style="font-size: 13px; font-weight: 600; color: #111827;">{esc(item["symbol"])}{badge_html}</div>')

                if item.get("is_multiple_schedule"):
                    html_lines.append('                    <div style="font-size: 12px; color: #6B7280; margin-top: 3px;">Önce:</div>')
                    for line in item.get("old_schedule_lines", []):
                        html_lines.append(f'                    <div style="font-size: 12px; color: #4B5563; margin-left: 8px; text-decoration: line-through;">• {esc(line)}</div>')
                    html_lines.append('                    <div style="font-size: 12px; color: #1E40AF; font-weight: 600; margin-top: 4px;">Sonra:</div>')
                    for line in item.get("new_schedule_lines", []):
                        html_lines.append(f'                    <div style="font-size: 12px; color: #1E40AF; margin-left: 8px; font-weight: 600;">• {esc(line)}</div>')
                else:
                    old_d = esc(item.get("old_date", ""))
                    new_d = esc(item.get("new_date", ""))
                    if old_d or new_d:
                        html_lines.append(f'                    <div style="font-size: 12px; color: #4B5563; margin-top: 2px;"><span style="text-decoration: line-through;">{old_d}</span> &nbsp;<span style="color: #1E40AF; font-weight: bold;">→</span>&nbsp; <strong style="color: #1E40AF;">{new_d}</strong></div>')
                html_lines.append('                  </td></tr>')
            if diffs["changed_remaining"] > 0:
                html_lines.append(f'                  <tr><td style="font-size: 12px; color: #4B5563; padding-top: 6px; font-style: italic;">... ve {diffs["changed_remaining"]} kayıt daha</td></tr>')
            html_lines.append('                </table>')
            html_lines.append('              </div>')

        # Removed
        if diffs["removed_items"]:
            html_lines.append('              <div style="margin-bottom: 12px; background-color: #FEF2F2; border: 1px solid #FECACA; border-radius: 6px; padding: 10px 14px;">')
            html_lines.append('                <div style="font-size: 12px; font-weight: 700; color: #991B1B; margin-bottom: 6px;">✕ KALDIRILANLAR</div>')
            html_lines.append('                <table width="100%" cellpadding="0" cellspacing="0" border="0">')
            for item in diffs["removed_items"]:
                badge_html = f' <span style="color: #6B7280; font-weight: normal;">• {esc(item["badge"])}</span>' if item.get("badge") else ""
                date_html = f'<div style="font-size: 12px; color: #991B1B; margin-top: 2px;">{esc(item["date_line"])}</div>' if item.get("date_line") else ""
                html_lines.append('                  <tr><td style="padding: 4px 0; border-bottom: 1px dashed #FEE2E2;">')
                html_lines.append(f'                    <div style="font-size: 13px; font-weight: 600; color: #111827;">{esc(item["symbol"])}{badge_html}</div>')
                if date_html:
                    html_lines.append(f'                    {date_html}')
                html_lines.append('                  </td></tr>')
            if diffs["removed_remaining"] > 0:
                html_lines.append(f'                  <tr><td style="font-size: 12px; color: #4B5563; padding-top: 6px; font-style: italic;">... ve {diffs["removed_remaining"]} kayıt daha</td></tr>')
            html_lines.append('                </table>')
            html_lines.append('              </div>')

        # Carried forward
        if diffs["carried_items"]:
            carried_str = ", ".join(diffs["carried_items"])
            html_lines.append('              <div style="margin-bottom: 12px; background-color: #FFFBEB; border: 1px solid #FDE68A; border-radius: 6px; padding: 10px 14px;">')
            html_lines.append(f'                <div style="font-size: 12px; font-weight: 700; color: #92400E; margin-bottom: 4px;">⇄ ÖNCEKİ CACHE\'TEN TAŞINAN SEMBOLLER: <span style="font-weight: 400;">{esc(carried_str)}</span></div>')
            if diffs["carried_remaining"] > 0:
                html_lines.append(f'                <div style="font-size: 12px; color: #78350F; font-style: italic;">... ve {diffs["carried_remaining"]} sembol daha</div>')
            html_lines.append('              </div>')

        html_lines.append('            </div>')

    # 4. DATA SOURCES SECTION
    srcs = view["sources"]
    kap_s = srcs["kap"]
    ir_s = srcs["ir"]
    nasdaq_s = srcs["nasdaq"]

    html_lines.append('            <!-- DATA SOURCES -->')
    html_lines.append('            <div style="margin-top: 20px;">')
    html_lines.append('              <div style="font-size: 13px; font-weight: 700; color: #4B5563; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px;">VERİ KAYNAKLARI</div>')
    html_lines.append('              <table width="100%" cellpadding="8" cellspacing="0" border="0" style="border: 1px solid #E5E7EB; border-radius: 6px; font-size: 13px; background-color: #F9FAFB;">')
    html_lines.append('                <tr>')
    html_lines.append('                  <td style="color: #374151; font-weight: 500; border-bottom: 1px solid #E5E7EB;">BIST Veri Akışı (KAP)</td>')
    html_lines.append('                  <td align="right" style="border-bottom: 1px solid #E5E7EB;">')
    html_lines.append(f'                    <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; color: {kap_s["color"]}; background-color: {kap_s["background"]}; border: 1px solid {kap_s["border"]};">{esc(kap_s["status_text"])}</span>')
    html_lines.append('                  </td>')
    html_lines.append('                </tr>')
    html_lines.append('                <tr>')
    html_lines.append('                  <td style="color: #374151; font-weight: 500; border-bottom: 1px solid #E5E7EB;">Official IR Sağlayıcıları</td>')
    html_lines.append('                  <td align="right" style="border-bottom: 1px solid #E5E7EB;">')
    html_lines.append(f'                    <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; color: {ir_s["color"]}; background-color: {ir_s["background"]}; border: 1px solid {ir_s["border"]};">{esc(ir_s["status_text"])}</span>')
    html_lines.append('                  </td>')
    html_lines.append('                </tr>')
    html_lines.append('                <tr>')
    html_lines.append('                  <td style="color: #374151; font-weight: 500;">Yahoo Finance (NASDAQ)</td>')
    html_lines.append('                  <td align="right">')
    html_lines.append(f'                    <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; color: {nasdaq_s["color"]}; background-color: {nasdaq_s["background"]}; border: 1px solid {nasdaq_s["border"]};">{esc(nasdaq_s["status_text"])}</span>')
    html_lines.append('                  </td>')
    html_lines.append('                </tr>')
    html_lines.append('              </table>')

    if srcs["problematic_ir"]:
        html_lines.append('              <div style="margin-top: 6px; padding: 8px 12px; background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FEE2E2;">')
        for sym, reason in srcs["problematic_ir"]:
            html_lines.append(f'                <div style="font-size: 12px; color: #991B1B; margin: 2px 0;">• <strong>{esc(sym)}</strong>: {esc(reason)}</div>')
        html_lines.append('              </div>')
    html_lines.append('            </div>')

    # 5. ALERTS SECTION (Only if alerts exist)
    if view["alerts"]:
        alert_bg = "#FEE2E2" if view["status"] == "failed" else "#FEF3C7"
        alert_border = "#FECACA" if view["status"] == "failed" else "#FDE68A"
        alert_text_color = "#991B1B" if view["status"] == "failed" else "#92400E"

        html_lines.append('            <!-- ALERTS & WARNINGS -->')
        html_lines.append('            <div style="margin-top: 20px;">')
        html_lines.append(f'              <div style="background-color: {alert_bg}; border: 1px solid {alert_border}; border-radius: 6px; padding: 12px 14px;">')
        html_lines.append(f'                <div style="font-size: 13px; font-weight: 700; color: {alert_text_color}; margin-bottom: 6px;">UYARILAR VE BİLDİRİMLER</div>')
        html_lines.append('                <table width="100%" cellpadding="0" cellspacing="0" border="0">')
        for alert_msg in view["alerts"]:
            html_lines.append(f'                  <tr><td style="font-size: 12px; color: {alert_text_color}; padding: 2px 0; line-height: 1.4;">• {esc(alert_msg)}</td></tr>')
        html_lines.append('                </table>')
        html_lines.append('              </div>')
        html_lines.append('            </div>')

    html_lines.append('          </td>')
    html_lines.append('        </tr>')

    # 6. FOOTER
    html_lines.append('        <!-- FOOTER -->')
    html_lines.append('        <tr>')
    html_lines.append('          <td bgcolor="#F9FAFB" align="center" style="background-color: #F9FAFB; padding: 14px 20px; border-top: 1px solid #E5E7EB; font-size: 12px; color: #6B7280; line-height: 1.4;">')
    html_lines.append('            FinBot Otomatik Bilanço Servisi &nbsp;•&nbsp; Bu e-posta günlük operasyonel bilgilendirme amacıyla otomatik oluşturulmuştur.')
    html_lines.append('          </td>')
    html_lines.append('        </tr>')

    html_lines.append('      </table>')
    html_lines.append('    </td>')
    html_lines.append('  </tr>')
    html_lines.append('</table>')
    html_lines.append('</body>')
    html_lines.append('</html>')

    return "\n".join(html_lines)


def format_earnings_refresh_email_text(result: EarningsRefreshJobResult) -> str:
    """Format structured EarningsRefreshJobResult into clean, readable Turkish operational plain text."""
    view = build_earnings_refresh_email_view(result)
    conf = view["status_conf"]

    lines: List[str] = []
    lines.append(f"FİNBOT BİLANÇO TAKVİMİ — {conf['text_title']}")
    lines.append("=" * 50)
    lines.append(f"Tarih: {view['date_formatted']}" + (f" • {view['time_formatted']}" if view['time_formatted'] else ""))
    lines.append(f"Kapsam: {view['coverage_formatted']}")
    lines.append(f"Çalışma Süresi: {view['duration_formatted']}")
    if view["header_desc"]:
        lines.append(f"Durum Notu: {view['header_desc']}")
    lines.append("")

    # BIST100
    bist_c = view["bist_card"]
    lines.append("[BIST100]")
    lines.append(f"• {bist_c['completed_str']}")
    if bist_c["scheduled_str"]:
        lines.append(f"• {bist_c['scheduled_str']}")
    if bist_c["healthy_str"]:
        lines.append(f"• {bist_c['healthy_str']}")
    lines.append(f"• Değişiklikler: {bist_c['changes_str']}")
    lines.append(f"• Durum: {bist_c['sync_action_str']}")
    if bist_c["unavailable_symbols"]:
        lines.append(f"• Kontrol Edilemeyen Semboller: {', '.join(bist_c['unavailable_symbols'])}")
    lines.append("")

    # NASDAQ100
    nasdaq_c = view["nasdaq_card"]
    lines.append("[NASDAQ100]")
    lines.append(f"• {nasdaq_c['events_str']}")
    lines.append(f"• Değişiklikler: {nasdaq_c['changes_str']}")
    lines.append(f"• Durum: {nasdaq_c['sync_action_str']}")
    lines.append("")

    # CHANGES DETAILS (if any)
    diffs = view["diffs"]
    if view["has_changes"]:
        lines.append("[DEĞİŞİKLİK DETAYLARI]")
        if diffs["added_items"]:
            lines.append("• Yeni Eklenenler:")
            for item in diffs["added_items"]:
                lines.append(f"  + {item['text_line']}")
            if diffs["added_remaining"] > 0:
                lines.append(f"  ... ve {diffs['added_remaining']} kayıt daha")

        if diffs["changed_items"]:
            lines.append("• Tarihi Değişenler:")
            for item in diffs["changed_items"]:
                lines.append(f"  ↻ {item['text_line']}")
            if diffs["changed_remaining"] > 0:
                lines.append(f"  ... ve {diffs['changed_remaining']} kayıt daha")

        if diffs["removed_items"]:
            lines.append("• Kaldırılanlar:")
            for item in diffs["removed_items"]:
                lines.append(f"  ✕ {item['text_line']}")
            if diffs["removed_remaining"] > 0:
                lines.append(f"  ... ve {diffs['removed_remaining']} kayıt daha")

        if diffs["carried_items"]:
            lines.append(f"• Önceki Durumdan Taşınanlar: {', '.join(diffs['carried_items'])}")
            if diffs["carried_remaining"] > 0:
                lines.append(f"  ... ve {diffs['carried_remaining']} sembol daha")
        lines.append("")

    # DATA SOURCES
    srcs = view["sources"]
    lines.append("[VERİ KAYNAKLARI]")
    lines.append(f"• BIST Veri Akışı (KAP): {srcs['kap']['status_text']}")
    lines.append(f"• Official IR: {srcs['ir']['status_text']}")
    lines.append(f"• Yahoo Finance: {srcs['nasdaq']['status_text']}")
    if srcs["problematic_ir"]:
        for sym, reason in srcs["problematic_ir"]:
            lines.append(f"  - {sym}: {reason}")
    lines.append("")

    # ALERTS
    if view["alerts"]:
        lines.append("[UYARILAR VE BİLDİRİMLER]")
        for alert_msg in view["alerts"]:
            lines.append(f"• {alert_msg}")
        lines.append("")

    return "\n".join(lines)


def format_earnings_refresh_email(result: EarningsRefreshJobResult) -> str:
    """Backward-compatible delegation to plain text formatter."""
    return format_earnings_refresh_email_text(result)
