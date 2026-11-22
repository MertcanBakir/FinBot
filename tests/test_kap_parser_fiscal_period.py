import json
import os

from app.providers.earnings.kap_provider import (
    extract_fiscal_period_from_row,
    parse_kap_financial_calendar_html,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "kap")


def load_fixture_json(filename: str):
    filepath = os.path.join(FIXTURE_DIR, filename)
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def test_extract_fiscal_period_taxonomy_classes():
    class DummyTr:
        def __init__(self, cls):
            self.cls = cls
        def get(self, attr, default=None):
            if attr == "class":
                return self.cls
            return default

    assert extract_fiscal_period_from_row(DummyTr(["oda_FirstQuarter|"]), ["oda_FirstQuarter|"]) == "Q1"
    assert extract_fiscal_period_from_row(DummyTr(["oda_SecondQuarter|"]), ["oda_SecondQuarter|"]) == "Q2"
    assert extract_fiscal_period_from_row(DummyTr(["oda_ThirdQuarter|"]), ["oda_ThirdQuarter|"]) == "Q3"
    assert extract_fiscal_period_from_row(DummyTr(["oda_Annual|"]), ["oda_Annual|"]) == "FY"


def test_extract_fiscal_period_turkish_and_english_label_fallbacks():
    class DummyTr:
        def get(self, attr, default=None):
            return []

    assert extract_fiscal_period_from_row(DummyTr(), ["1. Çeyrek"]) == "Q1"
    assert extract_fiscal_period_from_row(DummyTr(), ["First Quarter"]) == "Q1"
    assert extract_fiscal_period_from_row(DummyTr(), ["2. Çeyrek"]) == "Q2"
    assert extract_fiscal_period_from_row(DummyTr(), ["Second Quarter"]) == "Q2"
    assert extract_fiscal_period_from_row(DummyTr(), ["3. Çeyrek"]) == "Q3"
    assert extract_fiscal_period_from_row(DummyTr(), ["Third Quarter"]) == "Q3"
    assert extract_fiscal_period_from_row(DummyTr(), ["Yıllık"]) == "FY"
    assert extract_fiscal_period_from_row(DummyTr(), ["Annual"]) == "FY"


def test_ccola_golden_fixture_q2_period():
    fixture = load_fixture_json("ccola_1627747.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 1
    row = parse_res.rows[0]
    assert row["period_start"] == "2026-04-01"
    assert row["period_end"] == "2026-06-30"
    assert row["planned_date"] == "2026-08-10"
    assert row["fiscal_period"] == "Q2"


def test_dohol_golden_fixture_q1_and_q2():
    fixture = load_fixture_json("dohol_1642837.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 2
    r1, r2 = parse_res.rows

    assert r1["fiscal_period"] == "Q1"
    assert r1["period_start"] == "2026-01-01"
    assert r1["period_end"] == "2026-03-31"
    assert r1["planned_date"] == "2026-05-07"

    assert r2["fiscal_period"] == "Q2"
    assert r2["period_start"] == "2026-01-01"
    assert r2["period_end"] == "2026-06-30"
    assert r2["planned_date"] == "2026-08-11"


def test_doas_golden_fixture_q2():
    fixture = load_fixture_json("doas_1643124.json")
    html_body = "".join(fixture[0]["disclosureBody"])
    parse_res = parse_kap_financial_calendar_html(html_body)

    assert not parse_res.parser_failed
    assert len(parse_res.rows) == 1
    row = parse_res.rows[0]
    assert row["fiscal_period"] == "Q2"
    assert row["period_start"] == "2026-01-01"
    assert row["period_end"] == "2026-06-30"
    assert row["planned_date"] == "2026-08-19"
    # Ensure 2026-01-01 is period_start, NOT planned_date
    assert row["planned_date"] != "2026-01-01"


def test_pgsus_golden_fixtures():
    # 1. FY 2025
    fix_fy = load_fixture_json("pgsus_1557942.json")
    res_fy = parse_kap_financial_calendar_html("".join(fix_fy[0]["disclosureBody"]))
    assert not res_fy.parser_failed
    assert res_fy.rows[0]["fiscal_period"] == "FY"
    assert res_fy.rows[0]["period_start"] == "2025-01-01"
    assert res_fy.rows[0]["period_end"] == "2025-12-31"
    assert res_fy.rows[0]["planned_date"] == "2026-03-04"

    # 2. Q1 2026
    fix_q1 = load_fixture_json("pgsus_1594449.json")
    res_q1 = parse_kap_financial_calendar_html("".join(fix_q1[0]["disclosureBody"]))
    assert not res_q1.parser_failed
    assert res_q1.rows[0]["fiscal_period"] == "Q1"
    assert res_q1.rows[0]["period_start"] == "2026-01-01"
    assert res_q1.rows[0]["period_end"] == "2026-03-31"
    assert res_q1.rows[0]["planned_date"] == "2026-05-11"

    # 3. Q2 2026
    fix_q2 = load_fixture_json("pgsus_1637651.json")
    res_q2 = parse_kap_financial_calendar_html("".join(fix_q2[0]["disclosureBody"]))
    assert not res_q2.parser_failed
    assert res_q2.rows[0]["fiscal_period"] == "Q2"
    assert res_q2.rows[0]["period_start"] == "2026-01-01"
    assert res_q2.rows[0]["period_end"] == "2026-06-30"
    assert res_q2.rows[0]["planned_date"] == "2026-08-12"


def test_explanation_text_with_dates_is_ignored():
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda-54000_Financial-Calender-row-10 data-input-row">
            <td>oda_ExplanationTextBlock|</td>
            <td></td>
            <td>Şirketimizin 01.01.2026 – 30.06.2026 ara hesap dönemine ilişkin finansal raporlarının 19 Ağustos 2026 tarihinde kamuya açıklanması planlanmaktadır.</td>
        </tr>
        <tr class="oda_SecondQuarter| data-input-row">
            <td>oda_SecondQuarter|</td>
            <td></td>
            <td>2. Çeyrek</td>
            <td>01/01/2026</td>
            <td>30/06/2026</td>
            <td>19/08/2026</td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert not res.parser_failed
    assert len(res.rows) == 1
    assert res.rows[0]["planned_date"] == "2026-08-19"


def test_long_narrative_text_mentioning_second_quarter_is_ignored():
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr>
            <td>Açıklama</td>
            <td>Şirketimizin 2. Çeyrek finansal raporlarının 19.08.2026 tarihinde açıklanması planlanmaktadır. Ancak bu uzun açıklama metnidir.</td>
        </tr>
        <tr class="oda_SecondQuarter| data-input-row">
            <td>oda_SecondQuarter|</td>
            <td></td>
            <td>2. Çeyrek</td>
            <td>01/01/2026</td>
            <td>30/06/2026</td>
            <td>19/08/2026</td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert not res.parser_failed
    assert len(res.rows) == 1
    assert res.rows[0]["fiscal_period"] == "Q2"


def test_blank_fiscal_taxonomy_rows_do_not_fail_parser():
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda_FirstQuarter| data-input-row">
            <td>oda_FirstQuarter|</td>
            <td></td>
            <td>1. Çeyrek</td>
            <td></td>
            <td></td>
            <td></td>
        </tr>
        <tr class="oda_ThirdQuarter| data-input-row">
            <td>oda_ThirdQuarter|</td>
            <td></td>
            <td>3. Çeyrek</td>
            <td>-</td>
            <td>-</td>
            <td>-</td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert not res.parser_failed
    assert len(res.rows) == 0


def test_malformed_planned_date_in_taxonomy_row_fails_parser():
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda_SecondQuarter| data-input-row">
            <td>oda_SecondQuarter|</td>
            <td></td>
            <td>2. Çeyrek</td>
            <td>01/01/2026</td>
            <td>30/06/2026</td>
            <td>31/02/2026</td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert res.parser_failed
    assert any("Malformed" in w or "Invalid" in w for w in res.warnings)


def test_fiscal_row_with_only_period_start_populated_does_not_set_planned_date():
    html = """
    <table class="tbl_oda-54000_Financial-Calender">
        <tr class="oda_SecondQuarter| data-input-row">
            <td>oda_SecondQuarter|</td>
            <td></td>
            <td>2. Çeyrek</td>
            <td>01/01/2026</td>
            <td></td>
            <td></td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert not res.parser_failed
    assert len(res.rows) == 0


def test_generic_three_date_table_without_header_or_taxonomy_evidence_is_rejected():
    html = """
    <table>
        <tr>
            <td>Tarih 1</td>
            <td>Tarih 2</td>
            <td>Tarih 3</td>
        </tr>
        <tr>
            <td>01/01/2026</td>
            <td>30/06/2026</td>
            <td>19/08/2026</td>
        </tr>
    </table>
    """
    res = parse_kap_financial_calendar_html(html)
    assert res.parser_failed
    assert any("Unrecognized" in w or "No <table>" in w for w in res.warnings)
