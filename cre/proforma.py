"""STAGE 1 — Pro Forma.
Line taxonomy and conventions taken from the uploaded workbooks:
  GPR (rent roll: units x market rent x 12, grown)        [Rent Roll Summary]
  - loss-to-lease (% of GPR)                              [Direct Cap row 11]
  - concessions (% of GPR, optional)                      [Levered DCF B21]
  + other income lines (grown individually)               [Levered DCF B23:26]
  = gross revenue
  - general vacancy & credit loss (% of gross revenue)    [Levered DCF C27, 5%]
  = EGR
  - opex (controllable/fixed grown; mgmt as % of EGR)     [Levered DCF B29:35]
  = NOI
  - capital reserves ($/unit grown)                       [Levered DCF B36:37]
  = cash flow from operations
Exit: sale price = FORWARD-year NOI / exit cap            [Levered DCF R98 = S83/C13]
      less sale expense % of price                        [R99]
The pro forma is therefore built for hold+1 years so year N+1 NOI exists.
"""
from __future__ import annotations
import sqlite3
from .db import get_assumption


def build_proforma(con: sqlite3.Connection, deal_id: int, scenario_id: int) -> list[dict]:
    deal = con.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    scen = con.execute("SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
    if deal is None or scen is None:
        raise ValueError("deal or scenario not found")

    hold = deal["hold_period_years"]
    rent_growth = get_assumption(con, deal_id, "market_rent_growth") + scen["rent_growth_delta"]
    ltl_pct = get_assumption(con, deal_id, "loss_to_lease_pct", 0.0)
    conc_pct = get_assumption(con, deal_id, "concessions_pct", 0.0)
    gen_vac = get_assumption(con, deal_id, "general_vacancy_pct")
    reserve_per_unit = get_assumption(con, deal_id, "capital_reserve_per_unit", 0.0)
    reserve_growth = get_assumption(con, deal_id, "capital_reserve_growth", 0.0) + scen["opex_growth_delta"]

    rr = con.execute("SELECT * FROM rent_roll WHERE deal_id=?", (deal_id,)).fetchall()
    gpr_y1 = sum(r["unit_count"] * r["market_rent_month"] * 12 for r in rr)
    rev_lines = con.execute("SELECT * FROM revenue_lines WHERE deal_id=?", (deal_id,)).fetchall()
    exp_lines = con.execute("SELECT * FROM expense_lines WHERE deal_id=?", (deal_id,)).fetchall()

    con.execute("DELETE FROM proforma_lines WHERE deal_id=? AND scenario_id=?", (deal_id, scenario_id))
    out = []
    for year in range(1, hold + 2):  # hold+1 for forward NOI
        g = (1 + rent_growth) ** (year - 1)
        gpr = gpr_y1 * g
        ltl = -gpr * ltl_pct
        conc = -gpr * conc_pct
        other = sum(l["year1_amount"] * (1 + l["growth_rate"] + scen["rent_growth_delta"]) ** (year - 1)
                    for l in rev_lines)
        gross = gpr + ltl + conc + other
        vac = -gross * gen_vac
        egr = gross + vac

        opex = 0.0
        for e in exp_lines:
            if e["category"] == "reserve":
                continue
            if e["pct_of_egr"] is not None:
                opex += egr * e["pct_of_egr"]
            else:
                opex += e["year1_amount"] * (1 + e["growth_rate"] + scen["opex_growth_delta"]) ** (year - 1)
        noi = egr - opex
        reserves = (deal["units"] or 0) * reserve_per_unit * (1 + reserve_growth) ** (year - 1)
        cfo = noi - reserves

        con.execute(
            """INSERT INTO proforma_lines (deal_id, scenario_id, year, gpr, loss_to_lease,
               concessions, other_income, gross_revenue, general_vacancy, egr, opex, noi,
               reserves, cfo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (deal_id, scenario_id, year, gpr, ltl, conc, other, gross, vac, egr, opex, noi,
             reserves, cfo))
        out.append(dict(year=year, gpr=gpr, noi=noi, cfo=cfo, egr=egr, opex=opex))
    con.commit()
    return out


def unlevered_cash_flows(con: sqlite3.Connection, deal_id: int, scenario_id: int) -> dict:
    """Vector t=0..hold: t0 = -(price x (1+closing%)) [Levered DCF H45];
    years 1..hold = CFO; year hold += net sale (forward NOI / exit cap,
    less sale expense). Returns components for IRR partitioning too."""
    deal = con.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    scen = con.execute("SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
    rows = con.execute(
        "SELECT year, noi, cfo FROM proforma_lines WHERE deal_id=? AND scenario_id=? ORDER BY year",
        (deal_id, scenario_id)).fetchall()
    if not rows:
        raise ValueError("run build_proforma first")
    hold = deal["hold_period_years"]
    noi = {r["year"]: r["noi"] for r in rows}
    cfo = {r["year"]: r["cfo"] for r in rows}

    exit_cap = deal["exit_cap_rate"] + scen["exit_cap_delta"]
    sale_price = noi[hold + 1] / exit_cap
    sale_net = sale_price * (1 - deal["sale_cost_pct"])

    all_in = deal["purchase_price"] * (1 + deal["closing_cost_pct"])
    investment = [-all_in] + [0.0] * hold
    operating = [0.0] + [cfo[y] for y in range(1, hold + 1)]
    reversion = [0.0] * hold + [sale_net]
    total = [i + o + r for i, o, r in zip(investment, operating, reversion)]
    return dict(total=total, investment=investment, operating=operating,
                reversion=reversion, sale_price=sale_price, sale_net=sale_net,
                all_in_cost=all_in, noi_by_year=noi)
