# Deploying the API (backend for the website)

The site (Lovable) is the front end; this API runs the verified engine.

## Local test
    pip install -r requirements.txt
    uvicorn api:app --reload            # http://127.0.0.1:8000/docs

## Deploy on Render (free tier)
1. Push this folder to a GitHub repo.
2. render.com -> New -> Web Service -> connect the repo.
3. Build command:  pip install -r requirements.txt
   Start command:  uvicorn api:app --host 0.0.0.0 --port $PORT
4. Deploy. Your API base URL is https://<service-name>.onrender.com
   (Railway/Fly work the same way with the same start command.)

## Endpoints
- POST /api/run                 run the 5-stage pipeline on a deal payload
- POST /api/extract             upload OM / rent roll -> draft payload (needs ANTHROPIC_API_KEY)
- GET  /api/download/{run}/{f}  download a generated workbook
- GET  /api/sample              Meladon placeholder payload (demo autofill)
- GET  /api/health

## Notes
- Generated files live in temp storage; they disappear on restart. Users
  should download right after a run (the UI presents them immediately).
- CORS is open (*) for setup; restrict allow_origins to your site's
  domain in api.py before sharing widely.
- Free-tier services sleep when idle; the first request after a while
  can take ~30-60s to wake. The UI's loading state covers this.
- Statement workbooks contain live Excel formulas; totals/IRR compute
  when opened in Excel, Google Sheets, or LibreOffice.

## Enabling document extraction
1. Get an API key at console.anthropic.com (usage-billed; a typical
   OM extraction costs cents).
2. On Render: your service -> Environment -> Add Environment Variable:
   key ANTHROPIC_API_KEY, value your key. Save; Render redeploys.
Without the key the endpoint returns a clear 503 and the rest of the
API works normally.
