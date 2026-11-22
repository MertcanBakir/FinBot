import asyncio
import logging
from datetime import datetime
from typing import Any, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from app.core.database import get_db_connection
from app.models.earnings import NasdaqEarningsEvent
from app.providers.earnings.base import BaseEarningsProvider
from app.repositories.instrument_catalog_repository import InstrumentCatalogRepository

logger = logging.getLogger(__name__)

def normalize_session(raw_timing: Any) -> Tuple[str, str]:
    """Normalize timing/session values into (session_key, session_label).
    
    Allowed session keys: pre_market, after_hours, time_not_supplied
    """
    if raw_timing is None or pd.isna(raw_timing):
        return "time_not_supplied", "Time Not Supplied"

    timing_str = str(raw_timing).strip().lower()

    if any(k in timing_str for k in ["pre-market", "before market open", "bmo", "pre_market", "pre market"]):
        return "pre_market", "Pre-Market"
    elif any(k in timing_str for k in ["after hours", "after market close", "amc", "after_hours", "post market"]):
        return "after_hours", "After Hours"
    else:
        return "time_not_supplied", "Time Not Supplied"

def parse_date_val(val: Any) -> Optional[str]:
    """Safely extract YYYY-MM-DD string from date/datetime object or string."""
    if val is None or pd.isna(val):
        return None
    try:
        if isinstance(val, (list, tuple)):
            if not val:
                return None
            val = val[0]
        if hasattr(val, "date"):
            val = val.date()
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        return str(val)
    except Exception:
        return None

class YahooEarningsProvider(BaseEarningsProvider):
    """NASDAQ100 Earnings Provider fetching global earnings calendar from Yahoo Finance / yfinance."""

    def _load_nasdaq100_symbols(self) -> Tuple[List[str], set]:
        try:
            with get_db_connection() as conn:
                clean_list = InstrumentCatalogRepository.get_active_symbols(
                    conn, market="NASDAQ100", asset_type="stock"
                )
            return clean_list, set(clean_list)
        except Exception as exc:
            logger.error("Failed to load NASDAQ100 catalog from Supabase: %s", type(exc).__name__)
            return [], set()

    def _fetch_global_calendar_sync(
        self,
        start_str: str,
        end_str: str,
        whitelist_set: set
    ) -> Tuple[List[NasdaqEarningsEvent], List[str]]:
        warnings: List[str] = []
        events: List[NasdaqEarningsEvent] = []
        
        try:
            calendars = yf.Calendars()
            offset = 0
            limit = 100
            max_pages = 10

            processed_symbols_dates = set()

            for _page in range(max_pages):
                try:
                    df = calendars.get_earnings_calendar(
                        start=start_str,
                        end=end_str,
                        filter_most_active=False,
                        limit=limit,
                        offset=offset
                    )
                except Exception as page_err:
                    warnings.append(f"Yahoo calendar fetch error at offset {offset}: {page_err}")
                    break

                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    break

                # Determine column names defensively
                col_map = {col.lower(): col for col in df.columns}
                
                sym_col = col_map.get("symbol") or col_map.get("ticker")
                comp_col = col_map.get("company") or col_map.get("company name") or col_map.get("name")
                date_col = col_map.get("earnings date") or col_map.get("date") or col_map.get("earnings_date") or col_map.get("event start date")
                timing_col = col_map.get("timing") or col_map.get("event time") or col_map.get("earnings call time") or col_map.get("session")

                if not date_col:
                    warnings.append(f"Unexpected dataframe columns from Yahoo Finance: {df.columns.tolist()}")
                    break

                for idx_val, row in df.iterrows():
                    raw_sym = ""
                    if sym_col and pd.notna(row[sym_col]):
                        raw_sym = str(row[sym_col]).strip().upper()
                    elif idx_val is not None and not str(idx_val).isdigit():
                        raw_sym = str(idx_val).strip().upper()

                    if not raw_sym or raw_sym not in whitelist_set:
                        continue

                    raw_date = row[date_col] if pd.notna(row[date_col]) else None
                    earnings_date = parse_date_val(raw_date)
                    if not earnings_date:
                        continue

                    # Deduplicate (symbol, earnings_date)
                    if (raw_sym, earnings_date) in processed_symbols_dates:
                        continue
                    processed_symbols_dates.add((raw_sym, earnings_date))

                    company_name = str(row[comp_col]).strip() if comp_col and pd.notna(row[comp_col]) else raw_sym
                    raw_timing = row[timing_col] if timing_col and pd.notna(row[timing_col]) else None

                    session_key, session_label = normalize_session(raw_timing)

                    events.append(NasdaqEarningsEvent(
                        symbol=raw_sym,
                        company_name=company_name,
                        earnings_date=earnings_date,
                        session=session_key,
                        session_label=session_label,
                        source="Yahoo Finance"
                    ))

                if len(df) < limit:
                    break

                offset += limit

        except Exception as e:
            logger.error(f"Failed to query Yahoo Finance earnings calendar: {e}", exc_info=True)
            warnings.append(f"Yahoo Finance earnings query failed: {e}")

        return events, warnings

    async def get_earnings_events(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> Tuple[List[NasdaqEarningsEvent], List[str]]:
        symbols_list, whitelist_set = self._load_nasdaq100_symbols()
        if not whitelist_set:
            return [], ["No NASDAQ100 symbols loaded"]

        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # Run blocking pandas/yfinance in executor
        events, warnings = await asyncio.to_thread(
            self._fetch_global_calendar_sync,
            start_str,
            end_str,
            whitelist_set
        )

        return events, warnings
