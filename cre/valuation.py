"""Income-approach valuations, replicating the firm's two templates.

METHOD #1 — DIRECT CAP  [Direct-Cap_Cap_Rate.xlsx]
  Value = stabilized year-1 NOI / market cap rate, where NOI EXCLUDES
  capital reserves (reserves sit below NOI: 'Direct Cap'!I42..I46,
  value applied on 'Sales Comps'!I14 = I42/L14).

METHOD #2 — DCF  [Discounted_Cash_Flow-Completed-Template_2.xlsx]
  Indicated price = year-1 NOI / going-in cap ....... C9 = I80/C12
  where this sheet's NOI INCLUDES reserves in opex (I78 sums row 77),
  i.e. our cfo line.
  Going-out cap = going-in + 0.0005 x hold years .... C14 = C12+0.0005*10
  Sale = forward-year NOI / going-out cap ........... C15 = S80/C14
  Sale proceeds net of sale expense ................. C17
  All-in = price x (1 + closing %) .................. H44
  Unlevered CF: t0 = -all-in; years 1..H-1 = CFO;
  year H = CFO + net sale ........................... row 92
  Returns: IRR / EM / Profit ........................ I5:I7

The two sheets intentionally use different NOI definitions; each
function follows its own source exactly.
"""
from __future__ import annotations
from .finance import irr, equity_multiple


def direct_cap_value(noi_before_reserves: float, cap_rate: float) -> float:
    """'Sales Comps'!I14 = 'Direct Cap'!I42 / cap rate."""
    if cap_rate <= 0:
        raise ValueError("cap rate must be positive")
    return noi_before_reserves / cap_rate


def dcf_valuation(cfo_by_year: dict[int, float], hold: int, going_in_cap: float,
                  closing_cost_pct: float, sale_expense_pct: float,
                  going_out_cap: float | None = None,
                  cap_drift_per_year: float = 0.0005) -> dict:
    """Replicates the DCF template end to end. cfo_by_year must cover
    years 1..hold+1 (the forward year prices the exit)."""
    if going_in_cap <= 0:
        raise ValueError("going-in cap must be positive")
    if going_out_cap is None:
        going_out_cap = going_in_cap + cap_drift_per_year * hold      # C14
    price = cfo_by_year[1] / going_in_cap                             # C9
    all_in = price * (1 + closing_cost_pct)                           # H44
    sale_price = cfo_by_year[hold + 1] / going_out_cap                # C15
    sale_proceeds = sale_price * (1 - sale_expense_pct)               # C17
    cf = [-all_in] + [cfo_by_year[y] for y in range(1, hold + 1)]     # row 92
    cf[hold] += sale_proceeds
    return dict(indicated_price=price, all_in_cost=all_in,
                going_in_cap=going_in_cap, going_out_cap=going_out_cap,
                sale_price=sale_price, sale_proceeds=sale_proceeds,
                cash_flows=cf, unlevered_irr=irr(cf),                 # I5
                equity_multiple=equity_multiple(cf),                  # I6
                profit=sum(cf))                                       # I7


def run_valuation(con, deal_id: int, scenario_id: int, params: dict) -> dict:
    """Both methods from the deal's stored pro forma, plus a comparison
    against the actual purchase price (labeled supplementary — the
    templates derive price rather than take it as input)."""
    deal = con.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    rows = con.execute(
        """SELECT year, noi, reserves, cfo FROM proforma_lines
           WHERE deal_id=? AND scenario_id=? ORDER BY year""",
        (deal_id, scenario_id)).fetchall()
    if not rows:
        raise ValueError("run build_proforma first")
    noi = {r["year"]: r["noi"] for r in rows}
    cfo = {r["year"]: r["cfo"] for r in rows}
    hold = deal["hold_period_years"]

    dc_cap = params["direct_cap_rate"]
    dc_value = direct_cap_value(noi[1], dc_cap)
    units = deal["units"] or 0
    sf = units * (deal["avg_unit_sf"] or 0)
    direct = dict(cap_rate=dc_cap, noi=noi[1], reserves=rows[0]["reserves"],
                  cfo=cfo[1], value=dc_value,
                  value_per_unit=dc_value / units if units else None,
                  value_per_sf=dc_value / sf if sf else None,
                  sensitivity=[(round(dc_cap + d, 4), noi[1] / (dc_cap + d))
                               for d in (-0.005, -0.0025, 0.0, 0.0025, 0.005)])

    gi = params.get("dcf_going_in_cap", dc_cap)
    dcf = dcf_valuation(cfo, hold, gi, deal["closing_cost_pct"],
                        deal["sale_cost_pct"],
                        going_out_cap=params.get("dcf_going_out_cap"))
    for d in (direct, dcf):
        basis = d.get("value") or d.get("indicated_price")
        d["purchase_price"] = deal["purchase_price"]
        d["premium_to_purchase"] = basis / deal["purchase_price"] - 1
    # supplementary: DCF returns at the ACTUAL purchase price
    all_in_actual = deal["purchase_price"] * (1 + deal["closing_cost_pct"])
    cf_actual = list(dcf["cash_flows"])
    cf_actual[0] = -all_in_actual
    dcf["irr_at_purchase_price"] = irr(cf_actual)
    return dict(direct_cap=direct, dcf=dcf, per_unit_basis=units, per_sf_basis=sf)
