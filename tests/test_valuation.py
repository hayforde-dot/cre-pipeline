"""Valuation replication tests — the engine must reproduce the two
income-approach templates cell-for-cell, fed with the workbooks' own
NOI/CFO vectors."""
import pytest
from cre.valuation import direct_cap_value, dcf_valuation, run_valuation
from run_pipeline import run

# 'Direct Cap'!I42 and 'Sales Comps'!L14 -> I14
DC_NOI = 3611337.516654117
DC_CAP = 0.065
DC_VALUE = 55559038.717755646

# DCF - Template: NOI row 80, years 1..11 (cols I..S)
DCF_NOI = {1: 3564225.381476833, 2: 3685264.4341401714, 3: 3810243.2747535952,
           4: 3939286.928546658, 5: 4072524.324619207, 6: 4210088.416531809,
           7: 4352116.306593663, 8: 4498749.373960793, 9: 4650133.406660727,
           10: 4806418.737663396, 11: 4967760.385121621}


def test_direct_cap_matches_sales_comps_cell():
    assert direct_cap_value(DC_NOI, DC_CAP) == pytest.approx(DC_VALUE, abs=1e-6)


def test_dcf_replicates_template():
    d = dcf_valuation(DCF_NOI, hold=10, going_in_cap=0.065,
                      closing_cost_pct=0.03, sale_expense_pct=0.03)
    assert d["indicated_price"] == pytest.approx(54834236.638105124, abs=1e-5)   # C9
    assert d["all_in_cost"] == pytest.approx(56479263.73724828, abs=1e-5)        # H44
    assert d["going_out_cap"] == pytest.approx(0.07, abs=1e-12)                  # C14
    assert d["sale_price"] == pytest.approx(70968005.50173745, abs=1e-5)         # C15
    assert d["sale_proceeds"] == pytest.approx(68838965.33668531, abs=1e-4)      # C17
    # per-period unlevered CF vector, row 92 cols H..R
    row92 = [-56479263.73724828, 3564225.381476833, 3685264.4341401714,
             3810243.2747535952, 3939286.928546658, 4072524.324619207,
             4210088.416531809, 4352116.306593663, 4498749.373960793,
             4650133.406660727, 73645384.07434872]
    assert d["cash_flows"] == pytest.approx(row92, abs=1e-4)
    assert d["unlevered_irr"] == pytest.approx(0.08662612454338725, abs=1e-7)    # I5
    assert d["equity_multiple"] == pytest.approx(1.9551957411371936, abs=1e-9)   # I6
    assert d["profit"] == pytest.approx(53948752.18438389, abs=1e-4)             # I7


def test_dcf_going_out_cap_override():
    d = dcf_valuation(DCF_NOI, hold=10, going_in_cap=0.065, closing_cost_pct=0.03,
                      sale_expense_pct=0.03, going_out_cap=0.075)
    assert d["going_out_cap"] == 0.075
    assert d["sale_price"] == pytest.approx(DCF_NOI[11] / 0.075, abs=1e-6)


def test_pipeline_valuation_integration():
    con, report, ctx = run()
    v = report["stages"]["valuation"]["base"]
    sid = ctx["scens"]["base"]
    row1 = con.execute(
        "SELECT noi, cfo FROM proforma_lines WHERE deal_id=? AND scenario_id=? AND year=1",
        (ctx["deal_id"], sid)).fetchone()
    # Direct Cap capitalizes reserve-EXCLUSIVE NOI
    assert v["direct_cap"]["value"] == pytest.approx(
        row1["noi"] / v["direct_cap"]["cap_rate"], abs=0.01)
    # DCF derives price from reserve-INCLUSIVE NOI (cfo)
    assert v["dcf"]["indicated_price"] == pytest.approx(
        row1["cfo"] / v["dcf"]["going_in_cap"], abs=0.01)
    assert v["dcf"]["going_out_cap"] == pytest.approx(
        v["dcf"]["going_in_cap"] + 0.0005 * 5, abs=1e-12)
    assert v["direct_cap"]["value"] > 0 and v["dcf"]["unlevered_irr"] is not None
    mid = v["direct_cap"]["sensitivity"][2]
    assert mid[0] == pytest.approx(v["direct_cap"]["cap_rate"])
    assert mid[1] == pytest.approx(v["direct_cap"]["value"], abs=0.01)
