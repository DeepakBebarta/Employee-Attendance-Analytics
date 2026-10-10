# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run

    python -m venv .venv
    .venv\Scripts\activate        (Linux/Mac: source .venv/bin/activate)
    pip install -r requirements.txt
    set MONGO_URI and MONGO_DB (environment variables or a local .env)
    uvicorn app.main:app --port 8000

Indexes are created automatically at startup. API docs: http://localhost:8000/docs

## Notes
- `test_rules.py` checks the business rules R1-R5 (`python test_rules.py`).
- Analytics are MongoDB aggregation pipelines; the trend uses `$densify` and `$setWindowFields`, the leaderboard uses `$rank`.
- Requires MongoDB 6.0+.
