# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All application code is in `app/main.py`.

## Run (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"   # or your Atlas URI
$env:MONGO_DB  = "attendance_db"
uvicorn app.main:app --port 8000
```

A local MongoDB can be started with `docker run -d --name mongo -p 27017:27017 mongo:7`.
`MONGO_URI` / `MONGO_DB` can also be put in a local `.env` (not committed); real environment variables win.
Indexes are created automatically at startup. Docs: http://localhost:8000/docs

## Notes

- All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
- `moving_avg_7d` averages the unrounded daily rates and rounds once at the end (R8); every reported number is rounded half-up.
- A PATCH that changes none of `status`, `punch_in`, `punch_out` is a 422, even if stored derived values would differ.
- Concurrent PATCH requests on one record use an optimistic guard; the loser gets 409.
- `scripts/verify_local.py` is an optional self-check (not part of the app): it seeds awkward data into a throw-away database and compares the analytics with an independent implementation.

## Nothing missing

Every endpoint in the contract is implemented.
