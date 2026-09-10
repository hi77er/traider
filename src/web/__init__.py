"""TRAIDER Web Portal — FastAPI dashboard for monitoring the bot.

- ``app.py``            FastAPI app + uvicorn entry
- ``auth.py``           HTTP Basic auth (WEB_PORTAL_* config)
- ``routes/``           HTTP endpoints (dashboard page + dataset API)
- ``services/``         business logic (dataset status, rows, async backfill)
- ``templates/``        dashboard page
- ``static/``           JS/CSS assets
"""
