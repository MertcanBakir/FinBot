import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from app.models.earnings import (
    BistEarningsEvent,
    EarningsCalendarStatus,
    FiscalPeriod,
    OfficialIrIssue,
    OfficialIrProviderResult,
)
from app.providers.earnings.official_ir.base import (
    DEFAULT_HEADERS,
    RETRIABLE_EXCEPTIONS,
    BaseOfficialIrProvider,
)
from app.providers.earnings.official_ir.common import (
    get_standard_period_dates,
    normalize_text,
    parse_date_flexible,
)

logger = logging.getLogger(__name__)

PRIMARY_URL = "https://www.halkbank.com.tr/tr/yatirimci-iliskileri/finansal-bilgiler/takvim/finansal-verilerin-aciklanmasi"
SECONDARY_URL = "https://www.halkbank.com.tr/tr/yatirimci-iliskileri/finansal-bilgiler/finansal-bilgiler/mali-yapi-sunumlari"


class HalkbIrProvider(BaseOfficialIrProvider):
    symbol = "HALKB"
    company_name = "TÜRKİYE HALK BANKASI A.Ş."
    source_provider = "HALKB Investor Relations"
    primary_url = PRIMARY_URL
    secondary_url = SECONDARY_URL
    url = PRIMARY_URL  # Backward compatibility for BaseOfficialIrProvider

    def parse_primary_html(self, html: str) -> OfficialIrProviderResult:
        """Parse primary financial calendar page for HALKB."""
        if not html or not html.strip():
            msg = "Empty HTML content from HALKB IR primary page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select(".cmp-accordion__panel table")

        # Strict: Do NOT fallback to scanning all <table> tags across the entire page
        if not tables:
            msg = ".cmp-accordion__panel table not found on HALKB IR primary page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        events: List[BistEarningsEvent] = []
        issues: List[OfficialIrIssue] = []
        warnings: List[str] = []
        rows_seen = 0
        accepted_count = 0
        rejected_count = 0

        for table in tables:
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds or len(tds) < 2:
                    continue

                rows_seen += 1
                date_raw = tds[0].get_text(strip=True)
                title_raw = tds[1].get_text(strip=True)
                title = normalize_text(title_raw)

                # Skip teleconferences, webcasts, etc.
                if any(k in title.lower() for k in ["telekonferans", "webcast", "konferans", "sessiz dönem"]):
                    rejected_count += 1
                    continue

                m_q = re.search(r"(\d{4})\s*Y[ıi]l[ıi]\s*(\d)\.\s*[Çç]eyrek\s+Finansal\s+(?:Sonu[çc]lar|Veriler)", title, re.IGNORECASE)
                m_fy1 = re.search(r"(\d{4})\s*Y[ıi]l[ıi]\s*Y[ıi]l\s*Sonu\s+Finansal\s+(?:Sonu[çc]lar|Veriler)", title, re.IGNORECASE)
                m_fy2 = re.search(r"(\d{4})\s*Y[ıi]l[ıi]\s*Finansal\s+(?:Sonu[çc]lar|Veriler)[ıi]n[ıi]n\s+A[çc][ıi]klanmas[ıi]", title, re.IGNORECASE)

                if m_q:
                    fiscal_year = int(m_q.group(1))
                    q_num = int(m_q.group(2))
                    period_map = {1: "Q1", 2: "Q2", 3: "Q3", 4: "FY"}
                    fiscal_period: FiscalPeriod = period_map.get(q_num, "Q1")  # type: ignore
                elif m_fy1:
                    fiscal_year = int(m_fy1.group(1))
                    fiscal_period = "FY"
                elif m_fy2:
                    fiscal_year = int(m_fy2.group(1))
                    fiscal_period = "FY"
                else:
                    rejected_count += 1
                    continue

                earnings_date = parse_date_flexible(date_raw)
                if not earnings_date:
                    warn_msg = f"Could not parse valid date from '{date_raw}' in HALKB primary row: {title}"
                    warnings.append(warn_msg)
                    issues.append(OfficialIrIssue(
                        message=warn_msg,
                        kind="row_validation",
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        is_global=False
                    ))
                    rejected_count += 1
                    continue

                period_start, period_end = get_standard_period_dates(fiscal_year, fiscal_period)

                if earnings_date < period_end:
                    warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for HALKB primary row: {title}"
                    warnings.append(warn_msg)
                    issues.append(OfficialIrIssue(
                        message=warn_msg,
                        kind="row_validation",
                        earnings_date=earnings_date,
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        is_global=False
                    ))
                    rejected_count += 1
                    continue

                event = BistEarningsEvent(
                    symbol=self.symbol,
                    company_name=self.company_name,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    earnings_date=earnings_date,
                    period_start=period_start,
                    period_end=period_end,
                    publish_date=None,
                    date_type="company_announced",
                    source="Official IR",
                    source_provider=self.source_provider,
                    source_type="official_financial_calendar",
                    source_event_id=f"halkb:{fiscal_year}:{fiscal_period}",
                    disclosure_index=None,
                    source_url=self.primary_url
                )
                events.append(event)
                accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial results events found in primary .cmp-accordion__panel table"]
            if not issues:
                for w in fallback_warn:
                    issues.append(OfficialIrIssue(message=w, kind="schema_failure", is_global=True))
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                rows_seen=rows_seen,
                accepted_row_count=0,
                rejected_row_count=rejected_count,
                warnings=fallback_warn,
                issues=issues
            )

        status: EarningsCalendarStatus = "partial" if warnings else "success"

        return OfficialIrProviderResult(
            symbol=self.symbol,
            source_provider=self.source_provider,
            status=status,
            events=events,
            rows_seen=rows_seen,
            accepted_row_count=accepted_count,
            rejected_row_count=rejected_count,
            parser_failed=False,
            warnings=warnings,
            issues=issues
        )

    def parse_secondary_html(self, html: str) -> OfficialIrProviderResult:
        """Parse secondary/fallback financial presentations page ('Mali Yapı Sunumları') for HALKB."""
        if not html or not html.strip():
            msg = "Empty HTML content from HALKB IR secondary page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select(".cmp-accordion__panel table, .financial-presentations table, table.cmp-table")
        if not tables:
            tables = soup.find_all("table")

        if not tables:
            msg = "Recognized table not found on HALKB IR secondary page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        events: List[BistEarningsEvent] = []
        issues: List[OfficialIrIssue] = []
        warnings: List[str] = []
        rows_seen = 0
        accepted_count = 0
        rejected_count = 0

        for table in tables:
            # Check for matrix header: columns corresponding to Q1, Q2, Q3, FY (or 1. ÇEYREK, 2. ÇEYREK, etc.)
            header_col_map: Dict[int, FiscalPeriod] = {}
            header_rows = table.find_all("tr")

            for tr in header_rows:
                ths = tr.find_all(["th", "td"])
                for col_idx, th in enumerate(ths):
                    txt = normalize_text(th.get_text(strip=True)).upper()
                    if "1." in txt and ("ÇEYREK" in txt or "CEYREK" in txt):
                        header_col_map[col_idx] = "Q1"
                    elif "2." in txt and ("ÇEYREK" in txt or "CEYREK" in txt):
                        header_col_map[col_idx] = "Q2"
                    elif "3." in txt and ("ÇEYREK" in txt or "CEYREK" in txt):
                        header_col_map[col_idx] = "Q3"
                    elif ("4." in txt and ("ÇEYREK" in txt or "CEYREK" in txt)) or "YIL SONU" in txt or "FY" in txt:
                        header_col_map[col_idx] = "FY"

                if header_col_map:
                    break

            for tr in header_rows:
                tds = tr.find_all("td")
                if not tds or len(tds) < 2:
                    continue

                rows_seen += 1

                # If matrix header was found, first cell is usually Year
                if header_col_map:
                    row_txt = tds[0].get_text(strip=True)
                    m_yr = re.search(r"\b(20\d{2})\b", row_txt)
                    if not m_yr:
                        rejected_count += 1
                        continue

                    fiscal_year = int(m_yr.group(1))

                    for col_idx, fiscal_period in header_col_map.items():
                        if col_idx >= len(tds):
                            continue

                        cell = tds[col_idx]
                        cell_raw = cell.get_text(strip=True)
                        cell_norm = normalize_text(cell_raw)

                        # Skip empty / dash / placeholder cells
                        if not cell_norm or cell_norm in ("-", "--", "YOK", "N/A", "YIL SONU", "1. ÇEYREK", "2. ÇEYREK", "3. ÇEYREK", "4. ÇEYREK"):
                            continue

                        # Skip teleconferences / webcasts
                        if any(k in cell_norm.lower() for k in ["telekonferans", "webcast", "sessiz dönem"]):
                            rejected_count += 1
                            continue

                        earnings_date = parse_date_flexible(cell_norm)
                        if not earnings_date:
                            warn_msg = f"Could not parse valid date from '{cell_raw}' in HALKB secondary table for {fiscal_year} {fiscal_period}"
                            warnings.append(warn_msg)
                            issues.append(OfficialIrIssue(
                                message=warn_msg,
                                kind="row_validation",
                                fiscal_year=fiscal_year,
                                fiscal_period=fiscal_period,
                                is_global=False
                            ))
                            rejected_count += 1
                            continue

                        period_start, period_end = get_standard_period_dates(fiscal_year, fiscal_period)

                        if earnings_date < period_end:
                            warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for HALKB secondary {fiscal_year} {fiscal_period}"
                            warnings.append(warn_msg)
                            issues.append(OfficialIrIssue(
                                message=warn_msg,
                                kind="row_validation",
                                earnings_date=earnings_date,
                                fiscal_year=fiscal_year,
                                fiscal_period=fiscal_period,
                                is_global=False
                            ))
                            rejected_count += 1
                            continue

                        event = BistEarningsEvent(
                            symbol=self.symbol,
                            company_name=self.company_name,
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            earnings_date=earnings_date,
                            period_start=period_start,
                            period_end=period_end,
                            publish_date=None,
                            date_type="company_announced",
                            source="Official IR",
                            source_provider=self.source_provider,
                            source_type="official_financial_calendar",
                            source_event_id=f"halkb:{fiscal_year}:{fiscal_period}",
                            disclosure_index=None,
                            source_url=self.secondary_url
                        )
                        events.append(event)
                        accepted_count += 1
                else:
                    # Alternative row-based structure (e.g. <tr><td>2026</td><td>2. Çeyrek</td><td>7 Ağustos 2026</td></tr>)
                    full_row_text = normalize_text(" ".join(td.get_text(strip=True) for td in tds))
                    m_yr = re.search(r"\b(20\d{2})\b", full_row_text)
                    m_q = re.search(r"(\d)\.\s*[Çç]eyrek", full_row_text, re.IGNORECASE)
                    m_fy = re.search(r"(?:Y[ıi]l\s*Sonu|4\.\s*[Çç]eyrek)", full_row_text, re.IGNORECASE)

                    if not m_yr or (not m_q and not m_fy):
                        rejected_count += 1
                        continue

                    fiscal_year = int(m_yr.group(1))
                    if m_q:
                        q_num = int(m_q.group(1))
                        period_map = {1: "Q1", 2: "Q2", 3: "Q3", 4: "FY"}
                        fiscal_period = period_map.get(q_num, "Q1")  # type: ignore
                    else:
                        fiscal_period = "FY"

                    # Find date cell among tds
                    parsed_d = None
                    for td in tds:
                        txt = td.get_text(strip=True)
                        d = parse_date_flexible(txt)
                        if d:
                            parsed_d = d
                            break

                    if not parsed_d:
                        warn_msg = f"Could not parse valid date in HALKB secondary row: {full_row_text}"
                        warnings.append(warn_msg)
                        issues.append(OfficialIrIssue(
                            message=warn_msg,
                            kind="row_validation",
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            is_global=False
                        ))
                        rejected_count += 1
                        continue

                    period_start, period_end = get_standard_period_dates(fiscal_year, fiscal_period)
                    if parsed_d < period_end:
                        warn_msg = f"Earnings date {parsed_d} before period_end {period_end} for HALKB secondary {fiscal_year} {fiscal_period}"
                        warnings.append(warn_msg)
                        issues.append(OfficialIrIssue(
                            message=warn_msg,
                            kind="row_validation",
                            earnings_date=parsed_d,
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            is_global=False
                        ))
                        rejected_count += 1
                        continue

                    event = BistEarningsEvent(
                        symbol=self.symbol,
                        company_name=self.company_name,
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        earnings_date=parsed_d,
                        period_start=period_start,
                        period_end=period_end,
                        publish_date=None,
                        date_type="company_announced",
                        source="Official IR",
                        source_provider=self.source_provider,
                        source_type="official_financial_calendar",
                        source_event_id=f"halkb:{fiscal_year}:{fiscal_period}",
                        disclosure_index=None,
                        source_url=self.secondary_url
                    )
                    events.append(event)
                    accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial presentation events found on HALKB secondary page"]
            if not issues:
                for w in fallback_warn:
                    issues.append(OfficialIrIssue(message=w, kind="schema_failure", is_global=True))
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                rows_seen=rows_seen,
                accepted_row_count=0,
                rejected_row_count=rejected_count,
                warnings=fallback_warn,
                issues=issues
            )

        status: EarningsCalendarStatus = "partial" if warnings else "success"

        return OfficialIrProviderResult(
            symbol=self.symbol,
            source_provider=self.source_provider,
            status=status,
            events=events,
            rows_seen=rows_seen,
            accepted_row_count=accepted_count,
            rejected_row_count=rejected_count,
            parser_failed=False,
            warnings=warnings,
            issues=issues
        )

    def merge_primary_and_fallback(
        self,
        primary_res: OfficialIrProviderResult,
        secondary_res: OfficialIrProviderResult
    ) -> OfficialIrProviderResult:
        """Merge primary financial calendar results with secondary fallback presentations identity-wise."""
        merged_warnings: List[str] = []
        merged_issues: List[OfficialIrIssue] = list(primary_res.issues) + list(secondary_res.issues)

        # Deduplicate warnings
        for w in primary_res.warnings + secondary_res.warnings:
            if w not in merged_warnings:
                merged_warnings.append(w)

        total_rows_seen = primary_res.rows_seen + secondary_res.rows_seen
        total_rejected = primary_res.rejected_row_count + secondary_res.rejected_row_count
        total_retries = primary_res.retry_count + secondary_res.retry_count

        # Case 1: Both failed
        if primary_res.status == "failed" and secondary_res.status == "failed":
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                events=[],
                rows_seen=total_rows_seen,
                accepted_row_count=0,
                rejected_row_count=total_rejected,
                retry_count=total_retries,
                parser_failed=(primary_res.parser_failed and secondary_res.parser_failed),
                warnings=merged_warnings,
                issues=merged_issues
            )

        # Merge events by identity key: (symbol, fiscal_year, fiscal_period)
        primary_by_id: Dict[Tuple[str, int, str], BistEarningsEvent] = {
            (ev.symbol, ev.fiscal_year, ev.fiscal_period): ev for ev in primary_res.events
        }
        secondary_by_id: Dict[Tuple[str, int, str], BistEarningsEvent] = {
            (ev.symbol, ev.fiscal_year, ev.fiscal_period): ev for ev in secondary_res.events
        }

        final_events: List[BistEarningsEvent] = []
        has_conflict = False

        # 1. Add all primary events (authoritative)
        for key, p_ev in primary_by_id.items():
            final_events.append(p_ev)
            if key in secondary_by_id:
                s_ev = secondary_by_id[key]
                if s_ev.earnings_date != p_ev.earnings_date:
                    has_conflict = True
                    conf_warn = (
                        f"Conflict for {p_ev.symbol} {p_ev.fiscal_period} {p_ev.fiscal_year}: "
                        f"primary date '{p_ev.earnings_date}' retained over fallback date '{s_ev.earnings_date}'"
                    )
                    if conf_warn not in merged_warnings:
                        merged_warnings.append(conf_warn)
                        merged_issues.append(OfficialIrIssue(
                            message=conf_warn,
                            kind="conflict",
                            earnings_date=p_ev.earnings_date,
                            fiscal_year=p_ev.fiscal_year,
                            fiscal_period=p_ev.fiscal_period,
                            is_global=False
                        ))

        # 2. Add fallback events for identities NOT present in primary
        for key, s_ev in secondary_by_id.items():
            if key not in primary_by_id:
                final_events.append(s_ev)

        # Sort events deterministically
        final_events.sort(key=lambda e: (e.earnings_date, e.fiscal_year, e.fiscal_period))

        # Status determination
        if primary_res.status == "failed" or secondary_res.status == "failed":
            final_status: EarningsCalendarStatus = "partial"
        elif primary_res.status == "partial" or secondary_res.status == "partial" or has_conflict or len(merged_warnings) > 0:
            final_status = "partial"
        else:
            final_status = "success"

        return OfficialIrProviderResult(
            symbol=self.symbol,
            source_provider=self.source_provider,
            status=final_status,
            events=final_events,
            rows_seen=total_rows_seen,
            accepted_row_count=len(final_events),
            rejected_row_count=total_rejected,
            retry_count=total_retries,
            parser_failed=False,
            warnings=merged_warnings,
            issues=merged_issues
        )

    async def _fetch_single_url(
        self,
        client: httpx.AsyncClient,
        target_url: str,
        parser_fn
    ) -> OfficialIrProviderResult:
        """Fetch a single URL with exponential retry/backoff and parse it with the given parser function."""
        retry_count = 0
        last_error_msg = ""

        for attempt in range(1, self.max_retries + 1):
            try:
                res = await client.get(target_url)
                if res.status_code == 429 or (500 <= res.status_code < 600):
                    last_error_msg = f"HTTP {res.status_code} fetching from {target_url}"
                    if attempt < self.max_retries:
                        retry_count += 1
                        delay = self.base_backoff_seconds * (2 ** (attempt - 1))
                        logger.warning(f"[{self.symbol}] Retry attempt {retry_count} after HTTP {res.status_code} at {target_url} (waiting {delay}s)")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        msg = f"HTTP {res.status_code} fetching from {target_url} after {retry_count} retries"
                        return OfficialIrProviderResult(
                            symbol=self.symbol,
                            source_provider=self.source_provider,
                            status="failed",
                            events=[],
                            retry_count=retry_count,
                            warnings=[msg],
                            issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                        )

                if res.status_code != 200:
                    logger.warning(f"[{self.symbol}] Non-retriable HTTP {res.status_code} at {target_url}")
                    msg = f"HTTP {res.status_code} fetching from {target_url}"
                    return OfficialIrProviderResult(
                        symbol=self.symbol,
                        source_provider=self.source_provider,
                        status="failed",
                        events=[],
                        retry_count=retry_count,
                        warnings=[msg],
                        issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                    )

                parsed_result = parser_fn(res.text)
                parsed_result.retry_count = retry_count
                return parsed_result

            except RETRIABLE_EXCEPTIONS as exc:
                last_error_msg = str(exc)
                if attempt < self.max_retries:
                    retry_count += 1
                    delay = self.base_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(f"[{self.symbol}] Retry attempt {retry_count} after error {exc} at {target_url} (waiting {delay}s)")
                    await asyncio.sleep(delay)
                    continue
                else:
                    msg = f"Network error fetching from {target_url} after {retry_count} retries: {exc}"
                    return OfficialIrProviderResult(
                        symbol=self.symbol,
                        source_provider=self.source_provider,
                        status="failed",
                        events=[],
                        retry_count=retry_count,
                        warnings=[msg],
                        issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                    )
            except Exception as unhandled:
                msg = f"Unhandled exception fetching from {target_url}: {str(unhandled)}"
                return OfficialIrProviderResult(
                    symbol=self.symbol,
                    source_provider=self.source_provider,
                    status="failed",
                    events=[],
                    retry_count=retry_count,
                    warnings=[msg],
                    issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                )

        return OfficialIrProviderResult(
            symbol=self.symbol,
            source_provider=self.source_provider,
            status="failed",
            events=[],
            retry_count=retry_count,
            warnings=[f"Fetch failed after {retry_count} retries from {target_url}: {last_error_msg}"]
        )

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        """Backward compatible entrypoint mapping to parse_primary_html."""
        return self.parse_primary_html(html)

    async def fetch_calendar(self, client: Optional[httpx.AsyncClient] = None) -> OfficialIrProviderResult:
        """Fetch both primary financial calendar and secondary presentation pages and merge them identity-wise."""
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS)
            close_client = True

        try:
            primary_res = await self._fetch_single_url(client, self.primary_url, self.parse_primary_html)
            secondary_res = await self._fetch_single_url(client, self.secondary_url, self.parse_secondary_html)
            return self.merge_primary_and_fallback(primary_res, secondary_res)
        finally:
            if close_client:
                await client.aclose()
