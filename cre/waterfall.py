"""STAGE 4 — Waterfall Engine.
Two engines, each an exact replication of an uploaded workbook:

1. style='irr_hurdle'  (Waterfalls-Completed_2.xlsx, 'GP-LP Returns' and
   'Co-GP Returns'). Per-period, per-hurdle hypothetical LP capital
   accounts. For each hurdle k with annual rate r and effective splits:
     begin_k   = prior ending balance
     accrual_k = begin_k x ((1+r)^(1/periods_per_year) - 1)          [F41]
     contrib   = -MIN(netCF,0) x lp_equity_pct                        [F42]
     lp_dist_k = MIN(begin_k + accrual_k - lp_dists_this_period_at_
                 lower_tiers, remaining_cash x lp_split_k), floor 0   [F43/F58/F73]
     end_k     = begin_k + accrual_k + contrib - prior - lp_dist_k    [F44/F59]
     gp_dist_k = lp_dist_k / lp_split_k x gp_split_k                  [F48]
   Residual tier: straight split of remainder                         [F84/F85]
   Same-period contributions do NOT accrue or distribute in-period.

2. style='american_dual_track'  (American-Style-Equity-Waterfall.xlsx).
   Annual. Operating track on operating CF:
     Tier 1 pref: LP req'd = rate x cumulative LP contributions through
     the PRIOR period (simple, non-compounding)                       [G40]
     lp_dist = MIN(req'd, avail x lp_split)                           [G41]
     Tier 2: residual split of remainder                              [G50/G51]
   Capital-event track on capital-event CF, accounts compound on
   balance, ALL LP distributions (both tracks) count as 'past
   distributions', same-period contributions ARE distributable:
     lp_dist = MIN(avail x lp_split, begin + contrib + accrual - past) [G60/G77]
   Final tier: residual split                                          [G89/G90]

Recursion: an entity with parent_entity/parent_partner set takes as its
gross cash flow the parent partner's stream (distributions -
contributions), exactly as 'Co-GP Returns' F33 = 'GP-LP Returns'!F29.
"""
from __future__ import annotations
import sqlite3
from .capital import effective_splits
from .finance import irr, xirr, equity_multiple


class WaterfallResult:
    def __init__(self, entity_id, partner_names, periods):
        self.entity_id = entity_id
        self.partner_names = partner_names          # {partner_id: name}
        self.periods = periods
        self.contributions = {pid: [0.0] * periods for pid in partner_names}
        self.distributions = {pid: [0.0] * periods for pid in partner_names}
        # tier detail: {(pid, period, track, tier_no): amount}
        self.tier_detail = {}
        self.hurdle_accounts = {}                    # {(track, tier_no): [ending balances]}
        self.checks = []

    def net_cf(self, pid):
        return [d - c for d, c in zip(self.distributions[pid], self.contributions[pid])]

    def add_dist(self, pid, period, track, tier_no, amount):
        if amount <= 0:
            return
        self.distributions[pid][period] += amount
        key = (pid, period, track, tier_no)
        self.tier_detail[key] = self.tier_detail.get(key, 0.0) + amount


def _partner_classes(con, entity_id):
    """Group partners into an LP class and a GP class. Waterfall math runs
    at the class level (exactly the workbook convention — their 'LP' and
    'GP' columns ARE classes); each member then receives its pro-rata
    within-class share. Because capital calls fund pro rata on the same
    schedule, every member's hypothetical account is a scaled copy of the
    class account, so class-level hurdle math is exact for members too.
    Members within a class are pari passu; per-member side letters
    (e.g. a different pref for one LP) are not supported."""
    rows = con.execute("SELECT * FROM partners WHERE entity_id=? ORDER BY partner_id",
                       (entity_id,)).fetchall()
    out = {}
    for role in ("LP", "GP"):
        mem = [r for r in rows if r["role"] == role]
        if not mem:
            raise ValueError(f"entity needs at least one {role}")
        pct = sum(r["equity_pct"] for r in mem)
        out[role] = dict(pct=pct, members=[
            (r["partner_id"], r["name"], r["equity_pct"] / pct) for r in mem])
    return out["LP"], out["GP"]


def _credit_contrib(res, cls, p, amount):
    for pid, _, share in cls["members"]:
        res.contributions[pid][p] += amount * share


def _fan_dist(res, cls, p, track, tier_no, amount):
    for pid, _, share in cls["members"]:
        res.add_dist(pid, p, track, tier_no, amount * share)


def _names(*classes):
    return {pid: name for cls in classes for pid, name, _ in cls["members"]}


def run_irr_hurdle(con, entity_id, net_cf: list[float],
                   periods_per_year: int) -> WaterfallResult:
    lp, gp = _partner_classes(con, entity_id)
    tiers = con.execute(
        "SELECT * FROM waterfall_tiers WHERE entity_id=? AND track='all' ORDER BY tier_no",
        (entity_id,)).fetchall()
    if not tiers or tiers[-1]["hurdle_rate"] is not None:
        raise ValueError("last tier must be residual (hurdle_rate NULL)")
    n = len(net_cf)
    res = WaterfallResult(entity_id, _names(lp, gp), n)
    hurdles = [t for t in tiers if t["hurdle_rate"] is not None]
    residual = tiers[-1]
    balances = {t["tier_no"]: 0.0 for t in hurdles}
    for t in hurdles + [residual]:
        res.hurdle_accounts[("all", t["tier_no"])] = []

    for p in range(n):
        cf = net_cf[p]
        c_lp = -min(cf, 0.0) * lp["pct"]
        c_gp = -min(cf, 0.0) * gp["pct"]
        _credit_contrib(res, lp, p, c_lp)
        _credit_contrib(res, gp, p, c_gp)
        avail = max(cf, 0.0)
        lp_dists_so_far = 0.0
        for t in hurdles:
            r = t["hurdle_rate"]
            lp_split, gp_split = effective_splits(lp["pct"], gp["pct"],
                                                  t["promote_pct"])
            begin = balances[t["tier_no"]]
            accrual = begin * ((1 + r) ** (1 / periods_per_year) - 1)
            cap_by_account = begin + accrual - lp_dists_so_far
            lp_d = max(min(cap_by_account, avail * lp_split), 0.0)
            gp_d = lp_d / lp_split * gp_split if lp_split else 0.0
            balances[t["tier_no"]] = begin + accrual + c_lp - lp_dists_so_far - lp_d
            res.hurdle_accounts[("all", t["tier_no"])].append(balances[t["tier_no"]])
            _fan_dist(res, lp, p, "all", t["tier_no"], lp_d)
            _fan_dist(res, gp, p, "all", t["tier_no"], gp_d)
            lp_dists_so_far += lp_d
            avail = max(avail - lp_d - gp_d, 0.0)
        lp_split, gp_split = effective_splits(lp["pct"], gp["pct"],
                                              residual["promote_pct"])
        _fan_dist(res, lp, p, "all", residual["tier_no"], avail * lp_split)
        _fan_dist(res, gp, p, "all", residual["tier_no"], avail * gp_split)
        res.hurdle_accounts[("all", residual["tier_no"])].append(0.0)
    return res


def run_american_dual_track(con, entity_id, operating_cf: list[float],
                            capital_event_cf: list[float],
                            funding_cf: list[float]) -> WaterfallResult:
    """funding_cf: negative amounts are equity funding needs (the
    workbook's 'Investment Cash Flow' row 30, sign-flipped)."""
    lp, gp = _partner_classes(con, entity_id)
    ops_tiers = con.execute(
        "SELECT * FROM waterfall_tiers WHERE entity_id=? AND track='operating' ORDER BY tier_no",
        (entity_id,)).fetchall()
    ev_tiers = con.execute(
        "SELECT * FROM waterfall_tiers WHERE entity_id=? AND track='capital_event' ORDER BY tier_no",
        (entity_id,)).fetchall()
    for ts, nm in ((ops_tiers, "operating"), (ev_tiers, "capital_event")):
        if not ts or ts[-1]["hurdle_rate"] is not None:
            raise ValueError(f"{nm} track must end with a residual tier")
    n = len(operating_cf)
    res = WaterfallResult(entity_id, _names(lp, gp), n)
    ev_balances = {t["tier_no"]: 0.0 for t in ev_tiers if t["hurdle_rate"] is not None}
    for t in ev_tiers:
        res.hurdle_accounts[("capital_event", t["tier_no"])] = []
    cum_lp_contrib_prior = 0.0

    for p in range(n):
        c_need = -min(funding_cf[p], 0.0)
        c_lp = c_need * lp["pct"]
        c_gp = c_need * gp["pct"]
        _credit_contrib(res, lp, p, c_lp)
        _credit_contrib(res, gp, p, c_gp)

        # ---- operating track ----
        avail = max(operating_cf[p], 0.0)
        lp_ops_dist = 0.0
        for t in ops_tiers:
            lp_split, gp_split = effective_splits(lp["pct"], gp["pct"],
                                                  t["promote_pct"])
            if t["hurdle_rate"] is not None:
                req = t["hurdle_rate"] * cum_lp_contrib_prior          # G40: simple, prior contribs
                lp_d = max(min(req - lp_ops_dist, avail * lp_split), 0.0)
                gp_d = lp_d / lp_split * gp_split if lp_split else 0.0
            else:
                lp_d, gp_d = avail * lp_split, avail * gp_split
            _fan_dist(res, lp, p, "operating", t["tier_no"], lp_d)
            _fan_dist(res, gp, p, "operating", t["tier_no"], gp_d)
            lp_ops_dist += lp_d
            avail = max(avail - lp_d - gp_d, 0.0)

        # ---- capital-event track ----
        avail = max(capital_event_cf[p], 0.0)
        lp_past = lp_ops_dist                                           # G59 = ops LP dists
        for t in ev_tiers:
            lp_split, gp_split = effective_splits(lp["pct"], gp["pct"],
                                                  t["promote_pct"])
            if t["hurdle_rate"] is not None:
                begin = ev_balances[t["tier_no"]]
                accrual = begin * t["hurdle_rate"]                      # G58: on balance
                cap = begin + c_lp + accrual - lp_past                  # G60: contrib distributable
                lp_d = max(min(avail * lp_split, cap), 0.0)
                gp_d = lp_d / lp_split * gp_split if lp_split else 0.0
                ev_balances[t["tier_no"]] = begin + c_lp + accrual - lp_past - lp_d
                res.hurdle_accounts[("capital_event", t["tier_no"])].append(
                    ev_balances[t["tier_no"]])
                lp_past += lp_d
            else:
                lp_d, gp_d = avail * lp_split, avail * gp_split
                res.hurdle_accounts[("capital_event", t["tier_no"])].append(0.0)
            _fan_dist(res, lp, p, "capital_event", t["tier_no"], lp_d)
            _fan_dist(res, gp, p, "capital_event", t["tier_no"], gp_d)
            avail = max(avail - lp_d - gp_d, 0.0)
        cum_lp_contrib_prior += c_lp
    return res


def run_entity(con, entity_id: int, scenario_id: int,
               deal_period_cfs: dict | None = None,
               parent_result: WaterfallResult | None = None,
               fees: list[float] | None = None) -> WaterfallResult:
    """Dispatch + persistence. deal_period_cfs: {'operating': [...],
    'capital_event': [...], 'dates': [...] or None} — required for a root
    entity; a child entity instead consumes parent_result."""
    ent = con.execute("SELECT * FROM partnership_entities WHERE entity_id=?",
                      (entity_id,)).fetchone()
    deal = con.execute("SELECT * FROM deals WHERE deal_id=?", (ent["deal_id"],)).fetchone()
    ppy = deal["periods_per_year"]

    if ent["parent_entity_id"] is not None:
        if parent_result is None:
            raise ValueError("child entity requires parent_result")
        gross = parent_result.net_cf(ent["parent_partner_id"])           # Co-GP F33
        operating, capital_event = gross, [0.0] * len(gross)
    else:
        operating = deal_period_cfs["operating"]
        capital_event = deal_period_cfs["capital_event"]
        gross = [o + c for o, c in zip(operating, capital_event)]

    fees = fees or [0.0] * len(gross)
    net = [g - f for g, f in zip(gross, fees)]                           # F34 = F33 - F32

    if ent["waterfall_style"] == "irr_hurdle":
        res = run_irr_hurdle(con, entity_id, net, ppy)
        distributable = [max(x, 0.0) for x in net]
    elif ent["waterfall_style"] == "american_dual_track":
        if ppy != 1:
            raise ValueError("american_dual_track replicates an annual workbook")
        op_net = [o - f for o, f in zip(operating, fees)]
        # funding = negative parts of BOTH streams (workbook row 30): a
        # capital call can coincide with an operating distribution — the
        # two tracks are separate, unlike the netted irr_hurdle stream.
        funding = [min(o, 0.0) + min(e, 0.0) for o, e in zip(op_net, capital_event)]
        res = run_american_dual_track(con, entity_id, op_net, capital_event, funding)
        distributable = [max(o, 0.0) + max(e, 0.0)
                         for o, e in zip(op_net, capital_event)]
    else:
        raise ValueError(f"unknown waterfall style {ent['waterfall_style']}")

    # conservation check: every period, sum of partner dists == cash distributed
    for p in range(len(net)):
        total_d = sum(res.distributions[pid][p] for pid in res.distributions)
        assert abs(total_d - distributable[p]) < 0.01, \
            f"period {p}: distributed {total_d:,.2f} != available {distributable[p]:,.2f}"

    # ---- persist distributions + capital accounts ----
    con.execute("DELETE FROM distributions WHERE entity_id=? AND scenario_id=?",
                (entity_id, scenario_id))
    for (pid, p, track, tier), amt in res.tier_detail.items():
        con.execute("INSERT INTO distributions VALUES (?,?,?,?,?,?,?)",
                    (entity_id, scenario_id, pid, p, track, tier, amt))
    con.execute("DELETE FROM capital_accounts WHERE entity_id=? AND scenario_id=?",
                (entity_id, scenario_id))
    for pid in res.partner_names:
        cc = cd = 0.0
        for p in range(len(net)):
            c, d = res.contributions[pid][p], res.distributions[pid][p]
            cc += c
            cd += d
            con.execute("INSERT INTO capital_accounts VALUES (?,?,?,?,?,?,?,?,?)",
                        (entity_id, scenario_id, pid, p, c, d, cc, cd, cd - cc))
    con.commit()
    return res


def partner_metrics(res: WaterfallResult, periods_per_year: int,
                    dates=None) -> dict:
    out = {}
    for pid, name in res.partner_names.items():
        cf = res.net_cf(pid)
        r = xirr(cf, dates) if dates else irr(cf)
        if r is not None and dates is None and periods_per_year != 1:
            r = (1 + r) ** periods_per_year - 1
        out[name] = dict(
            contributions=sum(res.contributions[pid]),
            distributions=sum(res.distributions[pid]),
            profit=sum(cf), irr=r, em=equity_multiple(cf))
    return out
