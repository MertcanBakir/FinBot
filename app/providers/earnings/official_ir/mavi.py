import logging
import re
from datetime import date
from typing import List, Set, Tuple

from bs4 import BeautifulSoup

from app.models.earnings import (
    BistEarningsEvent,
    FiscalPeriod,
    OfficialIrIssue,
    OfficialIrProviderResult,
)
from app.providers.earnings.official_ir.base import BaseOfficialIrProvider
from app.providers.earnings.official_ir.common import (
    normalize_text,
    parse_date_flexible,
)

logger = logging.getLogger(__name__)

MAVI_URL = "https://www.mavicompany.com/en/investor-relations/financial-calendar"


def get_mavi_special_period_dates(fiscal_year: int, period: FiscalPeriod) -> Tuple[str, str]:
    """Calculate period_start and period_end for Mavi's non-standard fiscal year (Feb 1 - Jan 31).

    Period definitions:
    - Q1: Feb 1, YYYY - Apr 30, YYYY
    - Q2: Feb 1, YYYY - Jul 31, YYYY
    - Q3: Feb 1, YYYY - Oct 31, YYYY
    - FY: Feb 1, YYYY - Jan 31, YYYY+1
    """
    if period == "Q1":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-04-30"
    elif period == "Q2":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-07-31"
    elif period == "Q3":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-10-31"
    elif period == "FY":
        return f"{fiscal_year}-02-01", f"{fiscal_year + 1}-01-31"
    return f"{fiscal_year}-02-01", f"{fiscal_year}-04-30"


class MaviIrProvider(BaseOfficialIrProvider):
    symbol = "MAVI"
    company_name = "MAVİ GİYİM SANAYİ VE TİCARET A.Ş."
    source_provider = "MAVI Investor Relations"
    url = MAVI_URL

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from MAVI IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        
        # 1. Primary data source in raw server-rendered HTML: #event-list table tr
        rows = []
        event_list = soup.select_one("#event-list")
        if event_list:
            table = event_list.find("table")
            if table:
                rows = table.find_all("tr")

        # 2. Controlled fallback: #upcoming-events or #past-events if #event-list not present
        if not rows:
            for tbl_id in ["upcoming-events", "past-events"]:
                tbl = soup.find(id=tbl_id)
                if tbl:
                    rows.extend(tbl.find_all("tr"))

        if not rows:
            msg = "Recognized events table (#event-list / #upcoming-events) not found on MAVI IR page"
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
        informational_warnings: List[str] = []
        validation_warnings: List[str] = []
        rows_seen = 0
        accepted_count = 0
        rejected_count = 0
        seen_events: Set[Tuple[str, str]] = set()

        for row in rows:
            tds = row.find_all("td")
            if not tds or len(tds) < 2:
                continue

            rows_seen += 1
            # First visible td is authoritative date
            date_raw = tds[0].get_text(strip=True)
            # Second td is title
            title_raw = tds[1].get_text(strip=True)
            title = normalize_text(title_raw)

            row_id = row.get("id") or f"{title}:{date_raw}"
            if row_id in seen_events:
                continue
            seen_events.add(row_id)

            # Reject non-earnings results (webcasts, general assemblies, investor conferences)
            if any(k in title.lower() for k in ["webcast", "general assembly", "conference", "silent period"]):
                rejected_count += 1
                continue

            # Patterns:
            # - (Q1|Q2|Q3|Q4|H1|9M|FY) (YYYY) Financial Results
            # - (YYYY) (Q1|Q2|Q3|Q4|H1|9M|FY) Financial Results
            m1 = re.search(r"\b(Q1|Q2|Q3|Q4|H1|9M|FY)\s+(\d{4})\s+Financial\s+Results\b", title, re.IGNORECASE)
            m2 = re.search(r"\b(\d{4})\s+(Q1|Q2|Q3|Q4|H1|9M|FY)\s+Financial\s+Results\b", title, re.IGNORECASE)

            if m1:
                period_str = m1.group(1).upper()
                fiscal_year = int(m1.group(2))
            elif m2:
                fiscal_year = int(m2.group(1))
                period_str = m2.group(2).upper()
            else:
                rejected_count += 1
                continue

            period_map = {
                "Q1": "Q1",
                "Q2": "Q2",
                "H1": "Q2",
                "Q3": "Q3",
                "9M": "Q3",
                "Q4": "FY",
                "FY": "FY"
            }
            fiscal_period: FiscalPeriod = period_map.get(period_str, "Q1")  # type: ignore

            visible_earnings_date = parse_date_flexible(date_raw)
            if not visible_earnings_date:
                warn_msg = f"Could not parse valid date from '{date_raw}' in MAVI item: {title}"
                validation_warnings.append(warn_msg)
                issues.append(OfficialIrIssue(
                    message=warn_msg,
                    kind="row_validation",
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    is_global=False
                ))
                rejected_count += 1
                continue

            # MAVI SPECIAL FISCAL PERIOD: Feb 1 - Jan 31
            period_start, period_end = get_mavi_special_period_dates(fiscal_year, fiscal_period)

            earnings_date = visible_earnings_date
            # Check if visible date is semantically impossible (before period_end)
            if visible_earnings_date < period_end:
                # Strict Metadata Fallback for FY only
                fallback_applied = False
                if fiscal_period == "FY":
                    meta_end = row.get("data-event-end-date", "")
                    if meta_end:
                        meta_date_str = meta_end.strip().split()[0] if meta_end.strip() else ""
                        meta_date = parse_date_flexible(meta_date_str)
                        if meta_date and meta_date >= period_end:
                            try:
                                vis_d = date.fromisoformat(visible_earnings_date)
                                meta_d = date.fromisoformat(meta_date)
                                if (
                                    meta_d.month == vis_d.month
                                    and meta_d.day == vis_d.day
                                    and meta_d.year == vis_d.year + 1
                                    and meta_d.year == fiscal_year + 1
                                ):
                                    earnings_date = meta_date
                                    fallback_applied = True
                                    info_msg = (
                                        f"Corrected MAVI FY {fiscal_year} visible date {visible_earnings_date} to {meta_date} using validated row metadata"
                                    )
                                    informational_warnings.append(info_msg)
                                    issues.append(OfficialIrIssue(
                                        message=info_msg,
                                        kind="informational",
                                        earnings_date=meta_date,
                                        fiscal_year=fiscal_year,
                                        fiscal_period=fiscal_period,
                                        is_global=False
                                    ))
                            except Exception:
                                pass

                if not fallback_applied:
                    warn_msg = f"Earnings date {visible_earnings_date} before period_end {period_end} for MAVI item: {title}"
                    validation_warnings.append(warn_msg)
                    issues.append(OfficialIrIssue(
                        message=warn_msg,
                        kind="row_validation",
                        earnings_date=visible_earnings_date,
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        is_global=False
                    ))
                    rejected_count += 1
                    continue

            event_id = row.get("id") or f"mavi:{fiscal_year}:{fiscal_period}"
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
                source_event_id=str(event_id),
                disclosure_index=None,
                source_url=self.url
            )
            events.append(event)
            accepted_count += 1

        all_warnings = informational_warnings + validation_warnings
        if accepted_count == 0:
            fallback_warn = all_warnings or ["No valid financial results events parsed from MAVI IR page"]
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

        status = "partial" if validation_warnings else "success"

        return OfficialIrProviderResult(
            symbol=self.symbol,
            source_provider=self.source_provider,
            status=status,
            events=events,
            rows_seen=rows_seen,
            accepted_row_count=accepted_count,
            rejected_row_count=rejected_count,
            parser_failed=False,
            warnings=all_warnings,
            issues=issues
        )


# Backward compatibility alias
MaviOfficialIrProvider = MaviIrProvider
