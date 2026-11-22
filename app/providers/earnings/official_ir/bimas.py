import logging
import re
from typing import List

from bs4 import BeautifulSoup

from app.models.earnings import (
    BistEarningsEvent,
    FiscalPeriod,
    OfficialIrIssue,
    OfficialIrProviderResult,
)
from app.providers.earnings.official_ir.base import BaseOfficialIrProvider
from app.providers.earnings.official_ir.common import (
    get_standard_period_dates,
    normalize_text,
    parse_date_flexible,
)

logger = logging.getLogger(__name__)


class BimasIrProvider(BaseOfficialIrProvider):
    symbol = "BIMAS"
    company_name = "BİM BİRLEŞİK MAĞAZALAR A.Ş."
    source_provider = "BIMAS Investor Relations"
    url = "https://www.bim.com.tr/Categories/640/finansal-takvim.aspx"

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from BIMAS IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="table1") or soup.select_one("table#table1")

        # Strict: Do NOT fallback to generic soup.find("table")
        if not table:
            msg = "table#table1 not found on BIMAS IR page"
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

        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue

            rows_seen += 1
            title_raw = th.get_text(strip=True)
            date_raw = td.get_text(strip=True)
            title = normalize_text(title_raw)

            # Skip silent periods and non-announcement rows
            if any(k in title.lower() for k in ["sessiz dönem", "silent period", "webcast", "telekonferans"]):
                rejected_count += 1
                continue

            # Pattern: (YYYY) (1|2|3|4)\. Çeyrek Finansal Sonuçlarının Açıklanması
            m = re.search(r"(\d{4})\s*(\d)\.\s*[Çç]eyrek\s+Finansal\s+Sonu[çc]lar[ıi]n[ıi]n\s+A[çc][ıi]klanmas[ıi]", title, re.IGNORECASE)
            if not m:
                rejected_count += 1
                continue

            fiscal_year = int(m.group(1))
            q_num = int(m.group(2))
            if q_num not in (1, 2, 3, 4):
                warn_msg = f"Invalid quarter number {q_num} in BIMAS row: {title}"
                warnings.append(warn_msg)
                issues.append(OfficialIrIssue(
                    message=warn_msg,
                    kind="row_validation",
                    fiscal_year=fiscal_year,
                    is_global=False
                ))
                rejected_count += 1
                continue

            period_map = {1: "Q1", 2: "Q2", 3: "Q3", 4: "FY"}
            fiscal_period: FiscalPeriod = period_map[q_num]  # type: ignore

            earnings_date = parse_date_flexible(date_raw)
            if not earnings_date:
                warn_msg = f"Could not parse valid date from '{date_raw}' in BIMAS row: {title}"
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
                warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for BIMAS row: {title}"
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
                source_event_id=f"bimas:{fiscal_year}:{fiscal_period}",
                disclosure_index=None,
                source_url=self.url
            )
            events.append(event)
            accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial results events found in table#table1"]
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

        status = "partial" if warnings else "success"

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
