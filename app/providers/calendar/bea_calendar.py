import logging
import re
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from app.models.market import EconomicEvent
from app.providers.calendar.base import (
    BaseCalendarProvider,
    CalendarProviderFetchResult,
)
from app.providers.calendar.http_retry import describe_exception, http_get_with_retry
from app.providers.calendar.tracked_events import (
    CANONICAL_EVENT_CODES,
    match_tracked_event,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

MONTH_MAP_EN = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
}


def _extract_bea_reference_period(text: str) -> Optional[str]:
    # e.g. "Gross Domestic Product (Advance Estimate), 2nd Quarter 2026" -> "2026-Q2"
    m_q = re.search(r"(\d)(?:st|nd|rd|th)\s+Quarter,?\s+(20\d\d)", text, re.IGNORECASE)
    if m_q:
        return f"{m_q.group(2)}-Q{m_q.group(1)}"

    # e.g. "Personal Income and Outlays, June 2026" -> "2026-06"
    m_m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(20\d\d)", text, re.IGNORECASE)
    if m_m:
        month_name = m_m.group(1).upper()
        month_num = MONTH_MAP_EN.get(month_name)
        if month_num:
            return f"{m_m.group(2)}-{month_num:02d}"

    return None


def _extract_bea_source_event_id(matched_title: str, text: str, ref_period: Optional[str]) -> Optional[str]:
    if not ref_period:
        return None

    text_lower = text.lower()
    if matched_title == "ABD GSYH":
        estimate = "annual"
        if "advance" in text_lower:
            estimate = "advance"
        elif "second" in text_lower:
            estimate = "second"
        elif "third" in text_lower:
            estimate = "third"
        elif "initial" in text_lower:
            estimate = "initial"
        elif "updated" in text_lower or "revised" in text_lower:
            estimate = "revised"

        return f"BEA:US_GDP:{ref_period}:{estimate}"

    if matched_title == "ABD PCE / Personal Income and Outlays":
        return f"BEA:US_PCE:{ref_period}"

    return None


class BeaCalendarProvider(BaseCalendarProvider):
    provider_id: str = "BEA"
    provider_name: str = "U.S. Bureau of Economic Analysis"
    url: str = "https://www.bea.gov/news/schedule/full"

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    async def fetch_events(self, start: datetime, end: datetime) -> CalendarProviderFetchResult:
        tz_ny = ZoneInfo("America/New_York")
        tz_tr = ZoneInfo("Europe/Istanbul")

        if start.tzinfo is None:
            start = start.replace(tzinfo=tz_tr)
        else:
            start = start.astimezone(tz_tr)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz_tr)
        else:
            end = end.astimezone(tz_tr)

        events: List[EconomicEvent] = []
        warnings: List[str] = []
        structure_validated = False
        observed_candidate_count = 0
        candidate_count = 0
        malformed_count = 0

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
                res = await http_get_with_retry(client, self.url)
                res.raise_for_status()

                soup = BeautifulSoup(res.text, "html.parser")

                cal_table = soup.find("table", id="release-schedule-table")
                if not cal_table:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (table#release-schedule-table bulunamadı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

                # Extract table year from headers or caption
                table_year = start.year
                year_match = re.search(r"Year\s+(20\d\d)", cal_table.get_text(), re.IGNORECASE)
                if year_match:
                    table_year = int(year_match.group(1))

                rows = cal_table.find_all("tr")
                if len(rows) < 2:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (yetersiz tablo satırı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

                structure_validated = True

                for row in rows:
                    td_date = row.find(class_=re.compile(r"scheduled-date"))
                    td_title = row.find(class_=re.compile(r"release-title"))

                    if not td_date or not td_title:
                        continue

                    title_text = td_title.get_text(" ", strip=True)
                    # Exclude office closures, holidays, or state-only releases without national headline
                    if any(ignore_kw in title_text.lower() for ignore_kw in ["office closure", "holiday", "federal holiday"]):
                        continue

                    matched_title = match_tracked_event("BEA", title_text)
                    if not matched_title:
                        continue

                    observed_candidate_count += 1
                    date_cell_text = td_date.get_text(" ", strip=True)

                    # Extract date and time: e.g. "January 22 8:30 AM", "September 30 8:30 AM"
                    m = re.search(
                        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s+(20\d\d))?(?:\s+(\d{1,2}:\d{2}\s*(?:AM|PM)))?",
                        date_cell_text,
                        re.IGNORECASE
                    )
                    if not m:
                        malformed_count += 1
                        warnings.append(f"{self.provider_name}: '{matched_title}' için tarih formatı ayrıştırılamadı: '{date_cell_text[:50]}'")
                        continue

                    month_name = m.group(1).upper()
                    day_str = m.group(2)
                    row_year = int(m.group(3)) if m.group(3) else table_year
                    time_str = m.group(4) or "8:30 AM"

                    month_num = MONTH_MAP_EN.get(month_name)
                    if not month_num:
                        malformed_count += 1
                        warnings.append(f"{self.provider_name}: '{matched_title}' için geçersiz ay adı: '{month_name}'")
                        continue

                    try:
                        time_clean = re.sub(r"\s+", " ", time_str).strip().upper()
                        dt_time = datetime.strptime(time_clean, "%I:%M %p").time()
                        dt_ny = datetime.combine(date(row_year, month_num, int(day_str)), dt_time, tzinfo=tz_ny)
                        dt_tr = dt_ny.astimezone(tz_tr)

                        if start <= dt_tr <= end:
                            candidate_count += 1
                            ref_period = _extract_bea_reference_period(title_text)
                            source_ev_id = _extract_bea_source_event_id(matched_title, title_text, ref_period)

                            events.append(EconomicEvent(
                                name=matched_title,
                                country="US",
                                scheduled_at=dt_ny.isoformat(),
                                scheduled_at_istanbul=dt_tr.isoformat(),
                                source="BEA",
                                provider_id=self.provider_id,
                                event_code=CANONICAL_EVENT_CODES.get(matched_title),
                                reference_period=ref_period,
                                source_event_id=source_ev_id,
                                source_url=self.url
                            ))
                    except Exception as e:
                        malformed_count += 1
                        warnings.append(f"{self.provider_name}: '{matched_title}' için tarih oluşturulamadı: {describe_exception(e)}")
                        continue

        except httpx.HTTPError as http_err:
            msg = f"{self.provider_name} HTTP isteği başarısız: {describe_exception(http_err)}"
            logger.warning(msg)
            warnings.append(msg)
        except Exception as e:
            msg = f"{self.provider_name} beklenmeyen hata: {describe_exception(e)}"
            logger.warning(msg)
            warnings.append(msg)

        events.sort(key=lambda x: datetime.fromisoformat(x.scheduled_at_istanbul))
        return CalendarProviderFetchResult(
            events=events,
            warnings=warnings,
            structure_validated=structure_validated,
            observed_candidate_count=observed_candidate_count,
            candidate_count=candidate_count,
            malformed_count=malformed_count
        )
