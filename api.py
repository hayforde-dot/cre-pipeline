"""Web API for the CRE pipeline — the backend a website (e.g. built in
Lovable) talks to. Run locally:  uvicorn api:app --reload
Deploy: any Python host (Render/Railway/Fly). See README_API.md.

POST /api/run        payload -> runs all 5 stages, returns metrics + file ids
GET  /api/download/{run_id}/{filename}   -> generated xlsx
GET  /api/sample     -> the Meladon placeholder payload (demo autofill)
GET  /api/health
"""
from __future__ import annotations
import os
import tempfile
import uuid
from pathlib import Path
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from cre.intake import run_full_pipeline, validate_payload, MELADON_PAYLOAD
from cre.extract import extract_from_documents

app = FastAPI(title="CRE Deal Pipeline API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])   # tighten to your site's domain in production

RUNS_DIR = Path(tempfile.gettempdir()) / "cre_runs"
RUNS_DIR.mkdir(exist_ok=True)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/sample")
def sample():
    return MELADON_PAYLOAD


@app.post("/api/run")
def run(payload: dict):
    errs = validate_payload(payload)
    if errs:
        raise HTTPException(status_code=422, detail=errs)
    run_id = uuid.uuid4().hex[:12]
    out_dir = RUNS_DIR / run_id
    try:
        r = run_full_pipeline(payload, db_path=str(out_dir / "deal.db"),
                              out_dir=out_dir)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=[str(e)])
    files = {k: {"filename": Path(v).name,
                 "url": f"/api/download/{run_id}/{Path(v).name}"}
             for k, v in r["files"].items()}
    return {"run_id": run_id, "report": r["report"], "files": files}


@app.get("/api/download/{run_id}/{filename}")
def download(run_id: str, filename: str):
    path = (RUNS_DIR / run_id / filename).resolve()
    if not path.is_file() or RUNS_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="file not found (runs are "
                            "kept in temporary storage and expire on restart)")
    return FileResponse(path, media_type=XLSX, filename=filename)


@app.post("/api/extract")
async def extract(files: list[UploadFile] = File(...)):
    """Upload an offering memorandum (pdf) and/or rent roll (xlsx/csv/pdf);
    returns a DRAFT payload to prefill the wizard, plus reviewer notes and
    a list of still-missing required fields. Never runs the pipeline."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail=[
            "Document extraction is not configured: set the ANTHROPIC_API_KEY "
            "environment variable on the server (get a key at "
            "console.anthropic.com)."])
    if len(files) > 4:
        raise HTTPException(status_code=422, detail=["upload at most 4 files"])
    payload_files = []
    for f in files:
        data = await f.read()
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(status_code=422,
                                detail=[f"{f.filename} exceeds the 15 MB limit"])
        payload_files.append((f.filename or "upload", data, f.content_type or ""))
    try:
        return extract_from_documents(payload_files)
    except ValueError as e:
        raise HTTPException(status_code=422 if "unsupported" in str(e) else 502,
                            detail=[str(e)])
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=[
            f"extraction model call failed: {e.response.status_code}"])
