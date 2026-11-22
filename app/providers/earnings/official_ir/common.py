import re
from datetime import date
from typing import Optional, Tuple

from app.models.earnings import FiscalPeriod

MONTH_MAP = {
    # Turkish
    "ocak": 1, "subat": 2, "şubat": 2, "mart": 3, "nisan": 4, "mayis": 5, "mayıs": 5,
    "haziran": 6, "temmuz": 7, "agustos": 8, "ağustos": 8, "eylul": 9, "eylül": 9,
    "ekim": 10, "kasim": 11, "kasım": 11, "aralik": 12, "aralık": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}


def normalize_text(text: Optional[str]) -> str:
    """Normalize text: strip unicode NBSP, normalize whitespace."""
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ").replace("\u200b", " ")
    return " ".join(cleaned.split())


def parse_date_flexible(text: Optional[str]) -> Optional[str]:
    """Parse flexible date strings into ISO YYYY-MM-DD format."""
    if not text:
        return None
    cleaned = normalize_text(text)
    if not cleaned or cleaned in ("-", "None", "null"):
        return None

    # 1. Standard ISO YYYY-MM-DD
    m_iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None

    # 2. Dot/Slash/Dash DD.MM.YYYY or DD/MM/YYYY
    m_numeric = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", cleaned)
    if m_numeric:
        d, m, y = int(m_numeric.group(1)), int(m_numeric.group(2)), int(m_numeric.group(3))
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None

    # 3. Textual month DD Month YYYY (e.g., '30 Temmuz 2026', '16 September 2026')
    m_textual = re.search(r"(\d{1,2})\s+([a-zA-ZçÇğĞıİöÖşŞüÜ]+)\s+(\d{4})", cleaned)
    if m_textual:
        d_val = int(m_textual.group(1))
        month_word = m_textual.group(2).lower()
        y_val = int(m_textual.group(3))
        m_val = MONTH_MAP.get(month_word)
        if m_val:
            try:
                return date(y_val, m_val, d_val).isoformat()
            except ValueError:
                return None

    return None


def get_standard_period_dates(fiscal_year: int, fiscal_period: FiscalPeriod) -> Tuple[str, str]:
    """Calculate standard calendar fiscal period start and end dates."""
    if fiscal_period == "Q1":
        return f"{fiscal_year}-01-01", f"{fiscal_year}-03-31"
    if fiscal_period == "Q2":
        return f"{fiscal_year}-01-01", f"{fiscal_year}-06-30"
    if fiscal_period == "Q3":
        return f"{fiscal_year}-01-01", f"{fiscal_year}-09-30"
    if fiscal_period == "FY":
        return f"{fiscal_year}-01-01", f"{fiscal_year}-12-31"
    raise ValueError(f"Unknown fiscal period: {fiscal_period}")


def get_mavi_period_dates(fiscal_year: int, fiscal_period: FiscalPeriod) -> Tuple[str, str]:
    """Calculate MAVI special fiscal period (Feb 1 - Jan 31) start and end dates."""
    if fiscal_period == "Q1":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-04-30"
    if fiscal_period == "Q2":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-07-31"
    if fiscal_period == "Q3":
        return f"{fiscal_year}-02-01", f"{fiscal_year}-10-31"
    if fiscal_period == "FY":
        return f"{fiscal_year}-02-01", f"{fiscal_year + 1}-01-31"
    raise ValueError(f"Unknown fiscal period for MAVI: {fiscal_period}")


get_mavi_special_period_dates = get_mavi_period_dates

