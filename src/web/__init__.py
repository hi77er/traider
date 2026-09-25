"""TRAIDER Web Portal — the FastAPI process behind two pages: the Strategy lab, where a
strategy is built and measured, and the Session monitor, where the loop's day is read and
trading is switched.

- ``app.py``            FastAPI app + uvicorn entry
- ``routes/``           HTTP endpoints (the two pages + the dataset API)
- ``services/``         business logic (dataset status, rows, async backfill)
- ``templates/``        the pages themselves
- ``static/``           JS/CSS assets
"""
