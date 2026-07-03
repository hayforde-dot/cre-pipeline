"""Multiple LPs (and GPs) per entity: class-level waterfall math with
pro-rata member fan-out. The core check is EQUIVALENCE — a 3-LP entity
must produce exactly the class economics of a 1-LP entity on the same
cash flows, with each member holding its within-class share."""
import pytest
from pathlib import Path
import openpyxl
from cre.db import connect
from cre.capital import create_entity, build_capital_calls
from cre.waterfall import run_entity, partner_metrics
from cre.reporting import write_lp_statement_workbook
from datetime import date

TIERS = [{"tier_no": 1, "hurdle_rate": 0.08, "promote_pct": 0.0},
         {"tier_no": 2, "hurdle_rate": 0.12, "promote_pct": 0.20},
         {"tier_no": 3, "hurdle_rate": None, "promote_pct": 0.35}]
CFS = [-10_000_000, 550_000, -400_000, 700_000, 800_000, 14_500_000]


def _deal(con, ppy=1):
    con.execute("""INSERT INTO deals (deal_id, name, purchase_price, hold_period_years,
        periods_per_year, exit_cap_rate) VALUES (1,'X',1,5,?,0.05)""", (ppy,))
    con.execute("INSERT INTO scenarios (scenario_id, deal_id, name) VALUES (1,1,'base')")
    con.commit()


def _run(con, partners, tiers=TIERS, style="irr_hurdle", cfs=None):
    ent = create_entity(con, 1, f"E{len(partners)}", style, partners=partners, tiers=tiers)
    c = cfs or CFS
    res = run_entity(con, ent["entity_id"], 1,
                     deal_period_cfs=dict(operating=c, capital_event=[0.0] * len(c)))
    return ent, res


def test_three_lp_equivalence_irr_hurdle():
    con = connect()
    _deal(con)
    _, single = _run(con, [{"name": "LP", "role": "LP", "equity_pct": 0.9},
                           {"name": "GP", "role": "GP", "equity_pct": 0.1}])
    ent3, multi = _run(con, [{"name": "LP-A", "role": "LP", "equity_pct": 0.45},
                             {"name": "LP-B", "role": "LP", "equity_pct": 0.30},
                             {"name": "LP-C", "role": "LP", "equity_pct": 0.15},
                             {"name": "GP", "role": "GP", "equity_pct": 0.1}])
    sid = {v: k for k, v in single.partner_names.items()}
    mid = {v: k for k, v in multi.partner_names.items()}
    lp_class_single_d = single.distributions[sid["LP"]]
    lp_class_single_c = single.contributions[sid["LP"]]
    shares = {"LP-A": 0.5, "LP-B": 30 / 90, "LP-C": 15 / 90}
    for p in range(len(CFS)):
        # class aggregate identical to the single-LP run
        agg_d = sum(multi.distributions[mid[n]][p] for n in shares)
        agg_c = sum(multi.contributions[mid[n]][p] for n in shares)
        assert agg_d == pytest.approx(lp_class_single_d[p], abs=1e-6)
        assert agg_c == pytest.approx(lp_class_single_c[p], abs=1e-6)
        # each member holds exactly its within-class share
        for n, s in shares.items():
            assert multi.distributions[mid[n]][p] == pytest.approx(
                lp_class_single_d[p] * s, abs=1e-6)
    # GP untouched by the LP split
    assert multi.distributions[mid["GP"]] == pytest.approx(
        single.distributions[sid["GP"]], abs=1e-6)
    # pro-rata scaling preserves IRR: every LP member == class IRR
    m_single = partner_metrics(single, 1)
    m_multi = partner_metrics(multi, 1)
    for n in shares:
        assert m_multi[n]["irr"] == pytest.approx(m_single["LP"]["irr"], abs=1e-9)
        assert m_multi[n]["em"] == pytest.approx(m_single["LP"]["em"], abs=1e-9)


def test_two_lp_equivalence_american():
    con = connect()
    _deal(con)
    tiers = [{"track": "operating", "tier_no": 1, "hurdle_rate": 0.06, "promote_pct": 0.0},
             {"track": "operating", "tier_no": 2, "hurdle_rate": None, "promote_pct": 0.15},
             {"track": "capital_event", "tier_no": 1, "hurdle_rate": 0.06, "promote_pct": 0.0},
             {"track": "capital_event", "tier_no": 2, "hurdle_rate": 0.12, "promote_pct": 0.15},
             {"track": "capital_event", "tier_no": 3, "hurdle_rate": None, "promote_pct": 0.45}]
    ops = [0, 500_000, 620_000, 700_000]
    ev = [-8_000_000, 0, -300_000, 12_000_000]

    def build(partners, tag):
        ent = create_entity(con, 1, tag, "american_dual_track", partners, tiers)
        return run_entity(con, ent["entity_id"], 1,
                          deal_period_cfs=dict(operating=ops, capital_event=ev))
    single = build([{"name": "LP", "role": "LP", "equity_pct": 0.9},
                    {"name": "GP", "role": "GP", "equity_pct": 0.1}], "S")
    multi = build([{"name": "LP-A", "role": "LP", "equity_pct": 0.60},
                   {"name": "LP-B", "role": "LP", "equity_pct": 0.30},
                   {"name": "GP", "role": "GP", "equity_pct": 0.1}], "M")
    sid = {v: k for k, v in single.partner_names.items()}
    mid = {v: k for k, v in multi.partner_names.items()}
    for p in range(len(ops)):
        agg = multi.distributions[mid["LP-A"]][p] + multi.distributions[mid["LP-B"]][p]
        assert agg == pytest.approx(single.distributions[sid["LP"]][p], abs=1e-6)
        assert multi.distributions[mid["LP-A"]][p] == pytest.approx(
            single.distributions[sid["LP"]][p] * (2 / 3), abs=1e-6)
    # event-track hurdle accounts are class-level and must be identical
    for k in single.hurdle_accounts:
        assert multi.hurdle_accounts[k] == pytest.approx(single.hurdle_accounts[k], abs=1e-6)


def test_multi_gp_promote_shared_pro_rata():
    con = connect()
    _deal(con)
    _, res = _run(con, [{"name": "LP", "role": "LP", "equity_pct": 0.9},
                        {"name": "GP-1", "role": "GP", "equity_pct": 0.06},
                        {"name": "GP-2", "role": "GP", "equity_pct": 0.04}])
    ids = {v: k for k, v in res.partner_names.items()}
    for p in range(len(CFS)):
        d1, d2 = res.distributions[ids["GP-1"]][p], res.distributions[ids["GP-2"]][p]
        if d1 + d2 > 0:
            assert d1 / (d1 + d2) == pytest.approx(0.6, abs=1e-9)


def test_capital_calls_and_conservation_three_lps():
    con = connect()
    _deal(con)
    ent, res = _run(con, [{"name": "LP-A", "role": "LP", "equity_pct": 0.45},
                          {"name": "LP-B", "role": "LP", "equity_pct": 0.30},
                          {"name": "LP-C", "role": "LP", "equity_pct": 0.15},
                          {"name": "GP", "role": "GP", "equity_pct": 0.1}])
    calls = build_capital_calls(con, ent["entity_id"], 1, CFS)
    total_need = sum(-min(c, 0) for c in CFS)
    assert calls["total_equity"] == pytest.approx(total_need)
    assert calls["totals"]["LP-B"] == pytest.approx(total_need * 0.30)
    for pid in res.contributions:  # engine contributions == call schedule
        assert sum(res.contributions[pid]) == pytest.approx(
            sum(calls["schedule"][pid]), abs=1e-6)


def test_validation_errors():
    con = connect()
    _deal(con)
    with pytest.raises(ValueError, match="at least one LP"):
        create_entity(con, 1, "NoGP", "irr_hurdle",
                      [{"name": "A", "role": "LP", "equity_pct": 1.0}], TIERS)
    with pytest.raises(ValueError, match="unique"):
        create_entity(con, 1, "Dup", "irr_hurdle",
                      [{"name": "LP", "role": "LP", "equity_pct": 0.5},
                       {"name": "LP", "role": "LP", "equity_pct": 0.4},
                       {"name": "GP", "role": "GP", "equity_pct": 0.1}], TIERS)


def test_statement_workbook_one_sheet_per_lp(tmp_path):
    con = connect()
    _deal(con)
    ent, _ = _run(con, [{"name": "LP-A", "role": "LP", "equity_pct": 0.45},
                        {"name": "LP-B", "role": "LP", "equity_pct": 0.30},
                        {"name": "LP-C", "role": "LP", "equity_pct": 0.15},
                        {"name": "GP", "role": "GP", "equity_pct": 0.1}])
    path = tmp_path / "stmt.xlsx"
    write_lp_statement_workbook(con, ent["entity_id"], 1, path, as_of=date(2026, 7, 2),
                                period_dates=[f"202{6+i}-06-30" for i in range(6)])
    wb = openpyxl.load_workbook(path)
    assert {"Summary", "LP-A", "LP-B", "LP-C", "GP"} <= set(wb.sheetnames)
