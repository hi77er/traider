"""FastAPI application for the TRAIDER Web Portal.

Run locally:
    uvicorn src.web.app:app --reload --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.web.routes import account as account_routes
from src.web.routes import chart as chart_routes
from src.web.routes import config as config_routes
from src.web.routes import backtest as backtest_routes
from src.web.routes import dataset as dataset_routes
from src.web.routes import delta as delta_routes
from src.web.routes import market as market_routes
from src.web.routes import pages as page_routes
from src.web.routes import report as report_routes
from src.web.routes import rules as rules_routes
from src.web.routes import signal as signal_routes

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="TRAIDER Web Portal", version="0.1.0")


@app.middleware("http")
async def no_store_static(request, call_next):
    """Never cache static assets (dev).

    Without this, browsers cache ``app.js``/``style.css`` and keep running the
    OLD code after a server restart (the file mtime is unchanged, so the
    server answers ``304 Not Modified``). no-store forces every load to fetch
    the current file from disk.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
app.include_router(page_routes.router)
app.include_router(dataset_routes.router)
app.include_router(delta_routes.router)
app.include_router(market_routes.router)
app.include_router(chart_routes.router)
app.include_router(config_routes.router)
app.include_router(account_routes.router)
app.include_router(rules_routes.router)
app.include_router(signal_routes.router)
app.include_router(backtest_routes.router)
app.include_router(report_routes.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.web.app:app", host="0.0.0.0", port=8000, reload=False)
