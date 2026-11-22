import logging
import re
from typing import List
from urllib.parse import urljoin

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


class GaranIrProvider(BaseOfficialIrProvider):
    symbol = "GARAN"
    company_name = "TÜRKİYE GARANTİ BANKASI A.Ş."
    source_provider = "GARAN Investor Relations"
    url = "https://www.garantibbvainvestorrelations.com/tr/yatirimci-bilgileri/yatirimciTakvimi/Yatirimci-Takvimi/382/0/0"

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from GARAN IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        calendar_container = soup.select_one(".containerCalendar")
        
        # Strict: Do NOT fallback to scanning all <a> tags across the entire page
        if not calendar_container:
            msg = ".containerCalendar not found on GARAN IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        event_links = calendar_container.find_all("a")
        if not event_links:
            msg = "No event links found inside .containerCalendar"
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

        for a_tag in event_links:
            rows_seen += 1
            h3 = a_tag.find("h3")
            p = a_tag.find("p")
            if not h3 or not p:
                rejected_count += 1
                continue

            date_raw = h3.get_text(strip=True)
            title_raw = p.get_text(strip=True)
            title = normalize_text(title_raw)

            # Skip webcasts, conferences, etc.
            if any(k in title.lower() for k in ["webcast", "telekonferans", "konferans"]):
                rejected_count += 1
                continue

            # Patterns:
            # - Garanti BBVA (1Ç|2Ç|3Ç|4Ç|1YY|2YY|3A|6A|9A|12A)(\d{2}) Finansal Sonuçları
            # - Garanti BBVA (\d{4}) (1Ç|2Ç|3Ç|4Ç|1YY|2YY|3A|6A|9A|12A) Finansal Sonuçları
            m1 = re.search(r"Garanti\s+BBVA\s*(1[ÇçCc]|2[ÇçCc]|3[ÇçCc]|4[ÇçCc]|1YY|2YY|3A|6A|9A|12A)\s*(\d{2})", title, re.IGNORECASE)
            m2 = re.search(r"Garanti\s+BBVA\s*(\d{4})\s*(1[ÇçCc]|2[ÇçCc]|3[ÇçCc]|4[ÇçCc]|1YY|2YY|3A|6A|9A|12A)", title, re.IGNORECASE)

            if m1:
                period_raw = m1.group(1).upper()
                year_2d = int(m1.group(2))
                fiscal_year = 2000 + year_2d
            elif m2:
                fiscal_year = int(m2.group(1))
                period_raw = m2.group(2).upper()
            else:
                rejected_count += 1
                continue

            period_map = {
                "1Ç": "Q1",
                "1C": "Q1",
                "3A": "Q1",
                "1YY": "Q2",
                "2Ç": "Q2",
                "2C": "Q2",
                "6A": "Q2",
                "3Ç": "Q3",
                "3C": "Q3",
                "9A": "Q3",
                "2YY": "FY",
                "4Ç": "FY",
                "4C": "FY",
                "12A": "FY"
            }
            fiscal_period: FiscalPeriod = period_map.get(period_raw.replace("ç", "Ç").replace("c", "C"), "Q1")  # type: ignore

            earnings_date = parse_date_flexible(date_raw)
            if not earnings_date:
                warn_msg = f"Could not parse valid date from '{date_raw}' in GARAN item: {title}"
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
                warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for GARAN item: {title}"
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

            href = a_tag.get("href", "")
            full_url = urljoin(self.url, href) if href else self.url

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
                source_event_id=f"garan:{fiscal_year}:{fiscal_period}",
                disclosure_index=None,
                source_url=full_url
            )
            events.append(event)
            accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial results events found in .containerCalendar"]
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
