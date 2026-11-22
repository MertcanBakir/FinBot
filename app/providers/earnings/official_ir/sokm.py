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


class SokmIrProvider(BaseOfficialIrProvider):
    symbol = "SOKM"
    company_name = "ŞOK MARKETLER TİCARET A.Ş."
    source_provider = "SOKM Investor Relations"
    url = "https://sokmarketyatirimciiliskileri.com/tr/finansal-takvim"

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from SOKM IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find(id="itemstable")
        
        # Strict: Do NOT fallback to scanning all .item-line across the entire page
        if not table:
            msg = "#itemstable not found on SOKM IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        items = table.find_all("div", class_="item-line") or table.find_all("tr")
        if not items:
            msg = "No .item-line elements found inside #itemstable"
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

        for item in items:
            title_el = item.select_one(".item-title") or item.find("th") or item.find("td")
            tds = item.find_all("td")
            date_el = item.select_one(".item-date") or (tds[-1] if tds else None)
            if not title_el or not date_el:
                continue

            rows_seen += 1
            title_raw = title_el.get_text(strip=True)
            date_raw = date_el.get_text(strip=True)
            title = normalize_text(title_raw)

            # CRITICAL RULE: Reject regulatory deadline / Son Tarih rows
            if any(k in title.lower() for k in ["son tarih", "deadline", "sessiz dönem", "webcast", "telekonferans"]):
                rejected_count += 1
                continue

            # Match patterns: (YYYY) (1|2|3|4)\. Çeyrek Finansal Sonuçları OR (YYYY) Yıllık Finansal Sonuçları
            m_q = re.search(r"(\d{4})\s*(\d)\.\s*[Çç]eyrek\s+Finansal\s+Sonu[çc]lar[ıi]", title, re.IGNORECASE)
            m_fy = re.search(r"(\d{4})\s*Y[ıi]ll[ıi]k\s+Finansal\s+Sonu[çc]lar[ıi]", title, re.IGNORECASE)

            if m_q:
                fiscal_year = int(m_q.group(1))
                q_num = int(m_q.group(2))
                period_map = {1: "Q1", 2: "Q2", 3: "Q3", 4: "FY"}
                fiscal_period: FiscalPeriod = period_map.get(q_num, "Q1")  # type: ignore
            elif m_fy:
                fiscal_year = int(m_fy.group(1))
                fiscal_period = "FY"
            else:
                rejected_count += 1
                continue

            earnings_date = parse_date_flexible(date_raw)
            if not earnings_date:
                warn_msg = f"Could not parse valid date from '{date_raw}' in SOKM item: {title}"
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
                warn_msg = f"Earnings date {earnings_date} before period_end {period_end} in SOKM item: {title}"
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
                source_event_id=f"sokm:{fiscal_year}:{fiscal_period}",
                disclosure_index=None,
                source_url=self.url
            )
            events.append(event)
            accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial results events parsed from SOKM IR page"]
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
