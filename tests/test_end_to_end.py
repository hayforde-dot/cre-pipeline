"""Full-chain verification on the Meladon Haymarket placeholder deal.
Asserts the invariants that make it ONE data model:
  - Stage 3 capital calls == Stage 4 engine contributions == Stage 2 equity
  - every period: sum of partner distributions == distributable cash
  - Co-GP layer conserves the GP stream exactly
  - provided ACTUAL cash flows override projections in the waterfall
  - the generated LP statement (Excel formulas, recalculated by
    LibreOffice) reproduces the engine's totals and IRR
"""
import json
import subprocess
import sys
from pathlib import Path
import pytest
import openpyxl

sys.path.insert(0, str(Path(__file__).parent.parent))
from run_pipeline import run, OUT
from cre.underwriting import record_actual_cf
from cre.waterfall import run_entity

RECALC = Path("/mnt/skills/public/xlsx/scripts/recalc.py")


@pytest.fixture(scope="module")
def pipeline():
    con, report, ctx = run()
    return con, report, ctx


def test_equity_ties_across_stages(pipeline):
    con, report, ctx = pipeline
    sid = ctx["scens"]["base"]
    equity_stage2 = ctx["uw"][("base", "senior")]["metrics"]["equity"]
    calls = report["stages"]["3_capital"]["base"]
    assert sum(calls.values()) == pytest.approx(equity_stage2, abs=0.01)
    res = ctx["wf"]["base"]
    total_contrib = sum(sum(res.contributions[pid]) for pid in res.contributions)
    assert total_contrib == pytest.approx(equity_stage2, abs=0.01)
    # per-partner: capital_calls table == engine contributions
    for pid in res.contributions:
        db_calls = con.execute(
            """SELECT COALESCE(SUM(amount),0) s FROM capital_calls
               WHERE entity_id=? AND scenario_id=? AND partner_id=?""",
            (ctx["fund"]["entity_id"], sid, pid)).fetchone()["s"]
        assert db_calls == pytest.approx(sum(res.contributions[pid]), abs=0.01)


def test_cash_conservation_every_period(pipeline):
    con, report, ctx = pipeline
    for name, res in ctx["wf"].items():
        sid = ctx["scens"][name]
        pcf = con.execute(
            """SELECT operating_cf + capital_event_cf cf FROM period_cash_flows
               WHERE deal_id=? AND scenario_id=? ORDER BY period""",
            (ctx["deal_id"], sid)).fetchall()
        for p, row in enumerate(pcf):
            avail = max(row["cf"], 0.0)
            dist = sum(res.distributions[pid][p] for pid in res.distributions)
            assert dist == pytest.approx(avail, abs=0.01), f"{name} period {p}"


def test_cogp_conserves_gp_stream(pipeline):
    con, report, ctx = pipeline
    res = ctx["wf"]["base"]
    gp_id = ctx["fund"]["partner_ids"]["General Partner"]
    gp_stream = res.net_cf(gp_id)
    # Co-GP contributions + distributions must reconstruct the GP stream
    rows = con.execute(
        """SELECT period, SUM(distribution) d, SUM(contribution) c
           FROM capital_accounts WHERE entity_id=? AND scenario_id=?
           GROUP BY period ORDER BY period""",
        (ctx["cogp"]["entity_id"], ctx["scens"]["base"])).fetchall()
    for p, r in enumerate(rows):
        assert r["d"] - r["c"] == pytest.approx(gp_stream[p], abs=0.01), f"period {p}"


def test_scenarios_ordered_sensibly(pipeline):
    _, report, _ = pipeline
    wf = report["stages"]["4_waterfall"]
    lp = "Institutional LP"
    assert wf["downside"][lp]["irr"] < wf["base"][lp]["irr"] < wf["upside"][lp]["irr"]
    gp = "General Partner"
    # promote: GP outperforms LP in upside, matches LP when sub-pref
    assert wf["upside"][gp]["irr"] > wf["upside"][lp]["irr"]


def test_actuals_override_projection(pipeline):
    con, _, ctx = pipeline
    sid = ctx["scens"]["base"]
    ent = ctx["fund"]["entity_id"]
    before = con.execute(
        "SELECT operating_cf FROM period_cash_flows WHERE deal_id=? AND scenario_id=? AND period=2",
        (ctx["deal_id"], sid)).fetchone()["operating_cf"]
    record_actual_cf(con, ctx["deal_id"], sid, period=2,
                     operating_cf=before + 100000)   # actual came in $100k better
    pcf = con.execute(
        """SELECT operating_cf, capital_event_cf, is_actual FROM period_cash_flows
           WHERE deal_id=? AND scenario_id=? ORDER BY period""",
        (ctx["deal_id"], sid)).fetchall()
    assert pcf[2]["is_actual"] == 1
    cfs = dict(operating=[r["operating_cf"] for r in pcf],
               capital_event=[r["capital_event_cf"] for r in pcf])
    res2 = run_entity(con, ent, sid, deal_period_cfs=cfs)
    dist_p2 = sum(res2.distributions[pid][2] for pid in res2.distributions)
    dist_p2_old = sum(ctx["wf"]["base"].distributions[pid][2]
                      for pid in ctx["wf"]["base"].distributions)
    assert dist_p2 == pytest.approx(dist_p2_old + 100000, abs=0.01)
    # restore projection state for other tests
    record_actual_cf(con, ctx["deal_id"], sid, period=2, operating_cf=before)
    run_entity(con, ent, sid, deal_period_cfs=dict(
        operating=[r["operating_cf"] for r in con.execute(
            "SELECT operating_cf FROM period_cash_flows WHERE deal_id=? AND scenario_id=? ORDER BY period",
            (ctx["deal_id"], sid))],
        capital_event=[r["capital_event_cf"] for r in con.execute(
            "SELECT capital_event_cf FROM period_cash_flows WHERE deal_id=? AND scenario_id=? ORDER BY period",
            (ctx["deal_id"], sid))]))


def test_statement_workbook_reconciles_after_recalc(pipeline):
    con, report, ctx = pipeline
    path = OUT / "Meladon_Haymarket_LP_Statements.xlsx"
    assert path.exists()
    r = subprocess.run([sys.executable, str(RECALC), str(path)],
                       capture_output=True, text=True, timeout=120)
    payload = json.loads(r.stdout[r.stdout.index("{"):])
    assert payload["status"] == "success", payload
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Institutional LP"]
    lp_metrics = report["stages"]["4_waterfall"]["base"]["Institutional LP"]
    stats = {}
    for row in ws.iter_rows(min_col=2, max_col=3):
        if row[0].value in ("Total Contributed", "Total Distributed",
                            "Equity Multiple (DPI)", "IRR (periodic)"):
            stats[row[0].value] = row[1].value
    assert stats["Total Contributed"] == pytest.approx(lp_metrics["contributions"], abs=1.0)
    assert stats["Total Distributed"] == pytest.approx(lp_metrics["distributions"], abs=1.0)
    assert stats["Equity Multiple (DPI)"] == pytest.approx(lp_metrics["em"], abs=1e-4)
    assert stats["IRR (periodic)"] == pytest.approx(lp_metrics["irr"], abs=1e-4)


def test_lp_class_economics_unchanged_by_split(pipeline):
    """Splitting the 90% LP class into 63/27 must not move class economics:
    values pinned from the verified single-LP run."""
    _, report, _ = pipeline
    base = report["stages"]["4_waterfall"]["base"]
    inst, fam = base["Institutional LP"], base["Family Office LP"]
    assert inst["contributions"] + fam["contributions"] == pytest.approx(
        13167786.914277187, rel=1e-9)
    assert inst["distributions"] + fam["distributions"] == pytest.approx(
        22028248.0, rel=1e-4)
    assert inst["irr"] == pytest.approx(fam["irr"], abs=1e-9)      # pari passu
    assert inst["irr"] == pytest.approx(0.11637, abs=1e-4)
    assert inst["distributions"] / fam["distributions"] == pytest.approx(63 / 27, rel=1e-9)
