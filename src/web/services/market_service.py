"""Market landing-page service (sits between HTTP routes and the screener).

Assembles the seven panels of the market landing page from
``src.data.screener`` and adds a short TTL cache: the page fires one Yahoo
request per panel, so without caching a few reloads in quick succession would
start hitting Yahoo's rate limits.

Every panel is fetched independently and its failure is reported *inside* its
own payload, so one broken screen never blanks the whole page.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.data import screener as screener_data

logger = logging.getLogger(__name__)

# Rows shown per panel unless the caller asks for otherwise.
DEFAULT_PANEL_ROWS = 25
MAX_PANEL_ROWS = screener_data.MAX_PAGE_SIZE

# Yahoo is fine with the landing page's ~7 requests, but not with a user
# hammering reload — serve the assembled page from memory for this long.
CACHE_TTL_SECONDS = 90

_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "size": None, "payload": None}


# ── serialization helpers ───────────────────────────────────────────────
def _clean(value: Any) -> Any:
    """Make a pandas/numpy cell JSON-safe (NaN/Inf -> None, numpy -> python)."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        if value is pd.NA:
            return None
    except Exception:  # pragma: no cover - defensive
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _records(frame: pd.DataFrame) -> List[dict]:
    """DataFrame -> list of JSON-safe dicts."""
    if frame is None or frame.empty:
        return []
    return [
        {str(key): _clean(value) for key, value in row.items()}
        for row in frame.to_dict("records")
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _panel_payload(spec: screener_data.ScreenSpec, size: int, offset: int = 0) -> dict:
    """One panel: metadata + rows, or an inline error."""
    payload = {
        "key": spec.key,
        "label": spec.label,
        "description": spec.description,
        "criteria": spec.criteria(),
        "sort_field": spec.sort_field,
        "sort_asc": spec.sort_asc,
        "size": size,
        "offset": offset,
        "count": 0,
        "rows": [],
        "error": None,
    }
    try:
        frame = screener_data.run_screen(spec, size=size, offset=offset)
        payload["rows"] = _records(frame)
        payload["count"] = len(payload["rows"])
    except Exception as exc:  # one panel must not blank the page
        logger.warning("Market panel %s failed: %s", spec.key, exc)
        payload["error"] = str(exc)
    return payload


# ── public API ──────────────────────────────────────────────────────────
def overview(size: int = DEFAULT_PANEL_ROWS, force: bool = False) -> dict:
    """All landing-page panels (cached for ``CACHE_TTL_SECONDS``)."""
    size = max(1, min(int(size), MAX_PANEL_ROWS))
    now = time.monotonic()
    with _lock:
        fresh = _cache["payload"] is not None and (now - _cache["at"]) < CACHE_TTL_SECONDS
        if fresh and not force and _cache["size"] == size:
            return dict(_cache["payload"], cached=True)

    panels = [_panel_payload(screener_data.get_spec(key), size) for key in screener_data.PANEL_ORDER]
    payload = {
        "generated_at": _now_iso(),
        "source": "Yahoo Finance equity screener",
        "cached": False,
        "size": size,
        "panels": panels,
        "errors": [f"{p['label']}: {p['error']}" for p in panels if p["error"]],
    }

    with _lock:
        _cache["at"] = time.monotonic()
        _cache["size"] = size
        _cache["payload"] = payload
    return payload


def panel(key: str, size: int = DEFAULT_PANEL_ROWS, offset: int = 0) -> dict:
    """A single panel, uncached — used for paging a long list (e.g. whole market)."""
    spec = screener_data.get_spec(key)
    return _panel_payload(spec, max(1, min(int(size), MAX_PANEL_ROWS)), max(0, int(offset)))


def presets() -> dict:
    """The Yahoo preset screeners available for the screener panel."""
    options = screener_data.list_presets()
    return {
        "presets": options,
        "default": screener_data.DEFAULT_PRESET,
        "max_rows": screener_data.MAX_PRESET_ROWS,
        "error": None,
    }


def screen(
    preset: Optional[str] = None,
    size: int = DEFAULT_PANEL_ROWS,
    market_cap_min: Optional[int] = None,
    market_cap_max: Optional[int] = None,
    min_price: float = 1.0,
    min_volume: int = 0,
) -> dict:
    """Run one preset screener with the caller's filters."""
    preset = (preset or screener_data.DEFAULT_PRESET).strip()
    known = {item["key"] for item in screener_data.list_presets()}
    if preset not in known:
        return {
            "key": "screener",
            "label": preset,
            "criteria": "",
            "count": 0,
            "rows": [],
            "error": f"Unknown screener preset: {preset}",
            "presets": sorted(known),
        }
    spec = screener_data.get_preset_spec(
        preset,
        market_cap_min=market_cap_min,
        market_cap_max=market_cap_max,
        min_price=float(min_price),
        min_volume=int(min_volume),
    )
    # Yahoo's preset endpoint returns at most MAX_PRESET_ROWS rows.
    size = max(1, min(int(size), screener_data.MAX_PRESET_ROWS))
    payload = _panel_payload(spec, size)
    payload["preset"] = preset
    return payload


def refresh() -> dict:
    """Drop the cached overview (the page's Refresh button)."""
    with _lock:
        _cache["at"] = 0.0
        _cache["size"] = None
        _cache["payload"] = None
    return {"ok": True, "message": "Market cache cleared"}
