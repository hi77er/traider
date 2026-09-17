"""Order execution against a broker that is never actually contacted.

These tests exist to pin the things that would cost money if they were wrong: which
URL a key is sent to, whether an order is refused BEFORE it is sent, whether a
revoked key is retried (it must not be), whether a timeout is mistaken for a failure
(it must not be — by then the order is live), and what a live run is told when the
resting bracket already closed a position.

The HTTP layer is a stub, so CI never reaches Alpaca and no test ever holds a real
key. The stub answers by (method, path) and records every request, which is how the
"nothing was sent" assertions are made rather than trusted.
"""

from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.execution import (
    AlpacaBroker,
    AlpacaClient,
    AlpacaError,
    AlpacaExecutor,
    OrderRefused,
    build_order_payload,
    execute_with_retry,
    resolve_execution_target,
    shares_for,
)
from src.execution.alpaca_broker import broker_position
from src.strategy.broker import FILLED, NO_FILL, REJECTED, Fill
from src.strategy.engine import CLOSE, OPEN, Intent

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


# ---------------------------------------------------------------------------
# a broker that answers from a script
# ---------------------------------------------------------------------------
class StubResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = {} if body is None else body
        self.headers = headers or {}
        self.content = b"{}"

    def json(self):
        return self._body


class StubSession:
    """Answers by (METHOD, path). Queued responses are consumed in order.

    The LAST response for a route is sticky, which is what a poll needs: it is called
    with the same URL until the order reaches a terminal state.
    """

    def __init__(self, routes=None):
        self.routes = {key: list(value) for key, value in (routes or {}).items()}
        self.requests = []
        self.headers_seen = []

    def queue(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.replace(PAPER_URL, "").replace(LIVE_URL, "")
        self.requests.append(
            {"method": method, "path": path, "url": url, "params": params, "json": json}
        )
        self.headers_seen.append(dict(headers or {}))
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"the code called an unexpected endpoint: {key}")
        scripted = self.routes[key]
        if not scripted:
            raise AssertionError(f"no scripted answer left for {key}")
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]


def _settings(**overrides):
    values = dict(
        instrument="AAPL",
        execution_env="paper",
        alpaca_paper_api_key="PK-PAPER",
        alpaca_paper_api_secret="PAPER-SECRET",
        alpaca_live_api_key="PK-LIVE",
        alpaca_live_api_secret="LIVE-SECRET",
        execution_max_retries=2,
        execution_retry_base_delay_seconds=0.0,
        execution_order_timeout_seconds=5,
        # Risk settings named rather than inherited: empty means NOT APPLIED, and an
        # entry with no stop and no target has nothing to bracket.
        stop_loss_percent=2.0,
        take_profit_percent=4.0,
        risk_limit_percent=2.0,
        max_exposure_percent=90.0,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _filled(symbol="AAPL", price="101.50", qty="10", **extra):
    body = {
        "id": "ord-1", "client_order_id": "cid-1", "symbol": symbol,
        "side": "buy", "status": "filled", "filled_qty": qty,
        "filled_avg_price": price, "order_class": "bracket",
    }
    body.update(extra)
    return StubResponse(200, body)


def _executor(settings=None, routes=None, **kwargs):
    settings = settings or _settings()
    session = StubSession(routes)
    target = kwargs.pop("target", None) or resolve_execution_target(settings)
    client = AlpacaClient(target, session=session, timeout=1)
    # Open by default, and injected rather than read: the switch has its own tests.
    kwargs.setdefault("guard", lambda: None)
    return AlpacaExecutor(settings, client=client, sleep=lambda _s: None, **kwargs)


# ---------------------------------------------------------------------------
# where an order goes
# ---------------------------------------------------------------------------
def test_paper_and_live_are_the_same_api_with_different_urls_and_keys():
    paper = resolve_execution_target(_settings(execution_env="paper"))
    live = resolve_execution_target(_settings(execution_env="live"))

    assert paper.base_url == PAPER_URL and paper.key_id == "PK-PAPER"
    assert live.base_url == LIVE_URL and live.key_id == "PK-LIVE"
    assert live.live is True and paper.live is False
    assert paper.secret != live.secret


def test_a_missing_key_refuses_instead_of_downgrading():
    from src.execution import ExecutionConfigError

    # Live asked for, live keys absent: the answer is NO, not "paper will do". A live
    # key pair that is empty is not a paper key pair either — the two are different
    # credentials on the same API, and only the right one is accepted.
    with pytest.raises(ExecutionConfigError):
        resolve_execution_target(
            _settings(execution_env="live", alpaca_live_api_key="", alpaca_live_api_secret="")
        )


def test_the_secret_never_appears_in_a_repr_or_an_error():
    target = resolve_execution_target(_settings())
    assert "PAPER-SECRET" not in repr(target)

    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(401, {"code": 40110000, "message": "unauthorized"}))
    client = AlpacaClient(target, session=session)
    with pytest.raises(AlpacaError) as caught:
        client.account()
    assert "PAPER-SECRET" not in str(caught.value)


def test_every_request_is_authenticated_with_the_key_pair():
    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "1000"}))
    client = AlpacaClient(resolve_execution_target(_settings()), session=session)

    assert client.account()["equity"] == "1000"
    headers = session.headers_seen[0]
    assert headers["APCA-API-KEY-ID"] == "PK-PAPER"
    assert headers["APCA-API-SECRET-KEY"] == "PAPER-SECRET"


# ---------------------------------------------------------------------------
# refusing BEFORE sending
# ---------------------------------------------------------------------------
def test_trading_off_refuses_and_nothing_is_sent():
    executor = _executor(guard=lambda: "trading is OFF")

    with pytest.raises(OrderRefused):
        executor.place_order("AAPL", "BUY", 10, reference_price=100.0)

    assert executor.client.calls == [], "an order must not be built, let alone sent"


def test_a_risk_veto_refuses_and_nothing_is_sent():
    class Veto:
        def validate_signal(self, signal, state, **kwargs):
            return type("D", (), {"approved": False, "reason": "exposure exceeded"})()

    executor = _executor(validator=Veto())
    with pytest.raises(OrderRefused) as caught:
        executor.place_order("AAPL", "BUY", 10, reference_price=100.0)
    assert "exposure exceeded" in str(caught.value)
    assert executor.client.calls == []


def test_a_naked_entry_is_refused_rather_than_sent_without_its_stop():
    # Fractional sizes are DAY-only at Alpaca and cannot be bracketed. Sending the
    # entry anyway would open an unprotected position, which is worse than no entry.
    with pytest.raises(OrderRefused) as caught:
        build_order_payload(
            symbol="AAPL", side="BUY", quantity=1.5, client_order_id="c",
            stop_loss_price=95.0, take_profit_price=110.0, base_price=100.0,
        )
    assert "fractional" in str(caught.value).lower()


def test_one_leg_of_a_bracket_is_refused():
    with pytest.raises(OrderRefused) as caught:
        build_order_payload(
            symbol="AAPL", side="BUY", quantity=10, client_order_id="c",
            stop_loss_price=95.0, base_price=100.0,
        )
    assert "both" in str(caught.value).lower()


def test_a_stop_on_the_wrong_side_of_the_entry_is_refused():
    with pytest.raises(OrderRefused):
        build_order_payload(
            symbol="AAPL", side="BUY", quantity=10, client_order_id="c",
            stop_loss_price=105.0, take_profit_price=110.0, base_price=100.0,
        )
    # ...and for a short, the levels are the other way round.
    with pytest.raises(OrderRefused):
        build_order_payload(
            symbol="AAPL", side="SELL", quantity=10, client_order_id="c",
            stop_loss_price=95.0, take_profit_price=90.0, base_price=100.0,
        )


def test_a_stop_within_a_cent_of_the_base_price_is_refused():
    with pytest.raises(OrderRefused) as caught:
        build_order_payload(
            symbol="AAPL", side="BUY", quantity=10, client_order_id="c",
            stop_loss_price=99.995, take_profit_price=110.0, base_price=100.0,
        )
    assert "0.01" in str(caught.value)


def test_impossible_numbers_are_refused():
    for side, quantity in (("SIDEWAYS", 10), ("BUY", 0), ("BUY", -5)):
        with pytest.raises(OrderRefused):
            build_order_payload(symbol="AAPL", side=side, quantity=quantity, client_order_id="c")


# ---------------------------------------------------------------------------
# the order that goes out
# ---------------------------------------------------------------------------
def test_a_bracket_goes_out_as_ONE_bracket_order():
    executor = _executor()
    executor.client._session.queue(
        "POST", "/v2/orders",
        StubResponse(200, {
            "id": "ord-9", "client_order_id": "cid", "symbol": "AAPL", "side": "buy",
            "status": "accepted", "order_class": "bracket", "filled_qty": "0",
            "filled_avg_price": None,
        }),
    )
    executor.client._session.queue(
        "GET", "/v2/orders/ord-9",
        _filled(price="100.25", order_class="bracket"),
    )

    result = executor.place_order(
        "AAPL", "BUY", 10, stop_loss_price=95.0, take_profit_price=110.0, reference_price=100.0,
    )

    payload = next(r for r in executor.client.calls if r["method"] == "POST")["json"]
    assert payload["order_class"] == "bracket"
    assert payload["stop_loss"] == {"stop_price": "95.00"}
    assert payload["take_profit"] == {"limit_price": "110.00"}
    assert payload["qty"] == "10"
    assert payload["client_order_id"]
    # ONE submit: the exits ride along instead of being sent beside it.
    assert sum(1 for r in executor.client.calls if r["method"] == "POST") == 1
    assert result["status"] == "filled"
    assert result["filled_avg_price"] == 100.25
    assert result["env"] == "paper" and result["live"] is False


def test_the_client_order_id_is_generated_once_so_a_retry_cannot_double_fill():
    executor = _executor()
    session = executor.client._session
    session.queue("POST", "/v2/orders",
                  StubResponse(500, {"message": "server error"}),
                  StubResponse(200, {"id": "ord-1", "client_order_id": "cid", "symbol": "AAPL",
                                     "side": "buy", "status": "accepted", "filled_qty": "0"}))
    session.queue("GET", "/v2/orders/ord-1", _filled())

    executor.place_order("AAPL", "BUY", 10, reference_price=100.0)

    posted = [r for r in executor.client.calls if r["method"] == "POST"]
    assert len(posted) == 2, "a 5xx should have been retried"
    assert posted[0]["json"]["client_order_id"] == posted[1]["json"]["client_order_id"]


# ---------------------------------------------------------------------------
# what may be retried
# ---------------------------------------------------------------------------
def test_a_revoked_key_is_not_retried():
    executor = _executor()
    executor.client._session.queue(
        "POST", "/v2/orders",
        StubResponse(403, {"code": 40110000, "message": "forbidden"},
                     headers={"X-Request-ID": "req-42"}),
    )

    with pytest.raises(AlpacaError) as caught:
        executor.place_order("AAPL", "BUY", 10, reference_price=100.0)

    assert caught.value.retryable is False
    assert caught.value.rejected is True
    assert "req-42" in str(caught.value), "the request id is what support asks for"
    assert len([r for r in executor.client.calls if r["method"] == "POST"]) == 1


def test_a_wrong_order_is_not_retried():
    executor = _executor()
    executor.client._session.queue("POST", "/v2/orders", StubResponse(422, {"message": "bad qty"}))
    with pytest.raises(AlpacaError) as caught:
        executor.place_order("AAPL", "BUY", 10, reference_price=100.0)
    assert caught.value.retryable is False
    assert len([r for r in executor.client.calls if r["method"] == "POST"]) == 1


def test_a_busy_broker_is_retried_with_growing_delays():
    waited = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise AlpacaError("server error", status_code=503, retryable=True)
        return "ok"

    assert execute_with_retry(
        flaky, max_retries=3, base_delay=1.0, sleep=waited.append, label="test"
    ) == "ok"
    assert calls["n"] == 3
    assert waited == [1.0, 2.0], "backoff must grow, not repeat"


def test_retries_are_bounded_and_the_last_error_is_the_one_raised():
    calls = {"n": 0}

    def always_busy():
        calls["n"] += 1
        raise AlpacaError(f"busy {calls['n']}", status_code=503, retryable=True)

    with pytest.raises(AlpacaError) as caught:
        execute_with_retry(always_busy, max_retries=2, base_delay=0.0, sleep=lambda _s: None)
    assert calls["n"] == 3, "max_retries counts retries, so 2 means 3 attempts"
    assert "busy 3" in str(caught.value)


def test_a_transport_failure_is_classified_as_retryable():
    class Boom:
        def __init__(self):
            self.n = 0

        def request(self, *args, **kwargs):
            self.n += 1
            raise ConnectionError("connection reset")

    target = resolve_execution_target(_settings())
    client = AlpacaClient(target, session=Boom())
    with pytest.raises(AlpacaError) as caught:
        client.account()
    assert caught.value.retryable is True, "a reset may or may not have been received"


# ---------------------------------------------------------------------------
# what comes back
# ---------------------------------------------------------------------------
def test_a_timeout_is_reported_and_never_retried():
    executor = _executor()
    session = executor.client._session
    session.queue("POST", "/v2/orders",
                  StubResponse(200, {"id": "ord-7", "symbol": "AAPL", "side": "buy",
                                     "status": "new", "filled_qty": "0"}))
    session.queue("GET", "/v2/orders/ord-7",
                  StubResponse(200, {"id": "ord-7", "symbol": "AAPL", "side": "buy",
                                     "status": "new", "filled_qty": "0"}))

    result = executor.place_order("AAPL", "BUY", 10, reference_price=100.0, timeout=0.0001)

    assert result["timed_out"] is True
    assert result["status"] == "new"
    assert result["filled_avg_price"] is None
    assert len([r for r in executor.client.calls if r["method"] == "POST"]) == 1, (
        "an order that may be live must never be re-sent"
    )


def test_a_partial_fill_is_reported_as_it_happens():
    executor = _executor()
    session = executor.client._session
    session.queue("POST", "/v2/orders",
                  StubResponse(200, {"id": "ord-3", "symbol": "AAPL", "side": "buy",
                                     "status": "partially_filled", "filled_qty": "4"}))
    session.queue("GET", "/v2/orders/ord-3",
                  StubResponse(200, {"id": "ord-3", "symbol": "AAPL", "side": "buy",
                                     "status": "partially_filled", "filled_qty": "4",
                                     "filled_avg_price": "100.10"}))

    result = executor.place_order("AAPL", "BUY", 10, reference_price=100.0, timeout=0.0001)

    assert result["filled_qty"] == 4.0
    assert result["terminal"] is False


def test_a_flat_position_is_an_answer_not_an_error():
    executor = _executor()
    executor.client._session.queue("GET", "/v2/positions/AAPL", StubResponse(404, {"message": "position does not exist"}))
    assert executor.position("AAPL") is None


# ---------------------------------------------------------------------------
# the strategy's broker seam
# ---------------------------------------------------------------------------
def test_shares_come_from_equity_and_the_intents_weight():
    assert shares_for(1.0, 100.0, 1000.0) == 10
    assert shares_for(0.5, 100.0, 1000.0) == 5
    # Whole shares: a fraction of a share is not worth the residual it leaves behind.
    assert shares_for(1.0, 300.0, 1000.0) == 3
    assert shares_for(1.0, 2000.0, 1000.0) == 0
    assert shares_for(1.0, 0.0, 1000.0) == 0


def test_a_long_position_maps_with_a_positive_quantity_and_a_short_with_a_negative():
    long = broker_position({"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "101.5"})
    assert long.quantity == 10 and long.entry_price == 101.5 and long.short is False
    assert long.is_flat is False

    short = broker_position({"symbol": "AAPL", "qty": "-10", "side": "short", "avg_entry_price": "98.25"})
    assert short.quantity == -10 and short.short is True

    assert broker_position(None).is_flat is True


def test_the_broker_opens_the_position_the_intent_describes():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "10000"}))
    session.queue("POST", "/v2/orders", _filled(price="100.00", qty="50"))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(
        Intent(action=OPEN, reason="signal", expected_price=100.0, stop=95.0, take=110.0, weight=0.5)
    )

    payload = next(r for r in executor.client.calls if r["method"] == "POST")["json"]
    assert payload["side"] == "buy"
    assert payload["qty"] == "50", "0.5 weight of 10000 equity at 100.00 is 50 shares"
    assert payload["order_class"] == "bracket"
    assert fill.status == FILLED and fill.price == 100.0
    assert fill.quantity == 50.0


def test_a_short_intent_is_a_SELL_with_the_levels_the_other_way_up():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "10000"}))
    session.queue("POST", "/v2/orders", _filled(price="100.00", qty="25"))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(
        Intent(action=OPEN, reason="signal", short=True, expected_price=100.0,
               stop=105.0, take=90.0, weight=0.25)
    )

    payload = next(r for r in executor.client.calls if r["method"] == "POST")["json"]
    assert payload["side"] == "sell"
    assert payload["qty"] == "25"
    assert payload["stop_loss"] == {"stop_price": "105.00"}
    assert payload["take_profit"] == {"limit_price": "90.00"}
    assert fill.status == FILLED


def test_an_entry_the_equity_cannot_afford_is_reported_not_rounded_up():
    settings = _settings()
    broker = AlpacaBroker(settings, executor=_executor(settings), equity_provider=lambda: 50.0)

    fill = broker.submit(Intent(action=OPEN, reason="signal", expected_price=100.0, weight=1.0))

    assert fill.status == REJECTED
    assert "afford" in fill.detail


def test_a_broker_refusal_becomes_a_rejected_fill_and_does_not_raise():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "10000"}))
    session.queue("POST", "/v2/orders", StubResponse(403, {"message": "forbidden"}))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(Intent(action=OPEN, reason="signal", expected_price=100.0, weight=0.5))

    assert fill.status == REJECTED and fill.filled is False
    assert "403" in fill.detail


def test_the_broker_implements_the_seam_the_driver_is_written_against():
    """A Fill for every failure, and never a raise: the driver records, it does not catch."""
    from src.strategy.broker import Broker

    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "10000"}))
    session.queue("POST", "/v2/orders", StubResponse(422, {"message": "bad order"}))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    assert isinstance(broker, Broker)
    # A rejected entry comes back as a Fill, so a mid-tick failure cannot leave the
    # driver half-updated; and "nothing to submit" is answered, not raised.
    assert broker.submit(Intent(action=OPEN, expected_price=100.0, weight=0.5)).status == REJECTED
    assert broker.submit(Intent(action=OPEN, skipped=True)).status == NO_FILL
    assert broker.submit(None).status == NO_FILL


def test_closing_cancels_the_resting_exits_before_it_closes():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/orders",
                  StubResponse(200, [
                      {"id": "stop-1", "symbol": "AAPL", "side": "sell", "status": "new"},
                      {"id": "take-1", "symbol": "AAPL", "side": "sell", "status": "new"},
                  ]))
    session.queue("DELETE", "/v2/orders/stop-1", StubResponse(204))
    session.queue("DELETE", "/v2/orders/take-1", StubResponse(204))
    session.queue("GET", "/v2/positions/AAPL",
                  StubResponse(200, {"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "100"}))
    session.queue("DELETE", "/v2/positions/AAPL",
                  StubResponse(200, {"id": "close-1", "symbol": "AAPL", "status": "accepted",
                                     "filled_qty": "0"}))
    session.queue("GET", "/v2/orders/close-1", _filled(price="99.50", qty="10", order_class="simple"))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(Intent(action=CLOSE, reason="stop"))

    order_of_calls = [(r["method"], r["path"]) for r in session.requests]
    close_index = order_of_calls.index(("DELETE", "/v2/positions/AAPL"))
    assert ("DELETE", "/v2/orders/stop-1") in order_of_calls[:close_index], (
        "a resting stop left alive after the close can open the opposite position"
    )
    assert ("DELETE", "/v2/orders/take-1") in order_of_calls[:close_index]
    assert fill.status == FILLED and fill.price == 99.50


def test_an_exit_the_resting_bracket_already_filled_is_a_fill_at_the_brokers_price():
    settings = _settings()
    session = StubSession()
    # No resting orders to cancel, and the position is gone: the bracket got there first.
    session.queue("GET", "/v2/orders", StubResponse(200, []))
    session.queue("GET", "/v2/positions/AAPL", StubResponse(404, {"message": "position does not exist"}))
    session.queue("GET", "/v2/orders", StubResponse(200, [
        {"id": "stop-1", "symbol": "AAPL", "side": "sell", "status": "filled",
         "filled_avg_price": "94.75", "filled_qty": "10"},
    ]))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(Intent(action=CLOSE, reason="stop"))

    assert fill.status == FILLED, "the broker is flat, so the exit happened"
    assert fill.price == 94.75, "the price is read back from the broker, never re-derived"


def test_an_exit_that_is_accepted_but_unfilled_is_not_reported_as_a_fill():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, []))
    session.queue("GET", "/v2/positions/AAPL",
                  StubResponse(200, {"symbol": "AAPL", "qty": "10", "side": "long"}))
    session.queue("DELETE", "/v2/positions/AAPL",
                  StubResponse(200, {"id": "close-2", "symbol": "AAPL", "status": "new", "filled_qty": "0"}))
    session.queue("GET", "/v2/orders/close-2",
                  StubResponse(200, {"id": "close-2", "symbol": "AAPL", "status": "new", "filled_qty": "0"}))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    fill = broker.submit(Intent(action=CLOSE, reason="signal"))

    assert fill.status == NO_FILL
    assert fill.filled is False, "booking a price the broker never gave would be a fiction"


def test_a_broker_that_cannot_be_asked_never_answers_flat():
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/positions/AAPL", StubResponse(500, {"message": "boom"}))
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    with pytest.raises(AlpacaError):
        broker.position()


def test_an_equity_weight_is_never_more_than_the_account():
    # A weight above 1 is a sizing bug somewhere else; it must not become leverage here.
    assert shares_for(3.0, 100.0, 1000.0) == 10
    assert shares_for(-1.0, 100.0, 1000.0) == 0


def test_no_module_outside_the_execution_package_talks_to_the_broker():
    """The seam is a boundary, not a convention.

    Only ``src/execution`` may know Alpaca's order endpoints — if this fails, a second
    component grew its own way to place an order.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        if "execution" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "/v2/orders" in text or "/v2/positions" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"these modules reach the broker directly: {offenders}"


def test_the_fill_contract_the_driver_relies_on():
    fill = Fill(status=FILLED, price=100.0)
    assert fill.filled is True
    assert Fill(status=FILLED, price=None).filled is False, "a fill with no price is not usable"
    assert Fill(status=NO_FILL, price=100.0).filled is False
    assert Fill(status=REJECTED).filled is False


# ---------------------------------------------------------------------------
# the two halves, fitted together
# ---------------------------------------------------------------------------
def test_a_live_tick_places_a_bracketed_order_through_the_shared_engine(tmp_path):
    """The seam that matters: the strategy's decision becomes ONE real order.

    This is the only test that runs the whole chain — `LiveDriver` asks the engine,
    the engine's intent goes to `AlpacaBroker`, the broker turns it into an order — and
    it asserts the first two things anyone would want to know about a live run: that it
    sends exactly one bracket order, and that the position is only recorded once the
    broker says it filled.
    """
    import pandas as pd

    from src.strategy import StrategyConfig, StrategyEngine, bar_from_row
    from src.strategy.live import LiveDriver

    settings = _settings()
    session = StubSession()
    executor = AlpacaExecutor(
        settings, client=AlpacaClient(resolve_execution_target(settings), session=session),
        sleep=lambda _s: None, guard=lambda: None,
    )
    broker = AlpacaBroker(settings, executor=executor)

    class AlwaysBuy:
        """A generator shaped like the real one — the point here is the broker, not the rules."""

        def evaluate_frame(self, df):
            return df.assign(signal="BUY")

    config = StrategyConfig.from_settings(
        settings, slippage=0.0, commission=0.0
    )
    driver = LiveDriver(
        settings=settings,
        engine=StrategyEngine(config),
        generator=AlwaysBuy(),
        broker=broker,
        state_path=tmp_path / "state.json",
        name="TEST",
    )

    # The window is derived from the feature config, so the fixture is built from it
    # rather than from a number that could drift away from what the driver needs.
    need = driver.required_bars
    bars = need + 4
    frame = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(bars)],
            "high": [101.0 + i for i in range(bars)],
            "low": [99.0 + i for i in range(bars)],
            "close": [100.5 + i for i in range(bars)],
            "volume": [1_000] * bars,
        },
        index=pd.date_range("2026-09-14 09:00", periods=bars, freq="1h"),
    )
    candles = [bar_from_row(i, ts, row) for i, (ts, row) in enumerate(frame.iterrows())]

    # Ask the broker what it holds (flat), then let the entry fill.
    session.queue("GET", "/v2/positions/AAPL", StubResponse(404, {"message": "position does not exist"}))
    session.queue("GET", "/v2/account", StubResponse(200, {"equity": "20000"}))
    session.queue("POST", "/v2/orders", _filled(price="110.50", qty="90"))

    report = driver.on_bar_closed(frame.iloc[: need + 1], next_bar=candles[need + 1])

    assert report["action"] == "decided", report
    assert report["signal"] == "BUY"
    orders = [r for r in session.requests if r["method"] == "POST"]
    assert len(orders) == 1, "a decision is one order, not a burst of them"
    payload = orders[0]["json"]
    assert payload["side"] == "buy"
    assert payload["order_class"] == "bracket", "the protection travels with the entry"
    assert "stop_loss" in payload and "take_profit" in payload
    assert payload["client_order_id"].startswith("traider-TEST-"), payload["client_order_id"]

    # The position is recorded from the FILL, not from the expectation.
    assert driver.state.position is not None
    assert driver.state.position.entry_price == 110.50
    assert (tmp_path / "state.json").exists(), "the tick is remembered, so a restart cannot re-fire it"
    assert driver.on_bar_closed(frame.iloc[: need + 1], next_bar=candles[need + 1])["action"] == "noop"


# ---------------------------------------------------------------------------
# the name a broker deduplicates on
# ---------------------------------------------------------------------------
def test_the_order_id_is_stable_and_says_what_it_is():
    """Derived, not generated — and readable enough to find in Alpaca's own dashboard."""
    from src.strategy.live import order_id_for

    bar = "2026-09-17T13:30:00+00:00"
    base = order_id_for("Epsilon", "paper", bar, 0)

    assert base == order_id_for("Epsilon", "paper", bar, 0), "stable across processes"
    assert base != order_id_for("Epsilon", "paper", "2026-09-17T14:30:00+00:00", 0)
    assert base != order_id_for("Epsilon", "paper", bar, 1), "one bar can produce two orders"
    assert base != order_id_for("Beta", "paper", bar, 0)
    assert base != order_id_for("Epsilon", "live", bar, 0), "paper and live are different accounts"
    assert "Epsilon" in base, "an order in the broker's list should say whose it was"


def test_the_order_id_stays_inside_alpacas_limit():
    """48 characters, which a strategy name alone can blow through."""
    from src.strategy.live import order_id_for

    long_name = "Alpha - SXR8.DE with a really very long descriptive name indeed"
    for index in range(4):
        identifier = order_id_for(long_name, "paper", "2026-09-17T13:30:00+00:00", index)
        assert len(identifier) <= 48, identifier
        assert identifier.startswith("traider-")


def test_a_bar_decided_again_after_a_crash_names_the_SAME_order(tmp_path):
    """Why the id is derived rather than a fresh uuid, in one test.

    The driver saves its state LAST so a tick that dies mid-way leaves the bar undecided
    and retryable — but retryable is only SAFE if the retry is the same order. Alpaca
    deduplicates on the client order id, so a derived id turns the second submission into a
    refused duplicate, where a generated one would open a second position.

    The two drivers here are two processes: the first crashed without saving anything, so
    the second has no memory of the bar and decides it again.
    """
    import pandas as pd

    from src.data import dataset
    from src.strategy import StrategyConfig, StrategyEngine
    from src.strategy.broker import SimulatedBroker
    from src.strategy.live import LiveDriver, order_id_for

    class AlwaysBuy:
        def evaluate_frame(self, df):
            return df.assign(signal="BUY")

    class Recording(SimulatedBroker):
        def __init__(self):
            super().__init__()
            self.names = []

        def submit(self, intent, client_order_id=None):
            self.names.append(client_order_id)
            return super().submit(intent)

    settings = _settings(instrument="AAPL", historical_bar_size="1h")
    engine_settings = StrategyConfig.from_settings(settings, slippage=0.0, commission=0.0)
    need = LiveDriver(settings=settings, engine=StrategyEngine(engine_settings), generator=None).required_bars
    frame = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(need + 2)],
            "high": [101.0 + i for i in range(need + 2)],
            "low": [99.0 + i for i in range(need + 2)],
            "close": [100.5 + i for i in range(need + 2)],
            "volume": [1_000] * (need + 2),
        },
        index=pd.date_range("2026-09-14 09:30", periods=need + 2, freq="1h"),
    )

    brokers = []
    for attempt in range(2):
        broker = Recording()
        brokers.append(broker)
        LiveDriver(
            settings=settings,
            engine=StrategyEngine(engine_settings),
            generator=AlwaysBuy(),
            broker=broker,
            state_path=tmp_path / f"state-{attempt}.json",
            name="Epsilon",
        ).on_bar_closed(frame.iloc[: need + 1])

    first, second = brokers
    assert first.names and first.names == second.names, "the retry is the same order, not another"
    assert first.names[0] is not None, "the driver named the order rather than leaving it to the executor"
    signal_bar = dataset.bar_key(settings, frame.index[need])
    assert first.names[0] == order_id_for("Epsilon", "paper", signal_bar, 0)


# ---------------------------------------------------------------------------
# moving the exits after the fill (defect 2)
# ---------------------------------------------------------------------------
def _executor_on(session, settings=None):
    """An executor whose HTTP is the stub, with the switch forced open.

    The switch has its own tests; here it would only be a second thing that can make an
    order refuse.
    """
    settings = settings or _settings()
    client = AlpacaClient(resolve_execution_target(settings), session=session, timeout=1)
    return AlpacaExecutor(settings, client=client, sleep=lambda _s: None, guard=lambda: None)


def _open_bracket(stop="96.00", take="104.00", *, parent_type="market", legs=True):
    """A filled bracket entry with its two exits still resting — Alpaca's own shape."""
    order = {
        "id": "ord-1", "symbol": "AAPL", "side": "buy", "type": parent_type,
        "status": "filled", "order_class": "bracket", "filled_avg_price": "100.00",
    }
    if legs:
        order["legs"] = [
            {"id": "leg-stop", "symbol": "AAPL", "side": "sell", "type": "stop",
             "status": "new", "stop_price": stop},
            {"id": "leg-take", "symbol": "AAPL", "side": "sell", "type": "limit",
             "status": "new", "limit_price": take},
        ]
    return [order]


def test_the_resting_exits_are_patched_to_the_levels_the_fill_implies():
    """The money case: a long filled above expectation used to keep a stop measured from
    the price it never got, so the real risk per trade exceeded what was sized for."""
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket()))
    session.queue("PATCH", "/v2/orders/leg-stop", StubResponse(200, {"id": "leg-stop"}))
    session.queue("PATCH", "/v2/orders/leg-take", StubResponse(200, {"id": "leg-take"}))

    report = _executor_on(session).amend_exits("AAPL", stop=98.0, take=106.0)

    patches = [r for r in session.requests if r["method"] == "PATCH"]
    assert [p["path"] for p in patches] == ["/v2/orders/leg-stop", "/v2/orders/leg-take"]
    assert [p["json"] for p in patches] == [{"stop_price": "98.00"}, {"limit_price": "106.00"}]
    assert len(report["amended"]) == 2 and report["failed"] == []


def test_an_amendment_the_broker_rejects_leaves_the_leg_working():
    """Alpaca keeps the original order when a replace is refused, which is why a failed
    amendment is reported and never escalated: the position still has its exit."""
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket()))
    session.queue("PATCH", "/v2/orders/leg-stop", StubResponse(422, {"message": "stop too close"}))
    session.queue("PATCH", "/v2/orders/leg-take", StubResponse(200, {"id": "leg-take"}))

    report = _executor_on(session).amend_exits("AAPL", stop=99.99, take=106.0)

    assert len(report["failed"]) == 1 and report["failed"][0]["leg"] == "stop"
    assert len(report["amended"]) == 1, "the leg that could move still moved"
    assert not [r for r in session.requests if r["method"] == "DELETE"], (
        "an amendment must never cancel the protection it is trying to improve"
    )


def test_an_exit_already_at_the_right_level_is_not_replaced():
    """A replace that changes nothing is churn, and Alpaca rejects it anyway. Nothing is
    queued for a PATCH, so the stub would fail the test if one were sent."""
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket(stop="96.00", take="104.00")))

    report = _executor_on(session).amend_exits("AAPL", stop=96.0, take=104.0)

    assert report["amended"] == []
    assert not [r for r in session.requests if r["method"] == "PATCH"]


def test_a_level_that_is_not_a_level_beside_the_fill_is_left_alone():
    """A level on top of the fill is a position that closes instantly at a loss; Alpaca
    refuses it, and naming it here says which number was wrong."""
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket()))

    report = _executor_on(session).amend_exits("AAPL", stop=100.0, take=104.0, reference_price=100.0)

    assert report["amended"] == []
    assert len(report["left"]) == 1 and "not a level" in report["left"][0]["reason"]


def test_a_bracket_parent_is_not_an_exit_leg():
    """The parent is the ENTRY. Replacing it would mean replacing a filled market order,
    which is not an amendment — it would be a second order nobody decided to place."""
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket(legs=False)))

    report = _executor_on(session).amend_exits("AAPL", stop=98.0, take=106.0)

    assert report["amended"] == [] and report["failed"] == []
    assert "no resting exit legs" in report["reason"]
    assert not [r for r in session.requests if r["method"] == "PATCH"]


def test_there_is_nothing_to_amend_without_levels():
    """No levels means no configured exits, so there is nothing to move — and no call is
    made to find that out."""
    session = StubSession()
    report = _executor_on(session).amend_exits("AAPL")
    assert report["amended"] == [] and "no levels" in report["reason"]
    assert session.requests == [], "an empty amendment must not touch the broker"


# ---------------------------------------------------------------------------
# adopting an exit the broker made (defect 1)
# ---------------------------------------------------------------------------
def test_the_closing_fill_reads_the_reason_off_the_broker_order():
    """The reason is a property of the order that filled, not a guess: a stop is a stop
    because Alpaca says the order that closed the position was a stop."""
    from src.execution.alpaca_broker import closing_fill_from_history

    def history(kind, *, side="sell", status="filled", price="97.25"):
        return [{"id": "leg-x", "symbol": "AAPL", "side": side, "type": kind,
                 "status": status, "filled_avg_price": price, "filled_qty": "10",
                 "filled_at": "2026-09-16T15:00:00Z"}]

    assert closing_fill_from_history(history("stop"), symbol="AAPL", short=False).reason == "stop"
    assert closing_fill_from_history(history("limit"), symbol="AAPL", short=False).reason == "take"
    assert closing_fill_from_history(history("market"), symbol="AAPL", short=False).reason == "forced", (
        "a market close is not the bracket: it is reported as forced rather than invented"
    )
    # A cancelled leg did not close anything, and the ENTRY is the wrong side.
    assert closing_fill_from_history(history("stop", status="canceled"), symbol="AAPL", short=False) is None
    assert closing_fill_from_history(history("stop", side="buy"), symbol="AAPL", short=False) is None
    # A short is closed by a BUY, so the same order means the opposite there.
    assert closing_fill_from_history(history("stop", side="buy"), symbol="AAPL", short=True).reason == "stop"
    assert closing_fill_from_history([], symbol="AAPL", short=False) is None


def test_the_closing_fill_is_found_among_a_brackets_legs():
    """The usual shape: the parent is the entry and the exits hang off ``legs``. The two
    legs arrive with the same timestamp in a nested list, so the newest is decided by the
    timestamp rather than by position."""
    from src.execution.alpaca_broker import closing_fill_from_history

    parent = {
        "id": "ord-1", "symbol": "AAPL", "side": "buy", "type": "market",
        "status": "filled", "order_class": "bracket", "filled_avg_price": "100.00",
        "legs": [
            {"id": "leg-stop", "symbol": "AAPL", "side": "sell", "type": "stop",
             "status": "filled", "filled_avg_price": "96.50", "filled_at": "2026-09-16T14:00:00Z"},
            {"id": "leg-take", "symbol": "AAPL", "side": "sell", "type": "limit",
             "status": "canceled", "filled_avg_price": None, "filled_at": "2026-09-16T15:00:00Z"},
        ],
    }

    closing = closing_fill_from_history([parent], symbol="AAPL", short=False)

    assert closing is not None
    assert closing.price == 96.50 and closing.reason == "stop"
    assert closing.order_id == "leg-stop"


def test_the_broker_answers_both_live_only_questions():
    """``AlpacaBroker`` implements the two methods the seam grew, through its executor."""
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(200, _open_bracket()))
    session.queue("PATCH", "/v2/orders/leg-stop", StubResponse(200, {"id": "leg-stop"}))
    session.queue("PATCH", "/v2/orders/leg-take", StubResponse(200, {"id": "leg-take"}))
    broker = AlpacaBroker(settings, executor=_executor_on(session, settings))

    resolved = broker.reprice_exits(98.0, 106.0)
    assert [a["leg"] for a in resolved["amended"]] == ["stop", "limit"]

    # ...and the closing fill, from the same history endpoint.
    history_session = StubSession()
    history_session.queue("GET", "/v2/orders", StubResponse(200, [
        {"id": "leg-stop", "symbol": "AAPL", "side": "sell", "type": "stop",
         "status": "filled", "filled_avg_price": "97.25", "filled_at": "2026-09-16T15:00:00Z"},
    ]))
    history_broker = AlpacaBroker(settings, executor=_executor_on(history_session, settings))
    closing = history_broker.closing_fill(short=False)
    assert closing is not None and closing.price == 97.25 and closing.reason == "stop"


def test_an_unreadable_order_history_is_not_an_exit():
    """Fail closed: if the history cannot be read, nothing is proved, so the driver keeps
    refusing rather than booking a price it made up."""
    settings = _settings()
    session = StubSession()
    session.queue("GET", "/v2/orders", StubResponse(500, {"message": "boom"}))
    broker = AlpacaBroker(settings, executor=_executor_on(session, settings))

    assert broker.closing_fill(short=False) is None
