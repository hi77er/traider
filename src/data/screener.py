"""Whole-market screening via Yahoo's equity screener (free, no API key).

Why this module does not go through ``OpenBBClient``
----------------------------------------------------
``obb.equity.screener`` cannot express what the market panels need: it exposes
no sort field, no page size and no region filter, so "highest volume" would be
"the 250 rows Yahoo happened to return, sorted locally" — misleading. On top of
that its yfinance path is broken against the pinned yfinance: it reads
``YfData().user_agent_headers``, an attribute removed in yfinance 0.2.x.
(``src.data.openbb_session`` patches a different thing — the ``curl_cffi``
session used for ``obb.equity.price.historical``; the shim for the screener was
never needed because this module talks to the screener directly.)

This module therefore talks to Yahoo's screener directly — but through the SAME
``yfinance`` backend and a ``curl_cffi`` browser-impersonation session of its
own (``apply_yfinance_session``), so the project still has one market-data stack.
No OpenBB code is imported or executed on this path.

Yahoo API constraints (verified against the pinned yfinance):
  * at most **250** rows per request (larger ``size`` raises), so deep lists are
    paged with ``offset``;
  * the ``and`` operator requires **2 or more** operands;
  * only ``eq`` / ``gt`` / ``lt`` / ``btwn`` / ``and`` / ``or`` are valid —
    ``is-in`` / ``is-not-in`` are rejected, hence the local exchange filter;
  * results are **global** unless ``region`` is constrained.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from src.data.deadline import bounded

logger = logging.getLogger(__name__)

# ── Yahoo screener constants ────────────────────────────────────────────
REGION_US = "us"

# Yahoo exchange codes that make up the US primary tape. An allow-list (rather
# than denying "PNK") because pink sheets / OTC codes are numerous and change.
US_PRIMARY_EXCHANGES = ("NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS")

# Yahoo caps a screener request at 250 rows.
MAX_PAGE_SIZE = 250

# Yahoo's *preset* screeners ignore size/count and return at most this many rows.
MAX_PRESET_ROWS = 25

# Conventional "small cap" band, in USD.
SMALL_CAP_MIN = 300_000_000
SMALL_CAP_MAX = 2_000_000_000

# Raw screener field -> canonical column name.
_FIELD_MAP = {
    "symbol": "symbol",
    "shortName": "name",
    "longName": "name",
    "regularMarketPrice": "price",
    "regularMarketChangePercent": "change_percent",
    "regularMarketVolume": "volume",
    "averageDailyVolume3Month": "avg_volume_3m",
    "marketCap": "market_cap",
    "exchange": "exchange",
    "fullExchangeName": "exchange_name",
    "sector": "sector",
    "industry": "industry",
    "currency": "currency",
}

# Canonical output schema (stable contract for the web layer).
COLUMNS = [
    "symbol",
    "name",
    "price",
    "change_percent",
    "volume",
    "avg_volume_3m",
    "market_cap",
    "dollar_volume",
    "exchange",
    "exchange_name",
    "sector",
    "industry",
    "currency",
]


class ScreenerError(RuntimeError):
    """Raised when the screener is unavailable or a query is rejected."""


# ── session ─────────────────────────────────────────────────────────────
# (connect, read) seconds for the screener's own session — same reason as the OpenBB shim:
# curl_cffi reads on with no deadline at all unless one is given.
SCREENER_TIMEOUT_SECONDS = (10.0, 30.0)

# One Yahoo screener request. It answers in a second or two when it answers at all, so this is a
# bound on a dead connection rather than on a slow server.
SCREEN_DEADLINE_SECONDS = 30.0

_SESSION_PATCHED = False


def apply_yfinance_session() -> bool:
    """Point yfinance's HTTP singleton at a ``curl_cffi`` session.

    The market page fires one Yahoo request per panel, so browser TLS
    impersonation matters. Idempotent; returns True when a curl_cffi session is
    active, False when curl_cffi is unavailable (yfinance's own session is then
    used unchanged).
    """
    global _SESSION_PATCHED
    if _SESSION_PATCHED:
        return True
    try:
        from curl_cffi import requests as curl_requests
        from yfinance.data import YfData
    except ImportError as exc:  # pragma: no cover - depends on environment
        logger.warning("curl_cffi unavailable; screener calls may be rate-limited: %s", exc)
        return False

    session = curl_requests.Session(impersonate="chrome", timeout=SCREENER_TIMEOUT_SECONDS)
    data = YfData()
    setter = getattr(data, "_set_session", None)
    if callable(setter):
        setter(session)
    else:  # pragma: no cover - older yfinance
        data._session = session  # type: ignore[attr-defined]
    _SESSION_PATCHED = True
    logger.info("yfinance screener session -> curl_cffi (impersonate=chrome)")
    return True


# ── query construction ──────────────────────────────────────────────────
@dataclass(frozen=True)
class ScreenSpec:
    """Declarative description of one screener query."""

    key: str
    label: str
    description: str
    sort_field: str = "intradaymarketcap"
    sort_asc: bool = False
    preset: Optional[str] = None
    market_cap_min: Optional[int] = None
    market_cap_max: Optional[int] = None
    min_price: float = 1.0
    min_volume: int = 0
    min_avg_volume: int = 0
    sector: Optional[str] = None
    us_only: bool = True

    def criteria(self) -> str:
        """Human-readable summary of the filters actually applied."""
        parts: List[str] = []
        if self.preset:
            parts.append(f"Yahoo preset “{preset_label(self.preset)}”")
        if self.market_cap_min and self.market_cap_max:
            parts.append(
                f"market cap ${self.market_cap_min / 1e9:.2f}B–${self.market_cap_max / 1e9:.1f}B"
                if self.market_cap_max >= 1e9
                else f"market cap ${self.market_cap_min / 1e6:.0f}M–${self.market_cap_max / 1e6:.0f}M"
            )
        elif self.market_cap_min:
            parts.append(f"market cap > ${self.market_cap_min / 1e9:.2f}B")
        elif self.market_cap_max:
            parts.append(f"market cap < ${self.market_cap_max / 1e6:.0f}M")
        if self.min_price:
            parts.append(f"price > ${self.min_price:g}")
        if self.min_volume:
            parts.append(f"day volume > {self.min_volume:,}")
        if self.min_avg_volume:
            parts.append(f"3-month avg volume > {self.min_avg_volume:,}")
        if self.sector:
            parts.append(f"sector = {self.sector}")
        if self.us_only:
            parts.append("US primary tape (no OTC)")
        if not self.preset:
            direction = "ascending" if self.sort_asc else "descending"
            parts.append(f"sorted by {_SORT_LABELS.get(self.sort_field, self.sort_field)} ({direction})")
        return " · ".join(parts)


_SORT_LABELS = {
    "percentchange": "% change",
    "dayvolume": "volume",
    "intradaymarketcap": "market cap",
    "avgdailyvol3m": "3-month avg volume",
    "intradayprice": "price",
}

# Query sort field (Yahoo's screener vocabulary) -> canonical output column.
# These are NOT in ``_FIELD_MAP``: that maps response field names, and the two
# vocabularies differ (``percentchange`` vs ``regularMarketChangePercent``).
_SORT_TO_COLUMN = {
    "percentchange": "change_percent",
    "intradaypricechange": "change_percent",
    "dayvolume": "volume",
    "eodvolume": "volume",
    "intradaymarketcap": "market_cap",
    "intradayprice": "price",
    "eodprice": "price",
    "avgdailyvol3m": "avg_volume_3m",
}


def _equity_query_cls():
    try:
        from yfinance import EquityQuery
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ScreenerError(
            "yfinance is not installed. Run `pip install yfinance` in your virtual environment."
        ) from exc
    return EquityQuery


def build_query(spec: ScreenSpec) -> Any:
    """Translate ``spec`` into a yfinance ``EquityQuery``."""
    eq = _equity_query_cls()
    clauses: List[Any] = []
    if spec.us_only:
        clauses.append(eq("eq", ["region", REGION_US]))
    if spec.market_cap_min is not None:
        clauses.append(eq("gt", ["intradaymarketcap", spec.market_cap_min]))
    if spec.market_cap_max is not None:
        clauses.append(eq("lt", ["intradaymarketcap", spec.market_cap_max]))
    if spec.min_price:
        clauses.append(eq("gt", ["intradayprice", spec.min_price]))
    if spec.min_volume:
        clauses.append(eq("gt", ["dayvolume", spec.min_volume]))
    if spec.min_avg_volume:
        clauses.append(eq("gt", ["avgdailyvol3m", spec.min_avg_volume]))
    if spec.sector:
        clauses.append(eq("eq", ["sector", spec.sector]))
    if not clauses:  # an empty `and` is invalid
        return eq("gt", ["intradaymarketcap", 0])
    if len(clauses) == 1:  # `and` needs at least two operands
        return clauses[0]
    return eq("and", clauses)


# ── execution ───────────────────────────────────────────────────────────
def _raw_quotes(query: Any, sort_field: str, sort_asc: bool, size: int, offset: int) -> List[dict]:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ScreenerError("yfinance is not installed.") from exc
    apply_yfinance_session()
    size = max(1, min(int(size), MAX_PAGE_SIZE))
    try:
        result = bounded(
            lambda: yf.screen(
                query,
                sortField=sort_field,
                sortAsc=bool(sort_asc),
                size=size,
                offset=max(0, int(offset)),
            ),
            SCREEN_DEADLINE_SECONDS,
            what="the Yahoo screener",
            error=ScreenerError,
        )
    except Exception as exc:
        raise ScreenerError(f"Yahoo screener request failed: {exc}") from exc
    if not result:
        return []
    return list(result.get("quotes") or [])


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def _apply_local_filters(frame: pd.DataFrame, spec: ScreenSpec) -> pd.DataFrame:
    """Enforce the numeric screen filters locally.

    Query-based screens already constrain these server-side, but Yahoo's
    *preset* endpoint takes no query, so this is what makes the same filters
    true on both paths. A row whose value is missing cannot be shown to satisfy
    an active filter, so it is dropped (NaN comparisons are False).
    """
    checks = (
        ("market_cap", spec.market_cap_min, spec.market_cap_max),
        ("price", spec.min_price, None),
        ("volume", spec.min_volume, None),
        ("avg_volume_3m", spec.min_avg_volume, None),
    )
    for column, low, high in checks:
        if column not in frame.columns:
            continue
        if low:
            frame = frame[frame[column] >= low]
        if high:
            frame = frame[frame[column] <= high]
    return frame


def normalize(
    quotes: List[dict],
    spec: ScreenSpec,
    limit: Optional[int] = None,
    sort: bool = True,
) -> pd.DataFrame:
    """Normalize raw Yahoo quotes into the canonical schema.

    ``sort=False`` keeps the provider's own ordering (Yahoo's preset screeners
    rank server-side; re-sorting locally would throw that ranking away).
    """
    if not quotes:
        return _empty_frame()
    frame = pd.DataFrame(quotes)
    out = pd.DataFrame(index=frame.index)
    for raw, canonical in _FIELD_MAP.items():
        if raw in frame.columns and canonical not in out.columns:
            out[canonical] = frame[raw]
    for column in COLUMNS:
        if column not in out.columns:
            out[column] = None

    for column in ("price", "change_percent", "volume", "avg_volume_3m", "market_cap"):
        out[column] = pd.to_numeric(out[column], errors="coerce")

    out["name"] = out["name"].fillna(out["symbol"])
    out["dollar_volume"] = out["price"] * out["volume"]

    if spec.us_only:
        out = out[out["exchange"].isin(US_PRIMARY_EXCHANGES)]
    out = out.dropna(subset=["symbol", "price"])
    out = _apply_local_filters(out, spec)
    if spec.sort_field == "percentchange":
        out = out.dropna(subset=["change_percent"])
    if sort:
        out = out.sort_values(
            _canonical_sort(spec.sort_field), ascending=spec.sort_asc, na_position="last"
        )
    if limit is not None:
        out = out.head(limit)
    return out[COLUMNS].reset_index(drop=True)


def _canonical_sort(sort_field: str) -> str:
    """Canonical column to sort by, for a Yahoo screener sort field."""
    return _SORT_TO_COLUMN.get(sort_field, "market_cap")


def run_screen(spec: ScreenSpec, size: int = 25, offset: int = 0) -> pd.DataFrame:
    """Run one screener query and return the canonical frame."""
    if spec.preset:
        return run_preset(spec, size=size, offset=offset)
    query = build_query(spec)
    quotes = _raw_quotes(query, spec.sort_field, spec.sort_asc, size, offset)
    return normalize(quotes, spec)


def run_preset(spec: ScreenSpec, size: int = 25, offset: int = 0) -> pd.DataFrame:
    """Run a Yahoo *predefined* screener (its own ranking) plus our filters.

    Yahoo ranks these server-side and returns a fixed window of at most
    ``MAX_PRESET_ROWS`` rows — it ignores ``size``/``count``, and ``offset`` is
    unreliable (requesting a later offset still returns the same leading
    symbols). The caller's ``size`` is therefore applied locally as a plain
    limit, and this path is never paginated.
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ScreenerError("yfinance is not installed.") from exc
    apply_yfinance_session()
    try:
        result = bounded(
            lambda: yf.screen(spec.preset),
            SCREEN_DEADLINE_SECONDS,
            what=f"the Yahoo preset screener “{spec.preset}”",
            error=ScreenerError,
        )
    except Exception as exc:
        raise ScreenerError(f"Yahoo preset screener “{spec.preset}” failed: {exc}") from exc
    quotes = list((result or {}).get("quotes") or [])
    # Presets rank server-side; keep that order and only apply the local filters.
    return normalize(quotes, spec, limit=size, sort=False)


# ── presets ─────────────────────────────────────────────────────────────
def _preset_map() -> Dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError:  # pragma: no cover - depends on environment
        return {}
    return dict(getattr(yf, "PREDEFINED_SCREENER_QUERIES", {}) or {})


def preset_label(key: str) -> str:
    """Human label for a Yahoo preset key (``day_gainers`` -> ``Day gainers``)."""
    words = str(key).replace("-", "_").split("_")
    if not words:
        return str(key)
    return " ".join([words[0].capitalize()] + [w.lower() for w in words[1:]])


def list_presets() -> List[Dict[str, str]]:
    """Available Yahoo preset screeners, as ``{key, label}`` dicts."""
    return [{"key": key, "label": preset_label(key)} for key in sorted(_preset_map())]


# ── the landing-page panels ─────────────────────────────────────────────
PANELS: Dict[str, ScreenSpec] = {
    "top_gainers": ScreenSpec(
        key="top_gainers",
        label="Top Gainers",
        description="Biggest % gainers on the US primary tape today.",
        sort_field="percentchange",
        min_volume=100_000,
    ),
    "highest_volume": ScreenSpec(
        key="highest_volume",
        label="Highest Volume",
        description="Most shares traded today (raw volume, not relative volume).",
        sort_field="dayvolume",
        min_volume=0,
    ),
    "top_losers": ScreenSpec(
        key="top_losers",
        label="Top Losers",
        description="Biggest % decliners on the US primary tape today.",
        sort_field="percentchange",
        sort_asc=True,
        min_volume=100_000,
    ),
    "small_cap_gainers": ScreenSpec(
        key="small_cap_gainers",
        label="Top Gainers — Small Caps",
        description="Biggest % gainers among US small caps.",
        sort_field="percentchange",
        market_cap_min=SMALL_CAP_MIN,
        market_cap_max=SMALL_CAP_MAX,
        min_volume=250_000,
    ),
    "small_cap_volume": ScreenSpec(
        key="small_cap_volume",
        label="Highest Volume — Small Caps",
        description="Most traded US small caps today.",
        sort_field="dayvolume",
        market_cap_min=SMALL_CAP_MIN,
        market_cap_max=SMALL_CAP_MAX,
        min_volume=500_000,
    ),
    "whole_market": ScreenSpec(
        key="whole_market",
        label="Whole Market",
        description="Every US-listed equity, largest companies first.",
        sort_field="intradaymarketcap",
    ),
}

PANEL_ORDER = [
    "whole_market",
    "top_gainers",
    "highest_volume",
    "top_losers",
    "small_cap_gainers",
    "small_cap_volume",
]

DEFAULT_PRESET = "day_gainers"


def get_spec(key: str) -> ScreenSpec:
    """Panel spec by key (raises ``ScreenerError`` for an unknown panel)."""
    try:
        return PANELS[key]
    except KeyError as exc:
        raise ScreenerError(f"Unknown market panel: {key}") from exc


def get_preset_spec(
    preset: str,
    *,
    market_cap_min: Optional[int] = None,
    market_cap_max: Optional[int] = None,
    min_price: float = 1.0,
    min_volume: int = 0,
    us_only: bool = True,
) -> ScreenSpec:
    """Spec for the preset-driven screener panel, with the caller's filters.

    No ``sector``: Yahoo's preset endpoint accepts no query and its response
    carries no sector field, so a sector filter could not be enforced — and an
    unenforceable filter that still shows up in ``criteria()`` would be a lie.
    """
    return ScreenSpec(
        key="screener",
        label=preset_label(preset),
        description=f"Yahoo's “{preset_label(preset)}” preset with your filters applied.",
        preset=preset,
        market_cap_min=market_cap_min,
        market_cap_max=market_cap_max,
        min_price=min_price,
        min_volume=min_volume,
        us_only=us_only,
    )
