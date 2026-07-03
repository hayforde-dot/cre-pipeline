"""Extract pipeline inputs from deal documents (offering memorandum,
rent roll) using the Anthropic API. Produces a DRAFT payload for human
review in the wizard — extraction never runs the pipeline directly, and
the model is instructed to return null rather than invent any number.

Requires ANTHROPIC_API_KEY in the environment (console.anthropic.com).
"""
from __future__ import annotations
import base64
import io
import json
import os
import httpx
import openpyxl
from .intake import validate_payload

MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"
MAX_ROWS_PER_SHEET = 400

SCHEMA_HINT = """{
 "deal": {"name": str, "property_type": str, "units": int, "avg_unit_sf": number|null,
   "purchase_price": number|null, "closing_cost_pct": number|null,
   "hold_period_years": int|null, "exit_cap_rate": number|null,
   "sale_cost_pct": number|null, "analysis_start_year": int|null},
 "rent_roll": [{"floorplan": str, "unit_count": int, "avg_sf": number|null,
   "market_rent_month": number, "loss_to_lease_pct": number|null}],
 "revenue_lines": [{"name": str, "year1_amount": number, "growth_rate": number|null}],
 "expense_lines": [{"name": str, "category": "controllable"|"fixed",
   "year1_amount": number|null, "growth_rate": number|null, "pct_of_egr": number|null}],
 "assumptions": {"market_rent_growth": number|null, "loss_to_lease_pct": number|null,
   "concessions_pct": number|null, "general_vacancy_pct": number|null,
   "capital_reserve_per_unit": number|null, "capital_reserve_growth": number|null},
 "senior_loan": {"rate": number|null, "term_months": int|null, "amort_months": int|null,
   "io_months": int|null, "max_ltv": number|null, "min_dscr": number|null,
   "min_debt_yield": number|null},
 "valuation": {"direct_cap_rate": number|null, "dcf_going_in_cap": number|null},
 "partnership": {"name": str|null, "partners": [{"name": str, "role": "LP"|"GP",
   "equity_pct": number}]|null, "tiers": [{"tier_no": int, "hurdle_rate": number|null,
   "promote_pct": number}]|null}
}"""

PROMPT = f"""You are extracting inputs for a real-estate underwriting system
from the attached deal documents (offering memorandum and/or rent roll).

Return ONLY a JSON object, no prose, no markdown fences, of the form:
{{"draft_payload": <payload>, "notes": [<strings>]}}

<payload> must follow this schema:
{SCHEMA_HINT}

Rules — these are absolute:
1. NEVER invent, estimate, or round-trip a number that is not stated in
   the documents. If a value is absent, use null (or omit the list).
2. All rates and percentages as decimals (8% -> 0.08). Monthly rents as
   monthly numbers; annual amounts as annual.
3. Rent roll: one entry per floorplan/unit type with unit counts and
   market rents. Sum of unit_count should equal the property's units.
4. Classify expenses: repairs, payroll, utilities, admin, marketing,
   turnover, contract services = "controllable"; taxes, insurance,
   management = "fixed". If management is quoted as % of revenue, set
   pct_of_egr and leave year1_amount null.
5. If waterfall/promote terms appear, tiers must be ordered and the last
   tier must have hurdle_rate null (residual). If none appear, set
   "partners" and "tiers" to null.
6. In "notes", list: every field you were unsure about and why, any
   conflicting numbers between documents (state both), and anything the
   reviewer must confirm. Be specific.
7. Do not compute derived values (e.g. do not compute price from a cap
   rate); only transcribe what is stated."""


def xlsx_to_csv_text(data: bytes, filename: str) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS_PER_SHEET:
                rows.append("... (truncated)")
                break
            if any(v is not None for v in row):
                rows.append(",".join("" if v is None else str(v) for v in row))
        if rows:
            parts.append(f"### {filename} — sheet: {ws.title}\n" + "\n".join(rows))
    return "\n\n".join(parts)


def build_content_blocks(files: list[tuple[str, bytes, str]]) -> list[dict]:
    blocks = []
    for name, data, ctype in files:
        lower = name.lower()
        if lower.endswith(".pdf") or ctype == "application/pdf":
            blocks.append({"type": "document",
                           "source": {"type": "base64",
                                      "media_type": "application/pdf",
                                      "data": base64.b64encode(data).decode()}})
        elif lower.endswith((".xlsx", ".xlsm")):
            blocks.append({"type": "text",
                           "text": xlsx_to_csv_text(data, name)})
        elif lower.endswith((".csv", ".txt")):
            blocks.append({"type": "text",
                           "text": f"### {name}\n" + data.decode("utf-8", "replace")})
        else:
            raise ValueError(f"unsupported file type: {name} "
                             "(accepted: pdf, xlsx, csv, txt)")
    blocks.append({"type": "text", "text": PROMPT})
    return blocks


def _call_model(blocks: list[dict], api_key: str) -> str:
    r = httpx.post(API_URL, timeout=180.0,
                   headers={"x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json"},
                   json={"model": MODEL, "max_tokens": 8000,
                         "messages": [{"role": "user", "content": blocks}]})
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")


def extract_from_documents(files: list[tuple[str, bytes, str]],
                           api_key: str | None = None) -> dict:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server")
    text = _call_model(build_content_blocks(files), api_key).strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.index("{"):text.rindex("}") + 1]
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"model did not return valid JSON: {e}") from e
    draft = parsed.get("draft_payload") or {}
    draft.setdefault("is_placeholder", False)
    draft.setdefault("scenarios", [{"name": "base"}])
    draft.setdefault("statement_scenario", "base")
    missing = validate_payload(draft)      # incomplete draft is fine — reported, not fatal
    return {"draft_payload": draft,
            "notes": parsed.get("notes") or [],
            "missing": missing}
