"""STAGE 3 — Capital Stack & Partnership Structure.
Persists the partnership graph the waterfall sheets encode:
  Equity Contributions block  -> partners (role, equity %)
  Promote Structure block     -> waterfall_tiers (hurdle, promote %)
  'Co-GP Returns' recursion   -> partnership_entities.parent_entity/partner
Capital call schedule follows the workbooks' funding convention:
contributions are called against negative net cash flow periods,
pro rata by equity % (Waterfalls F42 = -MIN(CF,0) x C6; Draw Schedule
funds equity first). For a stabilized acquisition this collapses to a
single call at close.
"""
from __future__ import annotations
import sqlite3


def create_entity(con: sqlite3.Connection, deal_id: int, name: str, style: str,
                  partners: list[dict], tiers: list[dict], aum_fee_pct: float = 0.0,
                  parent_entity_id: int | None = None,
                  parent_partner_id: int | None = None) -> dict:
    """partners: [{name, role: 'LP'|'GP', equity_pct}]
    tiers: [{track: 'all'|'operating'|'capital_event', tier_no, hurdle_rate|None, promote_pct}]"""
    total_pct = sum(p["equity_pct"] for p in partners)
    if abs(total_pct - 1.0) > 1e-9:
        raise ValueError(f"partner equity percentages sum to {total_pct}, not 1.0")
    roles = [p["role"] for p in partners]
    if "LP" not in roles or "GP" not in roles:
        raise ValueError("entity needs at least one LP and at least one GP")
    if any(p["equity_pct"] <= 0 for p in partners):
        raise ValueError("every partner needs a positive equity percentage")
    names = [p["name"] for p in partners]
    if len(set(names)) != len(names):
        raise ValueError("partner names must be unique within an entity "
                         "(statements are keyed by name)")
    cur = con.execute(
        """INSERT INTO partnership_entities (deal_id, name, waterfall_style, aum_fee_pct,
           parent_entity_id, parent_partner_id) VALUES (?,?,?,?,?,?)""",
        (deal_id, name, style, aum_fee_pct, parent_entity_id, parent_partner_id))
    entity_id = cur.lastrowid
    partner_ids = {}
    for p in partners:
        c = con.execute("INSERT INTO partners (entity_id, name, role, equity_pct) VALUES (?,?,?,?)",
                        (entity_id, p["name"], p["role"], p["equity_pct"]))
        partner_ids[p["name"]] = c.lastrowid
    for t in tiers:
        con.execute("INSERT INTO waterfall_tiers VALUES (?,?,?,?,?)",
                    (entity_id, t.get("track", "all"), t["tier_no"],
                     t.get("hurdle_rate"), t.get("promote_pct", 0.0)))
    con.commit()
    return dict(entity_id=entity_id, partner_ids=partner_ids)


def effective_splits(lp_pct: float, gp_pct: float, promote_pct: float) -> tuple[float, float]:
    """Promote Structure cols H/I: promote to GP off the top, remainder
    pari passu by equity. H11 = 0.20 + 0.80 x 0.10 = 0.28; I11 = 0.72."""
    gp = promote_pct + (1 - promote_pct) * gp_pct
    lp = (1 - promote_pct) * lp_pct
    return lp, gp


def build_capital_calls(con: sqlite3.Connection, entity_id: int, scenario_id: int,
                        cash_flows: list[float]) -> dict:
    """Call schedule: each period's funding need = -min(net CF, 0),
    split pro rata by equity %. Persists to capital_calls."""
    partners = con.execute("SELECT * FROM partners WHERE entity_id=?", (entity_id,)).fetchall()
    con.execute("DELETE FROM capital_calls WHERE entity_id=? AND scenario_id=?",
                (entity_id, scenario_id))
    schedule = {p["partner_id"]: [] for p in partners}
    for period, cf in enumerate(cash_flows):
        need = -min(cf, 0.0)
        for p in partners:
            amt = need * p["equity_pct"]
            schedule[p["partner_id"]].append(amt)
            if amt > 0:
                con.execute("INSERT INTO capital_calls VALUES (?,?,?,?,?)",
                            (entity_id, scenario_id, p["partner_id"], period, amt))
    con.commit()
    totals = {p["name"]: sum(schedule[p["partner_id"]]) for p in partners}
    return dict(schedule=schedule, totals=totals, total_equity=sum(totals.values()))
