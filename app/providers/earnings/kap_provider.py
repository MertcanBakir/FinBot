import asyncio
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.earnings import (
    BistEarningsEvent,
    FiscalPeriod,
    KapCalendarParseResult,
    KapEarningsFetchMetrics,
    KapEarningsFetchResult,
    KapSymbolEarningsStatus,
)
from app.providers.earnings.base import BaseEarningsProvider
from app.repositories.instrument_catalog_repository import (
    InstrumentCatalogRepository,
    KapMemberMappingRepository,
)

logger = logging.getLogger(__name__)

def normalize_date_strict(date_str: Optional[str]) -> Optional[str]:
    """Strict date normalizer distinguishing valid blank cells and non-date text labels from malformed date strings.

    Returns:
      - None if date_str is empty, whitespace, "-", "None", "null", or a non-date text label.
      - YYYY-MM-DD string if date_str is a valid calendar date.
    Raises:
      - ValueError if date_str is an invalid/malformed date string (e.g. 31/02/2026, invalid-date).
    """
    if date_str is None:
        return None
    cleaned = date_str.strip()
    if not cleaned or cleaned in ("-", "None", "null"):
        return None

    if "invalid-date" in cleaned.lower() or "not-a-date" in cleaned.lower():
        raise ValueError(f"Malformed date string: {cleaned}")

    m_dmy = re.search(r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})', cleaned)
    if m_dmy:
        d_str, mon_str, y_str = m_dmy.groups()
        try:
            d, mon, y = int(d_str), int(mon_str), int(y_str)
            dt = date(y, mon, d)
            return dt.strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date: {cleaned}") from exc

    m_ymd = re.search(r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})', cleaned)
    if m_ymd:
        y_str, mon_str, d_str = m_ymd.groups()
        try:
            y, mon, d = int(y_str), int(mon_str), int(d_str)
            dt = date(y, mon, d)
            return dt.strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date: {cleaned}") from exc

    return None


def normalize_date(date_str: Optional[str]) -> Optional[str]:
    """Compatibility date normalizer."""
    try:
        return normalize_date_strict(date_str)
    except ValueError:
        return None


def normalize_publish_date(pub_date: Optional[Any]) -> Optional[str]:
    """Normalize KAP publishDate to YYYY-MM-DD format (date only, no time)."""
    if not pub_date:
        return None
    if isinstance(pub_date, (int, float)):
        try:
            ts = pub_date / 1000.0 if pub_date > 1e11 else float(pub_date)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    pub_str = str(pub_date).strip()
    try:
        norm_iso = normalize_date_strict(pub_str)
        if norm_iso:
            return norm_iso
    except ValueError:
        return None

    try:
        dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def normalize_subject(subj: Optional[str]) -> str:
    """Normalize subject string for exact matching (strip, whitespace fold, casefold)."""
    if not subj:
        return ""
    cleaned = " ".join(subj.strip().split())
    nfkd = unicodedata.normalize("NFKD", cleaned)
    return nfkd.casefold()


def get_rolling_coverage(as_of: date) -> Tuple[date, date]:
    """Get rolling 91-calendar-day coverage window [as_of, as_of + 90 days] inclusive."""
    return as_of, as_of + timedelta(days=90)


def extract_fiscal_period_from_row(tr: Any, cell_texts: List[str]) -> Optional[FiscalPeriod]:
    """Extract fiscal period (Q1, Q2, Q3, FY) from table row class, taxonomy tokens, or exact direct cell labels."""
    tr_class = " ".join(tr.get("class", [])).lower() if hasattr(tr, "get") else ""
    first_cell = cell_texts[0].strip().lower() if cell_texts else ""

    # Check taxonomy token in first cell or row class
    if "oda_firstquarter" in first_cell or "oda_firstquarter" in tr_class:
        return "Q1"
    if "oda_secondquarter" in first_cell or "oda_secondquarter" in tr_class:
        return "Q2"
    if "oda_thirdquarter" in first_cell or "oda_thirdquarter" in tr_class:
        return "Q3"
    if "oda_annual" in first_cell or "oda_annual" in tr_class:
        return "FY"

    # Narrative explanation / title check
    if any("explanation" in text.lower() or "açıklama" in text.lower() for text in cell_texts):
        return None

    # Check direct cells for exact period label (only if concise label)
    for text in cell_texts:
        t_clean = text.strip()
        t_lower = t_clean.lower()
        if "oda_firstquarter" in t_lower:
            return "Q1"
        if "oda_secondquarter" in t_lower:
            return "Q2"
        if "oda_thirdquarter" in t_lower:
            return "Q3"
        if "oda_annual" in t_lower:
            return "FY"

        if len(t_clean) <= 40:
            if any(t_lower == label or t_lower.startswith(label) for label in ["1. çeyrek", "first quarter"]):
                if not any(w in t_lower for w in ["açıklama", "şirketimiz", "kap", "güncelleme"]):
                    return "Q1"
            if any(t_lower == label or t_lower.startswith(label) for label in ["2. çeyrek", "second quarter"]):
                if not any(w in t_lower for w in ["açıklama", "şirketimiz", "kap", "güncelleme"]):
                    return "Q2"
            if any(t_lower == label or t_lower.startswith(label) for label in ["3. çeyrek", "third quarter"]):
                if not any(w in t_lower for w in ["açıklama", "şirketimiz", "kap", "güncelleme"]):
                    return "Q3"
            if any(t_lower == label or t_lower.startswith(label) for label in ["yıllık", "annual"]):
                if not any(w in t_lower for w in ["açıklama", "şirketimiz", "kap", "güncelleme"]):
                    return "FY"
    return None


def parse_kap_financial_calendar_html(html_content: str) -> KapCalendarParseResult:
    """Parse HTML fragment inside disclosureBody table for period rows and planned announcement dates."""
    if not html_content or not html_content.strip():
        return KapCalendarParseResult(rows=[], warnings=["Empty disclosureBody HTML"], parser_failed=True)

    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return KapCalendarParseResult(rows=[], warnings=["No <table> element found in disclosureBody"], parser_failed=True)

    parsed_rows: List[Dict[str, Optional[str]]] = []
    warnings: List[str] = []
    positional_fallback_count = 0
    parsed_any_table = False

    for table in tables:
        tbl_class = " ".join(table.get("class", [])).lower()
        trs = table.find_all("tr")
        if not trs:
            continue

        header_cell_count: Optional[int] = None
        header_planned_idx: Optional[int] = None
        header_start_idx: Optional[int] = None
        header_end_idx: Optional[int] = None

        for tr in trs:
            ths = tr.find_all(["th", "td"], recursive=False)
            cell_texts = [c.get_text(strip=True) for c in ths]
            combined = " ".join(cell_texts).lower()

            if ("dönem başlangıç" in combined or "period beginning" in combined) and \
               ("dönem bitiş" in combined or "period ending" in combined) and \
               ("planlanan kap" in combined or "planned" in combined):
                header_cell_count = len(cell_texts)
                for idx, text in enumerate(cell_texts):
                    t_lower = text.lower()
                    if "planlanan" in t_lower or "planned" in t_lower:
                        header_planned_idx = idx
                    elif "başlangıç" in t_lower or "beginning" in t_lower:
                        header_start_idx = idx
                    elif "bitiş" in t_lower or "ending" in t_lower:
                        header_end_idx = idx

        has_recognized_header = (
            header_start_idx is not None
            and header_end_idx is not None
            and header_planned_idx is not None
        )
        has_taxonomy_evidence = "financial-calender" in tbl_class or any("oda_" in " ".join(tr.get("class", [])).lower() for tr in trs)

        if not has_recognized_header and not has_taxonomy_evidence:
            continue

        parsed_any_table = True

        for tr in trs:
            tds = tr.find_all(["td", "th"], recursive=False)
            if not tds:
                continue

            texts = [td.get_text(strip=True) for td in tds]
            combined_txt = " ".join(texts).lower()

            # Skip header / title / metadata rows
            if any(k in combined_txt for k in [
                "dönem başlangıç", "period beginning", "planlanan kap",
                "ilgili şirketler", "bildirim içeriği", "finansal takvim",
                "explanationtextblock", "açıklama", "explanation", "güncelleme",
                "düzeltme", "ertelenmiş", "konuya ilişkin"
            ]):
                continue

            f_period = extract_fiscal_period_from_row(tr, texts)
            tr_class = " ".join(tr.get("class", [])).lower() if hasattr(tr, "get") else ""
            first_cell = texts[0].strip().lower() if texts else ""

            # Exclude non-fiscal oda_ taxonomy tokens
            if (first_cell.startswith("oda_") or "oda_" in tr_class) and not any(tok in first_cell or tok in tr_class for tok in ["oda_firstquarter", "oda_secondquarter", "oda_thirdquarter", "oda_annual"]):
                continue

            has_taxonomy_token = any(f"oda_{token}" in first_cell or f"oda_{token}" in tr_class for token in ["firstquarter", "secondquarter", "thirdquarter", "annual"])
            has_data_input = "data-input-row" in tr_class

            # Filtering out presentation duplicate rows
            if not has_taxonomy_token and not has_data_input and first_cell == "" and not (has_recognized_header and len(texts) == 3):
                continue

            # A row MUST have f_period OR be a direct 3-cell row in a 3-semantic-header table!
            if not f_period and not (has_recognized_header and len(texts) == 3):
                continue

            # Positional slot mapping
            if len(texts) >= 3:
                val_cells = texts[-3:]
            else:
                continue

            raw_p_start, raw_p_end, raw_p_plan = val_cells[0], val_cells[1], val_cells[2]

            try:
                p_start = normalize_date_strict(raw_p_start)
                p_end = normalize_date_strict(raw_p_end)
                p_plan = normalize_date_strict(raw_p_plan)
            except ValueError as ve:
                warnings.append(f"Malformed date in fiscal row: {ve}")
                return KapCalendarParseResult(rows=[], warnings=warnings, parser_failed=True)

            # Infer fiscal period from period_start/end if not explicitly labeled
            if not f_period and p_start and p_end:
                if p_start.endswith("-01-01") and p_end.endswith("-03-31"):
                    f_period = "Q1"
                elif (p_start.endswith("-01-01") or p_start.endswith("-04-01")) and p_end.endswith("-06-30"):
                    f_period = "Q2"
                elif (p_start.endswith("-01-01") or p_start.endswith("-07-01")) and p_end.endswith("-09-30"):
                    f_period = "Q3"
                elif p_start.endswith("-01-01") and p_end.endswith("-12-31"):
                    f_period = "FY"

            # Healthy empty row (all 3 slots empty/None) -> skip safely
            if p_start is None and p_end is None and p_plan is None:
                continue

            # If planned_date is populated, verify required period fields
            if p_plan is not None:
                if p_start is None or p_end is None or f_period is None:
                    warn_msg = f"Row has planned date '{p_plan}' but missing required period field (period_start='{p_start}', period_end='{p_end}', fiscal_period='{f_period}')"
                    warnings.append(warn_msg)
                    return KapCalendarParseResult(rows=[], warnings=warnings, parser_failed=True)

                parsed_rows.append({
                    "period_start": p_start,
                    "period_end": p_end,
                    "planned_date": p_plan,
                    "fiscal_period": f_period,
                })
            elif p_start is not None and p_end is not None and f_period is not None:
                # Valid blank planned_date row
                parsed_rows.append({
                    "period_start": p_start,
                    "period_end": p_end,
                    "planned_date": None,
                    "fiscal_period": f_period,
                })

    if not parsed_any_table and not parsed_rows:
        return KapCalendarParseResult(rows=[], warnings=["Unrecognized table structure in disclosureBody"], parser_failed=True)

    return KapCalendarParseResult(
        rows=parsed_rows,
        warnings=warnings,
        parser_failed=False,
        positional_fallback_count=positional_fallback_count
    )


class KapDisclosureEnvelope(BaseModel):
    disclosure: Dict[str, Any]
    source_oids: Set[str] = Field(default_factory=set)
    query_from: str
    query_to: str
    query_complete: bool = True


class KapPartitionResult(BaseModel):
    envelopes: List[KapDisclosureEnvelope] = Field(default_factory=list)
    completed_oids: Set[str] = Field(default_factory=set)
    incomplete_oids: Set[str] = Field(default_factory=set)
    saturated_oids: Set[str] = Field(default_factory=set)
    warnings: List[str] = Field(default_factory=list)


class KapEarningsProvider(BaseEarningsProvider):
    """BIST100 Earnings Provider fetching production data from KAP frontend JSON/XHR endpoints."""

    BASE_URL = "https://www.kap.org.tr/tr/api"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": "https://www.kap.org.tr/"
    }

    def __init__(
        self,
        concurrency: int = 2
    ):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.sleep_backoff_fn = asyncio.sleep

    def _load_bist100_symbols(self) -> List[str]:
        try:
            with get_db_connection() as conn:
                return InstrumentCatalogRepository.get_active_symbols(
                    conn, market="BIST100", asset_type="stock"
                )
        except Exception as exc:
            logger.error("Failed to load BIST100 catalog from Supabase: %s", type(exc).__name__)
            return []

    def load_member_mappings(self) -> Dict[str, Dict[str, Any]]:
        try:
            with get_db_connection() as conn:
                return KapMemberMappingRepository.get_active_bist_mappings(conn)
        except Exception as exc:
            logger.warning("Failed to load KAP member mappings from Supabase: %s", type(exc).__name__)
            return {}

    def save_member_mappings(self, mappings: Dict[str, Dict[str, Any]]) -> None:
        try:
            with get_db_connection() as conn:
                with conn.transaction():
                    written = KapMemberMappingRepository.upsert_bist_mappings(conn, mappings)
            logger.info("Saved %d KAP member mappings to Supabase", written)
        except Exception as exc:
            logger.error("Failed to save KAP member mappings to Supabase: %s", type(exc).__name__)

    def resolve_best_member_candidate(
        self,
        symbol: str,
        items: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Strict deterministic member candidate resolution."""
        if not items:
            return None

        exact_matches = []
        for item in items:
            code = str(item.get("companyCode") or item.get("code") or "").strip().upper()
            if code == symbol:
                exact_matches.append(item)

        if len(exact_matches) == 1:
            return exact_matches[0]

        return None

    async def get_member_mappings(
        self,
        client: httpx.AsyncClient,
        symbols: List[str]
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str], List[str], List[str], Dict[str, str]]:
        """Resolve symbol -> KAP member -> mkkMemberOid."""
        warnings: List[str] = []
        mappings = self.load_member_mappings()
        updated = False
        resolution_map: Dict[str, str] = {}

        target_set = set(symbols)
        extra_keys = [k for k in mappings.keys() if k not in target_set]
        if extra_keys:
            warnings.append(f"Extra/stale mappings in Supabase excluded from target scan: {extra_keys}")

        oid_to_symbols: Dict[str, List[str]] = {}
        for sym in symbols:
            if sym in mappings and mappings[sym].get("mkk_member_oid"):
                oid = mappings[sym]["mkk_member_oid"]
                oid_to_symbols.setdefault(oid, []).append(sym)

        ambiguous_symbols: Set[str] = set()
        for oid, syms in oid_to_symbols.items():
            if len(syms) > 1:
                warnings.append(f"Duplicate OID {oid} mapped to multiple BIST symbols: {syms}")
                ambiguous_symbols.update(syms)

        mapped_symbols = [s for s in symbols if s in mappings and mappings[s].get("mkk_member_oid") and s not in ambiguous_symbols]
        missing_symbols = [s for s in symbols if s not in mapped_symbols and s not in ambiguous_symbols]

        if missing_symbols:
            logger.info(f"Attempting live KAP lookup for missing {len(missing_symbols)} symbols: {missing_symbols}")
            for sym in missing_symbols:
                url = f"{self.BASE_URL}/member/filter/{sym}"
                data = await self._http_request(client, "GET", url, is_search=True)
                if data and isinstance(data, list):
                    candidate = self.resolve_best_member_candidate(sym, data)
                    if candidate and candidate.get("mkkMemberOid"):
                        mappings[sym] = {
                            "symbol": sym,
                            "company_name": candidate.get("title", sym),
                            "mkk_member_oid": candidate.get("mkkMemberOid"),
                            "last_verified_at": datetime.now(timezone.utc).isoformat()
                        }
                        updated = True
                        mapped_symbols.append(sym)
                    else:
                        warnings.append(f"KAP member lookup unresolved/ambiguous for symbol {sym} (candidate count: {len(data)})")
                else:
                    warnings.append(f"KAP member lookup failed for symbol {sym}")

        if updated:
            self.save_member_mappings(mappings)

        final_resolved: List[str] = []
        final_unresolved: List[str] = []

        for sym in symbols:
            if sym in ambiguous_symbols:
                resolution_map[sym] = "ambiguous"
                final_unresolved.append(sym)
            elif sym in mappings and mappings[sym].get("mkk_member_oid"):
                resolution_map[sym] = "resolved"
                final_resolved.append(sym)
            else:
                resolution_map[sym] = "unresolved"
                final_unresolved.append(sym)

        return mappings, warnings, final_resolved, final_unresolved, resolution_map

    async def _http_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        json_payload: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        metrics: Optional[KapEarningsFetchMetrics] = None,
        is_search: bool = False
    ) -> Optional[Any]:
        """Controlled HTTP request execution with exponential backoff (1s, 2s, 4s)."""
        async with self.semaphore:
            if metrics:
                if is_search:
                    metrics.search_request_count += 1
                else:
                    metrics.detail_request_count += 1

            for attempt in range(max_retries + 1):
                try:
                    if method.upper() == "GET":
                        resp = await client.get(url, headers=self.HEADERS, timeout=15.0)
                    else:
                        resp = await client.post(url, headers=self.HEADERS, json=json_payload, timeout=15.0)

                    if resp.status_code == 200:
                        try:
                            return resp.json()
                        except json.JSONDecodeError as e:
                            logger.error(f"Invalid JSON returned from {url}: {e}")
                            if metrics:
                                if is_search:
                                    metrics.search_failure_count += 1
                                else:
                                    metrics.detail_failure_count += 1
                            return None
                    elif resp.status_code in (429, 500, 502, 503, 504):
                        if attempt < max_retries:
                            if metrics:
                                if is_search:
                                    metrics.search_retry_count += 1
                                else:
                                    metrics.detail_retry_count += 1
                            logger.warning(f"KAP HTTP {resp.status_code} for {url} (attempt {attempt+1}/{max_retries+1})")
                            await self.sleep_backoff_fn(1.0 * (2 ** attempt))
                            continue
                        else:
                            if metrics:
                                if is_search:
                                    metrics.search_failure_count += 1
                                else:
                                    metrics.detail_failure_count += 1
                            return None
                    else:
                        logger.error(f"KAP Non-retriable HTTP {resp.status_code} error for {url}")
                        if metrics:
                            if is_search:
                                metrics.search_failure_count += 1
                            else:
                                metrics.detail_failure_count += 1
                        return None
                except (httpx.TimeoutException, httpx.RequestError) as e:
                    if attempt < max_retries:
                        if metrics:
                            if is_search:
                                metrics.search_retry_count += 1
                            else:
                                metrics.detail_retry_count += 1
                        logger.warning(f"KAP request exception {e} for {url} (attempt {attempt+1}/{max_retries+1})")
                        await self.sleep_backoff_fn(1.0 * (2 ** attempt))
                        continue
                    else:
                        if metrics:
                            if is_search:
                                metrics.search_failure_count += 1
                            else:
                                metrics.detail_failure_count += 1
                        return None

            return None

    async def _fetch_by_criteria_partitioned(
        self,
        client: httpx.AsyncClient,
        oid_group: List[str],
        from_date_str: str,
        to_date_str: str,
        metrics: KapEarningsFetchMetrics
    ) -> KapPartitionResult:
        """Adaptive byCriteria query partitioner carrying provenance and excluding saturated leaf rows."""
        result = KapPartitionResult()

        payload = {
            "fromDate": from_date_str,
            "toDate": to_date_str,
            "mkkMemberOidList": oid_group
        }
        url = f"{self.BASE_URL}/disclosure/members/byCriteria"
        res = await self._http_request(client, "POST", url, json_payload=payload, metrics=metrics, is_search=True)

        if res is None or not isinstance(res, list):
            result.incomplete_oids.update(oid_group)
            msg = f"KAP byCriteria query failed for OID chunk ({len(oid_group)} OIDs) between {from_date_str} and {to_date_str}"
            result.warnings.append(msg)
            return result

        rows_len = len(res)

        if rows_len < 2000:
            metrics.adaptive_leaf_count += 1
            metrics.max_leaf_row_count = max(metrics.max_leaf_row_count, rows_len)
            result.completed_oids.update(oid_group)
            for row in res:
                result.envelopes.append(
                    KapDisclosureEnvelope(
                        disclosure=row,
                        source_oids=set(oid_group),
                        query_from=from_date_str,
                        query_to=to_date_str,
                        query_complete=True
                    )
                )
            return result

        d_from = date.fromisoformat(from_date_str)
        d_to = date.fromisoformat(to_date_str)

        if d_from < d_to:
            mid = d_from + (d_to - d_from) // 2
            mid_str = mid.isoformat()
            mid_plus_1_str = (mid + timedelta(days=1)).isoformat()

            left_res = await self._fetch_by_criteria_partitioned(client, oid_group, from_date_str, mid_str, metrics)
            right_res = await self._fetch_by_criteria_partitioned(client, oid_group, mid_plus_1_str, to_date_str, metrics)

            result.envelopes = left_res.envelopes + right_res.envelopes
            result.completed_oids = left_res.completed_oids | right_res.completed_oids
            result.incomplete_oids = left_res.incomplete_oids | right_res.incomplete_oids
            result.saturated_oids = left_res.saturated_oids | right_res.saturated_oids
            result.warnings = left_res.warnings + right_res.warnings
            return result
        elif len(oid_group) > 1:
            half = len(oid_group) // 2
            g1, g2 = oid_group[:half], oid_group[half:]

            left_res = await self._fetch_by_criteria_partitioned(client, g1, from_date_str, to_date_str, metrics)
            right_res = await self._fetch_by_criteria_partitioned(client, g2, from_date_str, to_date_str, metrics)

            result.envelopes = left_res.envelopes + right_res.envelopes
            result.completed_oids = left_res.completed_oids | right_res.completed_oids
            result.incomplete_oids = left_res.incomplete_oids | right_res.incomplete_oids
            result.saturated_oids = left_res.saturated_oids | right_res.saturated_oids
            result.warnings = left_res.warnings + right_res.warnings
            return result
        else:
            # Single day, Single OID >= 2000 rows -> Hard Saturated Incomplete Leaf
            # CRITICAL RULE: EXCLUDE TRUNCATED ROWS ENTIRELY! Do NOT append envelopes.
            metrics.saturated_leaf_count += 1
            metrics.max_leaf_row_count = max(metrics.max_leaf_row_count, rows_len)
            msg = f"KAP 2000-row limit saturated on single day {from_date_str} for OID {oid_group[0]}. Truncated rows excluded."
            logger.error(msg)
            result.warnings.append(msg)
            result.saturated_oids.update(oid_group)
            result.incomplete_oids.update(oid_group)
            result.envelopes = []
            return result

    def _extract_matched_symbol(
        self,
        disc: Dict[str, Any],
        oid_to_symbol: Dict[str, str],
        target_symbols: Set[str],
        metrics: KapEarningsFetchMetrics
    ) -> Tuple[Optional[str], Optional[str]]:
        """Token-safe stockCode / OID mapping to target BIST symbol."""
        oid = disc.get("mkkMemberOid")
        if oid and oid in oid_to_symbol:
            return oid_to_symbol[oid], None

        raw_stocks = disc.get("stockCodes") or disc.get("relatedStocks")
        extracted_tokens: List[str] = []

        if isinstance(raw_stocks, str):
            parts = re.split(r'[,;\s]+', raw_stocks)
            for p in parts:
                tok = p.strip().upper().replace(".IS", "")
                if tok:
                    extracted_tokens.append(tok)
        elif isinstance(raw_stocks, list):
            for item in raw_stocks:
                tok = str(item).strip().upper().replace(".IS", "")
                if tok:
                    extracted_tokens.append(tok)

        matched_target_tokens = [t for t in set(extracted_tokens) if t in target_symbols]

        if len(matched_target_tokens) == 1:
            return matched_target_tokens[0], None
        elif len(matched_target_tokens) > 1:
            metrics.mapping_failure_count += 1
            return None, f"Ambiguous multi-stock disclosure for symbols {matched_target_tokens}"
        else:
            metrics.unmappable_disclosure_count += 1
            return None, f"Unmappable disclosure for stockCodes '{raw_stocks}'"

    async def fetch_calendar(
        self,
        coverage_start: date,
        coverage_end: date,
        published_from: date,
        published_to: date
    ) -> KapEarningsFetchResult:
        """Production KAP Earnings Fetcher with structured per-symbol results and rigorous data completeness guarantees."""
        metrics = KapEarningsFetchMetrics()
        warnings: List[str] = []

        symbols = self._load_bist100_symbols()
        metrics.input_symbol_count = len(symbols)
        target_set = set(symbols)

        if not symbols:
            warnings.append("No active BIST100 symbols available in Supabase instruments")
            return KapEarningsFetchResult(status="failed", warnings=warnings, metrics=metrics)

        incomplete_symbols: Set[str] = set()

        symbol_states: Dict[str, Dict[str, Any]] = {
            sym: {
                "member_resolution_status": "resolved",
                "refresh_status": "success",
                "calendar_status": "checked",
                "scheduled_events": [],
                "is_complete": True,
                "warnings": []
            }
            for sym in symbols
        }

        async with httpx.AsyncClient() as client:
            # 1. Member Resolution & Mapping Verification
            member_mappings, lookup_warnings, resolved_syms, unresolved_syms, resolution_map = await self.get_member_mappings(client, symbols)
            warnings.extend(lookup_warnings)

            metrics.resolved_symbol_count = len(resolved_syms)
            metrics.unresolved_symbol_count = len(unresolved_syms)

            for sym in symbols:
                res_status = resolution_map.get(sym, "unresolved")
                symbol_states[sym]["member_resolution_status"] = res_status
                if res_status != "resolved":
                    incomplete_symbols.add(sym)
                    symbol_states[sym]["refresh_status"] = "failed"
                    symbol_states[sym]["calendar_status"] = "unavailable"
                    symbol_states[sym]["is_complete"] = False
                    symbol_states[sym]["warnings"].append(f"Member resolution {res_status}")

            oid_to_symbol: Dict[str, str] = {}
            valid_oids: List[str] = []
            for sym in resolved_syms:
                if sym not in incomplete_symbols:
                    oid = member_mappings.get(sym, {}).get("mkk_member_oid")
                    if oid:
                        oid_to_symbol[oid] = sym
                        valid_oids.append(oid)

            if not valid_oids:
                warnings.append("No valid KAP member OIDs resolved for any target symbol")
                sym_results = [
                    KapSymbolEarningsStatus(
                        symbol=sym,
                        member_resolution_status=symbol_states[sym]["member_resolution_status"],
                        refresh_status="failed",
                        calendar_status="unavailable",
                        scheduled_event_count=0,
                        is_complete=False,
                        warnings=symbol_states[sym]["warnings"]
                    )
                    for sym in symbols
                ]
                return KapEarningsFetchResult(
                    status="failed",
                    warnings=warnings,
                    resolved_symbols=resolved_syms,
                    unresolved_symbols=unresolved_syms,
                    symbol_results=sym_results,
                    metrics=metrics
                )

            # 2. Partitioned byCriteria Disclosure Queries
            chunk_size = 15
            oid_chunks = [valid_oids[i:i + chunk_size] for i in range(0, len(valid_oids), chunk_size)]

            from_date_str = published_from.isoformat()
            to_date_str = published_to.isoformat()

            raw_envelopes: List[KapDisclosureEnvelope] = []
            incomplete_oids: Set[str] = set()

            for chunk in oid_chunks:
                part_res = await self._fetch_by_criteria_partitioned(client, chunk, from_date_str, to_date_str, metrics)
                raw_envelopes.extend(part_res.envelopes)
                incomplete_oids.update(part_res.incomplete_oids)
                warnings.extend(part_res.warnings)

            for oid in incomplete_oids:
                sym = oid_to_symbol.get(oid)
                if sym and sym in symbol_states:
                    incomplete_symbols.add(sym)
                    symbol_states[sym]["refresh_status"] = "failed"
                    symbol_states[sym]["calendar_status"] = "unavailable"
                    symbol_states[sym]["is_complete"] = False
                    symbol_states[sym]["warnings"].append("byCriteria query failed or saturated for member OID")

            metrics.raw_disclosure_count = len(raw_envelopes)

            # 3. Disclosure Deduplication by disclosureIndex with Provenance Merge
            envelope_by_index: Dict[int, KapDisclosureEnvelope] = {}
            for env in raw_envelopes:
                d = env.disclosure
                idx = d.get("disclosureIndex")
                if not idx:
                    metrics.mapping_failure_count += 1
                    msg = f"Missing disclosureIndex in payload from OIDs {env.source_oids}"
                    warnings.append(msg)
                    for oid in env.source_oids:
                        sym = oid_to_symbol.get(oid)
                        if sym:
                            incomplete_symbols.add(sym)
                            symbol_states[sym]["refresh_status"] = "failed"
                            symbol_states[sym]["calendar_status"] = "unavailable"
                            symbol_states[sym]["is_complete"] = False
                            symbol_states[sym]["warnings"].append(msg)
                    continue

                if idx in envelope_by_index:
                    metrics.duplicate_disclosure_count += 1
                    envelope_by_index[idx].source_oids.update(env.source_oids)
                else:
                    envelope_by_index[idx] = env

            unique_envelopes = list(envelope_by_index.values())
            metrics.unique_disclosure_count = len(unique_envelopes)

            # 4. Strict Subject Filter ("Finansal Takvim" exact + summary fallback)
            calendar_envelopes: List[KapDisclosureEnvelope] = []
            for env in unique_envelopes:
                disc = env.disclosure
                subj_raw = disc.get("subject")
                subj_norm = normalize_subject(subj_raw)
                summ_norm = normalize_subject(disc.get("summary"))

                if subj_norm == "finansal takvim":
                    calendar_envelopes.append(env)
                    metrics.financial_calendar_disclosure_count += 1
                elif not subj_raw and "finansal takvim" in summ_norm:
                    calendar_envelopes.append(env)
                    metrics.financial_calendar_disclosure_count += 1
                    metrics.fallback_subject_count += 1
                    warnings.append(f"Used summary fallback for Financial Calendar disclosureIndex {disc.get('disclosureIndex')}")

            # 5. Attachment Detail Fetching & Parsing
            detail_responses: Dict[int, KapCalendarParseResult] = {}
            failed_detail_disclosures: Set[int] = set()

            for env in calendar_envelopes:
                disc_idx = env.disclosure.get("disclosureIndex")
                if not disc_idx or disc_idx in detail_responses:
                    continue

                det_url = f"{self.BASE_URL}/notification/attachment-detail/{disc_idx}"
                det_res = await self._http_request(client, "GET", det_url, metrics=metrics, is_search=False)

                if det_res and isinstance(det_res, list) and det_res:
                    body_list = det_res[0].get("disclosureBody", [])
                    body_html = "".join(body_list) if isinstance(body_list, list) else str(body_list)

                    parse_res = parse_kap_financial_calendar_html(body_html)
                    detail_responses[disc_idx] = parse_res
                    metrics.positional_fallback_count += parse_res.positional_fallback_count
                    warnings.extend(parse_res.warnings)

                    if parse_res.parser_failed:
                        metrics.parser_failure_count += 1
                        failed_detail_disclosures.add(disc_idx)
                    else:
                        metrics.calendar_row_count += len(parse_res.rows)
                else:
                    failed_detail_disclosures.add(disc_idx)
                    detail_responses[disc_idx] = KapCalendarParseResult(rows=[], warnings=[f"Failed to fetch detail for {disc_idx}"], parser_failed=True)

            # 6. Disclosure to Symbol Match & Event Construction
            grouped_period_records: Dict[Tuple[str, Optional[str], Optional[str]], List[Dict[str, Any]]] = {}

            for env in calendar_envelopes:
                disc = env.disclosure
                disc_idx = disc.get("disclosureIndex")
                if not disc_idx:
                    continue

                pub_date = normalize_publish_date(disc.get("publishDate"))
                if not pub_date:
                    metrics.mapping_failure_count += 1
                    msg = f"Invalid or missing publishDate in disclosure {disc_idx}"
                    warnings.append(msg)
                    for oid in env.source_oids:
                        sym = oid_to_symbol.get(oid)
                        if sym:
                            incomplete_symbols.add(sym)
                            symbol_states[sym]["refresh_status"] = "failed"
                            symbol_states[sym]["calendar_status"] = "unavailable"
                            symbol_states[sym]["is_complete"] = False
                            symbol_states[sym]["warnings"].append(msg)
                    continue

                matched_sym, match_err = self._extract_matched_symbol(disc, oid_to_symbol, target_set, metrics)
                if match_err:
                    warnings.append(f"Disclosure {disc_idx}: {match_err}")

                if not matched_sym:
                    # Unmappable disclosure provenance handling:
                    for oid in env.source_oids:
                        sym = oid_to_symbol.get(oid)
                        if sym:
                            incomplete_symbols.add(sym)
                            symbol_states[sym]["refresh_status"] = "failed"
                            symbol_states[sym]["calendar_status"] = "unavailable"
                            symbol_states[sym]["is_complete"] = False
                            symbol_states[sym]["warnings"].append(f"Unmappable disclosure {disc_idx} in provenance group for OID {oid}")
                    continue

                if disc_idx in failed_detail_disclosures or detail_responses.get(disc_idx, KapCalendarParseResult()).parser_failed:
                    incomplete_symbols.add(matched_sym)
                    symbol_states[matched_sym]["refresh_status"] = "failed"
                    symbol_states[matched_sym]["calendar_status"] = "unavailable"
                    symbol_states[matched_sym]["is_complete"] = False
                    symbol_states[matched_sym]["warnings"].append(f"Attachment detail or parser failed for disclosure {disc_idx}")
                    continue

                comp_name = member_mappings.get(matched_sym, {}).get("company_name", matched_sym)
                parse_res = detail_responses.get(disc_idx, KapCalendarParseResult())

                for period_data in parse_res.rows:
                    p_start = period_data.get("period_start")
                    p_end = period_data.get("period_end")
                    p_plan = period_data.get("planned_date")
                    f_period = period_data.get("fiscal_period")

                    key = (matched_sym, p_start, p_end)
                    record = {
                        "symbol": matched_sym,
                        "company_name": comp_name,
                        "disclosure_index": disc_idx,
                        "publish_date": pub_date,
                        "period_start": p_start,
                        "period_end": p_end,
                        "planned_date": p_plan,
                        "fiscal_period": f_period,
                    }
                    grouped_period_records.setdefault(key, []).append(record)

            # Resolve Latest-Wins & Blank-Date Preservation
            final_events: List[BistEarningsEvent] = []

            for key, rec_list in grouped_period_records.items():
                sym, p_start, p_end = key
                if sym in incomplete_symbols:
                    continue

                rec_list.sort(key=lambda r: (r["publish_date"] or "", r["disclosure_index"] or 0), reverse=True)

                chosen_planned_date: Optional[str] = None
                chosen_rec: Dict[str, Any] = rec_list[0]

                for rec in rec_list:
                    if rec["planned_date"]:
                        chosen_planned_date = rec["planned_date"]
                        chosen_rec = rec
                        break

                if len(rec_list) > 1:
                    metrics.superseded_event_count += (len(rec_list) - 1)

                if chosen_planned_date:
                    metrics.valid_planned_date_count += 1
                    f_year = date.fromisoformat(p_end).year if p_end else None
                    f_period = chosen_rec.get("fiscal_period")
                    event = BistEarningsEvent(
                        symbol=sym,
                        company_name=chosen_rec["company_name"],
                        fiscal_year=f_year,
                        fiscal_period=f_period,
                        earnings_date=chosen_planned_date,
                        period_start=p_start,
                        period_end=p_end,
                        publish_date=chosen_rec["publish_date"],
                        date_type="company_announced",
                        source="KAP",
                        source_type="financial_calendar",
                        disclosure_index=chosen_rec["disclosure_index"],
                        source_url=f"https://www.kap.org.tr/tr/Bildirim/{chosen_rec['disclosure_index']}"
                    )
                    final_events.append(event)
                else:
                    metrics.invalid_planned_date_count += 1

            # 7. Apply 91-Day Rolling Output Coverage Filter [coverage_start, coverage_end]
            cov_start_str = coverage_start.isoformat()
            cov_end_str = coverage_end.isoformat()

            # EXCLUDE incomplete symbols from final_events!
            filtered_events: List[BistEarningsEvent] = [
                ev for ev in final_events
                if cov_start_str <= ev.earnings_date <= cov_end_str
                and ev.symbol not in incomplete_symbols
            ]
            filtered_events.sort(key=lambda ev: (ev.earnings_date, ev.symbol))
            metrics.normalized_event_count = len(final_events)
            metrics.final_event_count = len(filtered_events)

            # 8. Construct Per-Symbol Results & Global Warning Summary
            events_by_symbol: Dict[str, List[BistEarningsEvent]] = {}
            for ev in filtered_events:
                events_by_symbol.setdefault(ev.symbol, []).append(ev)

            symbol_results_list: List[KapSymbolEarningsStatus] = []
            completed_symbol_count = 0

            for sym in symbols:
                st = symbol_states[sym]
                sym_events = events_by_symbol.get(sym, [])

                if sym in incomplete_symbols or not st["is_complete"]:
                    st["refresh_status"] = "failed"
                    st["calendar_status"] = "unavailable"
                    st["is_complete"] = False
                    st["scheduled_event_count"] = 0
                else:
                    st["refresh_status"] = "success"
                    st["is_complete"] = True
                    if sym_events:
                        st["calendar_status"] = "scheduled"
                        st["scheduled_event_count"] = len(sym_events)
                    else:
                        st["calendar_status"] = "no_announced_date_in_coverage"
                        st["scheduled_event_count"] = 0
                    completed_symbol_count += 1

                symbol_results_list.append(
                    KapSymbolEarningsStatus(
                        symbol=sym,
                        member_resolution_status=st["member_resolution_status"],
                        refresh_status=st["refresh_status"],
                        calendar_status=st["calendar_status"],
                        scheduled_event_count=st["scheduled_event_count"],
                        is_complete=st["is_complete"],
                        warnings=st["warnings"]
                    )
                )

            unavailable_syms = sorted([sym for sym, st in symbol_states.items() if not st["is_complete"]])
            if unavailable_syms:
                summary_warn = f"Unavailable KAP symbols: {', '.join(unavailable_syms)}"
                if summary_warn not in warnings:
                    warnings.insert(0, summary_warn)

            if completed_symbol_count == len(symbols):
                overall_status = "success"
            elif completed_symbol_count > 0:
                overall_status = "partial"
            else:
                overall_status = "failed"

            return KapEarningsFetchResult(
                status=overall_status,
                events=filtered_events,
                normalized_events=final_events,
                warnings=warnings,
                resolved_symbols=resolved_syms,
                unresolved_symbols=unresolved_syms,
                symbol_results=symbol_results_list,
                metrics=metrics
            )

    async def get_earnings_result(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> KapEarningsFetchResult:
        """Fetch KAP earnings result for given target date range with 365-day lookback."""
        tz = ZoneInfo(settings.TIMEZONE)
        as_of_dt = datetime.now(tz)
        as_of_date = as_of_dt.date()

        coverage_start = start_date.date() if isinstance(start_date, datetime) else start_date
        coverage_end = end_date.date() if isinstance(end_date, datetime) else end_date

        published_from = as_of_date - timedelta(days=365)
        published_to = as_of_date

        return await self.fetch_calendar(
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            published_from=published_from,
            published_to=published_to
        )

    async def get_earnings_events(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> Tuple[List[BistEarningsEvent], List[str]]:
        """Backward-compatible wrapper using get_earnings_result and returning Tuple[events, warnings]."""
        res = await self.get_earnings_result(start_date, end_date)

        warnings_out = list(res.warnings)
        unavailable_syms = sorted([s.symbol for s in res.symbol_results if not s.is_complete])

        if res.status == "partial":
            summary_warn = f"KAP earnings fetch completed partially; unavailable symbols: {', '.join(unavailable_syms)}"
            if summary_warn not in warnings_out:
                warnings_out.insert(0, summary_warn)
        elif res.status == "failed":
            summary_warn = f"KAP earnings fetch failed; no complete symbol results available. Unavailable symbols: {', '.join(unavailable_syms)}"
            if summary_warn not in warnings_out:
                warnings_out.insert(0, summary_warn)

        return res.events, warnings_out
