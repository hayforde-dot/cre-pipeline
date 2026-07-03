"""Payload-driven pipeline: one JSON document describes a deal end to end;
run_full_pipeline() executes all five stages and generates the report
workbooks. Used by both run_pipeline.py (Meladon demo) and api.py (web).
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from .db import connect, list_placeholders
from .proforma import build_proforma, unlevered_cash_flows
from .underwriting import underwrite
from .capital import create_entity, build_capital_calls
from .waterfall import run_entity, partner_metrics
from .reporting import (write_lp_statement_workbook, write_lp_statement_markdown,
                        write_underwriting_summary)

ASSUMPTION_KEYS = ("market_rent_growth", "loss_to_lease_pct", "concessions_pct",
                   "general_vacancy_pct", "capital_reserve_per_unit",
                   "capital_reserve_growth")


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "deal"


def validate_payload(p: dict) -> list[str]:
    errs = []
    d = p.get("deal", {})
    for k in ("name", "units", "purchase_price", "hold_period_years", "exit_cap_rate"):
        if not d.get(k):
            errs.append(f"deal.{k} is required")
    if not p.get("rent_roll"):
        errs.append("at least one rent_roll row is required")
    pr = p.get("partnership", {})
    partners = pr.get("partners", [])
    if partners:
        s = sum(x.get("equity_pct", 0) for x in partners)
        if abs(s - 1.0) > 1e-6:
            errs.append(f"partner equity percentages sum to {s:.4f}, must be 1.0")
        roles = {x.get("role") for x in partners}
        if not {"LP", "GP"} <= roles:
            errs.append("partnership needs at least one LP and one GP")
    else:
        errs.append("partnership.partners is required")
    tiers = pr.get("tiers", [])
    if not tiers:
        errs.append("partnership.tiers is required")
    elif tiers[-1].get("hurdle_rate") is not None:
        errs.append("the last waterfall tier must be the residual (hurdle_rate null)")
    if not p.get("senior_loan"):
        errs.append("senior_loan is required")
    return errs


def load_deal(con, p: dict) -> dict:
    d = p["deal"]
    ph = 1 if p.get("is_placeholder") else 0
    start_year = int(d.get("analysis_start_year", date.today().year))
    con.execute("""INSERT INTO deals (name, deal_type, property_type, units, avg_unit_sf,
        purchase_price, closing_cost_pct, hold_period_years, periods_per_year,
        analysis_start, exit_cap_rate, sale_cost_pct, notes)
        VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)""",
        (d["name"], d.get("deal_type", "acquisition"), d.get("property_type", "multifamily"),
         d["units"], d.get("avg_unit_sf"), d["purchase_price"],
         d.get("closing_cost_pct", 0.02), d["hold_period_years"],
         f"{start_year}-07-01", d["exit_cap_rate"], d.get("sale_cost_pct", 0.02),
         d.get("notes", "")))
    deal_id = con.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    for r in p["rent_roll"]:
        con.execute("""INSERT INTO rent_roll (deal_id, floorplan, unit_count, avg_sf,
            market_rent_month, loss_to_lease_pct) VALUES (?,?,?,?,?,?)""",
            (deal_id, r["floorplan"], r["unit_count"], r.get("avg_sf"),
             r["market_rent_month"], r.get("loss_to_lease_pct", 0.0)))
    for r in p.get("revenue_lines", []):
        con.execute("INSERT INTO revenue_lines (deal_id, name, year1_amount, growth_rate)"
                    " VALUES (?,?,?,?)",
                    (deal_id, r["name"], r["year1_amount"], r.get("growth_rate", 0.0)))
    for e in p.get("expense_lines", []):
        con.execute("""INSERT INTO expense_lines (deal_id, name, category, year1_amount,
            growth_rate, pct_of_egr) VALUES (?,?,?,?,?,?)""",
            (deal_id, e["name"], e.get("category", "controllable"),
             e.get("year1_amount"), e.get("growth_rate"), e.get("pct_of_egr")))
    for k in ASSUMPTION_KEYS:
        if k in p.get("assumptions", {}):
            con.execute("INSERT INTO deal_assumptions VALUES (?,?,?,?,?)",
                        (deal_id, k, p["assumptions"][k], ph,
                         "PLACEHOLDER — replace with actual" if ph else "user provided"))
    scens = p.get("scenarios") or [dict(name="base")]
    if not any(s["name"] == "base" for s in scens):
        scens.insert(0, dict(name="base"))
    for s in scens:
        con.execute("""INSERT INTO scenarios (deal_id, name, rent_growth_delta,
            exit_cap_delta, opex_growth_delta) VALUES (?,?,?,?,?)""",
            (deal_id, s["name"], s.get("rent_growth_delta", 0.0),
             s.get("exit_cap_delta", 0.0), s.get("opex_growth_delta", 0.0)))
    con.commit()
    return dict(deal_id=deal_id, start_year=start_year)


def run_full_pipeline(payload: dict, db_path=":memory:", out_dir: Path | None = None) -> dict:
    errs = validate_payload(payload)
    if errs:
        raise ValueError("; ".join(errs))
    out_dir = Path(out_dir or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)
    meta = load_deal(con, payload)
    deal_id = meta["deal_id"]
    scens = {s["name"]: s["scenario_id"] for s in
             con.execute("SELECT * FROM scenarios WHERE deal_id=?", (deal_id,))}
    report = {"stages": {}}

    # STAGE 1
    pf = {}
    for name, sid in scens.items():
        build_proforma(con, deal_id, sid)
        pf[name] = unlevered_cash_flows(con, deal_id, sid)
    hold = payload["deal"]["hold_period_years"]
    report["stages"]["1_proforma"] = {
        n: dict(noi_y1=v["noi_by_year"][1], noi_fwd=v["noi_by_year"][hold + 1],
                sale_net=v["sale_net"]) for n, v in pf.items()}

    # STAGE 2
    senior = dict(tranche="senior", **payload["senior_loan"])
    structures = [("senior", [senior])]
    if payload.get("mezz_loan"):
        structures.append(("senior_mezz", [senior, dict(tranche="mezz",
                                                        **payload["mezz_loan"])]))
    uw = {}
    for name, sid in scens.items():
        for st, specs in structures:
            uw[(name, st)] = underwrite(con, deal_id, sid, st, specs)
    for name, sid in scens.items():                 # waterfall capitalization = senior
        underwrite(con, deal_id, sid, "senior", [senior])

    # STAGE 3
    pr = payload["partnership"]
    fund = create_entity(con, deal_id, pr.get("name", "Partnership"), "irr_hurdle",
                         partners=pr["partners"], tiers=pr["tiers"])
    cogp = None
    if payload.get("cogp"):
        cg = payload["cogp"]
        gp_partner = next(p for p in pr["partners"] if p["role"] == "GP")
        cogp = create_entity(con, deal_id, cg.get("name", "Co-GP"), "irr_hurdle",
                             partners=cg["partners"], tiers=cg["tiers"],
                             parent_entity_id=fund["entity_id"],
                             parent_partner_id=fund["partner_ids"][gp_partner["name"]])
    calls = {n: build_capital_calls(con, fund["entity_id"], sid,
                                    uw[(n, "senior")]["levered_cf"])
             for n, sid in scens.items()}
    report["stages"]["3_capital"] = {n: c["totals"] for n, c in calls.items()}

    # STAGE 4
    wf, wf_cogp = {}, {}
    for name, sid in scens.items():
        pcf = con.execute("""SELECT operating_cf, capital_event_cf FROM period_cash_flows
            WHERE deal_id=? AND scenario_id=? ORDER BY period""", (deal_id, sid)).fetchall()
        cfs = dict(operating=[r["operating_cf"] for r in pcf],
                   capital_event=[r["capital_event_cf"] for r in pcf])
        wf[name] = run_entity(con, fund["entity_id"], sid, deal_period_cfs=cfs)
        if cogp:
            wf_cogp[name] = run_entity(con, cogp["entity_id"], sid, parent_result=wf[name])
    report["stages"]["4_waterfall"] = {n: partner_metrics(r, 1) for n, r in wf.items()}
    if cogp:
        report["stages"]["4_waterfall_cogp"] = {n: partner_metrics(r, 1)
                                                for n, r in wf_cogp.items()}

    # STAGE 5
    stmt_scen = payload.get("statement_scenario", "base")
    period_dates = [f"{meta['start_year'] + p}-06-30" for p in range(hold + 1)]
    ph_notes = list_placeholders(con, deal_id)
    slug = _slug(payload["deal"]["name"])
    files = {}
    files["lp_statements"] = str(write_lp_statement_workbook(
        con, fund["entity_id"], scens[stmt_scen], out_dir / f"{slug}_LP_Statements.xlsx",
        as_of=date.today(), period_dates=period_dates,
        placeholder_notes=ph_notes or None))
    if cogp:
        files["cogp_statements"] = str(write_lp_statement_workbook(
            con, cogp["entity_id"], scens[stmt_scen], out_dir / f"{slug}_CoGP_Statements.xlsx",
            as_of=date.today(), period_dates=period_dates,
            placeholder_notes=ph_notes or None))
    files["underwriting_summary"] = str(write_underwriting_summary(
        con, deal_id, out_dir / f"{slug}_Underwriting_Summary.xlsx",
        placeholder_notes=ph_notes or None))
    first_lp = next(p for p in pr["partners"] if p["role"] == "LP")
    report["statement_markdown"] = write_lp_statement_markdown(
        con, fund["entity_id"], scens[stmt_scen], fund["partner_ids"][first_lp["name"]],
        period_dates, placeholder=bool(ph_notes))
    report["outputs"] = list(files.values())
    ctx = dict(deal_id=deal_id, scens=scens, fund=fund, cogp=cogp, uw=uw, wf=wf)
    return dict(con=con, report=report, ctx=ctx, files=files)


MELADON_PAYLOAD = {
    "is_placeholder": True,
    "deal": {"name": "Meladon Haymarket", "property_type": "multifamily", "units": 180,
             "avg_unit_sf": 850, "purchase_price": 38500000, "closing_cost_pct": 0.02,
             "hold_period_years": 5, "exit_cap_rate": 0.0575, "sale_cost_pct": 0.02,
             "analysis_start_year": 2026,
             "notes": "ALL ECONOMICS ARE PLACEHOLDERS — structure mirrors firm templates"},
    "rent_roll": [
        {"floorplan": "A - 1/1", "unit_count": 60, "avg_sf": 700,
         "market_rent_month": 1450, "loss_to_lease_pct": 0.015},
        {"floorplan": "B - 2/1", "unit_count": 84, "avg_sf": 875,
         "market_rent_month": 1725, "loss_to_lease_pct": 0.015},
        {"floorplan": "C - 2/2", "unit_count": 36, "avg_sf": 1050,
         "market_rent_month": 1975, "loss_to_lease_pct": 0.015}],
    "revenue_lines": [
        {"name": "RUBS", "year1_amount": 90000, "growth_rate": 0.025},
        {"name": "Parking Income", "year1_amount": 45000, "growth_rate": 0.03},
        {"name": "Other Income", "year1_amount": 30000, "growth_rate": 0.03}],
    "expense_lines": [
        {"name": "Repairs and Maintenance", "category": "controllable",
         "year1_amount": 130000, "growth_rate": 0.025},
        {"name": "Payroll", "category": "controllable", "year1_amount": 290000,
         "growth_rate": 0.025},
        {"name": "Utilities", "category": "controllable", "year1_amount": 120000,
         "growth_rate": 0.025},
        {"name": "Administrative", "category": "controllable", "year1_amount": 55000,
         "growth_rate": 0.025},
        {"name": "Advertising and Marketing", "category": "controllable",
         "year1_amount": 40000, "growth_rate": 0.025},
        {"name": "Turnover", "category": "controllable", "year1_amount": 45000,
         "growth_rate": 0.025},
        {"name": "Property Taxes", "category": "fixed", "year1_amount": 385000,
         "growth_rate": 0.02},
        {"name": "Insurance", "category": "fixed", "year1_amount": 95000,
         "growth_rate": 0.02},
        {"name": "Property Management", "category": "fixed", "pct_of_egr": 0.03}],
    "assumptions": {"market_rent_growth": 0.030, "loss_to_lease_pct": 0.015,
                    "concessions_pct": 0.005, "general_vacancy_pct": 0.05,
                    "capital_reserve_per_unit": 250, "capital_reserve_growth": 0.02},
    "scenarios": [
        {"name": "base"},
        {"name": "upside", "rent_growth_delta": 0.0075, "exit_cap_delta": -0.0025},
        {"name": "downside", "rent_growth_delta": -0.010, "exit_cap_delta": 0.0050,
         "opex_growth_delta": 0.005}],
    "senior_loan": {"rate": 0.0625, "term_months": 120, "amort_months": 360,
                    "io_months": 24, "max_ltv": 0.65, "min_dscr": 1.25,
                    "min_debt_yield": 0.08},
    "mezz_loan": {"rate": 0.105, "combined_max_ltv": 0.75, "combined_min_dscr": 1.15},
    "partnership": {
        "name": "Meladon Haymarket Partners LP",
        "partners": [
            {"name": "Institutional LP", "role": "LP", "equity_pct": 0.63},
            {"name": "Family Office LP", "role": "LP", "equity_pct": 0.27},
            {"name": "General Partner", "role": "GP", "equity_pct": 0.10}],
        "tiers": [
            {"tier_no": 1, "hurdle_rate": 0.08, "promote_pct": 0.0},
            {"tier_no": 2, "hurdle_rate": 0.12, "promote_pct": 0.20},
            {"tier_no": 3, "hurdle_rate": 0.15, "promote_pct": 0.30},
            {"tier_no": 4, "hurdle_rate": None, "promote_pct": 0.40}]},
    "cogp": {
        "name": "Meladon GP Co-Invest LLC",
        "partners": [
            {"name": "Co-GP Investors", "role": "LP", "equity_pct": 0.95},
            {"name": "Sponsor", "role": "GP", "equity_pct": 0.05}],
        "tiers": [
            {"tier_no": 1, "hurdle_rate": 0.15, "promote_pct": 0.0},
            {"tier_no": 2, "hurdle_rate": None, "promote_pct": 0.40}]},
    "statement_scenario": "base",
}
