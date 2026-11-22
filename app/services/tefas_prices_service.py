import logging
import math
from collections import defaultdict
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import settings
from app.models.tefas_prices import (
    TefasCatalogObservation,
    TefasCatalogObservationResult,
    TefasCatalogReconciliationPlan,
    TefasDbInstrument,
    TefasNormalizationResult,
    TefasNormalizedPrice,
    TefasPriceSyncItem,
)

logger = logging.getLogger(__name__)

PRICE_SCALE = Decimal("0.000000000001")    # 12 decimal places (numeric(30, 12))
PERCENT_SCALE = Decimal("0.00000001")      # 8 decimal places (numeric(20, 8))


def plan_price_upserts(
    stored_map: Dict[UUID, dict],
    incoming_items: Sequence[TefasPriceSyncItem],
) -> Tuple[List[TefasPriceSyncItem], int, int]:
    """Calculate the exact list of items to upsert and count unchanged / stale skipped items.

    Enforces same-day merge rules:
    - If incoming date is newer than stored: write incoming (do not carry forward old day's change fields).
    - If incoming date matches stored:
      - If incoming previous_close is None and stored previous_close exists:
        preserve stored previous_close; recalculate changes if incoming price differs.
      - If incoming previous_close exists: authoritative update.
      - Clear stale flags (is_stale=False, stale_reason=None).
      - If all fields match stored: mark as unchanged (no-op).
    - If incoming date is older than stored: skip as older (stale_skipped_count += 1).

    Returns:
        (to_upsert_items, unchanged_count, stale_skipped_count)
    """
    to_upsert: List[TefasPriceSyncItem] = []
    unchanged_count = 0
    stale_skipped_count = 0

    for item in incoming_items:
        stored = stored_map.get(item.instrument_id)
        if stored is None:
            to_upsert.append(item)
            continue

        inc_at = item.price_at
        stored_at = stored["price_at"]

        if inc_at > stored_at:
            to_upsert.append(item)
        elif inc_at == stored_at:
            # Same day merge rules
            if item.previous_close is not None:
                eff_prev_close = item.previous_close
            else:
                eff_prev_close = stored.get("previous_close")

            if eff_prev_close is not None and eff_prev_close > 0:
                eff_prev_p = eff_prev_close.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                eff_change_amt = (item.price - eff_prev_p).quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                eff_change_pct = (((item.price - eff_prev_p) / eff_prev_p) * Decimal("100")).quantize(
                    PERCENT_SCALE, rounding=ROUND_HALF_UP
                )
            else:
                eff_prev_p = None
                eff_change_amt = None
                eff_change_pct = None

            effective_item = item.model_copy(
                update={
                    "previous_close": eff_prev_p,
                    "change_amount": eff_change_amt,
                    "change_percent": eff_change_pct,
                    "is_stale": False,
                    "stale_reason": None,
                }
            )

            is_different = (
                effective_item.price != stored["price"]
                or effective_item.previous_close != stored["previous_close"]
                or effective_item.change_amount != stored["change_amount"]
                or effective_item.change_percent != stored["change_percent"]
                or effective_item.currency != stored["currency"]
                or effective_item.provider_code != stored["provider_code"]
                or stored["is_stale"] is True
                or stored["stale_reason"] is not None
            )

            if is_different:
                to_upsert.append(effective_item)
            else:
                unchanged_count += 1
        else:
            # Incoming date is older than stored date
            stale_skipped_count += 1

    return to_upsert, unchanged_count, stale_skipped_count


class TefasPricesService:
    """Validate raw TEFAS pricing data and align dynamically with active FinBot instruments."""

    def __init__(self, timezone_name: str = settings.TIMEZONE):
        self.tz = ZoneInfo(timezone_name)

    def extract_catalog_observations(
        self,
        df: pd.DataFrame,
    ) -> TefasCatalogObservationResult:
        """Extract observed YAT fund codes, names, and track unidentifiable catalog row count.

        Returns:
            TefasCatalogObservationResult containing observed_funds, conflicting_codes,
            warnings, identity_invalid_count, and identity_complete flag.
        """
        warnings: List[str] = []
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) == 0:
            return TefasCatalogObservationResult(
                observed_funds={},
                conflicting_codes=set(),
                warnings=["TEFAS returned empty or invalid DataFrame for catalog observations."],
                identity_invalid_count=0,
                identity_complete=True,
            )

        records = df.to_dict(orient="records")
        seen_names_by_code: Dict[str, Set[str]] = {}
        identity_invalid_count = 0

        for idx, row in enumerate(records):
            raw_kind = row.get("kind")
            if pd.isna(raw_kind) or raw_kind is None or raw_kind is pd.NA:
                identity_invalid_count += 1
                warnings.append(f"Row {idx}: missing kind value in catalog observation.")
                continue
            kind_str = str(raw_kind).strip().upper()
            if kind_str != "YAT":
                identity_invalid_count += 1
                warnings.append(f"Row {idx}: non-YAT fund kind '{kind_str}' in catalog observation.")
                continue

            raw_code = row.get("fund_code")
            if pd.isna(raw_code) or raw_code is None or raw_code is pd.NA:
                identity_invalid_count += 1
                warnings.append(f"Row {idx}: missing or NA fund_code in catalog observation.")
                continue
            code_str = str(raw_code).strip().upper()
            if not code_str or code_str in ("NAN", "NONE", "<NA>", "NAT"):
                identity_invalid_count += 1
                warnings.append(f"Row {idx}: empty or unidentifiable fund_code in catalog observation.")
                continue

            raw_name = row.get("fund_name")
            name_str = ""
            if not pd.isna(raw_name) and raw_name is not None and raw_name is not pd.NA:
                name_str = str(raw_name).strip()

            if code_str not in seen_names_by_code:
                seen_names_by_code[code_str] = set()
            if name_str and name_str not in ("NAN", "NONE", "<NA>", "NAT"):
                seen_names_by_code[code_str].add(name_str)

        observed: Dict[str, str] = {}
        conflicting_codes: Set[str] = set()

        for code_str, names in seen_names_by_code.items():
            if len(names) > 1:
                conflicting_codes.add(code_str)
                observed[code_str] = ""
                warnings.append(
                    f"Conflicting names observed for fund code '{code_str}': {sorted(list(names))}. "
                    f"Fund presence acknowledged but name update / addition skipped."
                )
            elif len(names) == 1:
                observed[code_str] = next(iter(names))
            else:
                observed[code_str] = ""

        identity_complete = (identity_invalid_count == 0)
        if not identity_complete:
            warnings.append(
                f"{identity_invalid_count} TEFAS catalog rows could not be identified. "
                f"Disappearance reconciliation will be skipped for safety."
            )

        return TefasCatalogObservationResult(
            observed_funds=observed,
            conflicting_codes=conflicting_codes,
            warnings=warnings,
            identity_invalid_count=identity_invalid_count,
            identity_complete=identity_complete,
        )

    def normalize_and_validate_dataframe(
        self,
        df: pd.DataFrame,
        data_date: date,
    ) -> TefasNormalizationResult:
        """Normalize raw current DataFrame rows into 3 explicit, type-safe price classes.

        Classes:
        1. positive_price_codes: Structurally valid, correct date, unique, parsed price > 0.
        2. clean_nonpositive_price_codes: Structurally valid, correct date, unique, parsed price <= 0.
        3. ambiguous_or_invalid_price_codes: Malformed, corrupt, missing, wrong date, or duplicate rows.
        """
        warnings: List[str] = []
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) == 0:
            return TefasNormalizationResult(
                valid_prices=[],
                positive_price_codes=set(),
                clean_nonpositive_price_codes=set(),
                ambiguous_or_invalid_price_codes=set(),
                warnings=["TEFAS returned empty or invalid DataFrame."],
                invalid_count=0,
                duplicate_count=0,
            )

        required_cols = {"fund_code", "fund_name", "kind", "price", "date"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            return TefasNormalizationResult(
                valid_prices=[],
                positive_price_codes=set(),
                clean_nonpositive_price_codes=set(),
                ambiguous_or_invalid_price_codes=set(),
                warnings=[f"TEFAS DataFrame missing required columns: {sorted(list(missing_cols))}"],
                invalid_count=len(df),
                duplicate_count=0,
            )

        raw_records = df.to_dict(orient="records")
        invalid_count = 0
        duplicate_count = 0

        positive_price_codes: Set[str] = set()
        clean_nonpositive_price_codes: Set[str] = set()
        ambiguous_or_invalid_price_codes: Set[str] = set()
        valid_prices: List[TefasNormalizedPrice] = []

        # 1. Group rows by valid fund code to identify duplicates upfront across all price types
        records_by_code: Dict[str, List[dict]] = defaultdict(list)

        for idx, row in enumerate(raw_records):
            raw_kind = row.get("kind")
            if pd.isna(raw_kind) or raw_kind is None or raw_kind is pd.NA:
                warnings.append(f"Row {idx}: missing kind value skipped.")
                invalid_count += 1
                continue
            kind_str = str(raw_kind).strip().upper()
            if kind_str != "YAT":
                warnings.append(f"Row {idx}: non-YAT fund kind '{kind_str}' skipped.")
                invalid_count += 1
                continue

            raw_code = row.get("fund_code")
            if pd.isna(raw_code) or raw_code is None or raw_code is pd.NA:
                warnings.append(f"Row {idx}: missing or NA fund_code skipped.")
                invalid_count += 1
                continue
            code_str = str(raw_code).strip().upper()
            if not code_str or code_str in ("NAN", "NONE", "<NA>", "NAT"):
                warnings.append(f"Row {idx}: empty or invalid fund_code skipped.")
                invalid_count += 1
                continue

            records_by_code[code_str].append(row)

        # 2. Evaluate each fund code group
        for code_str, rows in records_by_code.items():
            if len(rows) > 1:
                # Ambiguous duplicate records (two positive, pos+zero, zero+zero, pos+malformed, etc.)
                duplicate_count += (len(rows) - 1)
                ambiguous_or_invalid_price_codes.add(code_str)
                warnings.append(
                    f"Duplicate fund code '{code_str}' encountered ({len(rows)} times). Excluded as ambiguous."
                )
                continue

            # Exactly 1 row for this fund code
            row = rows[0]

            raw_name = row.get("fund_name")
            if pd.isna(raw_name) or raw_name is None or raw_name is pd.NA:
                warnings.append(f"Fund {code_str}: missing or NA fund_name.")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue
            name_str = str(raw_name).strip()
            if not name_str or name_str in ("NAN", "NONE", "<NA>", "NAT"):
                warnings.append(f"Fund {code_str}: empty fund_name.")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            raw_date = row.get("date")
            if pd.isna(raw_date) or raw_date is None or raw_date is pd.NA or raw_date is pd.NaT:
                warnings.append(f"Fund {code_str}: missing date value.")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            row_date: Optional[date] = None
            try:
                if isinstance(raw_date, (datetime, pd.Timestamp)):
                    row_date = raw_date.date()
                elif isinstance(raw_date, date):
                    row_date = raw_date
                elif isinstance(raw_date, str):
                    row_date = date.fromisoformat(raw_date.strip()[:10])
                else:
                    raise ValueError(f"Unsupported date type: {type(raw_date)}")
            except Exception as exc:
                warnings.append(f"Fund {code_str}: unparseable date '{raw_date}' ({exc}).")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            if row_date != data_date:
                warnings.append(
                    f"Fund {code_str}: date mismatch (row date {row_date} != expected {data_date})."
                )
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            raw_price = row.get("price")
            if pd.isna(raw_price) or raw_price is None or raw_price is pd.NA:
                warnings.append(f"Fund {code_str}: missing price value.")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            try:
                if isinstance(raw_price, float):
                    if math.isnan(raw_price) or math.isinf(raw_price):
                        raise ValueError("Non-finite float")
                    dec_price = Decimal(str(raw_price))
                else:
                    dec_price = Decimal(str(raw_price).strip())
            except (InvalidOperation, ValueError, TypeError) as exc:
                warnings.append(f"Fund {code_str}: invalid price '{raw_price}' ({exc}).")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            if not dec_price.is_finite():
                warnings.append(f"Fund {code_str}: non-finite price '{dec_price}'.")
                invalid_count += 1
                ambiguous_or_invalid_price_codes.add(code_str)
                continue

            # Classify into positive vs clean non-positive
            if dec_price > 0:
                positive_price_codes.add(code_str)
                valid_prices.append(
                    TefasNormalizedPrice(
                        fund_code=code_str,
                        fund_name=name_str,
                        fund_kind="YAT",
                        price=dec_price,
                        price_date=data_date,
                        currency="TRY",
                        source="TEFAS",
                    )
                )
            else:
                clean_nonpositive_price_codes.add(code_str)

        valid_prices.sort(key=lambda p: p.fund_code)

        return TefasNormalizationResult(
            valid_prices=valid_prices,
            positive_price_codes=positive_price_codes,
            clean_nonpositive_price_codes=clean_nonpositive_price_codes,
            ambiguous_or_invalid_price_codes=ambiguous_or_invalid_price_codes,
            warnings=warnings,
            invalid_count=invalid_count,
            duplicate_count=duplicate_count,
        )

    def normalize_and_validate_previous_prices(
        self,
        df: pd.DataFrame,
        previous_date: date,
    ) -> Tuple[Dict[str, Decimal], List[str], int, int]:
        """Normalize raw previous DataFrame rows without requiring fund_name.

        Requires only fund_code, kind, price, and date.
        Returns:
            (valid_prev_map, warnings, invalid_count, duplicate_count)
        """
        warnings: List[str] = []
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) == 0:
            return {}, ["TEFAS returned empty or invalid previous DataFrame."], 0, 0

        required_cols = {"fund_code", "kind", "price", "date"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            return {}, [f"TEFAS previous DataFrame missing required columns: {sorted(list(missing_cols))}"], len(df), 0

        raw_records = df.to_dict(orient="records")
        invalid_count = 0
        duplicate_count = 0

        seen_code_counts: Dict[str, int] = {}
        parsed_entries: Dict[str, Decimal] = {}

        for _idx, row in enumerate(raw_records):
            raw_kind = row.get("kind")
            if pd.isna(raw_kind) or raw_kind is None or raw_kind is pd.NA:
                invalid_count += 1
                continue
            kind_str = str(raw_kind).strip().upper()
            if kind_str != "YAT":
                invalid_count += 1
                continue

            raw_code = row.get("fund_code")
            if pd.isna(raw_code) or raw_code is None or raw_code is pd.NA:
                invalid_count += 1
                continue
            code_str = str(raw_code).strip().upper()
            if not code_str or code_str in ("NAN", "NONE", "<NA>", "NAT"):
                invalid_count += 1
                continue

            raw_date = row.get("date")
            if pd.isna(raw_date) or raw_date is None or raw_date is pd.NA or raw_date is pd.NaT:
                invalid_count += 1
                continue

            row_date: Optional[date] = None
            try:
                if isinstance(raw_date, (datetime, pd.Timestamp)):
                    row_date = raw_date.date()
                elif isinstance(raw_date, date):
                    row_date = raw_date
                elif isinstance(raw_date, str):
                    row_date = date.fromisoformat(raw_date.strip()[:10])
                else:
                    raise ValueError(f"Unsupported date type: {type(raw_date)}")
            except Exception:
                invalid_count += 1
                continue

            if row_date != previous_date:
                invalid_count += 1
                continue

            raw_price = row.get("price")
            if pd.isna(raw_price) or raw_price is None or raw_price is pd.NA:
                invalid_count += 1
                continue

            try:
                if isinstance(raw_price, float):
                    if math.isnan(raw_price) or math.isinf(raw_price):
                        raise ValueError("Non-finite float")
                    dec_price = Decimal(str(raw_price))
                else:
                    dec_price = Decimal(str(raw_price).strip())
            except (InvalidOperation, ValueError, TypeError):
                invalid_count += 1
                continue

            if not dec_price.is_finite() or dec_price <= 0:
                invalid_count += 1
                continue

            seen_code_counts[code_str] = seen_code_counts.get(code_str, 0) + 1
            if seen_code_counts[code_str] > 1:
                duplicate_count += 1
                continue

            parsed_entries[code_str] = dec_price

        valid_prev_map: Dict[str, Decimal] = {}
        for code, count in seen_code_counts.items():
            if count == 1 and code in parsed_entries:
                valid_prev_map[code] = parsed_entries[code]

        return valid_prev_map, warnings, invalid_count, duplicate_count

    def build_catalog_reconciliation_plan(
        self,
        observed_funds: Dict[str, str],
        db_instruments: Dict[str, TefasDbInstrument],
        conflicting_codes: Optional[Set[str]] = None,
        positive_price_codes: Optional[Set[str]] = None,
        clean_nonpositive_price_codes: Optional[Set[str]] = None,
        ambiguous_price_codes: Optional[Set[str]] = None,
        stored_price_ids: Optional[Set[UUID]] = None,
        identity_complete: bool = True,
    ) -> TefasCatalogReconciliationPlan:
        """Calculate dynamic catalog reconciliation plan with 3-way price class awareness, disappearance safety, and 80% safety check.

        Classification Matrix:
        1. positive_price_codes:
           - If not in DB: add as ACTIVE (is_active=True).
           - If in DB and inactive: reactivate (is_active=True).
           - If in DB and active: keep active.
        2. clean_nonpositive_price_codes:
           - If no prior price history:
             - If not in DB: add as INACTIVE (is_active=False).
             - If in DB and active: deactivate (is_active=False).
             - If in DB and inactive: stay inactive.
           - If has prior price history:
             - Keep active (is_active=True, temporary price outage).
        3. ambiguous_or_invalid_price_codes / conflicting_codes:
             - DO NOT change instrument active/inactive status.
             - DO NOT add to to_add_active, to_add_inactive, to_reactivate_ids, or to_deactivate_ids.
             - Mark as ambiguous to preserve database state until clean data arrives.
        4. Disappearance from bulletin (identity_complete=True only):
           - If identity_complete is False: disappearance reconciliation is SKIPPED for safety.
           - If identity_complete is True: active DB instruments missing from observed_funds are deactivated (is_active=False).
        """
        observed_count = len(observed_funds)
        active_db_instruments = [inst for inst in db_instruments.values() if inst.is_active]
        active_db_count = len(active_db_instruments)
        conflicting = conflicting_codes or set()
        pos_codes = positive_price_codes if positive_price_codes is not None else set(observed_funds.keys())
        clean_nonpos_codes = clean_nonpositive_price_codes if clean_nonpositive_price_codes is not None else set()
        ambig_codes = (ambiguous_price_codes or set()) | conflicting
        stored_ids = stored_price_ids if stored_price_ids is not None else set()

        if active_db_count == 0:
            raise ValueError("No active TEFAS instruments found in database. Refusing reconciliation.")

        if (observed_count * 10) < (active_db_count * 8):
            raise ValueError(
                f"Safety check failed: Observed {observed_count} TEFAS funds, which is below 80% threshold "
                f"of active database count ({active_db_count}). Refusing reconciliation."
            )

        to_add_active: List[TefasCatalogObservation] = []
        to_add_inactive: List[TefasCatalogObservation] = []
        to_reactivate_ids: List[UUID] = []
        to_reactivate_symbols: List[str] = []
        to_update_names: List[Tuple[UUID, str, str]] = []
        to_deactivate_ids: List[UUID] = []
        to_deactivate_symbols: List[str] = []
        unpriced_deactivated_ids: List[UUID] = []
        unpriced_deactivated_symbols: List[str] = []
        disappeared_deactivated_ids: List[UUID] = []
        disappeared_deactivated_symbols: List[str] = []
        ambiguous_codes_list: List[str] = []

        for code, name in observed_funds.items():
            is_ambiguous = code in ambig_codes or (code not in pos_codes and code not in clean_nonpos_codes)

            if code not in db_instruments:
                if is_ambiguous or not name:
                    ambiguous_codes_list.append(code)
                    continue
                obs = TefasCatalogObservation(fund_code=code, fund_name=name)
                if code in pos_codes:
                    to_add_active.append(obs)
                elif code in clean_nonpos_codes:
                    to_add_inactive.append(obs)
            else:
                inst = db_instruments[code]
                if is_ambiguous:
                    # Ambiguous/malformed/conflicting: NEVER mutate instrument activity or rename
                    ambiguous_codes_list.append(code)
                    continue

                if code in pos_codes:
                    if not inst.is_active:
                        to_reactivate_ids.append(inst.id)
                        to_reactivate_symbols.append(inst.symbol)
                    if name and inst.company_name != name:
                        to_update_names.append((inst.id, inst.symbol, name))
                elif code in clean_nonpos_codes:
                    # Clean verified non-positive price
                    if inst.id in stored_ids:
                        # Has prior price history -> preserve active status
                        if name and inst.company_name != name:
                            to_update_names.append((inst.id, inst.symbol, name))
                    else:
                        # Never had a price history -> deactivate if currently active
                        if inst.is_active:
                            to_deactivate_ids.append(inst.id)
                            to_deactivate_symbols.append(inst.symbol)
                            unpriced_deactivated_ids.append(inst.id)
                            unpriced_deactivated_symbols.append(inst.symbol)
                        if name and inst.company_name != name:
                            to_update_names.append((inst.id, inst.symbol, name))

        # Disappearance reconciliation (only executed when catalog identity is complete)
        disappearance_reconciliation_skipped = False
        disappearance_skip_reason: Optional[str] = None

        if not identity_complete:
            disappearance_reconciliation_skipped = True
            disappearance_skip_reason = "unidentifiable_catalog_rows"
        else:
            for code, inst in db_instruments.items():
                if code not in observed_funds and inst.is_active:
                    to_deactivate_ids.append(inst.id)
                    to_deactivate_symbols.append(inst.symbol)
                    disappeared_deactivated_ids.append(inst.id)
                    disappeared_deactivated_symbols.append(inst.symbol)

        return TefasCatalogReconciliationPlan(
            observed_count=observed_count,
            to_add_active=to_add_active,
            to_add_inactive=to_add_inactive,
            to_reactivate_ids=to_reactivate_ids,
            to_reactivate_symbols=to_reactivate_symbols,
            to_update_names=to_update_names,
            to_deactivate_ids=to_deactivate_ids,
            to_deactivate_symbols=to_deactivate_symbols,
            unpriced_deactivated_ids=unpriced_deactivated_ids,
            unpriced_deactivated_symbols=unpriced_deactivated_symbols,
            disappeared_deactivated_ids=disappeared_deactivated_ids,
            disappeared_deactivated_symbols=disappeared_deactivated_symbols,
            ambiguous_codes=sorted(list(set(ambiguous_codes_list))),
            conflicting_codes=sorted(list(conflicting)),
            disappearance_reconciliation_skipped=disappearance_reconciliation_skipped,
            disappearance_skip_reason=disappearance_skip_reason,
        )

    def match_with_active_instruments(
        self,
        valid_prices: List[TefasNormalizedPrice],
        active_instruments: Dict[str, UUID],
        current_date: date,
        valid_previous_prices: Optional[Union[List[TefasNormalizedPrice], Dict[str, Decimal]]] = None,
    ) -> Tuple[List[TefasPriceSyncItem], List[str], int, int, int, int]:
        """Match normalized prices with active TEFAS instruments and compute daily price change metrics."""
        warnings: List[str] = []
        sync_items: List[TefasPriceSyncItem] = []
        unmatched_count = 0
        previous_matched_count = 0
        previous_missing_count = 0

        price_at_dt = datetime.combine(current_date, time.min, tzinfo=self.tz)
        present_codes: Set[str] = set()

        prev_map: Dict[str, Decimal] = {}
        if valid_previous_prices:
            if isinstance(valid_previous_prices, dict):
                prev_map = {k: v for k, v in valid_previous_prices.items() if v is not None and v > 0}
            else:
                for p in valid_previous_prices:
                    if p.price is not None and p.price > 0:
                        prev_map[p.fund_code] = p.price

        missing_prev_samples: List[str] = []

        for price_item in valid_prices:
            code = price_item.fund_code
            present_codes.add(code)
            instrument_id = active_instruments.get(code)
            if not instrument_id:
                warnings.append(
                    f"Fund '{code}' ({price_item.fund_name}) not found in active instruments. Skipped."
                )
                unmatched_count += 1
                continue

            cur_p = price_item.price.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
            prev_p: Optional[Decimal] = None
            change_amt: Optional[Decimal] = None
            change_pct: Optional[Decimal] = None

            if code in prev_map:
                prev_raw = prev_map[code]
                prev_p = prev_raw.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                change_amt = (cur_p - prev_p).quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                change_pct = (((cur_p - prev_p) / prev_p) * Decimal("100")).quantize(
                    PERCENT_SCALE, rounding=ROUND_HALF_UP
                )
                previous_matched_count += 1
            else:
                previous_missing_count += 1
                missing_prev_samples.append(code)

            sync_items.append(
                TefasPriceSyncItem(
                    instrument_id=instrument_id,
                    symbol=code,
                    price=cur_p,
                    currency="TRY",
                    previous_close=prev_p,
                    change_amount=change_amt,
                    change_percent=change_pct,
                    price_at=price_at_dt,
                    provider_code="TEFAS",
                    is_stale=False,
                    stale_reason=None,
                )
            )

        if missing_prev_samples:
            sample_str = ", ".join(missing_prev_samples[:5])
            more = f" and {len(missing_prev_samples) - 5} more" if len(missing_prev_samples) > 5 else ""
            warnings.append(
                f"{previous_missing_count} funds had no previous valid price ({sample_str}{more})."
            )

        missing_symbols = sorted(list(set(active_instruments.keys()) - present_codes))
        missing_price_count = len(missing_symbols)
        if missing_price_count > 0:
            sample = missing_symbols[:5]
            more_str = f" and {missing_price_count - 5} more" if missing_price_count > 5 else ""
            warnings.append(
                f"{missing_price_count} active TEFAS instruments had no valid current price ({', '.join(sample)}{more_str})."
            )

        return (
            sync_items,
            warnings,
            unmatched_count,
            missing_price_count,
            previous_matched_count,
            previous_missing_count,
        )
