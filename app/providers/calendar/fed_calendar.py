import logging
import re
from datetime import date, datetime, time
from typing import List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from app.models.market import EconomicEvent
from app.providers.calendar.base import (
    BaseCalendarProvider,
    CalendarProviderFetchResult,
)
from app.providers.calendar.http_retry import describe_exception, http_get_with_retry
from app.providers.calendar.tracked_events import CANONICAL_EVENT_CODES

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


class FedCalendarProvider(BaseCalendarProvider):
    provider_id: str = "FED"
    provider_name: str = "Federal Reserve FOMC Calendar"
    url: str = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

    def __init__(self, timeout: float = 10.0):
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

        years_to_search: Set[int] = {start.year, end.year}

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
                res = await http_get_with_retry(client, self.url)
                res.raise_for_status()

                soup = BeautifulSoup(res.text, "html.parser")

                panels = soup.find_all(class_=re.compile(r"panel", re.I))
                matched_panels: List[Tuple[int, Tag]] = []

                for p in panels:
                    heading = p.find(class_=re.compile(r"panel-heading|heading", re.I))
                    heading_text = heading.get_text(" ", strip=True) if heading else ""
                    for yr in years_to_search:
                        if str(yr) in heading_text and "FOMC" in heading_text.upper():
                            matched_panels.append((yr, p))

                if not matched_panels:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (FOMC yıl panelleri bulunamadı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

                structure_validated = True

                for yr, panel in matched_panels:
                    # Select only elements with the exact 'fomc-meeting' CSS class
                    raw_meetings = panel.select(".fomc-meeting")
                    meetings = [
                        el for el in raw_meetings
                        if isinstance(el, Tag) and "fomc-meeting" in el.get("class", [])
                    ]

                    if not meetings:
                        continue

                    for ordinal, m in enumerate(meetings, start=1):
                        observed_candidate_count += 1
                        month_elem = m.find(class_=re.compile(r"month", re.I))
                        date_elem = m.find(class_=re.compile(r"date", re.I))

                        if not month_elem or not date_elem:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: FOMC toplantı satırında ay veya gün elementi eksik ({yr}, sıra {ordinal})")
                            continue

                        raw_month = month_elem.get_text(" ", strip=True)
                        raw_date = date_elem.get_text(" ", strip=True)

                        # Clean asterisks / footnotes: e.g. "17-18*" -> "17-18"
                        clean_date = re.sub(r"[^\d\-/\s]", "", raw_date).strip()

                        decision_day: Optional[int] = None
                        decision_month_str: str = raw_month

                        if "-" in clean_date:
                            parts = clean_date.split("-")
                            try:
                                decision_day = int(parts[-1].strip())
                            except ValueError:
                                pass
                        else:
                            try:
                                decision_day = int(clean_date.strip())
                            except ValueError:
                                pass

                        if "/" in raw_month:
                            month_parts = raw_month.split("/")
                            decision_month_str = month_parts[-1].strip()

                        month_upper = decision_month_str.upper().strip()
                        month_num = MONTH_MAP_EN.get(month_upper)

                        if not month_num or decision_day is None:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: '{raw_month} {raw_date}' ({yr}) için tarih ayrıştırılamadı")
                            continue

                        try:
                            # Policy announcement time: 14:00 America/New_York
                            decision_date = date(yr, month_num, decision_day)
                            dt_ny = datetime.combine(decision_date, time(14, 0), tzinfo=tz_ny)
                            dt_tr = dt_ny.astimezone(tz_tr)

                            if start <= dt_tr <= end:
                                candidate_count += 1
                                std_name = "Fed Faiz Kararı (FOMC)"
                                ev_code = CANONICAL_EVENT_CODES.get(std_name, "US_FOMC")
                                source_id = f"FED:US_FOMC:{yr}-N{ordinal:02d}"

                                events.append(EconomicEvent(
                                    name=std_name,
                                    country="US",
                                    scheduled_at=dt_ny.isoformat(),
                                    scheduled_at_istanbul=dt_tr.isoformat(),
                                    source="Federal Reserve",
                                    provider_id=self.provider_id,
                                    event_code=ev_code,
                                    reference_period=f"{yr}-{month_num:02d}",
                                    source_event_id=source_id,
                                    source_url=self.url
                                ))
                        except Exception as e:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: '{raw_month} {raw_date}' için tarih oluşturulamadı: {describe_exception(e)}")
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
