"""Hand-checkable unit tests for Stages 1-3 primitives."""
import pytest
from datetime import date
from cre.finance import irr, xirr, pmt, remaining_balance, equity_multiple, irr_partition
from cre.db import connect
from cre.proforma import build_proforma, unlevered_cash_flows
from cre.capital import create_entity, build_capital_calls, effective_splits


def test_irr_closed_form():
    # invest 100, receive 161.051 in year 5 -> exactly 10%
    assert irr([-100, 0, 0, 0, 0, 161.051]) == pytest.approx(0.10, abs=1e-6)
    assert irr([-1000, 1100]) == pytest.approx(0.10, abs=1e-9)
    assert irr([100, 100]) is None                      # no sign change


def test_xirr_matches_annual():
    ds = [date(2020, 1, 1), date(2021, 1, 1)]
    assert xirr([-1000, 1100], ds) == pytest.approx(0.10, abs=1e-3)  # 366d year


def test_pmt_and_balance():
    # $1M, 6%, 360mo -> classic 5,995.51/mo
    assert pmt(0.06 / 12, 360, 1_000_000) == pytest.approx(5995.51, abs=0.01)
    assert remaining_balance(1_000_000, 0.06, 360, 0) == pytest.approx(1_000_000, abs=0.01)
    assert remaining_balance(1_000_000, 0.06, 360, 360) == pytest.approx(0.0, abs=0.01)


def test_equity_multiple_and_partition():
    assert equity_multiple([-100, 50, 150]) == pytest.approx(2.0)
    p = irr_partition([-100, 0, 0], [0, 10, 10], [0, 0, 120])
    assert p["cfo_share"] + p["reversion_share"] == pytest.approx(1.0)


def _seed_small(con):
    con.execute("""INSERT INTO deals (deal_id, name, units, purchase_price,
        closing_cost_pct, hold_period_years, periods_per_year, exit_cap_rate,
        sale_cost_pct) VALUES (1,'Hand Check',10,1000000,0.0,2,1,0.05,0.0)""")
    con.execute("INSERT INTO scenarios (scenario_id, deal_id, name) VALUES (1,1,'base')")
    con.execute("""INSERT INTO rent_roll (deal_id, floorplan, unit_count,
        market_rent_month) VALUES (1,'A',10,1000)""")
    for k, v in [("market_rent_growth", 0.03), ("general_vacancy_pct", 0.05),
                 ("loss_to_lease_pct", 0.0), ("capital_reserve_per_unit", 0.0)]:
        con.execute("INSERT INTO deal_assumptions VALUES (1,?,?,0,NULL)", (k, v))
    con.execute("""INSERT INTO expense_lines (deal_id, name, category, year1_amount,
        growth_rate) VALUES (1,'Opex','controllable',30000,0.02)""")
    con.commit()


def test_proforma_hand_computed():
    con = connect()
    _seed_small(con)
    build_proforma(con, 1, 1)
    rows = {r["year"]: dict(r) for r in con.execute(
        "SELECT * FROM proforma_lines WHERE deal_id=1 AND scenario_id=1")}
    # Year 1: GPR 10*1000*12 = 120,000; vacancy 5% -> EGR 114,000; NOI 84,000
    assert rows[1]["gpr"] == pytest.approx(120000)
    assert rows[1]["egr"] == pytest.approx(114000)
    assert rows[1]["noi"] == pytest.approx(84000)
    # Year 2: GPR 123,600; EGR 117,420; opex 30,600 -> NOI 86,820
    assert rows[2]["noi"] == pytest.approx(117420 - 30600)
    # Year 3 (forward for exit): GPR 127,308; EGR 120,942.60; opex 31,212 -> 89,730.60
    assert rows[3]["noi"] == pytest.approx(89730.60)
    u = unlevered_cash_flows(con, 1, 1)
    assert u["sale_price"] == pytest.approx(89730.60 / 0.05)      # forward NOI / exit cap
    assert u["total"][0] == pytest.approx(-1000000)
    assert u["total"][2] == pytest.approx(86820 + 89730.60 / 0.05)


def test_effective_splits_match_workbook():
    assert effective_splits(0.9, 0.1, 0.20) == (pytest.approx(0.72), pytest.approx(0.28))
    assert effective_splits(0.9, 0.1, 0.45) == (pytest.approx(0.495), pytest.approx(0.505))


def test_capital_calls_pro_rata_and_complete():
    con = connect()
    _seed_small(con)
    ent = create_entity(con, 1, "P", "irr_hurdle",
                        partners=[{"name": "LP", "role": "LP", "equity_pct": 0.9},
                                  {"name": "GP", "role": "GP", "equity_pct": 0.1}],
                        tiers=[{"tier_no": 1, "hurdle_rate": None, "promote_pct": 0.0}])
    cfs = [-1000000, 50000, -20000, 1200000]
    out = build_capital_calls(con, ent["entity_id"], 1, cfs)
    assert out["total_equity"] == pytest.approx(1020000)
    assert out["totals"]["LP"] == pytest.approx(918000)
    assert out["totals"]["GP"] == pytest.approx(102000)
    n = con.execute("SELECT COUNT(*) c FROM capital_calls").fetchone()["c"]
    assert n == 4  # two partners x two funding periods
