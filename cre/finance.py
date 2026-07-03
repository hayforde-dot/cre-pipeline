"""Core financial math. Conventions mirror the firm's Excel templates:
- IRR: periodic (Excel IRR); XIRR: dated, actual/365 (Excel XIRR)
- PMT / remaining balance: monthly, matching 'Financing the Foles' H10:H12
"""
from __future__ import annotations
from datetime import date


def npv(rate: float, cfs: list[float]) -> float:
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cfs))


def irr(cfs: list[float], lo: float = -0.9999, hi: float = 10.0) -> float | None:
    """Periodic IRR via bisection on NPV. Returns None if no sign change."""
    if not any(c > 0 for c in cfs) or not any(c < 0 for c in cfs):
        return None
    f_lo, f_hi = npv(lo, cfs), npv(hi, cfs)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cfs)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def xnpv(rate: float, cfs: list[float], dates: list[date]) -> float:
    d0 = dates[0]
    return sum(cf / (1 + rate) ** ((d - d0).days / 365.0) for cf, d in zip(cfs, dates))


def xirr(cfs: list[float], dates: list[date]) -> float | None:
    """Dated IRR (Excel XIRR convention, actual/365)."""
    if not any(c > 0 for c in cfs) or not any(c < 0 for c in cfs):
        return None
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = xnpv(lo, cfs, dates), xnpv(hi, cfs, dates)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, cfs, dates)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def pmt(rate_per_period: float, n_periods: int, pv: float) -> float:
    """Excel PMT(rate, nper, -pv): level payment that amortizes pv."""
    if rate_per_period == 0:
        return pv / n_periods
    return pv * rate_per_period / (1 - (1 + rate_per_period) ** -n_periods)


def remaining_balance(loan: float, annual_rate: float, amort_months: int,
                      months_amortized: int) -> float:
    """Balance after `months_amortized` of amortizing payments.
    Mirrors template H12 = PV(rate/12, amort - (term - io), -amort_pmt)."""
    r = annual_rate / 12
    p = pmt(r, amort_months, loan)
    remaining_n = amort_months - months_amortized
    if remaining_n <= 0:
        return 0.0
    if r == 0:
        return p * remaining_n
    return p * (1 - (1 + r) ** -remaining_n) / r


def equity_multiple(cfs: list[float]) -> float | None:
    """SUMIF(>0) / -SUMIF(<0), per template F36."""
    pos = sum(c for c in cfs if c > 0)
    neg = -sum(c for c in cfs if c < 0)
    return pos / neg if neg > 0 else None


def irr_partition(investment_cf: list[float], operating_cf: list[float],
                  reversion_cf: list[float]) -> dict:
    """Per IRR-Partitioning-v2: PV each component at the total-CF IRR;
    shares of positive PV attributable to operations vs reversion."""
    total = [i + o + r for i, o, r in zip(investment_cf, operating_cf, reversion_cf)]
    rate = irr(total)
    if rate is None:
        return {"irr": None, "cfo_share": None, "reversion_share": None}
    pv_ops = sum(cf / (1 + rate) ** t for t, cf in enumerate(operating_cf))
    pv_rev = sum(cf / (1 + rate) ** t for t, cf in enumerate(reversion_cf))
    pv_pos = pv_ops + pv_rev
    return {
        "irr": rate,
        "cfo_share": pv_ops / pv_pos if pv_pos else None,
        "reversion_share": pv_rev / pv_pos if pv_pos else None,
        "em_cfo_share": (sum(operating_cf) / (sum(operating_cf) + sum(reversion_cf)))
        if (sum(operating_cf) + sum(reversion_cf)) else None,
    }
