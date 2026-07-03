-- =====================================================================
-- CRE DEAL LIFECYCLE — SHARED DATA MODEL (SQLite)
-- One data model across: pro forma -> underwriting -> capital stack ->
-- waterfall -> LP reporting. Objects created at Stage 1 flow to Stage 5.
--
-- PROVENANCE:
--   [FROM WORKBOOKS] = structure exists in the uploaded Excel models
--   [ADDED]          = new table; the workbooks compute this in formulas
--                      but never store it as queryable records
-- =====================================================================

-- [FROM WORKBOOKS] Global assumptions header of every model
-- (Levered DCF B4:C16, DCF Template, Waterfalls 'Property-Level Cash Flow')
CREATE TABLE IF NOT EXISTS deals (
    deal_id            INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    deal_type          TEXT NOT NULL DEFAULT 'acquisition',  -- acquisition | development | land_dev
    property_type      TEXT,
    units              INTEGER,
    avg_unit_sf        REAL,
    purchase_price     REAL,
    closing_cost_pct   REAL DEFAULT 0.03,       -- Levered DCF C10
    hold_period_years  INTEGER,
    periods_per_year   INTEGER NOT NULL DEFAULT 1,  -- 1 = annual (American WF), 12 = monthly (Waterfalls-Completed)
    analysis_start     TEXT,                     -- ISO date, period 0
    exit_cap_rate      REAL,                     -- going-out cap, C13
    sale_cost_pct      REAL DEFAULT 0.03,        -- C15
    notes              TEXT
);

-- [ADDED] Scenario definitions (base/upside/downside). Workbooks are
-- single-scenario; Stage 2 requires base/upside/downside cases.
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id        INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    name               TEXT NOT NULL,            -- base | upside | downside
    rent_growth_delta  REAL DEFAULT 0,           -- added to every revenue growth rate
    exit_cap_delta     REAL DEFAULT 0,           -- added to exit cap
    opex_growth_delta  REAL DEFAULT 0,
    UNIQUE (deal_id, name)
);

-- [FROM WORKBOOKS] Rent Roll Summary sheet (Direct Cap / DCF workbooks):
-- floorplan, # units, avg SF, market rent, loss-to-lease, occupancy
CREATE TABLE IF NOT EXISTS rent_roll (
    rent_roll_id       INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    floorplan          TEXT NOT NULL,
    unit_count         INTEGER NOT NULL,
    avg_sf             REAL,
    market_rent_month  REAL NOT NULL,            -- per unit per month
    loss_to_lease_pct  REAL DEFAULT 0,           -- % of that floorplan's GPR
    occupancy_pct      REAL DEFAULT 1.0
);

-- [FROM WORKBOOKS] Other-income lines (RUBS, Storage, Parking, Other)
-- from Levered DCF B23:B26 / Direct Cap B17:B20
CREATE TABLE IF NOT EXISTS revenue_lines (
    revenue_line_id    INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    name               TEXT NOT NULL,
    year1_amount       REAL NOT NULL,            -- total for the property, year 1
    growth_rate        REAL DEFAULT 0
);

-- [FROM WORKBOOKS] Opex taxonomy (controllable/fixed) from Levered DCF
-- B29:B37 / Direct Cap B28:B39. pct_of_egr covers mgmt fee (3% of EGR).
CREATE TABLE IF NOT EXISTS expense_lines (
    expense_line_id    INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    name               TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT 'controllable',  -- controllable | fixed | reserve
    year1_amount       REAL,                     -- NULL if pct_of_egr set
    growth_rate        REAL DEFAULT 0,
    pct_of_egr         REAL                      -- e.g. property management 0.03
);

-- [ADDED] Typed assumption store with an explicit placeholder flag so a
-- placeholder can never silently masquerade as a real input.
CREATE TABLE IF NOT EXISTS deal_assumptions (
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    key                TEXT NOT NULL,            -- e.g. general_vacancy_pct, market_rent_growth
    value              REAL NOT NULL,
    is_placeholder     INTEGER NOT NULL DEFAULT 0,
    note               TEXT,
    PRIMARY KEY (deal_id, key)
);

-- [ADDED] Stage 1 OUTPUT: the multi-year pro forma, persisted per
-- scenario so Stages 2/4 consume records, not re-derived formulas.
CREATE TABLE IF NOT EXISTS proforma_lines (
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    year               INTEGER NOT NULL,         -- 1..hold+1 (hold+1 = forward NOI for exit)
    gpr                REAL, loss_to_lease REAL, concessions REAL,
    other_income       REAL, gross_revenue REAL, general_vacancy REAL,
    egr                REAL, opex REAL, noi REAL, reserves REAL,
    cfo                REAL,                     -- cash flow from operations
    PRIMARY KEY (deal_id, scenario_id, year)
);

-- [FROM WORKBOOKS] Loan block of 'Financing the Foles' (G4:H21) incl.
-- the three sizing constraints and the MIN() result.
CREATE TABLE IF NOT EXISTS loans (
    loan_id            INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    scenario_id        INTEGER REFERENCES scenarios(scenario_id),
    structure          TEXT NOT NULL,            -- 'senior' | 'senior_mezz'
    tranche            TEXT NOT NULL,            -- 'senior' | 'mezz'
    rate               REAL NOT NULL,
    term_months        INTEGER NOT NULL,
    amort_months       INTEGER,                  -- NULL = interest-only for full term
    io_months          INTEGER DEFAULT 0,
    max_ltv            REAL, min_dscr REAL, min_debt_yield REAL,
    sized_amount       REAL,
    binding_constraint TEXT
);

-- [ADDED] Stage 2 OUTPUT: underwriting metrics per scenario x structure.
CREATE TABLE IF NOT EXISTS underwriting_results (
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    structure          TEXT NOT NULL,            -- 'unlevered' | 'senior' | 'senior_mezz'
    metric             TEXT NOT NULL,
    value              REAL,
    PRIMARY KEY (deal_id, scenario_id, structure, metric)
);

-- [FROM WORKBOOKS] Waterfall entity headers. An entity is one waterfall
-- layer: 'GP-LP Returns' is one entity; 'Co-GP Returns' is a second
-- entity whose inflow is the GP's stream from the first (recursion).
CREATE TABLE IF NOT EXISTS partnership_entities (
    entity_id          INTEGER PRIMARY KEY,
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    name               TEXT NOT NULL,
    waterfall_style    TEXT NOT NULL,            -- 'irr_hurdle' (Waterfalls-Completed) | 'american_dual_track'
    aum_fee_pct        REAL DEFAULT 0,           -- D31/D32 'AUM Fee %'
    parent_entity_id   INTEGER REFERENCES partnership_entities(entity_id),
    parent_partner_id  INTEGER                   -- which partner's stream feeds this entity
);

-- [FROM WORKBOOKS] Equity Contributions block (B4:D7 of both waterfall sheets)
CREATE TABLE IF NOT EXISTS partners (
    partner_id         INTEGER PRIMARY KEY,
    entity_id          INTEGER NOT NULL REFERENCES partnership_entities(entity_id),
    name               TEXT NOT NULL,
    role               TEXT NOT NULL,            -- 'LP' | 'GP'
    equity_pct         REAL NOT NULL             -- C5/C6
);

-- [FROM WORKBOOKS] Promote Structure block (rows 10-13): hurdle rate,
-- GP promote %, remaining split pari passu by equity_pct. For the
-- american_dual_track style, track distinguishes operating vs capital
-- event tiers (rows 9-11 vs 13-16 of that workbook).
CREATE TABLE IF NOT EXISTS waterfall_tiers (
    entity_id          INTEGER NOT NULL REFERENCES partnership_entities(entity_id),
    track              TEXT NOT NULL DEFAULT 'all',  -- 'all' | 'operating' | 'capital_event'
    tier_no            INTEGER NOT NULL,
    hurdle_rate        REAL,                     -- NULL = residual tier
    promote_pct        REAL NOT NULL DEFAULT 0,  -- to GP off the top (col F)
    PRIMARY KEY (entity_id, track, tier_no)
);

-- [FROM WORKBOOKS] Property-Level Cash Flow sheet / Draw Schedule.
-- One row per period per scenario; is_actual lets provided actuals
-- override projections for the waterfall run.
CREATE TABLE IF NOT EXISTS period_cash_flows (
    deal_id            INTEGER NOT NULL REFERENCES deals(deal_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    period             INTEGER NOT NULL,         -- 0..N
    period_date        TEXT,
    operating_cf       REAL NOT NULL DEFAULT 0,  -- levered CF after financing
    capital_event_cf   REAL NOT NULL DEFAULT 0,  -- net reversion / refi proceeds
    is_actual          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (deal_id, scenario_id, period)
);

-- [FROM WORKBOOKS] Contributions rows (funded from negative net CF,
-- per Waterfalls F42 = -MIN(CF,0) x equity%; Draw Schedule equity-first)
CREATE TABLE IF NOT EXISTS capital_calls (
    entity_id          INTEGER NOT NULL REFERENCES partnership_entities(entity_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    partner_id         INTEGER NOT NULL REFERENCES partners(partner_id),
    period             INTEGER NOT NULL,
    amount             REAL NOT NULL,
    PRIMARY KEY (entity_id, scenario_id, partner_id, period)
);

-- [ADDED] Stage 4 OUTPUT: every distribution as a record
-- (partner x period x tier), which the workbooks only hold in formulas.
CREATE TABLE IF NOT EXISTS distributions (
    entity_id          INTEGER NOT NULL REFERENCES partnership_entities(entity_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    partner_id         INTEGER NOT NULL REFERENCES partners(partner_id),
    period             INTEGER NOT NULL,
    track              TEXT NOT NULL DEFAULT 'all',
    tier_no            INTEGER NOT NULL,
    amount             REAL NOT NULL,
    PRIMARY KEY (entity_id, scenario_id, partner_id, period, track, tier_no)
);

-- [ADDED] Stage 5 SOURCE: running capital accounts per partner.
CREATE TABLE IF NOT EXISTS capital_accounts (
    entity_id          INTEGER NOT NULL REFERENCES partnership_entities(entity_id),
    scenario_id        INTEGER NOT NULL REFERENCES scenarios(scenario_id),
    partner_id         INTEGER NOT NULL REFERENCES partners(partner_id),
    period             INTEGER NOT NULL,
    contribution       REAL NOT NULL DEFAULT 0,
    distribution       REAL NOT NULL DEFAULT 0,
    cum_contributed    REAL NOT NULL DEFAULT 0,
    cum_distributed    REAL NOT NULL DEFAULT 0,
    net_position       REAL NOT NULL DEFAULT 0,  -- cum_distributed - cum_contributed
    PRIMARY KEY (entity_id, scenario_id, partner_id, period)
);
