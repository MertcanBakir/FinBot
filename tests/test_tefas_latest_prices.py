from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.jobs.tefas_latest_prices_job import run_job, sanitize_error_message
from app.models.tefas_prices import (
    TefasDbInstrument,
    TefasPricesJobResult,
    TefasPriceSyncItem,
)
from app.providers.tefas.tefas_price_provider import (
    TefasPriceFetchError,
    TefasPriceProvider,
)
from app.repositories.tefas_latest_prices_repository import TefasLatestPricesRepository
from app.services.tefas_prices_report import (
    format_tefas_prices_email_html,
    format_tefas_prices_email_text,
    format_tefas_prices_subject,
)
from app.services.tefas_prices_service import TefasPricesService, plan_price_upserts

CURRENT_DATE = date(2026, 8, 24)   # Monday
PREVIOUS_DATE = date(2026, 8, 21)  # Friday


def make_raw_df(rows):
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. Provider Date Fallback & Previous Date Fetch Tests
# ---------------------------------------------------------------------------


def test_provider_fallback_to_previous_day():
    mock_crawler = MagicMock()
    today = date(2026, 8, 24)
    yesterday = date(2026, 8, 23)
    friday = date(2026, 8, 21)

    def fake_fetch(start, kind, columns):
        if start == today or start == yesterday:
            return pd.DataFrame()
        elif start == friday:
            return pd.DataFrame(
                [{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-21", "price": 10.5}]
            )
        return pd.DataFrame()

    mock_crawler.fetch.side_effect = fake_fetch
    provider = TefasPriceProvider(crawler=mock_crawler)
    df, fetched_date = provider.fetch_latest_prices(reference_date=today)

    assert fetched_date == friday
    assert len(df) == 1
    assert mock_crawler.fetch.call_count == 4


def test_provider_fetch_previous_prices_skips_weekend():
    mock_crawler = MagicMock()
    monday = date(2026, 8, 24)
    friday = date(2026, 8, 21)

    def fake_fetch(start, kind, columns):
        if start == friday:
            return pd.DataFrame(
                [{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-21", "price": 10.0}]
            )
        return pd.DataFrame()

    mock_crawler.fetch.side_effect = fake_fetch
    provider = TefasPriceProvider(crawler=mock_crawler)
    prev_df, prev_date = provider.fetch_previous_prices(monday)

    assert prev_date == friday
    assert len(prev_df) == 1
    assert prev_date < monday


def test_provider_fails_closed_after_7_empty_days_for_current():
    mock_crawler = MagicMock()
    mock_crawler.fetch.return_value = pd.DataFrame()

    provider = TefasPriceProvider(crawler=mock_crawler, max_lookback_days=7)
    with pytest.raises(TefasPriceFetchError) as exc_info:
        provider.fetch_latest_prices(reference_date=date(2026, 8, 24))

    assert exc_info.value.code == "no_data_found"


# ---------------------------------------------------------------------------
# 2. Dynamic Catalog Reconciliation & 80% Safety Check Tests
# ---------------------------------------------------------------------------


def test_catalog_observations_extracts_all_yat_funds_even_with_zero_price():
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "price": 10.0},
            {"kind": "YAT", "fund_code": "ZER", "fund_name": "Zero Price Fund", "price": 0.0},
            {"kind": "EMK", "fund_code": "EMK1", "fund_name": "Emeklilik", "price": 15.0},
            {"kind": "YAT", "fund_code": "   ", "fund_name": "Blank Code", "price": 5.0},
            {"kind": "YAT", "fund_code": np.nan, "fund_name": "NaN Code", "price": 5.0},
        ]
    )
    observed, conflicting, warnings = service.extract_catalog_observations(df)

    assert len(observed) == 2
    assert "AAK" in observed
    assert "ZER" in observed
    assert "EMK1" not in observed
    assert len(conflicting) == 0


def test_catalog_observations_handles_conflicting_names_safely():
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "SAME", "fund_name": "Same Name", "price": 10.0},
            {"kind": "YAT", "fund_code": "SAME", "fund_name": "Same Name", "price": 10.0},
            {"kind": "YAT", "fund_code": "CONF", "fund_name": "Name Option A", "price": 10.0},
            {"kind": "YAT", "fund_code": "CONF", "fund_name": "Name Option B", "price": 11.0},
        ]
    )
    observed, conflicting, warnings = service.extract_catalog_observations(df)

    assert "SAME" in observed
    assert observed["SAME"] == "Same Name"
    assert "SAME" not in conflicting

    assert "CONF" in observed
    assert "CONF" in conflicting
    assert any("Conflicting names observed for fund code 'CONF'" in w for w in warnings)


def test_catalog_reconciliation_conflicting_name_does_not_rename_or_add_or_reactivate():
    """Conflicting names make fund presence ambiguous: no rename, no addition, no reactivation."""
    service = TefasPricesService()

    id_active = uuid4()
    id_inactive = uuid4()

    db_instruments = {
        "EXIST_CONF": TefasDbInstrument(id=id_active, symbol="EXIST_CONF", company_name="Original Name", is_active=True),
        "INACT_CONF": TefasDbInstrument(id=id_inactive, symbol="INACT_CONF", company_name="Old Inactive Name", is_active=False),
    }

    observed_funds = {
        "EXIST_CONF": "",
        "INACT_CONF": "",
        "NEW_CONF": "",
    }
    conflicting_codes = {"EXIST_CONF", "INACT_CONF", "NEW_CONF"}

    plan = service.build_catalog_reconciliation_plan(observed_funds, db_instruments, conflicting_codes)

    assert not any(item.fund_code == "NEW_CONF" for item in plan.to_add)
    assert not any(item[1] == "EXIST_CONF" for item in plan.to_update_names)
    assert "EXIST_CONF" not in plan.to_deactivate_symbols
    assert id_inactive not in plan.to_reactivate_ids
    assert "INACT_CONF" in plan.ambiguous_codes



def test_catalog_reconciliation_detects_add_reactivate_rename_and_deactivate():
    service = TefasPricesService()

    id_active = uuid4()
    id_inactive = uuid4()
    id_rename = uuid4()
    id_missing = uuid4()

    db_instruments = {
        "ACT": TefasDbInstrument(id=id_active, symbol="ACT", company_name="Active Fund", is_active=True),
        "INACT": TefasDbInstrument(id=id_inactive, symbol="INACT", company_name="Inactive Fund", is_active=False),
        "RNM": TefasDbInstrument(id=id_rename, symbol="RNM", company_name="Old Name Fund", is_active=True),
        "HNC": TefasDbInstrument(id=id_missing, symbol="HNC", company_name="Missing Fund", is_active=True),
    }

    observed_funds = {
        "ACT": "Active Fund",
        "INACT": "Inactive Fund",
        "RNM": "New Name Fund",
        "ETN": "Brand New ETN Fund",
    }

    plan = service.build_catalog_reconciliation_plan(observed_funds, db_instruments)

    assert plan.observed_count == 4
    assert len(plan.to_add) == 1
    assert plan.to_add[0].fund_code == "ETN"
    assert plan.to_reactivate_ids == [id_inactive]
    assert len(plan.to_update_names) == 1
    assert plan.to_update_names[0] == (id_rename, "RNM", "New Name Fund")
    assert plan.to_deactivate_ids == [id_missing]


@pytest.mark.parametrize(
    "active_count, observed_count, should_pass",
    [
        (11, 8, False),   # 8/11 = 0.727 < 0.80 -> Fail
        (10, 8, True),    # 8/10 = 0.80 -> Pass
        (100, 79, False), # 79/100 = 0.79 < 0.80 -> Fail
        (100, 80, True),  # 80/100 = 0.80 -> Pass
    ],
)
def test_catalog_reconciliation_80_percent_boundary_check(active_count, observed_count, should_pass):
    service = TefasPricesService()
    db_instruments = {
        f"F{i:03d}": TefasDbInstrument(id=uuid4(), symbol=f"F{i:03d}", company_name=f"Fund {i}", is_active=True)
        for i in range(active_count)
    }
    observed_funds = {f"F{i:03d}": f"Fund {i}" for i in range(observed_count)}

    if should_pass:
        plan = service.build_catalog_reconciliation_plan(observed_funds, db_instruments)
        assert plan.observed_count == observed_count
    else:
        with pytest.raises(ValueError, match="Safety check failed.*80% threshold"):
            service.build_catalog_reconciliation_plan(observed_funds, db_instruments)


def test_catalog_reconciliation_fails_closed_on_empty_active_db():
    service = TefasPricesService()
    db_instruments = {
        "F001": TefasDbInstrument(id=uuid4(), symbol="F001", company_name="Fund 1", is_active=False)
    }
    observed_funds = {"F001": "Fund 1"}

    with pytest.raises(ValueError, match="No active TEFAS instruments found"):
        service.build_catalog_reconciliation_plan(observed_funds, db_instruments)


# ---------------------------------------------------------------------------
# 3. Price Normalization & Validation Tests
# ---------------------------------------------------------------------------


def test_service_accepts_valid_dates_and_positive_prices():
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": "12.345"},
            {"kind": "YAT", "fund_code": "TI2", "fund_name": "İş", "date": date(2026, 8, 24), "price": 1.25},
            {"kind": "YAT", "fund_code": "MAC", "fund_name": "Marmara", "date": pd.Timestamp("2026-08-24"), "price": Decimal("50.0")},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 3
    assert invalid_count == 0
    assert duplicate_count == 0
    assert valid[0].fund_code == "AAK"
    assert valid[0].price == Decimal("12.345")
    assert valid[0].price_date == CURRENT_DATE


def test_service_rejects_missing_and_mismatched_dates():
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "WRONG_DATE", "fund_name": "Wrong Date", "date": "2026-08-21", "price": 10.0},
            {"kind": "YAT", "fund_code": "NO_DATE", "fund_name": "No Date", "date": None, "price": 10.0},
            {"kind": "YAT", "fund_code": "NAT_DATE", "fund_name": "NaT Date", "date": pd.NaT, "price": 10.0},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 1
    assert valid[0].fund_code == "AAK"
    assert invalid_count == 3


def test_service_rejects_dataframe_missing_required_date_column():
    service = TefasPricesService()
    df = pd.DataFrame([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "price": 10.0}])
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 0
    assert invalid_count == 1
    assert any("missing required columns" in w for w in warnings)


@pytest.mark.parametrize(
    "bad_val",
    [np.nan, pd.NA, None, "", "   ", "NAN", "NONE", "<NA>"],
)
def test_service_handles_na_and_invalid_codes_names_cleanly(bad_val):
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": bad_val, "fund_name": "Fund Name", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "CODE", "fund_name": bad_val, "date": "2026-08-24", "price": 10.0},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 1
    assert valid[0].fund_code == "GOOD"
    assert invalid_count == 2


@pytest.mark.parametrize(
    "bad_price",
    [None, np.nan, float("nan"), float("inf"), float("-inf"), "bad_str"],
)
def test_service_rejects_malformed_and_nan_prices_as_invalid(bad_price):
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "BAD", "fund_name": "Bad Price", "date": "2026-08-24", "price": bad_price},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 1
    assert valid[0].fund_code == "GOOD"
    assert invalid_count == 1


@pytest.mark.parametrize(
    "zero_price",
    [0, 0.0, "0", "-1.5"],
)
def test_service_cleanly_skips_zero_and_negative_prices_without_invalid_count(zero_price):
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "ZERO", "fund_name": "Zero Price Fund", "date": "2026-08-24", "price": zero_price},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 1
    assert valid[0].fund_code == "GOOD"
    assert invalid_count == 0



def test_service_excludes_duplicate_fund_codes():
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup 1", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup 2", "date": "2026-08-24", "price": 11.0},
            {"kind": "YAT", "fund_code": "SOLO", "fund_name": "Solo", "date": "2026-08-24", "price": 20.0},
        ]
    )
    valid, warnings, invalid_count, duplicate_count = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert len(valid) == 1
    assert valid[0].fund_code == "SOLO"
    assert duplicate_count == 1


def test_match_with_active_instruments_ignores_inactive():
    service = TefasPricesService()
    id_aak = uuid4()
    id_new = uuid4()
    active_instruments = {
        "AAK": id_aak,
        "ETN": id_new,
        "MISSED": uuid4(),
    }

    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "ETN", "fund_name": "New ETN", "date": "2026-08-24", "price": 20.0},
            {"kind": "YAT", "fund_code": "UNMATCHED", "fund_name": "Unmatched", "date": "2026-08-24", "price": 30.0},
        ]
    )
    valid, _, _, _ = service.normalize_and_validate_dataframe(df, CURRENT_DATE)
    sync_items, match_warns, unmatched_count, missing_price_count, _, _ = service.match_with_active_instruments(
        valid, active_instruments, CURRENT_DATE
    )

    assert len(sync_items) == 2
    assert {s.symbol for s in sync_items} == {"AAK", "ETN"}
    assert unmatched_count == 1
    assert missing_price_count == 1


# ---------------------------------------------------------------------------
# 4. Previous Price Normalization & Change Calculation Tests
# ---------------------------------------------------------------------------


def test_service_normalize_previous_prices_does_not_require_fund_name():
    service = TefasPricesService()
    # Row has NO fund_name column/value
    prev_df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "AAK", "date": "2026-08-21", "price": 25.0},
            {"kind": "YAT", "fund_code": "ZTV", "fund_name": "", "date": "2026-08-21", "price": "10.50"},
        ]
    )
    valid_prev_map, warnings, invalid_count, duplicate_count = service.normalize_and_validate_previous_prices(
        prev_df, PREVIOUS_DATE
    )

    assert len(valid_prev_map) == 2
    assert valid_prev_map["AAK"] == Decimal("25.0")
    assert valid_prev_map["ZTV"] == Decimal("10.50")
    assert invalid_count == 0


def test_service_matches_current_and_previous_and_computes_changes_in_decimal():
    service = TefasPricesService()
    id_aak = uuid4()
    active_instruments = {"AAK": id_aak}

    cur_df = make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": "25.50"}])
    valid_cur, _, _, _ = service.normalize_and_validate_dataframe(cur_df, CURRENT_DATE)

    prev_df = make_raw_df([{"kind": "YAT", "fund_code": "AAK", "date": "2026-08-21", "price": "25.00"}])
    valid_prev_map, _, _, _ = service.normalize_and_validate_previous_prices(prev_df, PREVIOUS_DATE)

    (
        sync_items,
        warnings,
        unmatched,
        missing_cur,
        prev_matched,
        prev_missing,
    ) = service.match_with_active_instruments(
        valid_cur,
        active_instruments,
        current_date=CURRENT_DATE,
        valid_previous_prices=valid_prev_map,
    )

    assert len(sync_items) == 1
    item = sync_items[0]
    assert item.symbol == "AAK"
    assert item.price == Decimal("25.500000000000")
    assert item.previous_close == Decimal("25.000000000000")
    assert item.change_amount == Decimal("0.500000000000")
    assert item.change_percent == Decimal("2.00000000")
    assert prev_matched == 1
    assert prev_missing == 0


def test_service_handles_missing_previous_price_cleanly():
    service = TefasPricesService()
    id_new = uuid4()
    active_instruments = {"NEW": id_new}

    cur_df = make_raw_df([{"kind": "YAT", "fund_code": "NEW", "fund_name": "New Fund", "date": "2026-08-24", "price": "10.00"}])
    valid_cur, _, _, _ = service.normalize_and_validate_dataframe(cur_df, CURRENT_DATE)

    (
        sync_items,
        warnings,
        unmatched,
        missing_cur,
        prev_matched,
        prev_missing,
    ) = service.match_with_active_instruments(
        valid_cur,
        active_instruments,
        current_date=CURRENT_DATE,
        valid_previous_prices={},
    )

    assert len(sync_items) == 1
    item = sync_items[0]
    assert item.price == Decimal("10.000000000000")
    assert item.previous_close is None
    assert item.change_amount is None
    assert item.change_percent is None
    assert prev_matched == 0
    assert prev_missing == 1


# ---------------------------------------------------------------------------
# 5. Same-Day Merge & Idempotency Rules Tests
# ---------------------------------------------------------------------------


def test_same_day_incoming_previous_null_preserves_stored_previous_and_is_noop():
    id_inst = uuid4()
    t_stored = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_inst: {
            "price": Decimal("10.000000000000"),
            "currency": "TRY",
            "previous_close": Decimal("9.500000000000"),
            "change_amount": Decimal("0.500000000000"),
            "change_percent": Decimal("5.26315789"),
            "price_at": t_stored,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    # Incoming item has same date & price, but previous_close is None
    incoming = [
        TefasPriceSyncItem(
            instrument_id=id_inst,
            symbol="TEST",
            price=Decimal("10.000000000000"),
            previous_close=None,
            change_amount=None,
            change_percent=None,
            price_at=t_stored,
            provider_code="TEFAS",
        )
    ]

    to_upsert, unchanged, stale_skipped = plan_price_upserts(stored_map, incoming)

    assert len(to_upsert) == 0
    assert unchanged == 1
    assert stale_skipped == 0


def test_same_day_corrected_price_with_incoming_previous_null_recalculates_changes_from_stored():
    id_inst = uuid4()
    t_stored = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_inst: {
            "price": Decimal("10.000000000000"),
            "currency": "TRY",
            "previous_close": Decimal("9.500000000000"),
            "change_amount": Decimal("0.500000000000"),
            "change_percent": Decimal("5.26315789"),
            "price_at": t_stored,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    # Incoming price corrected to 10.45 on same day, but previous is None in this run
    incoming = [
        TefasPriceSyncItem(
            instrument_id=id_inst,
            symbol="TEST",
            price=Decimal("10.450000000000"),
            previous_close=None,
            change_amount=None,
            change_percent=None,
            price_at=t_stored,
            provider_code="TEFAS",
        )
    ]

    to_upsert, unchanged, stale_skipped = plan_price_upserts(stored_map, incoming)

    assert len(to_upsert) == 1
    assert unchanged == 0
    item = to_upsert[0]
    assert item.price == Decimal("10.450000000000")
    assert item.previous_close == Decimal("9.500000000000")
    assert item.change_amount == Decimal("0.950000000000")
    assert item.change_percent == Decimal("10.00000000")


def test_same_day_incoming_previous_fills_missing_stored_previous():
    id_inst = uuid4()
    t_stored = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_inst: {
            "price": Decimal("10.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_stored,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    incoming = [
        TefasPriceSyncItem(
            instrument_id=id_inst,
            symbol="TEST",
            price=Decimal("10.000000000000"),
            previous_close=Decimal("9.500000000000"),
            change_amount=Decimal("0.500000000000"),
            change_percent=Decimal("5.26315789"),
            price_at=t_stored,
            provider_code="TEFAS",
        )
    ]

    to_upsert, unchanged, stale_skipped = plan_price_upserts(stored_map, incoming)

    assert len(to_upsert) == 1
    assert unchanged == 0
    assert to_upsert[0].previous_close == Decimal("9.500000000000")


def test_newer_date_incoming_previous_null_does_not_carry_forward_old_change_fields():
    id_inst = uuid4()
    t_old = datetime(2026, 8, 21, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    t_new = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_inst: {
            "price": Decimal("10.000000000000"),
            "currency": "TRY",
            "previous_close": Decimal("9.500000000000"),
            "change_amount": Decimal("0.500000000000"),
            "change_percent": Decimal("5.26315789"),
            "price_at": t_old,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    incoming = [
        TefasPriceSyncItem(
            instrument_id=id_inst,
            symbol="TEST",
            price=Decimal("11.000000000000"),
            previous_close=None,
            change_amount=None,
            change_percent=None,
            price_at=t_new,
            provider_code="TEFAS",
        )
    ]

    to_upsert, unchanged, stale_skipped = plan_price_upserts(stored_map, incoming)

    assert len(to_upsert) == 1
    assert to_upsert[0].previous_close is None
    assert to_upsert[0].change_amount is None


def test_older_incoming_date_is_skipped():
    id_inst = uuid4()
    t_new = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    t_old = datetime(2026, 8, 21, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_inst: {
            "price": Decimal("11.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_new,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    incoming = [
        TefasPriceSyncItem(
            instrument_id=id_inst,
            symbol="OLD",
            price=Decimal("10.000000000000"),
            price_at=t_old,
            provider_code="TEFAS",
        )
    ]

    to_upsert, unchanged, stale_skipped = plan_price_upserts(stored_map, incoming)

    assert len(to_upsert) == 0
    assert unchanged == 0
    assert stale_skipped == 1


# ---------------------------------------------------------------------------
# 6. Stale Reason Validation & Dry-Run Realistic Stale Counting Tests
# ---------------------------------------------------------------------------


def test_mark_stale_prices_rejects_invalid_reason_without_sql():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(ValueError, match="Invalid stale reason 'arbitrary_text'"):
        TefasLatestPricesRepository.mark_stale_prices(conn, [uuid4()], "arbitrary_text")

    cursor.execute.assert_not_called()


def test_dry_run_stale_calculation_respects_existing_rows_and_current_reason():
    id_fresh = uuid4()
    id_already_stale = uuid4()
    id_diff_reason = uuid4()
    id_no_row = uuid4()

    stored_map = {
        id_fresh: {"is_stale": False, "stale_reason": None},
        id_already_stale: {"is_stale": True, "stale_reason": "price_not_published"},
        id_diff_reason: {"is_stale": True, "stale_reason": "instrument_inactive"},
    }

    missing_ids = [id_fresh, id_already_stale, id_diff_reason, id_no_row]

    # Calculate would mark price_not_published
    count = sum(
        1
        for uid in missing_ids
        if uid in stored_map
        and (not stored_map[uid]["is_stale"] or stored_map[uid]["stale_reason"] != "price_not_published")
    )

    # id_fresh (1) + id_diff_reason (1) = 2. id_already_stale (0) and id_no_row (0)
    assert count == 2


# ---------------------------------------------------------------------------
# 7. Error Sanitization & Secret Masking Tests
# ---------------------------------------------------------------------------


def test_sanitize_error_message_masks_sensitive_credentials_and_safety_prefix():
    exc1 = RuntimeError("Database error at postgresql://postgres:SuperSecretPassword123@supabase.com:6543/postgres")
    assert "SuperSecretPassword123" not in sanitize_error_message(exc1)
    assert sanitize_error_message(exc1) == "Veritabanı veya servis erişim hatası."

    exc2 = RuntimeError("Safety check failed: password=Secret123")
    assert "Secret123" not in sanitize_error_message(exc2)
    assert sanitize_error_message(exc2) == "Veritabanı veya servis erişim hatası."

    exc3 = ValueError("Safety check failed: Observed 5 funds, which is below 80% threshold of active database count (10).")
    assert "Safety check failed" in sanitize_error_message(exc3)


# ---------------------------------------------------------------------------
# 8. Email Formatting & Outlook Table Layout Tests
# ---------------------------------------------------------------------------


def test_email_formatting_and_outlook_table_layout():
    result = TefasPricesJobResult(
        status="partial",
        started_at="2026-08-24T18:00:00+00:00",
        finished_at="2026-08-24T18:00:05+00:00",
        duration_seconds=5.0,
        tefas_data_date="2026-08-24",
        previous_data_date="2026-08-21",
        fetched_count=2036,
        valid_count=2030,
        invalid_count=6,
        duplicate_count=0,
        matched_count=2030,
        previous_price_matched_count=2028,
        previous_price_missing_count=2,
        price_not_published_stale_count=6,
        instrument_inactive_stale_count=1,
        catalog_observed_count=2036,
        catalog_added_count=2,
        catalog_name_updated_count=1,
        catalog_reactivated_count=0,
        catalog_deactivated_count=1,
        inserted_or_updated_count=2030,
        warnings=["Fund AES: price 0.0 skipped."],
    )

    subj = format_tefas_prices_subject(result)
    assert "⚠️ FinBot TEFAS Fiyatları — Kısmi güncelleme" in subj
    assert "24 Ağustos 2026" in subj

    txt = format_tefas_prices_email_text(result)
    assert "Önceki TEFAS Veri Tarihi: 2026-08-21" in txt
    assert "Önceki Fiyatı Eşleşen: 2028" in txt
    assert "Stale Yapılan (Yayımlanmayan Fiyat): 6" in txt

    html_out = format_tefas_prices_email_html(result)
    assert "display: flex" not in html_out
    assert "display:flex" not in html_out
    assert "2026-08-21" in html_out
    assert "21:00:00" in html_out


def test_email_delivery_classification():
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 0.0}]),
        CURRENT_DATE,
        None,
        None,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    db_inst = {"AAK": TefasDbInstrument(id=uuid4(), symbol="AAK", company_name="Ata", is_active=True)}
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst):
        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)
        assert res.email_status == "skipped_not_configured"
        assert res.email_error is None

    # send_email returns False
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = False
    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst):
        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)
        assert res.email_status == "failed"
        assert res.email_error == "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."


# ---------------------------------------------------------------------------
# 9. Single Connection Transaction, Rollback & Dry-Run Orchestration Tests
# ---------------------------------------------------------------------------


def test_dry_run_executes_no_writes_and_no_email():
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0}]),
        CURRENT_DATE,
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "date": "2026-08-21", "price": 9.5}]),
        PREVIOUS_DATE,
    )
    mock_email = MagicMock()

    id_aak = uuid4()
    db_inst = {"AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True)}

    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation") as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.dry_run is True
        assert res.previous_price_matched_count == 1
        mock_recon.assert_not_called()
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_not_called()


def test_previous_price_missing_enforces_partial_status():
    mock_provider = MagicMock()
    # AAK is valid current, but previous price set does NOT have AAK
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0}]),
        CURRENT_DATE,
        make_raw_df([{"kind": "YAT", "fund_code": "OTHER", "date": "2026-08-21", "price": 5.0}]),
        PREVIOUS_DATE,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    id_aak = uuid4()
    db_inst = {"AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True)}

    conn_mock = MagicMock()
    tx_mock = MagicMock()
    conn_mock.transaction.return_value = tx_mock
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={}), \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)) as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "partial"
        assert res.previous_price_missing_count == 1
        assert res.inserted_or_updated_count == 1
        mock_email.send_email.assert_called_once()


def test_meaningful_transaction_rollback_when_stale_marking_fails():
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0}]),
        CURRENT_DATE,
        None,
        None,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    id_aak = uuid4()
    db_inst = {"AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True)}

    # Track transaction lifecycle explicitly
    class StatefulTransaction:
        def __init__(self):
            self.entered = False
            self.exited = False
            self.committed = False
            self.rolled_back = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.exited = True
            if exc_type is not None:
                self.rolled_back = True
                return False  # Propagate exception
            self.committed = True
            return True

    tx_list = []
    def make_tx():
        tx = StatefulTransaction()
        tx_list.append(tx)
        return tx

    conn_mock = MagicMock()
    conn_mock.transaction.side_effect = make_tx
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={}) as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)) as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", side_effect=RuntimeError("Crash in stale marking")):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        # 1. Catalog and price upsert were invoked inside transaction
        mock_recon.assert_called_once()
        mock_upsert.assert_called_once()

        # 2. Transaction entered and rolled back
        assert any(tx.rolled_back for tx in tx_list)

        # 3. Overall job outcome is failed
        assert res.status == "failed"
        assert res.error == "Veritabanı işlem (transaction) hatası." or "hata" in res.error

        # 4. Failed alert email delivered
        mock_email.send_email.assert_called_once()
        subj = mock_email.send_email.call_args.kwargs["subject"]
        assert "Güncelleme başarısız" in subj


def test_provider_logs_and_exceptions_do_not_leak_secrets(caplog):
    import logging
    mock_crawler = MagicMock()
    secret_url = "https://user:SecretPassword@example.com/api?token=ABC123"
    mock_crawler.fetch.side_effect = RuntimeError(f"Request failed: {secret_url}")

    provider = TefasPriceProvider(crawler=mock_crawler, max_lookback_days=2)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(TefasPriceFetchError) as exc_info:
            provider.fetch_latest_prices(reference_date=CURRENT_DATE)

    # 1. Secret must not appear in raised exception message
    exc_msg = str(exc_info.value)
    assert "SecretPassword" not in exc_msg
    assert "ABC123" not in exc_msg
    assert "last_error_type=RuntimeError" in exc_msg

    # 2. Secret must not appear in captured log output
    assert "SecretPassword" not in caplog.text
    assert "ABC123" not in caplog.text

    # 3. Test previous price fetch path
    caplog.clear()
    secret_prev_url = "https://user:SecretPasswordPrev@example.com/api?token=XYZ789"
    mock_crawler.fetch.side_effect = RuntimeError(f"Prev request failed: {secret_prev_url}")

    with caplog.at_level(logging.WARNING):
        prev_df, prev_date = provider.fetch_previous_prices(CURRENT_DATE)
        assert prev_df is None
        assert prev_date is None

    assert "SecretPasswordPrev" not in caplog.text
    assert "XYZ789" not in caplog.text


def test_smtp_exception_safe_handling_and_secret_masking(caplog):
    import logging
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 0.0}]),
        CURRENT_DATE,
        None,
        None,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.side_effect = RuntimeError("SMTP failed token=SecretSmtpToken123")

    db_inst = {"AAK": TefasDbInstrument(id=uuid4(), symbol="AAK", company_name="Ata", is_active=True)}
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with caplog.at_level(logging.WARNING):
        with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
             patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst):

            res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

            assert res.email_status == "failed"
            assert res.email_error == "E-posta gönderimi sırasında hata oluştu."
            assert "SecretSmtpToken123" not in (res.error or "")
            assert "SecretSmtpToken123" not in (res.email_error or "")
            assert "SecretSmtpToken123" not in caplog.text
            assert mock_email.send_email.call_count == 1


def test_no_valid_prices_fail_closed_and_rollback_in_apply():
    mock_provider = MagicMock()
    # YAT fund exists in raw DF, but price is 0.0 so valid prices list is empty
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 0.0}]),
        CURRENT_DATE,
        None,
        None,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    id_aak = uuid4()
    db_inst = {"AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True)}

    class StatefulTransaction:
        def __init__(self):
            self.entered = False
            self.exited = False
            self.committed = False
            self.rolled_back = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.exited = True
            if exc_type is not None:
                self.rolled_back = True
                return False
            self.committed = True
            return True

    tx_list = []
    def make_tx():
        tx = StatefulTransaction()
        tx_list.append(tx)
        return tx

    conn_mock = MagicMock()
    conn_mock.transaction.side_effect = make_tx
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={}) as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert any(tx.rolled_back for tx in tx_list)
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_called_once()
        subj = mock_email.send_email.call_args.kwargs["subject"]
        assert "Güncelleme başarısız" in subj

    # Also verify dry-run equivalent
    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        dry_res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert dry_res.status == "failed"
        assert dry_res.dry_run is True


def test_successful_apply_sends_no_email():
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0}]),
        CURRENT_DATE,
        make_raw_df([{"kind": "YAT", "fund_code": "AAK", "date": "2026-08-21", "price": 9.5}]),
        PREVIOUS_DATE,
    )
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    id_aak = uuid4()
    db_inst = {"AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True)}

    conn_mock = MagicMock()
    tx_mock = MagicMock()
    conn_mock.transaction.return_value = tx_mock
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={}), \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)), \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.email_status is None
        mock_email.send_email.assert_not_called()


def test_dry_run_counters_via_production_job_execution():
    id_aak = uuid4()
    id_ti2 = uuid4()
    id_old = uuid4()
    id_mis_fresh = uuid4()
    id_mis_already = uuid4()
    id_mis_diff = uuid4()
    id_mis_norow = uuid4()
    id_deact_fresh = uuid4()
    id_deact_already = uuid4()

    db_inst = {
        "AAK": TefasDbInstrument(id=id_aak, symbol="AAK", company_name="Ata", is_active=True),
        "TI2": TefasDbInstrument(id=id_ti2, symbol="TI2", company_name="İş", is_active=True),
        "OLD": TefasDbInstrument(id=id_old, symbol="OLD", company_name="Old Date", is_active=True),
        "MIS_FRESH": TefasDbInstrument(id=id_mis_fresh, symbol="MIS_FRESH", company_name="Missing Fresh", is_active=True),
        "MIS_ALREADY": TefasDbInstrument(id=id_mis_already, symbol="MIS_ALREADY", company_name="Missing Already Stale", is_active=True),
        "MIS_DIFF": TefasDbInstrument(id=id_mis_diff, symbol="MIS_DIFF", company_name="Missing Diff Stale", is_active=True),
        "MIS_NOROW": TefasDbInstrument(id=id_mis_norow, symbol="MIS_NOROW", company_name="Missing No Row", is_active=True),
        "DEACT_FRESH": TefasDbInstrument(id=id_deact_fresh, symbol="DEACT_FRESH", company_name="Deact Fresh", is_active=True),
        "DEACT_ALREADY": TefasDbInstrument(id=id_deact_already, symbol="DEACT_ALREADY", company_name="Deact Already Stale", is_active=True),
    }

    t_cur = datetime(2026, 8, 24, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    t_newer = datetime(2026, 8, 25, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    stored_map = {
        id_aak: {
            "price": Decimal("10.000000000000"),
            "currency": "TRY",
            "previous_close": Decimal("9.500000000000"),
            "change_amount": Decimal("0.500000000000"),
            "change_percent": Decimal("5.26315789"),
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        },
        id_ti2: {
            "price": Decimal("1.000000000000"),  # Different from incoming 1.25
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        },
        id_old: {
            "price": Decimal("5.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_newer,  # Stored is newer than incoming 2026-08-24
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        },
        id_mis_fresh: {
            "price": Decimal("20.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        },
        id_mis_already: {
            "price": Decimal("30.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": True,
            "stale_reason": "price_not_published",
        },
        id_mis_diff: {
            "price": Decimal("40.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": True,
            "stale_reason": "instrument_inactive",
        },
        id_deact_fresh: {
            "price": Decimal("50.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        },
        id_deact_already: {
            "price": Decimal("60.000000000000"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": t_cur,
            "provider_code": "TEFAS",
            "is_stale": True,
            "stale_reason": "instrument_inactive",
        },
    }

    # TEFAS crawler observes AAK, TI2, OLD, PLUS1 (valid) and MIS_FRESH, MIS_ALREADY, MIS_DIFF, MIS_NOROW (price 0.0).
    # Total observed = 8 out of 9 active in DB (88.8% > 80% safety check).
    # DEACT_FRESH and DEACT_ALREADY are not observed in TEFAS, so they will be deactivated.
    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "AAK", "fund_name": "Ata", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "TI2", "fund_name": "İş", "date": "2026-08-24", "price": 1.25},
                {"kind": "YAT", "fund_code": "OLD", "fund_name": "Old Date", "date": "2026-08-24", "price": 4.0},
                {"kind": "YAT", "fund_code": "PLUS1", "fund_name": "Plus 1", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "MIS_FRESH", "fund_name": "Missing Fresh", "date": "2026-08-24", "price": 0.0},
                {"kind": "YAT", "fund_code": "MIS_ALREADY", "fund_name": "Missing Already", "date": "2026-08-24", "price": 0.0},
                {"kind": "YAT", "fund_code": "MIS_DIFF", "fund_name": "Missing Diff", "date": "2026-08-24", "price": 0.0},
                {"kind": "YAT", "fund_code": "MIS_NOROW", "fund_name": "Missing No Row", "date": "2026-08-24", "price": 0.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "AAK", "date": "2026-08-21", "price": 9.5},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation") as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        # 1. Unchanged: AAK (1)
        assert res.unchanged_count == 1
        # 2. Inserted or Updated: TI2 (1) + PLUS1 (1) = 2
        assert res.inserted_or_updated_count == 2
        # 3. Older date skipped: OLD (1)
        assert res.stale_skipped_count == 1
        # 4. Stale price_not_published: MIS_FRESH (1) + MIS_DIFF (1) = 2
        assert res.price_not_published_stale_count == 2
        # 5. Stale instrument_inactive: DEACT_FRESH (1) = 1
        assert res.instrument_inactive_stale_count == 1

        # 6. Verify zero writes executed
        mock_recon.assert_not_called()
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Unpriced Fund Classification & Advanced History-Aware Edge Case Tests
# ---------------------------------------------------------------------------


def test_reconciliation_active_unpriced_no_history_is_deactivated():
    """Active fund with 0 price and NO stored price row is deactivated."""
    service = TefasPricesService()
    id_act = uuid4()
    db_inst = {
        "UNP": TefasDbInstrument(id=id_act, symbol="UNP", company_name="Unpriced Fund", is_active=True),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"UNP": "Unpriced Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    clean_nonpos_codes = {"UNP"}
    stored_price_ids = set()  # UNP has never had a price row

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=stored_price_ids,
    )

    assert id_act in plan.to_deactivate_ids
    assert "UNP" in plan.to_deactivate_symbols
    assert id_act in plan.unpriced_deactivated_ids
    assert "UNP" in plan.unpriced_deactivated_symbols
    assert id_act not in plan.disappeared_deactivated_ids


def test_reconciliation_inactive_unpriced_no_history_remains_inactive():
    """Inactive fund with 0 price and NO stored price row remains inactive (not reactivated)."""
    service = TefasPricesService()
    id_inact = uuid4()
    db_inst = {
        "UNP": TefasDbInstrument(id=id_inact, symbol="UNP", company_name="Unpriced Inactive Fund", is_active=False),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"UNP": "Unpriced Inactive Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    clean_nonpos_codes = {"UNP"}
    stored_price_ids = set()

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=stored_price_ids,
    )

    assert id_inact not in plan.to_reactivate_ids
    assert "UNP" not in plan.to_reactivate_symbols
    assert id_inact not in plan.to_deactivate_ids


def test_reconciliation_not_in_db_unpriced_added_as_inactive():
    """New fund in bulletin with 0 price and not in DB is added as inactive."""
    service = TefasPricesService()
    db_inst = {
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"NEW_UNP": "New Unpriced Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    clean_nonpos_codes = {"NEW_UNP"}

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=set(),
    )

    assert any(item.fund_code == "NEW_UNP" for item in plan.to_add_inactive)
    assert not any(item.fund_code == "NEW_UNP" for item in plan.to_add_active)
    assert any(item.fund_code == "NEW_UNP" for item in plan.to_add)


def test_reconciliation_malformed_active_without_history_is_not_deactivated():
    """Active fund with malformed/corrupt price and no history is NOT deactivated."""
    service = TefasPricesService()
    id_malf = uuid4()
    db_inst = {
        "MALF": TefasDbInstrument(id=id_malf, symbol="MALF", company_name="Malformed Fund", is_active=True),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"MALF": "Malformed Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    ambiguous_codes = {"MALF"}

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=set(),
        ambiguous_price_codes=ambiguous_codes,
        stored_price_ids=set(),
    )

    assert id_malf not in plan.to_deactivate_ids
    assert id_malf not in plan.unpriced_deactivated_ids
    assert id_malf not in plan.to_reactivate_ids
    assert "MALF" in plan.ambiguous_codes


def test_reconciliation_malformed_inactive_is_not_reactivated():
    """Inactive fund with malformed price is NOT reactivated."""
    service = TefasPricesService()
    id_malf = uuid4()
    db_inst = {
        "MALF": TefasDbInstrument(id=id_malf, symbol="MALF", company_name="Malformed Inactive Fund", is_active=False),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"MALF": "Malformed Inactive Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    ambiguous_codes = {"MALF"}

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=set(),
        ambiguous_price_codes=ambiguous_codes,
        stored_price_ids=set(),
    )

    assert id_malf not in plan.to_reactivate_ids
    assert id_malf not in plan.to_deactivate_ids
    assert "MALF" in plan.ambiguous_codes


def test_reconciliation_malformed_not_in_db_is_not_added():
    """New fund in bulletin with malformed price is NOT added as active or inactive."""
    service = TefasPricesService()
    db_inst = {
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    observed = {"NEW_MALF": "New Malformed Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    ambiguous_codes = {"NEW_MALF"}

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=set(),
        ambiguous_price_codes=ambiguous_codes,
        stored_price_ids=set(),
    )

    assert not any(item.fund_code == "NEW_MALF" for item in plan.to_add_active)
    assert not any(item.fund_code == "NEW_MALF" for item in plan.to_add_inactive)
    assert "NEW_MALF" in plan.ambiguous_codes


def test_normalization_wrong_date_and_missing_price_classified_as_ambiguous():
    """Wrong date and missing price are classified into ambiguous_or_invalid_price_codes with invalid_count."""
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "WRONG_DATE", "fund_name": "Wrong Date", "date": "2026-08-20", "price": 10.0},
            {"kind": "YAT", "fund_code": "MISS_PRICE", "fund_name": "Missing Price", "date": "2026-08-24", "price": np.nan},
            {"kind": "YAT", "fund_code": "CORRUPT_P", "fund_name": "Corrupt Price", "date": "2026-08-24", "price": "invalid_number"},
        ]
    )
    norm = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert norm.positive_price_codes == {"GOOD"}
    assert norm.clean_nonpositive_price_codes == set()
    assert norm.ambiguous_or_invalid_price_codes == {"WRONG_DATE", "MISS_PRICE", "CORRUPT_P"}
    assert norm.invalid_count == 3
    assert len(norm.valid_prices) == 1


@pytest.mark.parametrize(
    "rows, expected_dup_count",
    [
        (
            [
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 12.0},
            ],
            1,
        ),
        (
            [
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 0.0},
            ],
            1,
        ),
        (
            [
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 0.0},
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 0.0},
            ],
            1,
        ),
        (
            [
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "DUP", "fund_name": "Dup Fund", "date": "2026-08-24", "price": "corrupt"},
            ],
            1,
        ),
    ],
)
def test_normalization_duplicate_rows_are_ambiguous_and_excluded_from_positive_and_clean_nonpos(rows, expected_dup_count):
    """Duplicate fund codes in current bulletin are classified as ambiguous and excluded from price writing."""
    service = TefasPricesService()
    df = make_raw_df(rows)
    norm = service.normalize_and_validate_dataframe(df, CURRENT_DATE)

    assert "DUP" in norm.ambiguous_or_invalid_price_codes
    assert "DUP" not in norm.positive_price_codes
    assert "DUP" not in norm.clean_nonpositive_price_codes
    assert norm.duplicate_count == expected_dup_count
    assert len(norm.valid_prices) == 0


def test_job_unpriced_deactivation_alone_does_not_cause_partial_or_email():
    """When an unpriced fund with no history is deactivated and all active funds have prices, status is SUCCESS."""
    id_norm = uuid4()
    id_unp = uuid4()

    db_inst = {
        "NORM": TefasDbInstrument(id=id_norm, symbol="NORM", company_name="Normal Fund", is_active=True),
        "UNP": TefasDbInstrument(id=id_unp, symbol="UNP", company_name="Unpriced Fund", is_active=True),
    }
    stored_map = {
        id_norm: {
            "price": Decimal("10.0"),
            "currency": "TRY",
            "previous_close": Decimal("9.5"),
            "change_amount": Decimal("0.5"),
            "change_percent": Decimal("5.26315789"),
            "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.5},
                {"kind": "YAT", "fund_code": "UNP", "fund_name": "Unpriced Fund", "date": "2026-08-24", "price": 0.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation") as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)) as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.catalog_unpriced_deactivated_count == 1
        assert res.catalog_deactivated_count == 1
        assert res.missing_price_count == 0
        assert res.price_not_published_stale_count == 0
        assert res.matched_count == 1
        mock_email.send_email.assert_not_called()


def test_reconciliation_inactive_unpriced_gains_positive_price_is_reactivated_and_priced():
    """Inactive fund with 0 history gains positive price -> reactivated and price upserted in same job."""
    id_react = uuid4()
    db_inst = {
        "REACT": TefasDbInstrument(id=id_react, symbol="REACT", company_name="Reactivating Fund", is_active=False),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    stored_map = {}

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "REACT", "fund_name": "Reactivating Fund", "date": "2026-08-24", "price": 15.0},
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation") as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(2, 0, 0)) as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.catalog_reactivated_count == 1
        assert res.matched_count == 2
        # REACT had no previous price in prev_df -> partial due to previous_price_missing_count = 1
        assert res.previous_price_missing_count == 1
        assert res.status == "partial"


def test_active_fund_with_prior_price_missing_today_remains_active_and_marked_price_not_published():
    """Active fund with prior price in DB, but 0 price today, stays active, gets marked price_not_published."""
    id_had_price = uuid4()
    db_inst = {
        "TEMP_FAIL": TefasDbInstrument(id=id_had_price, symbol="TEMP_FAIL", company_name="Temp Fail Fund", is_active=True),
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }
    stored_map = {
        id_had_price: {
            "price": Decimal("5.0"),
            "currency": "TRY",
            "previous_close": Decimal("4.8"),
            "change_amount": Decimal("0.2"),
            "change_percent": Decimal("4.16666667"),
            "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "TEMP_FAIL", "fund_name": "Temp Fail Fund", "date": "2026-08-24", "price": 0.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation") as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)), \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=1) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        # TEMP_FAIL was NOT deactivated because it had price history!
        assert res.catalog_deactivated_count == 0
        assert res.missing_price_count == 1
        assert res.price_not_published_stale_count == 1
        assert res.status == "partial"
        mock_stale.assert_any_call(conn_mock, [id_had_price], "price_not_published")
        mock_email.send_email.assert_called_once()


def test_new_positive_fund_added_active_and_priced_in_same_tx():
    """Brand new positive fund added active and price written in same transaction."""
    id_new = uuid4()
    db_inst = {
        "NORM": TefasDbInstrument(id=uuid4(), symbol="NORM", company_name="Normal Fund", is_active=True),
    }

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "ETN", "fund_name": "New ETN Fund", "date": "2026-08-24", "price": 25.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={"ETN": id_new}) as mock_recon, \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(2, 0, 0)) as mock_upsert, \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.catalog_added_count == 1
        assert res.catalog_active_added_count == 1
        assert res.catalog_unpriced_added_count == 0
        assert res.matched_count == 2
        # ETN has no prev price -> previous_price_missing_count = 1
        assert res.previous_price_missing_count == 1


def test_fund_disappeared_from_bulletin_soft_deactivated_and_marked_instrument_inactive():
    """Fund missing completely from bulletin is deactivated and marked instrument_inactive."""
    id_disapp = uuid4()
    db_inst = {
        f"NORM{i}": TefasDbInstrument(id=uuid4(), symbol=f"NORM{i}", company_name=f"Normal Fund {i}", is_active=True)
        for i in range(9)
    }
    db_inst["DISAPP"] = TefasDbInstrument(id=id_disapp, symbol="DISAPP", company_name="Disappeared Fund", is_active=True)

    stored_map = {
        id_disapp: {
            "price": Decimal("20.0"),
            "currency": "TRY",
            "previous_close": None,
            "change_amount": None,
            "change_percent": None,
            "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    service = TefasPricesService()
    observed = {f"NORM{i}": f"Normal Fund {i}" for i in range(9)}
    positive_codes = set(observed.keys())

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        stored_price_ids=set(stored_map.keys()),
    )

    assert id_disapp in plan.to_deactivate_ids
    assert id_disapp in plan.disappeared_deactivated_ids
    assert id_disapp not in plan.unpriced_deactivated_ids


def test_zero_price_funds_count_toward_catalog_shrink_guard():
    """80% shrink safety guard calculates over ALL observed YAT funds, including 0.0 price funds."""
    service = TefasPricesService()
    db_inst = {
        f"F{i:03d}": TefasDbInstrument(id=uuid4(), symbol=f"F{i:03d}", company_name=f"Fund {i}", is_active=True)
        for i in range(100)
    }

    # 80 total observed: 70 positive, 10 zero-price
    observed = {f"F{i:03d}": f"Fund {i}" for i in range(80)}
    positive_codes = {f"F{i:03d}" for i in range(70)}
    clean_nonpos_codes = {f"F{i:03d}" for i in range(70, 80)}

    # 80/100 = 80.0% -> Must pass 80% safety guard because total observed is 80
    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=set(),
    )
    assert plan.observed_count == 80


def test_dry_run_and_apply_produce_identical_classification_plan():
    """Verify that dry-run and apply-db compute the exact same reconciliation classification."""
    service = TefasPricesService()
    id_norm = uuid4()
    id_unp = uuid4()
    id_temp = uuid4()
    id_disapp = uuid4()

    db_inst = {
        "NORM": TefasDbInstrument(id=id_norm, symbol="NORM", company_name="Normal Fund", is_active=True),
        "UNP": TefasDbInstrument(id=id_unp, symbol="UNP", company_name="Unpriced Fund", is_active=True),
        "TEMP": TefasDbInstrument(id=id_temp, symbol="TEMP", company_name="Temp Outage Fund", is_active=True),
        "DISAPP": TefasDbInstrument(id=id_disapp, symbol="DISAPP", company_name="Disappeared Fund", is_active=True),
    }
    stored_map = {
        id_norm: {"price": Decimal("10.0"), "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc), "is_stale": False, "stale_reason": None},
        id_temp: {"price": Decimal("5.0"), "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc), "is_stale": False, "stale_reason": None},
        id_disapp: {"price": Decimal("20.0"), "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc), "is_stale": False, "stale_reason": None},
    }
    stored_price_ids = set(stored_map.keys())

    observed = {
        "NORM": "Normal Fund",
        "UNP": "Unpriced Fund",
        "TEMP": "Temp Outage Fund",
        "NEW_POS": "New Positive Fund",
        "NEW_UNP": "New Unpriced Fund",
    }
    positive_codes = {"NORM", "NEW_POS"}
    clean_nonpos_codes = {"UNP", "TEMP", "NEW_UNP"}

    plan = service.build_catalog_reconciliation_plan(
        observed,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=stored_price_ids,
    )

    assert [item.fund_code for item in plan.to_add_active] == ["NEW_POS"]
    assert [item.fund_code for item in plan.to_add_inactive] == ["NEW_UNP"]
    assert plan.unpriced_deactivated_ids == [id_unp]
    assert plan.disappeared_deactivated_ids == [id_disapp]
    # TEMP must NOT be in to_deactivate_ids because it has price history
    assert id_temp not in plan.to_deactivate_ids


def test_db_error_during_price_upsert_rolls_back_catalog_reconciliation():
    """Simulated DB error during price upsert step rolls back catalog reconciliation in atomic transaction."""
    id_norm = uuid4()
    db_inst = {
        "NORM": TefasDbInstrument(id=id_norm, symbol="NORM", company_name="Normal Fund", is_active=True),
    }

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.0},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    conn_mock = MagicMock()
    tx_mock = MagicMock()
    conn_mock.transaction.return_value = tx_mock
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", return_value={}), \
         patch.object(TefasLatestPricesRepository, "upsert_prices", side_effect=RuntimeError("Database constraint error")):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert "Database constraint error" in res.error or "Veritabanı" in res.error or "Operasyonel" in res.error


def test_no_hardcoded_funds_exclusion_list():
    """Verify that seven zero-price funds (FNN, GKE, GKO, GMV, GSU, RPE, TMH) are NOT hardcoded in source files."""
    import inspect

    import app.jobs.tefas_latest_prices_job as tpj
    import app.models.tefas_prices as tpm
    import app.repositories.tefas_latest_prices_repository as tpr
    import app.services.tefas_prices_service as tps

    sources = [
        inspect.getsource(tps),
        inspect.getsource(tpj),
        inspect.getsource(tpr),
        inspect.getsource(tpm),
    ]
    hardcoded_funds = ["FNN", "GKE", "GKO", "GMV", "GSU", "RPE", "TMH"]
    for src in sources:
        for f in hardcoded_funds:
            assert f not in src, f"Found hardcoded fund symbol '{f}' in source code!"


def test_no_is_selectable_in_tefas_source_files():
    """Verify that is_selectable is not present in TEFAS models, repositories, or services."""
    import inspect

    import app.jobs.tefas_latest_prices_job as tpj
    import app.models.tefas_prices as tpm
    import app.repositories.tefas_latest_prices_repository as tpr
    import app.services.tefas_prices_service as tps

    sources = [
        inspect.getsource(tps),
        inspect.getsource(tpj),
        inspect.getsource(tpr),
        inspect.getsource(tpm),
    ]
    for src in sources:
        assert "is_selectable" not in src, "Found 'is_selectable' in source code!"


def test_malformed_record_causes_invalid_count_and_partial_job():
    """Malformed/unparseable price row causes invalid_count > 0 and job status PARTIAL with warning email.

    Explicitly verifies that the malformed fund is NOT deactivated, NOT reactivated, and NOT added.
    """
    id_norm = uuid4()
    id_malf = uuid4()
    db_inst = {
        "NORM": TefasDbInstrument(id=id_norm, symbol="NORM", company_name="Normal Fund", is_active=True),
        "MALF": TefasDbInstrument(id=id_malf, symbol="MALF", company_name="Malformed Fund", is_active=True),
    }
    stored_map = {
        id_norm: {
            "price": Decimal("10.0"),
            "currency": "TRY",
            "previous_close": Decimal("10.0"),
            "change_amount": Decimal("0.0"),
            "change_percent": Decimal("0.0"),
            "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
    }

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "fund_name": "Normal Fund", "date": "2026-08-24", "price": 10.0},
                {"kind": "YAT", "fund_code": "MALF", "fund_name": "Malformed Fund", "date": "2026-08-24", "price": "corrupt_val"},
            ]
        ),
        CURRENT_DATE,
        make_raw_df(
            [
                {"kind": "YAT", "fund_code": "NORM", "date": "2026-08-21", "price": 10.0},
            ]
        ),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    captured_plan = None

    def fake_apply(conn, plan):
        nonlocal captured_plan
        captured_plan = plan
        return {}

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", side_effect=fake_apply), \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(1, 0, 0)), \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.invalid_count == 1
        assert res.status == "partial"
        mock_email.send_email.assert_called_once()

        # Crucial: Malformed fund MUST NOT be deactivated or added as inactive
        assert captured_plan is not None
        assert id_malf not in captured_plan.to_deactivate_ids
        assert id_malf not in captured_plan.unpriced_deactivated_ids
        assert id_malf not in captured_plan.to_reactivate_ids
        assert not any(item.fund_code == "MALF" for item in captured_plan.to_add_active)
        assert not any(item.fund_code == "MALF" for item in captured_plan.to_add_inactive)
        assert "MALF" in captured_plan.ambiguous_codes


def test_unidentifiable_fund_code_null_skips_disappearance_reconciliation_regression_f0_f9():
    """Specific regression test: 10 active DB funds (F0..F9), DataFrame has 9 valid (F0..F8) and 1 fund_code=NULL.

    80% guard passes (9/10 = 90%), but because 1 catalog row is unidentifiable, F9 MUST NOT be deactivated as disappeared!
    """
    db_inst = {
        f"F{i}": TefasDbInstrument(id=uuid4(), symbol=f"F{i}", company_name=f"Fund {i}", is_active=True)
        for i in range(10)
    }
    stored_map = {
        inst.id: {
            "price": Decimal("10.0"),
            "currency": "TRY",
            "previous_close": Decimal("10.0"),
            "change_amount": Decimal("0.0"),
            "change_percent": Decimal("0.0"),
            "price_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
            "provider_code": "TEFAS",
            "is_stale": False,
            "stale_reason": None,
        }
        for inst in db_inst.values()
    }

    # 9 valid funds (F0..F8) + 1 NULL fund_code
    current_rows = [
        {"kind": "YAT", "fund_code": f"F{i}", "fund_name": f"Fund {i}", "date": "2026-08-24", "price": 10.0}
        for i in range(9)
    ]
    current_rows.append(
        {"kind": "YAT", "fund_code": None, "fund_name": "Unidentifiable Fund", "date": "2026-08-24", "price": 10.0}
    )

    prev_rows = [
        {"kind": "YAT", "fund_code": f"F{i}", "date": "2026-08-21", "price": 10.0}
        for i in range(9)
    ]

    mock_provider = MagicMock()
    mock_provider.fetch_current_and_previous_prices.return_value = (
        make_raw_df(current_rows),
        CURRENT_DATE,
        make_raw_df(prev_rows),
        PREVIOUS_DATE,
    )

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    conn_mock = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn_mock

    captured_plan = None

    def fake_apply(conn, plan):
        nonlocal captured_plan
        captured_plan = plan
        return {}

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value=db_inst), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value=stored_map), \
         patch.object(TefasLatestPricesRepository, "apply_catalog_reconciliation", side_effect=fake_apply), \
         patch.object(TefasLatestPricesRepository, "upsert_prices", return_value=(9, 0, 0)), \
         patch.object(TefasLatestPricesRepository, "mark_stale_prices", return_value=0):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.catalog_observed_count == 9
        assert res.catalog_identity_invalid_count == 1
        assert res.catalog_disappearance_reconciliation_skipped is True
        assert res.catalog_disappearance_skip_reason == "unidentifiable_catalog_rows"
        assert res.catalog_disappeared_deactivated_count == 0
        assert res.catalog_deactivated_count == 0
        assert res.status == "partial"
        mock_email.send_email.assert_called_once()

        # Check plan
        assert captured_plan is not None
        assert captured_plan.disappearance_reconciliation_skipped is True
        assert captured_plan.disappeared_deactivated_ids == []
        assert captured_plan.disappeared_deactivated_symbols == []
        assert "F9" not in captured_plan.disappeared_deactivated_symbols
        assert "F9" not in captured_plan.to_deactivate_symbols


@pytest.mark.parametrize(
    "bad_code",
    ["", "   ", None, np.nan, "NAN", "NONE", "<NA>", "NAT"],
)
def test_sentinel_and_missing_fund_codes_cause_identity_invalid_count(bad_code):
    """Empty, whitespace, NaN, and sentinel fund codes produce identity_invalid_count > 0 and identity_complete=False."""
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "VALID", "fund_name": "Valid Fund", "price": 10.0},
            {"kind": "YAT", "fund_code": bad_code, "fund_name": "Bad Code Fund", "price": 10.0},
        ]
    )
    obs = service.extract_catalog_observations(df)

    assert obs.identity_invalid_count == 1
    assert obs.identity_complete is False
    assert "VALID" in obs.observed_funds
    assert len(obs.observed_funds) == 1


def test_missing_or_non_yat_kind_causes_identity_invalid_count():
    """Missing or non-YAT kind values cause identity_invalid_count > 0 and identity_complete=False."""
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "F1", "fund_name": "Fund 1", "price": 10.0},
            {"kind": None, "fund_code": "F2", "fund_name": "Fund 2", "price": 10.0},
            {"kind": "EMK", "fund_code": "F3", "fund_name": "Fund 3", "price": 10.0},
        ]
    )
    obs = service.extract_catalog_observations(df)

    assert obs.identity_invalid_count == 2
    assert obs.identity_complete is False
    assert obs.observed_funds == {"F1": "Fund 1"}


def test_unidentifiable_row_prevents_disappearance_even_if_another_fund_missing():
    """When an unidentifiable row is present, NO disappearance deactivations happen even for missing DB funds."""
    service = TefasPricesService()
    db_inst = {
        f"F{i}": TefasDbInstrument(id=uuid4(), symbol=f"F{i}", company_name=f"Fund {i}", is_active=True)
        for i in range(10)
    }

    # Only F0..F7 in observed_funds (8 funds), identity_complete=False
    observed_funds = {f"F{i}": f"Fund {i}" for i in range(8)}
    positive_codes = set(observed_funds.keys())

    plan = service.build_catalog_reconciliation_plan(
        observed_funds,
        db_inst,
        positive_price_codes=positive_codes,
        stored_price_ids=set(),
        identity_complete=False,
    )

    assert plan.disappearance_reconciliation_skipped is True
    assert plan.disappeared_deactivated_ids == []
    assert plan.disappeared_deactivated_symbols == []
    assert plan.to_deactivate_ids == []


def test_unidentifiable_row_allows_clean_nonpositive_unpriced_deactivation():
    """Unidentifiable catalog row skips disappearance, but still deactivates clean non-positive funds with no history."""
    service = TefasPricesService()
    id_unp = uuid4()
    id_norm = uuid4()
    db_inst = {
        "UNP": TefasDbInstrument(id=id_unp, symbol="UNP", company_name="Unpriced Fund", is_active=True),
        "NORM": TefasDbInstrument(id=id_norm, symbol="NORM", company_name="Normal Fund", is_active=True),
    }

    observed_funds = {"UNP": "Unpriced Fund", "NORM": "Normal Fund"}
    positive_codes = {"NORM"}
    clean_nonpos_codes = {"UNP"}

    plan = service.build_catalog_reconciliation_plan(
        observed_funds,
        db_inst,
        positive_price_codes=positive_codes,
        clean_nonpositive_price_codes=clean_nonpos_codes,
        stored_price_ids=set(),
        identity_complete=False,
    )

    # UNP is still deactivated because its code is identifiable and its price is clean non-positive with no history
    assert id_unp in plan.unpriced_deactivated_ids
    assert id_unp in plan.to_deactivate_ids
    # Disappearance is skipped
    assert plan.disappearance_reconciliation_skipped is True
    assert plan.disappeared_deactivated_ids == []


def test_clean_catalog_identity_performs_normal_disappearance_deactivation():
    """When catalog identity is complete, active DB fund missing from observed_funds is deactivated."""
    service = TefasPricesService()
    id_f9 = uuid4()
    db_inst = {
        f"F{i}": TefasDbInstrument(id=uuid4(), symbol=f"F{i}", company_name=f"Fund {i}", is_active=True)
        for i in range(9)
    }
    db_inst["F9"] = TefasDbInstrument(id=id_f9, symbol="F9", company_name="Fund 9", is_active=True)

    observed_funds = {f"F{i}": f"Fund {i}" for i in range(9)}
    positive_codes = set(observed_funds.keys())

    plan = service.build_catalog_reconciliation_plan(
        observed_funds,
        db_inst,
        positive_price_codes=positive_codes,
        stored_price_ids=set(),
        identity_complete=True,
    )

    assert plan.disappearance_reconciliation_skipped is False
    assert id_f9 in plan.disappeared_deactivated_ids
    assert "F9" in plan.disappeared_deactivated_symbols
    assert id_f9 in plan.to_deactivate_ids


def test_corrupt_price_with_valid_fund_code_does_not_break_catalog_identity():
    """A row with valid fund_code and 'corrupt_val' price has a valid catalog identity (identity_complete=True)."""
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "price": 10.0},
            {"kind": "YAT", "fund_code": "CORRUPT_P", "fund_name": "Corrupt Price Fund", "price": "corrupt_val"},
        ]
    )
    obs = service.extract_catalog_observations(df)

    assert obs.identity_invalid_count == 0
    assert obs.identity_complete is True
    assert "GOOD" in obs.observed_funds
    assert "CORRUPT_P" in obs.observed_funds


def test_wrong_date_with_valid_fund_code_does_not_break_catalog_identity():
    """A row with valid fund_code and wrong date has a valid catalog identity (identity_complete=True)."""
    service = TefasPricesService()
    df = make_raw_df(
        [
            {"kind": "YAT", "fund_code": "GOOD", "fund_name": "Good Fund", "date": "2026-08-24", "price": 10.0},
            {"kind": "YAT", "fund_code": "WRONG_D", "fund_name": "Wrong Date Fund", "date": "2026-08-01", "price": 10.0},
        ]
    )
    obs = service.extract_catalog_observations(df)

    assert obs.identity_invalid_count == 0
    assert obs.identity_complete is True
    assert "GOOD" in obs.observed_funds
    assert "WRONG_D" in obs.observed_funds


def test_dry_run_and_apply_produce_identical_disappearance_skip_decision():
    """Dry-run and apply-db compute identical disappearance skip flags."""
    service = TefasPricesService()
    db_inst = {
        f"F{i}": TefasDbInstrument(id=uuid4(), symbol=f"F{i}", company_name=f"Fund {i}", is_active=True)
        for i in range(10)
    }
    observed_funds = {f"F{i}": f"Fund {i}" for i in range(9)}

    plan_dry = service.build_catalog_reconciliation_plan(
        observed_funds,
        db_inst,
        identity_complete=False,
    )
    plan_apply = service.build_catalog_reconciliation_plan(
        observed_funds,
        db_inst,
        identity_complete=False,
    )

    assert plan_dry.disappearance_reconciliation_skipped == plan_apply.disappearance_reconciliation_skipped is True
    assert plan_dry.disappeared_deactivated_ids == plan_apply.disappeared_deactivated_ids == []
    assert plan_dry.disappearance_skip_reason == plan_apply.disappearance_skip_reason == "unidentifiable_catalog_rows"


def test_reports_format_identity_invalid_and_disappearance_skip():
    """Plain-text and HTML email reports properly format catalog identity invalid count and skip explanation."""
    from app.services.tefas_prices_report import (
        format_tefas_prices_email_html,
        format_tefas_prices_email_text,
    )

    res = TefasPricesJobResult(
        status="partial",
        started_at="2026-08-21T18:00:00+00:00",
        finished_at="2026-08-21T18:00:05+00:00",
        duration_seconds=5.0,
        tefas_data_date="2026-08-21",
        catalog_observed_count=9,
        catalog_identity_invalid_count=1,
        catalog_disappearance_reconciliation_skipped=True,
        catalog_disappearance_skip_reason="unidentifiable_catalog_rows",
    )

    txt = format_tefas_prices_email_text(res)
    html_out = format_tefas_prices_email_html(res)

    assert "Kimliği Belirlenemeyen Katalog Satırı: 1" in txt
    assert "Kayıp Fon Mutabakatı: ATLANDI" in txt
    assert "Kimliği Belirsiz Katalog Satırı" in html_out
    assert "Kayıp Mutabakatı Atlandı" in html_out


def test_tefas_provider_request_executed_lifecycle_paths():
    """Verify job-level provider_request_executed lifecycle paths for TEFAS price job."""
    mock_provider = MagicMock()
    mock_email = MagicMock()

    # 1. Provider fetch raises exception -> provider_request_executed is True
    mock_provider.fetch_current_and_previous_prices.side_effect = RuntimeError("TEFAS scraper connection error")
    res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
    assert res.status == "failed"
    assert res.provider_request_executed is True

    # 2. Provider fetch succeeds -> provider_request_executed is True
    import pandas as pd
    t_date = date(2026, 8, 21)
    df_curr = pd.DataFrame([
        {"FONKODU": "AA1", "FONUNVAN": "Ak Portfoy Fonu", "FIYAT": 10.5, "TARIH": t_date}
    ])
    df_prev = pd.DataFrame([
        {"FONKODU": "AA1", "FIYAT": 10.0}
    ])
    mock_provider.fetch_current_and_previous_prices.side_effect = None
    mock_provider.fetch_current_and_previous_prices.return_value = (df_curr, t_date, df_prev, date(2026, 8, 20))

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.tefas_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(TefasLatestPricesRepository, "get_all_tefas_instruments", return_value={}), \
         patch.object(TefasLatestPricesRepository, "get_stored_latest_prices", return_value={}):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.provider_request_executed is True
