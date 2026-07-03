"""Extraction feature tests. The Anthropic model call is mocked — these
verify everything around it: file handling, prompt assembly, response
parsing, draft validation, and the endpoint contract. The live model
leg requires ANTHROPIC_API_KEY on the deployed server."""
import io
import json
import openpyxl
import pytest
from fastapi.testclient import TestClient
import cre.extract as ex
from api import app

client = TestClient(app)

CANNED = json.dumps({
    "draft_payload": {
        "deal": {"name": "Maple Court", "property_type": "multifamily",
                 "units": 120, "purchase_price": 21500000,
                 "hold_period_years": 5, "exit_cap_rate": 0.06},
        "rent_roll": [{"floorplan": "1BR", "unit_count": 72, "market_rent_month": 1300},
                      {"floorplan": "2BR", "unit_count": 48, "market_rent_month": 1650}],
        "expense_lines": [{"name": "Property Taxes", "category": "fixed",
                           "year1_amount": 240000}],
        "senior_loan": {"rate": 0.065, "term_months": 120, "amort_months": 360,
                        "io_months": 12, "max_ltv": 0.65, "min_dscr": 1.25,
                        "min_debt_yield": 0.08},
        "partnership": None,
    },
    "notes": ["OM quotes taxes two ways: $240,000 (p.12) vs $232,000 (p.40); "
              "used p.12 — confirm."],
})


def _xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rent Roll"
    ws.append(["Floorplan", "Units", "Rent"])
    ws.append(["1BR", 72, 1300])
    ws.append(["2BR", 48, 1650])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_to_csv_text():
    txt = ex.xlsx_to_csv_text(_xlsx_bytes(), "rr.xlsx")
    assert "Rent Roll" in txt and "1BR,72,1300" in txt


def test_content_blocks_pdf_and_unsupported():
    blocks = ex.build_content_blocks([("om.pdf", b"%PDF-1.4 fake", "application/pdf")])
    assert blocks[0]["type"] == "document"
    assert blocks[-1]["type"] == "text" and "NEVER invent" in blocks[-1]["text"]
    with pytest.raises(ValueError, match="unsupported"):
        ex.build_content_blocks([("virus.exe", b"x", "")])


def test_extract_endpoint_round_trip(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ex, "_call_model", lambda blocks, key: CANNED)
    r = client.post("/api/extract",
                    files=[("files", ("rentroll.xlsx", _xlsx_bytes(),
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_payload"]["deal"]["name"] == "Maple Court"
    assert body["draft_payload"]["is_placeholder"] is False
    # partnership was absent from the documents -> flagged missing, not invented
    assert any("partnership" in m for m in body["missing"])
    assert any("confirm" in n for n in body["notes"])


def test_extract_handles_fenced_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ex, "_call_model",
                        lambda blocks, key: "```json\n" + CANNED + "\n```")
    out = ex.extract_from_documents([("a.csv", b"x,y", "text/csv")])
    assert out["draft_payload"]["deal"]["units"] == 120


def test_extract_no_key_is_503(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/api/extract",
                    files=[("files", ("a.csv", b"x", "text/csv"))])
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"][0]


def test_extract_bad_model_output_is_502(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ex, "_call_model", lambda blocks, key: "sorry, no json here")
    r = client.post("/api/extract",
                    files=[("files", ("a.csv", b"x", "text/csv"))])
    assert r.status_code == 502


def test_extract_unsupported_type_is_422(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    r = client.post("/api/extract", files=[("files", ("x.exe", b"x", ""))])
    assert r.status_code == 422
