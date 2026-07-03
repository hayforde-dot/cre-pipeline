"""STAGE 2 — Underwriting. Consumes Stage 1 pro forma records.
Senior sizing exactly per 'Financing the Foles' G13:H21:
  Loan = MIN( LTV x purchase price,
              -PV(rate/12, amort_months, NOI_yr1/12/minDSCR),   [H19]
              NOI_yr1 / min_debt_yield )                        [H20]
Debt service per year: IO payment x 12 while year x 12 <= io_months,
else amortizing PMT x 12 [row 89]. Payoff at exit = remaining balance
after (months held - io_months) of amortization [H12].
Mezz (senior_mezz structure): the workbooks have no mezz template, so a
market-standard convention is used and FLAGGED: interest-only, sized to
the tighter of combined LTV and combined DSCR on year-1 NOI.
"""
from __future__ import annotations
import sqlite3
from .finance import pmt, remaining_balance, irr, equity_multiple, irr_partition
from .proforma import unlevered_cash_flows


def size_senior(price: float, noi_y1: float, rate: float, amort_months: int,
                max_ltv: float, min_dscr: float, min_dy: float) -> dict:
    by_ltv = max_ltv * price
    monthly_cap = noi_y1 / 12 / min_dscr
    r = rate / 12
    by_dscr = monthly_cap * (1 - (1 + r) ** -amort_months) / r if r else monthly_cap * amort_months
    by_dy = noi_y1 / min_dy
    amount = min(by_ltv, by_dscr, by_dy)
    binding = {by_ltv: "LTV", by_dscr: "DSCR", by_dy: "DY"}[amount]
    return dict(amount=amount, by_ltv=by_ltv, by_dscr=by_dscr, by_dy=by_dy, binding=binding)


def size_mezz(price: float, noi_y1: float, senior_amount: float, senior_ds_y1: float,
              mezz_rate: float, combined_max_ltv: float, combined_min_dscr: float) -> dict:
    """[MARKET-STANDARD CONVENTION — not from the workbooks] IO mezz sized to
    min(combined LTV headroom, combined DSCR headroom on yr-1 NOI)."""
    by_ltv = max(combined_max_ltv * price - senior_amount, 0.0)
    ds_headroom = max(noi_y1 / combined_min_dscr - senior_ds_y1, 0.0)
    by_dscr = ds_headroom / mezz_rate if mezz_rate else 0.0
    amount = min(by_ltv, by_dscr)
    binding = "combined LTV" if by_ltv <= by_dscr else "combined DSCR"
    return dict(amount=amount, by_ltv=by_ltv, by_dscr=by_dscr, binding=binding)


def annual_debt_service(loan: float, rate: float, amort_months: int | None,
                        io_months: int, year: int) -> float:
    """Row 89 convention: full-year switch — year is IO if year*12 <= io_months."""
    io_pmt = rate / 12 * loan
    if amort_months is None:  # full-term IO (mezz)
        return io_pmt * 12
    am_pmt = pmt(rate / 12, amort_months, loan)
    return (io_pmt if year * 12 <= io_months else am_pmt) * 12


def loan_payoff(loan: float, rate: float, amort_months: int | None,
                io_months: int, months_held: int) -> float:
    if amort_months is None:
        return loan
    months_amortized = max(months_held - io_months, 0)
    return remaining_balance(loan, rate, amort_months, months_amortized)


def underwrite(con: sqlite3.Connection, deal_id: int, scenario_id: int,
               structure: str, loan_specs: list[dict]) -> dict:
    """loan_specs: [{tranche:'senior', rate, term_months, amort_months, io_months,
                     max_ltv, min_dscr, min_debt_yield}, {tranche:'mezz', rate,
                     combined_max_ltv, combined_min_dscr}]
    Persists loans + underwriting_results + period_cash_flows (the handoff
    record Stage 4 consumes)."""
    deal = con.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    u = unlevered_cash_flows(con, deal_id, scenario_id)
    hold = deal["hold_period_years"]
    price = deal["purchase_price"]
    # sizing / DSCR / DY / going-in cap all use reserve-inclusive NOI
    # (= cfo), matching Levered DCF I83 where reserves sit inside opex
    noi = u["cfo_by_year"]

    loans = []
    if structure != "unlevered":
        s = next(l for l in loan_specs if l["tranche"] == "senior")
        sized = size_senior(price, noi[1], s["rate"], s["amort_months"],
                            s["max_ltv"], s["min_dscr"], s["min_debt_yield"])
        senior = dict(s, amount=sized["amount"], binding=sized["binding"])
        loans.append(senior)
        if structure == "senior_mezz":
            m = next(l for l in loan_specs if l["tranche"] == "mezz")
            senior_ds1 = annual_debt_service(senior["amount"], senior["rate"],
                                             senior["amort_months"], senior["io_months"], 1)
            # size against amortizing DS (conservative) — flagged convention
            am_ds = pmt(senior["rate"] / 12, senior["amort_months"], senior["amount"]) * 12
            msz = size_mezz(price, noi[1], senior["amount"], am_ds, m["rate"],
                            m["combined_max_ltv"], m["combined_min_dscr"])
            loans.append(dict(m, amount=msz["amount"], binding=msz["binding"],
                              amort_months=None, io_months=0,
                              term_months=senior["term_months"]))

    total_debt = sum(l["amount"] for l in loans)
    equity = u["all_in_cost"] - total_debt

    lev = [-equity]
    ds_by_year = []
    for y in range(1, hold + 1):
        ds = sum(annual_debt_service(l["amount"], l["rate"], l.get("amort_months"),
                                     l.get("io_months", 0), y) for l in loans)
        ds_by_year.append(ds)
        lev.append(u["operating"][y] - ds)
    payoff = sum(loan_payoff(l["amount"], l["rate"], l.get("amort_months"),
                             l.get("io_months", 0), hold * 12) for l in loans)
    lev[hold] += u["sale_net"] - payoff

    metrics = {
        "purchase_price": price,
        "all_in_cost": u["all_in_cost"],
        "going_in_cap": noi[1] / price,
        "sale_price": u["sale_price"],
        "total_debt": total_debt,
        "equity": equity,
        "ltv": total_debt / price if price else 0,
        "loan_payoff_at_exit": payoff,
        "unlevered_irr": irr(u["total"]),
        "unlevered_em": equity_multiple(u["total"]),
    }
    part = irr_partition(u["investment"], u["operating"], u["reversion"])
    metrics["unlevered_irr_cfo_share"] = part["cfo_share"]
    metrics["unlevered_irr_reversion_share"] = part["reversion_share"]
    if loans:
        metrics.update({
            "dscr_y1": noi[1] / ds_by_year[0],
            "dscr_min": min(noi[y] / ds_by_year[y - 1] for y in range(1, hold + 1)),
            "debt_yield_y1": noi[1] / total_debt,
            "coc_y1": lev[1] / equity,
            "coc_avg": sum(lev[1:hold] + [u["operating"][hold] - ds_by_year[-1]]) / hold / equity,
            "levered_irr": irr(lev),
            "levered_em": equity_multiple(lev),
            "levered_profit": sum(lev),
        })

    # ---- persist ----
    con.execute("DELETE FROM loans WHERE deal_id=? AND scenario_id=? AND structure=?",
                (deal_id, scenario_id, structure))
    for l in loans:
        con.execute(
            """INSERT INTO loans (deal_id, scenario_id, structure, tranche, rate,
               term_months, amort_months, io_months, max_ltv, min_dscr,
               min_debt_yield, sized_amount, binding_constraint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (deal_id, scenario_id, structure, l["tranche"], l["rate"], l["term_months"],
             l.get("amort_months"), l.get("io_months", 0), l.get("max_ltv"),
             l.get("min_dscr"), l.get("min_debt_yield"), l["amount"], l["binding"]))
    con.execute("DELETE FROM underwriting_results WHERE deal_id=? AND scenario_id=? AND structure=?",
                (deal_id, scenario_id, structure))
    for k, v in metrics.items():
        con.execute("INSERT INTO underwriting_results VALUES (?,?,?,?,?)",
                    (deal_id, scenario_id, structure, k, v))

    # handoff record: levered operating CF + capital-event CF per period
    con.execute("DELETE FROM period_cash_flows WHERE deal_id=? AND scenario_id=?",
                (deal_id, scenario_id))
    for p in range(0, hold + 1):
        op = lev[p] if p < hold else u["operating"][hold] - (ds_by_year[-1] if loans else 0)
        if p == 0:
            op = lev[0]
        cap_ev = (u["sale_net"] - payoff) if p == hold else 0.0
        con.execute("INSERT INTO period_cash_flows VALUES (?,?,?,?,?,?,?)",
                    (deal_id, scenario_id, p, None, op, cap_ev, 0))
    con.commit()
    return dict(metrics=metrics, levered_cf=lev, loans=loans, ds_by_year=ds_by_year)


def record_actual_cf(con, deal_id: int, scenario_id: int, period: int,
                     operating_cf: float, capital_event_cf: float = 0.0,
                     period_date: str | None = None):
    """Override a projected period with an ACTUAL cash flow. The waterfall
    consumes period_cash_flows, so actuals flow through automatically;
    is_actual=1 marks the record so reports can distinguish the two."""
    con.execute(
        """INSERT INTO period_cash_flows (deal_id, scenario_id, period, period_date,
           operating_cf, capital_event_cf, is_actual) VALUES (?,?,?,?,?,?,1)
           ON CONFLICT(deal_id, scenario_id, period) DO UPDATE SET
           operating_cf=excluded.operating_cf,
           capital_event_cf=excluded.capital_event_cf,
           period_date=excluded.period_date, is_actual=1""",
        (deal_id, scenario_id, period, period_date, operating_cf, capital_event_cf))
    con.commit()
