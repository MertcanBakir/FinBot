import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx

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

TUIK_API_URL_TEMPLATE = "https://takvim.tuik.gov.tr/Kurumsal/GetYillikHaberBulteniListesi?yil={year}"
TUIK_BASE_URL = "https://takvim.tuik.gov.tr/Kurumsal/Veri_Takvimi"

TR_MONTH_MAP = {
    "OCAK": 1, "ŞUBAT": 2, "SUBAT": 2, "MART": 3, "NİSAN": 4, "NISAN": 4,
    "MAYIS": 5, "HAZİRAN": 6, "HAZIRAN": 6, "TEMMUZ": 7, "AĞUSTOS": 8, "AGUSTOS": 8,
    "EYLÜL": 9, "EYLUL": 9, "EKİM": 10, "EKIM": 10, "KASIM": 11, "ARALIK": 12
}


def _clean_tuik_reference_period(period_raw: str) -> Optional[str]:
    if not period_raw:
        return None
    p = period_raw.strip()
    p_upper = p.upper()

    # Quarter check: e.g. "2. Çeyrek 2026", "II. Çeyrek: Nisan-Haziran, 2026", "II. Çeyrek: Nisan-Haziran 2026"
    m_q = re.search(r"(\d)\.\s*ÇEYREK.*?(\d{4})", p_upper)
    if m_q:
        return f"{m_q.group(2)}-Q{m_q.group(1)}"

    m_q_roman = re.search(r"(IV|III|II|I)\.\s*ÇEYREK.*?(\d{4})", p_upper)
    if m_q_roman:
        roman_map = {"I": "Q1", "II": "Q2", "III": "Q3", "IV": "Q4"}
        return f"{m_q_roman.group(2)}-{roman_map.get(m_q_roman.group(1), 'Q1')}"

    # Month check: e.g. "Ağustos 2026", "Temmuz 2026", "08-2026"
    for m_name, m_num in TR_MONTH_MAP.items():
        if m_name in p_upper:
            m_yr = re.search(r"(20\d\d)", p)
            if m_yr:
                return f"{m_yr.group(1)}-{m_num:02d}"

    # Annual check: e.g. "2025" or "2026"
    m_yr_only = re.fullmatch(r"(20\d\d)", p)
    if m_yr_only:
        return m_yr_only.group(1)

    return p


class TuikCalendarProvider(BaseCalendarProvider):
    provider_id: str = "TUIK"
    provider_name: str = "TÜİK Veri Takvimi"

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    async def fetch_events(self, start: datetime, end: datetime) -> CalendarProviderFetchResult:
        tz_tr = ZoneInfo("Europe/Istanbul")
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz_tr)
        else:
            start = start.astimezone(tz_tr)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz_tr)
        else:
            end = end.astimezone(tz_tr)

        warnings: List[str] = []
        structure_validated = False
        observed_candidate_count = 0
        candidate_count = 0
        malformed_count = 0

        # Map logical key -> (EconomicEvent, score)
        # Score priority: (has_non_zero_id: bool, has_direct_link: bool)
        logical_events: Dict[Tuple[str, str, str, str], Tuple[EconomicEvent, Tuple[int, int]]] = {}

        years: Set[int] = {start.year, end.year}

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
                for y in sorted(years):
                    url = TUIK_API_URL_TEMPLATE.format(year=y)
                    try:
                        res = await http_get_with_retry(client, url)
                        res.raise_for_status()

                        try:
                            data = res.json()
                        except Exception as json_err:
                            msg = f"TÜİK {y} yanıtı JSON olarak ayrıştırılamadı: {describe_exception(json_err)}"
                            logger.warning(msg)
                            warnings.append(msg)
                            continue

                        if not isinstance(data, dict) or ("yayindaOlanlarList" not in data and "yayindaOlmayanlarList" not in data):
                            msg = f"TÜİK {y} takvim veri yapısı doğrulanamadı (beklenen JSON listeleri bulunamadı)"
                            logger.warning(msg)
                            warnings.append(msg)
                            continue

                        structure_validated = True

                        raw_items: List[Dict[str, Any]] = []
                        if "yayindaOlanlarList" in data and isinstance(data["yayindaOlanlarList"], list):
                            raw_items.extend(data["yayindaOlanlarList"])
                        if "yayindaOlmayanlarList" in data and isinstance(data["yayindaOlmayanlarList"], list):
                            raw_items.extend(data["yayindaOlmayanlarList"])

                        for item in raw_items:
                            if not isinstance(item, dict):
                                continue

                            adi = item.get("adi", "")
                            matched_title = match_tracked_event("TÜİK", adi)
                            if not matched_title:
                                continue

                            observed_candidate_count += 1
                            g_tarih = item.get("gTarih")
                            if not g_tarih:
                                malformed_count += 1
                                warnings.append(f"TÜİK: '{matched_title}' için gTarih alanı eksik")
                                continue

                            try:
                                dt_parsed = datetime.fromisoformat(g_tarih)
                                if dt_parsed.tzinfo is None:
                                    dt_parsed = dt_parsed.replace(tzinfo=tz_tr)
                                else:
                                    dt_parsed = dt_parsed.astimezone(tz_tr)
                            except Exception as dt_err:
                                malformed_count += 1
                                warnings.append(f"TÜİK: '{matched_title}' için geçersiz tarih formatı '{g_tarih}': {describe_exception(dt_err)}")
                                continue

                            if start <= dt_parsed <= end:
                                candidate_count += 1
                                item_id = item.get("id") or 0
                                donemi_raw = str(item.get("donemi") or "")
                                ref_period = _clean_tuik_reference_period(donemi_raw)
                                raw_link = item.get("link")
                                source_url = raw_link if (raw_link and raw_link.startswith("http")) else TUIK_BASE_URL
                                ev_code = CANONICAL_EVENT_CODES.get(matched_title, "TR_UNKNOWN")

                                # Deterministic stable source ID independent of publication status (id=0 vs id=58000)
                                if ref_period and ev_code:
                                    source_id = f"TUIK:{ev_code}:{ref_period}"
                                elif ev_code:
                                    source_id = f"TUIK:{ev_code}:{dt_parsed.date().isoformat()}"
                                else:
                                    source_id = None

                                event_obj = EconomicEvent(
                                    name=matched_title,
                                    country="TR",
                                    scheduled_at=dt_parsed.isoformat(),
                                    scheduled_at_istanbul=dt_parsed.isoformat(),
                                    source="TÜİK",
                                    provider_id=self.provider_id,
                                    event_code=ev_code,
                                    reference_period=ref_period,
                                    source_event_id=source_id,
                                    source_url=source_url
                                )

                                # Logical deduplication key:
                                # Distinguishes different reference periods at the exact same timestamp
                                # (e.g. 2025 Annual GDP vs 2026-Q2 Quarterly GDP)
                                log_key = (
                                    self.provider_id,
                                    ev_code,
                                    ref_period or adi.strip().upper(),
                                    dt_parsed.isoformat()
                                )

                                # Score: (has_non_zero_id, has_direct_link)
                                score = (1 if item_id != 0 else 0, 1 if source_url != TUIK_BASE_URL else 0)

                                if log_key not in logical_events or score > logical_events[log_key][1]:
                                    logical_events[log_key] = (event_obj, score)

                    except httpx.HTTPError as http_err:
                        msg = f"TÜİK {y} HTTP isteği başarısız: {describe_exception(http_err)}"
                        logger.warning(msg)
                        warnings.append(msg)
                    except Exception as e:
                        msg = f"TÜİK {y} beklenmeyen hata: {describe_exception(e)}"
                        logger.warning(msg)
                        warnings.append(msg)

        except Exception as e:
            msg = f"TÜİK beklenmeyen istemci hatası: {describe_exception(e)}"
            logger.warning(msg)
            warnings.append(msg)

        final_events = [ev for ev, _ in logical_events.values()]
        final_events.sort(key=lambda x: (x.scheduled_at_istanbul, x.event_code or x.name, x.reference_period or ""))

        return CalendarProviderFetchResult(
            events=final_events,
            warnings=warnings,
            structure_validated=structure_validated,
            observed_candidate_count=observed_candidate_count,
            candidate_count=candidate_count,
            malformed_count=malformed_count
        )
