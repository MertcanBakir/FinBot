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


class ThyaoIrProvider(BaseOfficialIrProvider):
    symbol = "THYAO"
    company_name = "TÜRK HAVA YOLLARI A.O."
    source_provider = "THYAO Investor Relations"
    url = "https://investor.turkishairlines.com/en/announcements/calendar"

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from THYAO IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        report_links = soup.select("a.report-link")

        # Strict: Do NOT fallback to scanning all <a> tags across the entire page
        if not report_links:
            msg = "a.report-link not found on THYAO IR page"
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

        for a_tag in report_links:
            rows_seen += 1
            date_span = a_tag.select_one(".date") or a_tag.find("span")
            p_desc = a_tag.select_one(".report-aciklama") or a_tag.find("p")
            if not date_span or not p_desc:
                rejected_count += 1
                continue

            date_raw = date_span.get_text(strip=True)
            title_raw = p_desc.get_text(strip=True)
            title = normalize_text(title_raw)

            # Skip investor calls, conferences, etc.
            if any(k in title.lower() for k in ["earnings call", "teleconference", "webcast", "silent period", "conference"]):
                rejected_count += 1
                continue

            # Patterns:
            # - (1Q|2Q|3Q|4Q|FY)'(\d{2}) Financial Results
            # - (1Q|2Q|3Q|4Q|FY) (\d{4}) Financial Results
            # - (\d{4}) (1Q|2Q|3Q|4Q|FY) Financial Results
            m1 = re.search(r"\b(1Q|2Q|3Q|4Q|FY)'(\d{2})\s+Financial\s+Results\b", title, re.IGNORECASE)
            m2 = re.search(r"\b(1Q|2Q|3Q|4Q|FY)\s+(\d{4})\s+Financial\s+Results\b", title, re.IGNORECASE)
            m3 = re.search(r"\b(\d{4})\s+(1Q|2Q|3Q|4Q|FY)\s+Financial\s+Results\b", title, re.IGNORECASE)

            if m1:
                period_str = m1.group(1).upper()
                year_2d = int(m1.group(2))
                fiscal_year = 2000 + year_2d
            elif m2:
                period_str = m2.group(1).upper()
                fiscal_year = int(m2.group(2))
            elif m3:
                fiscal_year = int(m3.group(1))
                period_str = m3.group(2).upper()
            else:
                rejected_count += 1
                continue

            period_map = {"1Q": "Q1", "2Q": "Q2", "3Q": "Q3", "4Q": "FY", "FY": "FY"}
            fiscal_period: FiscalPeriod = period_map.get(period_str, "Q1")  # type: ignore

            earnings_date = parse_date_flexible(date_raw)
            if not earnings_date:
                warn_msg = f"Could not parse valid date from '{date_raw}' in THYAO item: {title}"
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
                warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for THYAO item: {title}"
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
                source_event_id=f"thyao:{fiscal_year}:{fiscal_period}",
                disclosure_index=None,
                source_url=full_url
            )
            events.append(event)
            accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid financial results events found in a.report-link"]
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
