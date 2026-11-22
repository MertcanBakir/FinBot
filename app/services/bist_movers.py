import asyncio
import logging
import math
from typing import List, Tuple

import yfinance as yf

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.market import BistMovers, MarketMover
from app.repositories.instrument_catalog_repository import InstrumentCatalogRepository

logger = logging.getLogger(__name__)

def _load_symbols() -> List[str]:
    try:
        with get_db_connection() as conn:
            return InstrumentCatalogRepository.get_active_symbols(
                conn,
                market="BIST100",
                asset_type="stock",
                yahoo_suffix=True,
            )
    except Exception as exc:
        logger.error("Failed to load BIST100 movers catalog from Supabase: %s", type(exc).__name__)
        return []

def _check_corporate_action_per_symbol(sym: str) -> bool:
    """
    Per-symbol verification called ONLY when a ticker displays suspicious price movement.
    Checks yfinance Ticker history/actions/splits for corporate actions or splits.
    Returns True if a corporate action / split is detected.
    """
    try:
        ticker = yf.Ticker(sym)
        # Check splits
        splits = getattr(ticker, "splits", None)
        if splits is not None and not splits.empty:
            recent_splits = splits.tail(5)
            if any(val != 0 for val in recent_splits):
                return True

        # Check actions
        actions = getattr(ticker, "actions", None)
        if actions is not None and not actions.empty:
            recent_actions = actions.tail(5)
            if "Stock Splits" in recent_actions.columns and any(val != 0 for val in recent_actions["Stock Splits"]):
                return True

        # Fallback: check 10d history
        hist = ticker.history(period="10d", interval="1d", auto_adjust=True, actions=True)
        if hist is not None and not hist.empty:
            if "Stock Splits" in hist.columns and any(val != 0 for val in hist["Stock Splits"].dropna()):
                return True

    except Exception as e:
        logger.debug(f"Per-symbol verification error for {sym}: {e}")
    return False

def _fetch_sync_movers() -> Tuple[BistMovers, List[str]]:
    symbols = _load_symbols()
    warnings: List[str] = []
    movers_list: List[MarketMover] = []

    total_count = len(symbols)
    valid_count = 0
    ca_skipped = 0
    susp_skipped = 0
    insuff_skipped = 0

    try:
        # Batch fetch recent 5d daily data using same auto_adjust strategy
        data = yf.download(symbols, period="5d", interval="1d", progress=False, group_by="ticker", auto_adjust=True, actions=True)
        if data is None or data.empty:
            warnings.append("Failed to download BIST 100 batch ticker data.")
            logger.info("BIST movers scan completed: total=%d, valid=0, corporate_action_skipped=0, suspicious_change_skipped=0, insufficient_data=%d", total_count, total_count)
            return BistMovers(gainers=[], losers=[]), warnings

        for sym in symbols:
            try:
                # Extract ticker dataframe from batch result
                if isinstance(data.columns, tuple) or hasattr(data.columns, "levels"):
                    ticker_df = data.get(sym)
                else:
                    ticker_df = data

                if ticker_df is None or ticker_df.empty:
                    insuff_skipped += 1
                    warnings.append(f"{sym} skipped: insufficient adjusted price history.")
                    continue

                if "Close" not in ticker_df.columns:
                    insuff_skipped += 1
                    warnings.append(f"{sym} skipped: insufficient adjusted price history.")
                    continue

                # Clean NaN rows from auto-adjusted Close series
                close_series = ticker_df["Close"].dropna()
                if len(close_series) < 2:
                    insuff_skipped += 1
                    warnings.append(f"{sym} skipped: insufficient adjusted price history.")
                    continue

                current_close = float(close_series.iloc[-1])
                previous_close = float(close_series.iloc[-2])

                # Hard Sanity Filter 1: Valid numeric finite prices > 0
                if not (math.isfinite(current_close) and math.isfinite(previous_close) and current_close > 0 and previous_close > 0):
                    insuff_skipped += 1
                    warnings.append(f"{sym} skipped: non-finite price data.")
                    continue

                # Calculate daily change percent strictly from same auto-adjusted series
                change_pct = round(((current_close / previous_close) - 1.0) * 100.0, 2)

                if not math.isfinite(change_pct):
                    insuff_skipped += 1
                    warnings.append(f"{sym} skipped: non-finite price data.")
                    continue

                # Hard Sanity Filter 2 & Corporate Action Verification
                abs_change = abs(change_pct)
                max_allowed_change = getattr(settings, "BIST_MOVER_MAX_ABS_CHANGE_PERCENT", 15.0)

                if abs_change > max_allowed_change:
                    # Trigger per-symbol verification to check for corporate action / split
                    has_ca = _check_corporate_action_per_symbol(sym)
                    if has_ca:
                        ca_skipped += 1
                        msg = f"{sym} excluded from movers: corporate action or split detected."
                        logger.warning(msg)
                        warnings.append(f"{sym} skipped: corporate action detected.")
                    else:
                        susp_skipped += 1
                        msg = f"{sym} excluded from movers: suspicious daily change {change_pct:.2f}%."
                        logger.warning(msg)
                        warnings.append(f"{sym} skipped: suspicious change {change_pct:.2f}%.")
                    continue

                # Valid mover
                clean_sym = sym.replace(".IS", "")
                movers_list.append(MarketMover(
                    symbol=clean_sym,
                    price=round(current_close, 2),
                    change_percent=change_pct
                ))
                valid_count += 1

            except Exception as e:
                logger.debug(f"Error processing mover {sym}: {e}")
                insuff_skipped += 1
                warnings.append(f"{sym} skipped: processing error.")
                continue

    except Exception as e:
        logger.error(f"Error in batch downloading BIST movers: {e}")
        warnings.append(f"Error fetching BIST movers: {str(e)}")

    # Log summary of movers scan
    logger.info(
        "BIST movers scan completed: total=%d, valid=%d, corporate_action_skipped=%d, suspicious_change_skipped=%d, insufficient_data=%d",
        total_count, valid_count, ca_skipped, susp_skipped, insuff_skipped
    )

    # Gainers: descending sort, ONLY positive change_percent > 0
    gainers = [m for m in movers_list if m.change_percent is not None and m.change_percent > 0]
    gainers.sort(key=lambda x: x.change_percent, reverse=True)

    # Losers: ascending sort, ONLY negative change_percent < 0
    losers = [m for m in movers_list if m.change_percent is not None and m.change_percent < 0]
    losers.sort(key=lambda x: x.change_percent)

    return BistMovers(gainers=gainers[:3], losers=losers[:3]), warnings

class BistMoversService:
    async def get_movers(self) -> Tuple[BistMovers, List[str]]:
        """Runs the batch mover calculation in a thread pool."""
        return await asyncio.to_thread(_fetch_sync_movers)

bist_movers_service = BistMoversService()
