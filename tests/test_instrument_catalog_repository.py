from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.repositories.instrument_catalog_repository import (
    InstrumentCatalogRepository,
    KapMemberMappingRepository,
)


def _connection_with_rows(rows):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows
    return conn, cursor


def test_active_symbols_are_read_from_supabase_and_normalized():
    conn, cursor = _connection_with_rows([("thyao",), ("ASELS",)])

    symbols = InstrumentCatalogRepository.get_active_symbols(
        conn,
        market="BIST100",
        asset_type="stock",
        yahoo_suffix=True,
    )

    assert symbols == ["THYAO.IS", "ASELS.IS"]
    query, params = cursor.execute.call_args.args
    assert "public.instruments" in query
    assert "is_active = true" in query
    assert params == ("BIST100", "stock")


def test_kap_mappings_are_joined_to_active_instruments():
    verified_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    conn, cursor = _connection_with_rows(
        [("THYAO", "Türk Hava Yolları", "oid-1", "THY KAP", verified_at)]
    )

    mappings = KapMemberMappingRepository.get_active_bist_mappings(conn)

    assert mappings == {
        "THYAO": {
            "symbol": "THYAO",
            "company_name": "THY KAP",
            "mkk_member_oid": "oid-1",
            "last_verified_at": verified_at.isoformat(),
        }
    }
    assert "public.kap_member_mappings" in cursor.execute.call_args.args[0]


def test_kap_mapping_upsert_targets_instrument_id_not_json_state():
    conn, cursor = _connection_with_rows([])

    written = KapMemberMappingRepository.upsert_bist_mappings(
        conn,
        {
            "THYAO": {
                "mkk_member_oid": "oid-1",
                "company_name": "Türk Hava Yolları",
            }
        },
    )

    assert written == 1
    query, rows = cursor.executemany.call_args.args
    assert "ON CONFLICT (instrument_id) DO UPDATE" in query
    assert rows == [("oid-1", "Türk Hava Yolları", "THYAO")]
