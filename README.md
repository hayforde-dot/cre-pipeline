# CRE Deal Lifecycle Pipeline

Pro forma → underwriting → capital stack → waterfall → LP reporting, on one
shared data model (SQLite). An object created at Stage 1 (a deal, a rent-roll
line, a scenario) flows through to the final LP statement without re-entry.

## Run it

```bash
pip install openpyxl pytest
python run_pipeline.py meladon.db      # full 5-stage run on the placeholder deal
python -m pytest tests/ -q             # 17 tests incl. workbook replication
```

Outputs land in `outputs/`: LP statement workbook (fund + Co-GP layers),
underwriting summary workbook, markdown statement, and the JSON stage report.
`meladon.db` is the shared data model itself — every stage's inputs and
outputs as queryable records.

## What each stage does and where its logic came from

| Stage | Module | Source of conventions |
|---|---|---|
| 1 Pro forma | `cre/proforma.py` | Line taxonomy from *Levered DCF* / *Direct Cap* / *Rent Roll Summary*; exit = **forward-year NOI ÷ exit cap** (R98 = S83/C13) |
| 2 Underwriting | `cre/underwriting.py` | Senior sizing = **MIN(LTV, DSCR, DY)** exactly per *Financing the Foles* G13:H21; IO→amort debt-service switch per row 89; payoff per H12; IRR partitioning per *IRR-Partitioning-v2* |
| 3 Capital stack | `cre/capital.py` | Equity/promote blocks of both waterfall sheets; calls funded against negative net CF (*Waterfalls* F42, *Draw Schedule* equity-first) |
| 4 Waterfall | `cre/waterfall.py` | Two engines: `irr_hurdle` replicates *Waterfalls-Completed_2* (monthly hypothetical LP capital accounts, 4 hurdles, Co-GP recursion via parent stream); `american_dual_track` replicates *American-Style-Equity-Waterfall* (operating pref simple-on-contributions + capital-event compounding accounts) |
| 5 LP reporting | `cre/reporting.py` | Statement per partner: contributions, distributions **by tier**, running capital position, DPI/IRR as live Excel formulas |

## Verification record (all in `tests/`)

- `test_workbook_replication.py` — the strongest checks: the Python engines
  reproduce your actual workbooks **per period, to the cent**:
  - GP-LP waterfall: LP & GP distribution vectors across all 121 monthly
    periods, hurdle-1 account balance vector, headline totals
    (LP 9,764,032.58 / GP 2,021,055.98), XIRRs to ±2e-4.
  - Co-GP layer: totals and XIRRs (Sponsor 34.90%, Investors 17.83%).
  - American waterfall: LP/GP vectors all 11 periods, capital-event tier-1
    account vector, IRRs to ±1e-5.
  - Loan sizing: all three constraints, MIN, binding = DSCR, IO and
    amortizing debt service, payoff 60,092,899.70 — all match.
- `test_stages.py` — closed-form IRR/PMT checks; pro forma hand-computed to
  the cent; capital-call pro-rata/completeness; effective-split algebra
  (0.20 promote → 72/28; 0.45 → 49.5/50.5, matching workbook cols H/I).
- `test_end_to_end.py` — full-chain invariants on the placeholder deal:
  Stage-2 equity == Stage-3 calls == Stage-4 contributions; **cash
  conservation every period**; Co-GP layer exactly conserves the GP stream;
  provided **actual** cash flows override projections; the generated Excel
  statement, recalculated by LibreOffice, reproduces the engine's totals,
  EMx and IRR (independent formula-level cross-check).

## Schema provenance (`cre/schema.sql`)

Tables marked `[FROM WORKBOOKS]` encode structures your Excel files already
have (deals, rent_roll, revenue/expense_lines, loans, partnership_entities,
partners, waterfall_tiers, period_cash_flows, capital_calls). Tables marked
`[ADDED]` are new because your files compute these in formulas but never
store them as records — they're what makes the chain queryable:

- `scenarios` — base/upside/downside deltas (files are single-scenario)
- `proforma_lines` — Stage 1 output persisted per scenario
- `underwriting_results` — Stage 2 metrics per scenario × structure
- `deal_assumptions` — typed assumptions with an **`is_placeholder` flag**;
  any placeholder propagates a banner onto every generated statement
- `distributions`, `capital_accounts` — Stage 4/5 outputs as records

## Flagged conventions (my choices — review these)

1. **Stack**: Python 3.12 + stdlib SQLite + openpyxl. Zero infrastructure,
   the DB file *is* the data model, trivially portable to Postgres later.
2. **Mezz sizing** — your workbooks have no mezz template. Implemented as
   market-standard: interest-only, sized to min(combined-LTV headroom,
   combined-DSCR headroom on year-1 NOI, senior tested at its amortizing
   payment). Marked in code; replace with your house convention if different.
3. **Base capitalization for the waterfall** = senior-only structure
   (senior+mezz is underwritten and reported side-by-side). One-line change
   in `run_pipeline.py` to switch.
4. **AUM fee base** — the Co-GP sheet carries an AUM Fee % (0.5%) but shows
   zero fee cash flows, so the base (committed equity? NAV?) is ambiguous.
   The engine accepts an explicit per-period fee vector; the percentage is
   stored but **not applied** until you confirm the base.
5. **coc_avg** = average levered operating cash flow (sale excluded) ÷ equity.
6. Annual placeholder deal uses the `irr_hurdle` engine with
   periods_per_year=1 (accrual generalizes: (1+r)^(1/ppy)−1). Monthly deals
   run unchanged — that's what the replication tests exercise.

## Loading a real deal (Meladon Haymarket / Seabay Hotel)

Copy `seed_meladon()` in `run_pipeline.py`; replace rent roll, expense lines,
price, exit cap, loan terms, tier structure — and set `is_placeholder=0` on
each assumption you confirm. Everything downstream regenerates from the DB.
To feed actuals into the waterfall as they occur:
`record_actual_cf(con, deal_id, scenario_id, period, operating_cf, capital_event_cf)`.

## Known limits (stubs, stated plainly)

- **No K-1 / tax layer** — capital accounts here are economic
  (contributions/distributions), not tax-basis 704(b) accounts.
- **Land-development model not implemented** — schema is extensible
  (deal_type, monthly periods, draw-style calls all supported) but
  `Resi_-Land-Development-Model.xlsx` phase/lot logic was not built.
- **Multiple LPs (and GPs) per entity are supported** as pari-passu
  classes: hurdle math runs at the class level (the workbooks' own
  convention — exact, since calls fund pro rata), and each member receives
  its within-class share of every tier. Each LP gets its own statement
  sheet. Per-member side letters (e.g. a different pref for one LP) are
  NOT supported — that would need per-member hurdle accounts.
- **No UI/API server** — it's a library + runner. Endpoints would wrap
  `build_proforma / underwrite / run_entity / write_lp_statement_*` 1:1.
- Loss-to-lease is modeled as a constant % of GPR (your Levered DCF burns
  it off over time — not replicated).
