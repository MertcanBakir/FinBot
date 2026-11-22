import logging
import re
from datetime import datetime, time
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from app.models.market import EconomicEvent
from app.providers.calendar.base import (
    BaseCalendarProvider,
    CalendarProviderFetchResult,
)
from app.providers.calendar.http_retry import describe_exception, http_get_with_retry

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

MONTH_MAP_TR = {
    "OCAK": 1, "ŞUBAT": 2, "SUBAT": 2, "MART": 3, "NİSAN": 4, "NISAN": 4,
    "MAYIS": 5, "HAZİRAN": 6, "HAZIRAN": 6, "TEMMUZ": 7, "AĞUSTOS": 8, "AGUSTOS": 8,
    "EYLÜL": 9, "EYLUL": 9, "EKİM": 10, "EKIM": 10, "KASIM": 11, "ARALIK": 12
}


def normalize_tr_text(text: str) -> str:
    if not text:
        return ""
    return (
        text.upper()
        .replace("Ö", "O")
        .replace("Ü", "U")
        .replace("Ş", "S")
        .replace("Ğ", "G")
        .replace("Ç", "C")
        .replace("İ", "I")
        .replace("İ", "I")
    )


class TcmbCalendarProvider(BaseCalendarProvider):
    provider_id: str = "TCMB"
    provider_name: str = "TCMB Takvimi"
    url: str = "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Duyurular/Takvim"

    def __init__(self, timeout: float = 10.0):
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

                cal_table = soup.find("table", id="midTable")
                if not cal_table:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (table#midTable bulunamadı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

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

                # Header row mapping
                header_cells = rows[0].find_all(["th", "td"])
                if not header_cells:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (başlık satırı bulunamadı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

                col_mapping: Dict[int, Dict[str, Any]] = {}
                for idx, cell in enumerate(header_cells):
                    header_txt = normalize_tr_text(cell.get_text(strip=True))
                    if "PARA POLITIKASI KURULU TOPLANTI KARARI" in header_txt or "PPK KARARI" in header_txt:
                        col_mapping[idx] = {
                            "name": "TCMB Faiz Kararı",
                            "code": "TR_POLICY_RATE",
                            "policy_time": time(14, 0)
                        }
                    elif "ENFLASYON RAPORU" in header_txt:
                        col_mapping[idx] = {
                            "name": "TCMB Enflasyon Raporu",
                            "code": "TR_INFLATION_REPORT",
                            "policy_time": time(10, 30)
                        }

                if not col_mapping:
                    warnings.append(f"{self.provider_name} sayfa yapısı doğrulanamadı (beklenen sütun başlıkları bulunamadı)")
                    return CalendarProviderFetchResult(
                        events=events,
                        warnings=warnings,
                        structure_validated=False,
                        observed_candidate_count=0,
                        candidate_count=0,
                        malformed_count=0
                    )

                structure_validated = True
                col_ordinals: Dict[int, int] = {idx: 0 for idx in col_mapping}

                for _row_idx, row in enumerate(rows[1:], start=1):
                    cells = row.find_all(["td", "th"])
                    for col_idx, col_cfg in col_mapping.items():
                        if col_idx >= len(cells):
                            continue

                        cell_text = cells[col_idx].get_text(" ", strip=True)
                        if not cell_text:
                            continue

                        observed_candidate_count += 1
                        col_ordinals[col_idx] += 1
                        ordinal = col_ordinals[col_idx]

                        pattern = r"(\d{1,2})\s+([A-Za-zĞÜŞİÖÇğüşiöç]+)\s+(20\d\d)"
                        m = re.search(pattern, cell_text, re.IGNORECASE)
                        if not m:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: '{col_cfg['name']}' için tarih formatı ayrıştırılamadı: '{cell_text[:50]}'")
                            continue

                        day_str = m.group(1)
                        raw_month = m.group(2)
                        year_str = m.group(3)

                        month_upper = raw_month.replace("i", "İ").replace("ı", "I").upper()
                        month_num = MONTH_MAP_TR.get(month_upper)
                        if not month_num:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: '{col_cfg['name']}' için geçersiz ay adı: '{raw_month}'")
                            continue

                        try:
                            ev_date = datetime(int(year_str), month_num, int(day_str)).date()
                            dt_event = datetime.combine(ev_date, col_cfg["policy_time"], tzinfo=tz_tr)

                            if start <= dt_event <= end:
                                candidate_count += 1
                                std_name = col_cfg["name"]
                                ev_code = col_cfg["code"]
                                source_id = f"TCMB:{ev_code}:{year_str}-N{ordinal:02d}"

                                events.append(EconomicEvent(
                                    name=std_name,
                                    country="TR",
                                    scheduled_at=dt_event.isoformat(),
                                    scheduled_at_istanbul=dt_event.isoformat(),
                                    source="TCMB",
                                    provider_id=self.provider_id,
                                    event_code=ev_code,
                                    reference_period=f"{year_str}-{month_num:02d}",
                                    source_event_id=source_id,
                                    source_url=self.url
                                ))
                        except Exception as e:
                            malformed_count += 1
                            warnings.append(f"{self.provider_name}: '{col_cfg['name']}' için tarih oluşturulamadı: {describe_exception(e)}")
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
