"""The trading log: the day's ticks and submitted orders, and the page that shows them.

The case that matters most is the one nobody tests by accident — a log that is not there. The
page exists to answer "what happened", and it is most needed precisely when something went
wrong; if a deleted or never-written log renders as an error, the page fails exactly when it
is wanted. So the absent-file behaviour is pinned here rather than assumed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.execution import store
from src.web.app import app

client = TestClient(app)


def _settings(tmp_path, **kwargs) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        live_dir=str(tmp_path / "data" / "live_results"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
    )
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    settings = _settings(tmp_path)
    write_state(settings, {"on": True, "strategy": "Alpha", "env": "paper"})
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.clear()


def _tick(settings, *, action="decided", reason="", when=None, order_ids=None, name="Alpha"):
    at = when or datetime.now(timezone.utc)
    record = store.tick_record(
        strategy=name, env="paper", action=action, reason=reason, settings=settings, at=at,
        order_ids=order_ids or [], bar="2026-09-17T17:30:00+00:00",
    )
    store.append_tick(settings, name, record, when=at)
    store.save_latest(settings, name, record)
    store.upsert_day(settings, name, record["day"], events=1, decided=1)
    return record


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------
def test_a_log_that_was_never_written_renders_as_an_empty_day(api_settings):
    """The never-run case: an empty day, not a 404 and not an error."""
    body = client.get("/api/v1/log").json()

    assert body["ok"] is True
    assert body["strategy"] == "Alpha"
    assert body["day"], "a day is always named, even with nothing in it"
    assert body["ticks"] == [] and body["orders"] == [] and body["days"] == []


def test_a_log_that_was_DELETED_renders_as_an_empty_day(api_settings):
    """Someone removed the log. The page has to keep working — that is when it is read.

    The broker half is unaffected, which is exactly why the page leads with it.
    """
    settings = api_settings
    _tick(settings, action="decided")
    ticks_dir = store.ticks_dir(settings, "Alpha")
    for path in ticks_dir.glob("*.jsonl"):
        path.unlink()
    store.index_path(settings, "Alpha").unlink()

    body = client.get("/api/v1/log").json()

    assert body["ok"] is True
    assert body["ticks"] == [] and body["days"] == []
    assert body["day"], "still names a day, so the picker has something to show"


def test_the_day_returns_ticks_newest_first(api_settings):
    settings = api_settings
    early = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    _tick(settings, action="refused", reason="stale bar", when=early)
    _tick(settings, action="decided", when=late)

    body = client.get("/api/v1/log").json()

    assert [tick["action"] for tick in body["ticks"]] == ["decided", "refused"], "newest first"
    assert body["day"] == "2026-09-17"


def test_the_orders_the_bot_submitted_come_from_the_loop_s_own_rows(api_settings):
    """The local half: what the machine tried to do, with the ids that join it to Alpaca."""
    settings = api_settings
    store.append_order(
        settings, "Alpha",
        store.order_record(
            settings=settings, strategy="Alpha", env="paper", at=datetime.now(timezone.utc),
            bar="2026-09-17T17:30:00+00:00",
            intent={"intent": "open", "status": "filled", "price": 101.5, "expected": 101.0,
                    "order_id": "ord-9", "client_order_id": "traider-Alpha-b3"},
        ),
    )

    body = client.get("/api/v1/log").json()

    assert len(body["orders"]) == 1
    assert body["orders"][0]["client_order_id"] == "traider-Alpha-b3"
    assert body["orders"][0]["order_id"] == "ord-9"
    assert body["orders"][0]["intent"] == "open"


def test_another_day_can_be_asked_for(api_settings):
    settings = api_settings
    _tick(settings, action="off", reason="trading is OFF", when=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))
    _tick(settings, action="decided", when=datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc))

    body = client.get("/api/v1/log?day=2026-09-16").json()

    assert body["day"] == "2026-09-16"
    assert [tick["action"] for tick in body["ticks"]] == ["off"]
    assert body["days"] == ["2026-09-17", "2026-09-16"], "the day menu, newest first"


def test_the_log_endpoint_never_fails_on_a_strategy_with_no_files(api_settings, monkeypatch):
    """A brand-new strategy, or a tree that was moved: an empty answer, not a 500."""
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    body = client.get("/api/v1/log?day=2026-09-17").json()

    assert body["ok"] is True and body["ticks"] == [] and body["orders"] == []


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
def test_the_page_is_served_with_its_own_placeholders(api_settings):
    response = client.get("/log")

    assert response.status_code == 200
    html = response.text
    for element in ("lg-state", "lg-days", "lg-positions", "lg-ticks", "lg-orders", "lg-trades"):
        assert f'id="{element}"' in html, element
    assert "/static/log.js" in html


def test_the_page_puts_the_account_before_the_local_record(api_settings):
    """Not decoration: the broker is the truth and the local rows are the explanation.

    A page that led with its own files would let a stale or deleted log pass for the state of
    the account, which is the one mistake this screen must not make.
    """
    html = client.get("/log").text

    assert html.index("Account") < html.index("What the loop did")
    assert html.index("What the loop did") < html.index("Trades closed")
