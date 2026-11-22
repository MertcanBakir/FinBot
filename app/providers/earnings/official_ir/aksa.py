import logging
import re
from typing import List, Optional

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


class AksaIrProvider(BaseOfficialIrProvider):
    symbol = "AKSA"
    company_name = "AKSA AKRİLİK KİMYA SANAYİİ A.Ş."
    source_provider = "AKSA Investor Relations"
    url = "https://www.aksa.com/yatirimci-iliskileri/yatirimci-takvimi"

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        if not html or not html.strip():
            msg = "Empty HTML content from AKSA IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        soup = BeautifulSoup(html, "html.parser")
        table = None

        # 1. Match table by caption containing 'Finansal Tabloların Yayınlanma Takvimi'
        for tbl in soup.find_all("table"):
            caption = tbl.find("caption")
            if caption and "finansal tablolar" in caption.get_text(strip=True).lower():
                table = tbl
                break

        # 2. Strict fallback: table containing quarter headers '1ç', '2ç', '3ç', '4ç' and 'yayınlanma'
        if not table:
            for tbl in soup.find_all("table"):
                txt = tbl.get_text(strip=True).lower()
                if "1ç" in txt and "2ç" in txt and "3ç" in txt and "4ç" in txt and "yayınlanma" in txt:
                    table = tbl
                    break

        # Strict: Do NOT fallback to generic soup.find("table")
        if not table:
            msg = "Recognized financial calendar table not found on AKSA IR page"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        trs = table.find_all("tr")
        if len(trs) < 2:
            msg = "AKSA table has fewer than 2 rows"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        # Row 1: Headers (e.g. ['2026 Yılı Finansal Tabloları', '1Ç', '2Ç', '3Ç', '4Ç'])
        # Row 2: Dates (e.g. ['Yayınlanma Tarihi', '07.05.2026', '13.08.2026', '04.11.2026', '17.02.2027'])
        header_cells = [normalize_text(c.get_text(strip=True)) for c in trs[0].find_all(["th", "td"])]
        date_cells = [normalize_text(c.get_text(strip=True)) for c in trs[1].find_all(["th", "td"])]

        # Extract fiscal year deterministically from header_cells[0] or caption
        fiscal_year: Optional[int] = None
        caption = table.find("caption")
        caption_txt = normalize_text(caption.get_text(strip=True)) if caption else ""
        
        m_yr = re.search(r"(\d{4})\s*Y[ıi]l[ıi]", header_cells[0] if header_cells else "")
        if not m_yr:
            m_yr = re.search(r"(\d{4})", header_cells[0] if header_cells else "")
        if not m_yr and caption_txt:
            m_yr = re.search(r"(\d{4})", caption_txt)

        if m_yr:
            fiscal_year = int(m_yr.group(1))

        if not fiscal_year:
            msg = "Could not deterministically determine fiscal year from AKSA table headers"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="schema_failure", is_global=True)]
            )

        period_col_map = {}
        for idx, col in enumerate(header_cells):
            col_clean = re.sub(r"[^0-9A-ZÇĞİÖŞÜ]", "", col.upper()).replace("Ç", "C")
            if "1C" in col_clean or "Q1" in col_clean:
                period_col_map["Q1"] = idx
            elif "2C" in col_clean or "Q2" in col_clean:
                period_col_map["Q2"] = idx
            elif "3C" in col_clean or "Q3" in col_clean:
                period_col_map["Q3"] = idx
            elif "4C" in col_clean or "Q4" in col_clean or "FY" in col_clean:
                period_col_map["FY"] = idx

        if len(period_col_map) < 4:
            msg = f"AKSA table missing quarter columns (found: {list(period_col_map.keys())})"
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
        rows_seen = 1
        accepted_count = 0
        rejected_count = 0

        for period_key in ["Q1", "Q2", "Q3", "FY"]:
            col_idx = period_col_map.get(period_key)
            if col_idx is None or col_idx >= len(date_cells):
                warn_msg = f"Missing date cell for {period_key} in AKSA table"
                warnings.append(warn_msg)
                issues.append(OfficialIrIssue(
                    message=warn_msg,
                    kind="row_validation",
                    fiscal_year=fiscal_year,
                    fiscal_period=period_key,  # type: ignore
                    is_global=False
                ))
                rejected_count += 1
                continue

            date_raw = date_cells[col_idx]
            earnings_date = parse_date_flexible(date_raw)
            if not earnings_date:
                warn_msg = f"Invalid date '{date_raw}' for {period_key} in AKSA table"
                warnings.append(warn_msg)
                issues.append(OfficialIrIssue(
                    message=warn_msg,
                    kind="row_validation",
                    fiscal_year=fiscal_year,
                    fiscal_period=period_key,  # type: ignore
                    is_global=False
                ))
                rejected_count += 1
                continue

            fiscal_period: FiscalPeriod = period_key  # type: ignore
            period_start, period_end = get_standard_period_dates(fiscal_year, fiscal_period)

            if earnings_date < period_end:
                warn_msg = f"Earnings date {earnings_date} before period_end {period_end} for {period_key} in AKSA table"
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
                source_event_id=f"aksa:{fiscal_year}:{fiscal_period}",
                disclosure_index=None,
                source_url=self.url
            )
            events.append(event)
            accepted_count += 1

        if accepted_count == 0:
            fallback_warn = warnings or ["No valid events parsed from AKSA table"]
            if not issues:
                for w in fallback_warn:
                    issues.append(OfficialIrIssue(message=w, kind="schema_failure", is_global=True))
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                parser_failed=True,
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
