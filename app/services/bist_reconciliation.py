import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

from app.models.earnings import (
    BistCombinedEarningsResult,
    BistEarningsEvent,
    EarningsCalendarStatus,
    KapEarningsFetchResult,
    OfficialIrIssue,
    OfficialIrProviderResult,
)
from app.providers.earnings.official_ir.common import parse_date_flexible

logger = logging.getLogger(__name__)

BIST_DAILY_MIN_EARNINGS_DATE = date(2026, 1, 1)


def scope_official_ir_results(
    ir_results: List[OfficialIrProviderResult],
    earnings_date_from: date,
    earnings_date_to: Optional[date] = None,
) -> List[OfficialIrProviderResult]:
    """Filter Official IR results to a specific earnings_date range without mutating inputs.

    Retains:
    - Events whose earnings_date falls within [earnings_date_from, earnings_date_to].
    - Global provider/schema issues (HTTP failures, table not found, network timeouts).
    - Row-level issues whose associated earnings_date / fiscal_year fall within the range.

    Eliminates:
    - Out-of-scope historical events.
    - Out-of-scope historical row validation, correction, or conflict warnings.

    Recalculates status:
    - If provider had only out-of-scope row warnings, its scoped status becomes 'success'
      (provided it has valid scoped events or is a healthy clean page).
    - Global failures and in-scope row failures always preserve their failed/partial status.
    """
    scoped_results: List[OfficialIrProviderResult] = []

    for ir_res in ir_results:
        # 1. Filter events
        scoped_events: List[BistEarningsEvent] = []
        for ev in ir_res.events:
            try:
                ev_d = date.fromisoformat(ev.earnings_date)
                if ev_d < earnings_date_from:
                    continue
                if earnings_date_to is not None and ev_d > earnings_date_to:
                    continue
                scoped_events.append(ev.model_copy())
            except Exception:
                pass

        # 2. Filter structured issues
        scoped_issues: List[OfficialIrIssue] = []
        for issue in ir_res.issues:
            if issue.is_global or issue.kind in ("provider_failure", "schema_failure"):
                scoped_issues.append(issue.model_copy())
                continue

            # Row-level issue: check earnings_date
            if issue.earnings_date:
                try:
                    if isinstance(issue.earnings_date, date):
                        iss_d = issue.earnings_date
                    else:
                        parsed_d = parse_date_flexible(str(issue.earnings_date))
                        iss_d = date.fromisoformat(parsed_d) if parsed_d else None
                    if iss_d:
                        if iss_d < earnings_date_from:
                            continue
                        if earnings_date_to is not None and iss_d > earnings_date_to:
                            continue
                        scoped_issues.append(issue.model_copy())
                        continue
                except Exception:
                    pass

            # Safe fallback: if earnings_date is absent or could not be parsed, keep the issue
            scoped_issues.append(issue.model_copy())

        # 3. Derive scoped warnings
        scoped_warnings: List[str] = []
        for iss in scoped_issues:
            if iss.message not in scoped_warnings:
                scoped_warnings.append(iss.message)

        # Fallback for legacy unstructured warnings when issues list was empty
        if not ir_res.issues and ir_res.warnings:
            for w in ir_res.warnings:
                if w not in scoped_warnings:
                    scoped_warnings.append(w)

        # 4. Status and parser_failed resolution
        has_global_failure = any(iss.kind in ("provider_failure", "schema_failure") for iss in scoped_issues)
        has_blocking_validation = any(iss.kind in ("row_validation", "conflict") for iss in scoped_issues)

        if ir_res.status == "failed":
            if has_global_failure or ir_res.parser_failed:
                scoped_status: EarningsCalendarStatus = "failed"
                scoped_parser_failed = ir_res.parser_failed
            elif len(scoped_events) > 0:
                scoped_status = "partial" if has_blocking_validation else "success"
                scoped_parser_failed = False
            else:
                scoped_status = "failed"
                scoped_parser_failed = ir_res.parser_failed
        else:
            if has_global_failure:
                scoped_status = "failed"
                scoped_parser_failed = ir_res.parser_failed
            elif has_blocking_validation:
                scoped_status = "partial"
                scoped_parser_failed = False
            else:
                scoped_status = "success"
                scoped_parser_failed = False

        scoped_accepted = len(scoped_events)
        scoped_rejected = len([iss for iss in scoped_issues if iss.kind == "row_validation"])
        scoped_rows_seen = scoped_accepted + scoped_rejected

        scoped_res = OfficialIrProviderResult(
            symbol=ir_res.symbol,
            source_provider=ir_res.source_provider,
            status=scoped_status,
            events=scoped_events,
            rows_seen=scoped_rows_seen,
            accepted_row_count=scoped_accepted,
            rejected_row_count=scoped_rejected,
            parser_failed=scoped_parser_failed,
            retry_count=ir_res.retry_count,
            warnings=scoped_warnings,
            issues=scoped_issues,
        )
        scoped_results.append(scoped_res)

    return scoped_results


def reconcile_kap_and_official_ir(
    kap_result: KapEarningsFetchResult,
    ir_results: List[OfficialIrProviderResult],
    relevance_start: Optional[date] = None,
    relevance_end: Optional[date] = None,
    emit_out_of_scope_warnings: bool = False,
) -> BistCombinedEarningsResult:
    """Reconcile KAP and Official IR earnings events according to strict precedence rules.

    Decision Matrix:
    - KAP success + all IR success -> success
    - KAP success + >=1 IR partial/failed/parser_failed -> partial
    - KAP partial + any -> partial
    - KAP failed + usable IR event exists -> partial
    - KAP failed + 0 usable IR events (even if IR success or empty list) -> failed
    - KAP failed + all IR failed / empty IR list -> failed
    - Any KAP/IR date conflict in relevance scope -> at least partial
    - Any internal IR date conflict in relevance scope -> at least partial
    """
    def _is_in_relevance_scope(date_val: Optional[str]) -> bool:
        if relevance_start is None and relevance_end is None:
            return True
        if not date_val:
            return False
        parsed = parse_date_flexible(date_val)
        if not parsed:
            return False
        try:
            d = date.fromisoformat(parsed)
            if relevance_start and d < relevance_start:
                return False
            if relevance_end and d > relevance_end:
                return False
            return True
        except Exception:
            return False

    warnings: List[str] = list(kap_result.warnings)
    has_relevant_conflict = False
    ir_incomplete_symbols: List[str] = []

    # 1. Map KAP normalized events by identity key
    kap_normalized_map: Dict[Tuple[str, Optional[str], Optional[str]], BistEarningsEvent] = {}
    for ev in kap_result.normalized_events:
        key = (ev.symbol, ev.period_start, ev.period_end)
        kap_normalized_map[key] = ev

    # 2. Process IR results and collect candidate IR events
    raw_ir_events_to_add: List[BistEarningsEvent] = []
    any_ir_incomplete = False

    for ir_res in ir_results:
        warnings.extend(ir_res.warnings)
        if ir_res.status != "success" or ir_res.parser_failed:
            any_ir_incomplete = True
            if ir_res.symbol not in ir_incomplete_symbols:
                ir_incomplete_symbols.append(ir_res.symbol)

        for ir_ev in ir_res.events:
            key = (ir_ev.symbol, ir_ev.period_start, ir_ev.period_end)
            if key in kap_normalized_map:
                kap_ev = kap_normalized_map[key]
                if kap_ev.earnings_date != ir_ev.earnings_date:
                    # KAP / IR conflict
                    in_scope = (
                        _is_in_relevance_scope(kap_ev.earnings_date)
                        or _is_in_relevance_scope(ir_ev.earnings_date)
                    )
                    if in_scope:
                        has_relevant_conflict = True
                        warn_msg = (
                            f"Conflict for {ir_ev.symbol} [{ir_ev.period_start} -> {ir_ev.period_end}]: "
                            f"KAP date '{kap_ev.earnings_date}' retained over Official IR date '{ir_ev.earnings_date}'"
                        )
                        if warn_msg not in warnings:
                            warnings.append(warn_msg)
                    elif emit_out_of_scope_warnings:
                        warn_msg = (
                            f"Historical KAP/IR conflict for {ir_ev.symbol} [{ir_ev.period_start} -> {ir_ev.period_end}] "
                            f"retained KAP date '{kap_ev.earnings_date}' over Official IR date '{ir_ev.earnings_date}' "
                            f"outside current relevance scope"
                        )
                        if warn_msg not in warnings:
                            warnings.append(warn_msg)
                # KAP event is already retained in kap_normalized_map
            else:
                raw_ir_events_to_add.append(ir_ev)

    # 3. Deduplicate IR-only events by identity key
    ir_events_by_identity: Dict[Tuple[str, Optional[str], Optional[str]], List[BistEarningsEvent]] = {}
    for ir_ev in raw_ir_events_to_add:
        key = (ir_ev.symbol, ir_ev.period_start, ir_ev.period_end)
        if key not in ir_events_by_identity:
            ir_events_by_identity[key] = []
        ir_events_by_identity[key].append(ir_ev)

    deduped_ir_events: List[BistEarningsEvent] = []
    for key, ev_list in ir_events_by_identity.items():
        if len(ev_list) == 1:
            deduped_ir_events.append(ev_list[0])
        else:
            dates = {e.earnings_date for e in ev_list}
            if len(dates) == 1:
                # Same date duplicate -> keep single event
                deduped_ir_events.append(ev_list[0])
            else:
                # Conflicting dates for same identity within IR fetch -> exclude identity from current output
                sym = ev_list[0].symbol
                in_scope = any(_is_in_relevance_scope(d) for d in dates)
                if in_scope:
                    has_relevant_conflict = True
                    if sym not in ir_incomplete_symbols:
                        ir_incomplete_symbols.append(sym)
                    warn_msg = (
                        f"Conflicting Official IR dates {sorted(dates)} for {sym} [{key[1]} -> {key[2]}]; "
                        f"current identity excluded because no authoritative publish ordering exists"
                    )
                    if warn_msg not in warnings:
                        warnings.append(warn_msg)
                elif emit_out_of_scope_warnings:
                    # Non-blocking historical conflict outside scope
                    warn_msg = (
                        f"Historical Official IR conflict {sorted(dates)} for {sym} [{key[1]} -> {key[2]}] "
                        f"excluded outside current relevance scope"
                    )
                    if warn_msg not in warnings:
                        warnings.append(warn_msg)
                # Exclude from deduped_ir_events so previous reliable cache is preserved

    # 4. Combine normalized events
    combined_normalized: List[BistEarningsEvent] = list(kap_result.normalized_events) + deduped_ir_events

    # Sort deterministically
    combined_normalized.sort(
        key=lambda ev: (ev.earnings_date, ev.symbol, ev.fiscal_year or 0, ev.fiscal_period or "")
    )

    # 5. Determine overall combined status strictly per decision table
    usable_ir_events_count = len(deduped_ir_events)

    if kap_result.status == "failed":
        if usable_ir_events_count > 0:
            combined_status: EarningsCalendarStatus = "partial"
        else:
            combined_status = "failed"
    elif kap_result.status == "partial":
        combined_status = "partial"
    elif kap_result.status == "success":
        if any_ir_incomplete or has_relevant_conflict:
            combined_status = "partial"
        else:
            combined_status = "success"
    else:
        combined_status = "partial"

    # Deduplicate warnings deterministically
    unique_warnings: List[str] = []
    for w in warnings:
        if w not in unique_warnings:
            unique_warnings.append(w)

    return BistCombinedEarningsResult(
        status=combined_status,
        rolling_events=[],  # To be populated by rolling calendar builder
        normalized_events=combined_normalized,
        kap_fetch_result=kap_result,
        official_ir_results=ir_results,
        ir_incomplete_symbols=sorted(ir_incomplete_symbols),
        warnings=unique_warnings
    )
