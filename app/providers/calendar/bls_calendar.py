import logging
import re
from datetime import date, datetime
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import httpx
import icalendar

from app.models.market import EconomicEvent
from app.providers.calendar.base import (
    BaseCalendarProvider,
    CalendarProviderFetchResult,
)
from app.providers.calendar.http_retry import describe_exception, http_get_with_retry

logger = logging.getLogger(__name__)

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
HEADERS_BLS = {
    "User-Agent": "FinBot/1.0 (contact: admin@finbot.com)"
}

BLS_EXACT_ALLOWLIST: Dict[str, Tuple[str, str]] = {
    "CONSUMER PRICE INDEX": ("ABD Tüketici Fiyat Endeksi (CPI)", "US_CPI"),
    "PRODUCER PRICE INDEX": ("ABD Üretici Fiyat Endeksi (PPI)", "US_PPI"),
    "EMPLOYMENT SITUATION": ("ABD İstihdam Raporu (NFP)", "US_NFP"),
    "JOB OPENINGS AND LABOR TURNOVER SURVEY": ("ABD JOLTS", "US_JOLTS"),
}

REF_PERIOD_REGEX = re.compile(
    r"(?:for|reference(?:\s+period)?)\s+([A-Za-z]+(?:\s+\d{4})?|\d{4}(?:-Q\d)?|Q[1-4]\s+\d{4})",
    re.IGNORECASE
)


class BlsCalendarProvider(BaseCalendarProvider):
    provider_id: str = "BLS"
    provider_name: str = "U.S. Bureau of Labor Statistics Release Calendar"

    def __init__(self, timeout: float = 15.0):
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
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=HEADERS_BLS) as client:
                res = await http_get_with_retry(client, BLS_ICS_URL)
                res.raise_for_status()
                raw_ics = res.content

            # Parse strictly in-memory via icalendar
            try:
                cal = icalendar.Calendar.from_ical(raw_ics)
            except Exception as ical_err:
                msg = f"BLS Release Calendar ICS parse error: {describe_exception(ical_err)}"
                logger.warning(msg)
                warnings.append(msg)
                return CalendarProviderFetchResult(
                    events=events,
                    warnings=warnings,
                    structure_validated=False,
                    observed_candidate_count=0,
                    candidate_count=0,
                    malformed_count=0
                )

            vevents = [comp for comp in cal.walk("VEVENT")]
            if not vevents:
                msg = "BLS Release Calendar sayfa yapısı doğrulanamadı (VEVENT kaydı bulunamadı)"
                logger.warning(msg)
                warnings.append(msg)
                return CalendarProviderFetchResult(
                    events=events,
                    warnings=warnings,
                    structure_validated=False,
                    observed_candidate_count=0,
                    candidate_count=0,
                    malformed_count=0
                )

            structure_validated = True

            for comp in vevents:
                try:
                    summary = str(comp.get("summary", ""))
                    # Clean and normalize whitespace and casing
                    norm_summary = re.sub(r"\s+", " ", summary).strip().upper()
                    if not norm_summary:
                        continue

                    # Exact allowlist match (strict protection against substring false positives)
                    if norm_summary not in BLS_EXACT_ALLOWLIST:
                        continue

                    observed_candidate_count += 1
                    matched_std_name, matched_code = BLS_EXACT_ALLOWLIST[norm_summary]

                    dtstart = comp.get("dtstart")
                    if dtstart is None or not hasattr(dtstart, "dt"):
                        malformed_count += 1
                        warnings.append(f"BLS: '{summary}' için dtstart alanı eksik")
                        continue

                    dt_val = dtstart.dt
                    if isinstance(dt_val, datetime):
                        if dt_val.tzinfo is None:
                            dt_ny = dt_val.replace(tzinfo=tz_ny)
                        else:
                            dt_ny = dt_val.astimezone(tz_ny)
                    elif isinstance(dt_val, date):
                        dt_ny = datetime.combine(dt_val, datetime.min.time(), tzinfo=tz_ny)
                    else:
                        malformed_count += 1
                        warnings.append(f"BLS: '{summary}' için geçersiz dtstart tipi: {type(dt_val)}")
                        continue

                    dt_tr = dt_ny.astimezone(tz_tr)

                    if start <= dt_tr <= end:
                        candidate_count += 1
                        uid = str(comp.get("uid", "")).strip() or None
                        desc = str(comp.get("description", "")).strip()
                        url_val = str(comp.get("url", "")).strip() or BLS_ICS_URL

                        ref_period = None
                        if desc:
                            m_ref = REF_PERIOD_REGEX.search(desc)
                            if m_ref:
                                ref_period = m_ref.group(1).strip()
                        if not ref_period:
                            m_ref = REF_PERIOD_REGEX.search(summary)
                            if m_ref:
                                ref_period = m_ref.group(1).strip()

                        events.append(EconomicEvent(
                            name=matched_std_name,
                            country="US",
                            scheduled_at=dt_ny.isoformat(),
                            scheduled_at_istanbul=dt_tr.isoformat(),
                            source="BLS",
                            provider_id=self.provider_id,
                            event_code=matched_code,
                            source_event_id=uid,
                            reference_period=ref_period,
                            source_url=url_val
                        ))
                except Exception as entry_err:
                    malformed_count += 1
                    warnings.append(f"BLS VEVENT ayrıştırma hatası: {describe_exception(entry_err)}")
                    continue

        except httpx.HTTPError as http_err:
            msg = f"BLS Release Calendar HTTP isteği başarısız: {describe_exception(http_err)}"
            logger.warning(msg)
            warnings.append(msg)
        except Exception as e:
            msg = f"BLS Release Calendar beklenmeyen hata: {describe_exception(e)}"
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
