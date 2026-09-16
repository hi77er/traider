"""The Alpaca Trading API, at the level of URLs and status codes.

Everything above this file talks about intents, fills and positions; this file talks
about `POST /v2/orders`. Keeping the split sharp means the order logic can be tested
against a stub that answers dicts, and the HTTP layer can be tested for the things
that are only true of HTTP: the auth headers, the timeout, what counts as a retryable
status, and where the request id lives when Alpaca refuses.

Three properties this module is responsible for, none of which any caller should
have to remember:

* **Authentication on every request.** Alpaca has no login and no token to refresh —
  the key pair rides on `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`, so there is no
  session state to get stale. The secret is never logged and never included in an
  error message; a refusal is described by the broker's own `code`/`message` and the
  `X-Request-ID` header, which is exactly what support asks for.
* **The truth about a failure.** `AlpacaError` carries the status code, whether it is
  worth retrying, and the broker's message. A caller that cannot tell "the key is
  revoked" from "the broker is busy" will retry the wrong one forever.
* **No surprises about shape.** A 404 on a position is not an error — it is "flat",
  the single most common answer a live bot asks for. It comes back as ``None``.

The session is injectable so tests can drive the whole executor without a socket and
without monkeypatching the requests module globally.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["AlpacaError", "AlpacaClient", "RETRYABLE_STATUS_CODES"]

# The broker is busy rather than disagreeing. Mirrors ``retry.RETRYABLE_STATUS_CODES``
# and is re-exported here so the classification lives beside the status codes it
# classifies.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

DEFAULT_TIMEOUT_SECONDS = 10.0


class AlpacaError(RuntimeError):
    """A request to Alpaca that did not produce a usable answer.

    ``retryable`` is decided here, where the status code is known, and read by
    ``execute_with_retry`` — never re-guessed at a call site.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retryable = retryable

    @property
    def rejected(self) -> bool:
        """The credential was refused. Retrying cannot fix this."""
        return self.status_code in (401, 403)


def _refusal_message(status_code: Optional[int], body: Any, request_id: Optional[str]) -> str:
    """Alpaca's own words, plus the id support will ask for. Never the credential."""
    detail = ""
    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message")
        if code or message:
            detail = f" [{code or ''}] {message or ''}".rstrip()
    elif body:
        detail = f" {str(body)[:200]}"
    requested = f" (X-Request-ID {request_id})" if request_id else ""
    if status_code is None:
        return f"Could not reach Alpaca{detail}"
    return f"Alpaca refused the request with {status_code}{detail}{requested}"


class AlpacaClient:
    """One environment's API. Constructed from an :class:`ExecutionTarget`."""

    def __init__(self, target, *, session=None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.target = target
        self.base_url = str(target.base_url).rstrip("/")
        self.key_id = target.key_id
        # repr=False on the dataclass already keeps this out of logs; this is the
        # only place it is read, and it goes straight into a header.
        self._secret = target.secret
        self.timeout = float(timeout)
        self._session = session
        self.calls: List[Dict[str, Any]] = []

    @property
    def label(self) -> str:
        """PAPER / LIVE — printed on every order line, because the portal is not."""
        return "LIVE" if getattr(self.target, "live", False) else "PAPER"

    @property
    def session(self):
        """The HTTP session, imported lazily so the module imports without requests."""
        if self._session is None:
            import requests  # noqa: PLC0415 - intentional: keeps import-time light

            self._session = requests.Session()
        return self._session

    # -- transport ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
    ) -> Any:
        """One authenticated call. Raises :class:`AlpacaError` on anything unusable.

        ``allow_404`` exists because "no position" is an answer, not a failure, and
        every live tick asks that question.
        """
        url = f"{self.base_url}{path}"
        attempt = {"method": method, "url": url, "params": params, "json": json}
        self.calls.append(attempt)
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json,
                headers=self.headers(),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure is "unknown"
            # Unknown, not refused: the request may have been received, which is why a
            # retried ORDER reuses its client_order_id.
            raise AlpacaError(
                f"Could not reach {self.base_url} ({exc})", retryable=True
            ) from exc

        request_id = None
        try:
            request_id = resp.headers.get("X-Request-ID")
        except Exception:  # noqa: BLE001 - a stub response may not carry headers
            request_id = None

        if resp.status_code == 404 and allow_404:
            return None
        if 200 <= resp.status_code < 300:
            if resp.status_code == 204 or not getattr(resp, "content", b""):
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise AlpacaError(
                    f"Alpaca answered {resp.status_code} with a body that is not JSON",
                    status_code=resp.status_code,
                    request_id=request_id,
                    retryable=True,
                ) from exc

        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = getattr(resp, "text", "") or ""

        retryable = resp.status_code in RETRYABLE_STATUS_CODES or resp.status_code >= 500
        raise AlpacaError(
            _refusal_message(resp.status_code, body, request_id),
            status_code=resp.status_code,
            code=(body or {}).get("code") if isinstance(body, dict) else None,
            request_id=request_id,
            retryable=retryable,
        )

    def headers(self) -> Dict[str, str]:
        """HTTP Basic by way of two headers — there is no token and no login call."""
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- account and market state -----------------------------------------
    def account(self) -> Dict[str, Any]:
        """Equity, buying power, status. The cheapest proof the key works."""
        return self.request("GET", "/v2/account") or {}

    def clock(self) -> Dict[str, Any]:
        """Is the market open, and when does it close? Used to skip a dead tick."""
        return self.request("GET", "/v2/clock") or {}

    # -- positions ---------------------------------------------------------
    def position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """The open position in ``symbol``, or ``None`` when flat."""
        return self.request("GET", f"/v2/positions/{symbol}", allow_404=True)

    def positions(self) -> List[Dict[str, Any]]:
        return list(self.request("GET", "/v2/positions") or [])

    def close_position(self, symbol: str, *, percentage: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Liquidate a position with one call.

        Alpaca builds the closing order itself, so there is no side to get wrong and no
        quantity to compute — the classic way to leave a residual position.
        """
        params = {"percentage": percentage} if percentage is not None else None
        return self.request(
            "DELETE", f"/v2/positions/{symbol}", params=params, allow_404=True
        )

    # -- orders ------------------------------------------------------------
    def submit_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/v2/orders", json=payload) or {}

    def order(self, order_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v2/orders/{order_id}") or {}

    def order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """Look an order up by OUR id — the safe way to retry a submit.

        A submit that timed out may well have been received, so the retry asks this
        first rather than sending a second order for the same intent.
        """
        return self.request(
            "GET", "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            allow_404=True,
        )

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"status": "open", "limit": 100}
        if symbol:
            params["symbols"] = symbol
        return list(self.request("GET", "/v2/orders", params=params) or [])

    def closed_orders(self, symbol: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Recently finished orders, newest first.

        This is how a live run finds out what a RESTING bracket exit actually filled at:
        by the time the strategy notices the position is gone, the order that closed it
        is history, and re-deriving the price locally would be a guess about the broker's
        behaviour rather than a record of it.
        """
        params: Dict[str, Any] = {
            "status": "closed", "limit": max(1, int(limit)), "direction": "desc",
        }
        if symbol:
            params["symbols"] = symbol
        return list(self.request("GET", "/v2/orders", params=params) or [])

    def replace_order(self, order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Amend a RESTING order in place (``PATCH /v2/orders/{order_id}``).

        Alpaca replaces ONE order per call and only these types: ``limit``, ``stop``,
        ``stop_limit``, ``trailing_stop``. A bracket's two exits are therefore two calls,
        and a replacement the broker rejects leaves the ORIGINAL order working — which is
        what makes this safe to attempt against a live position: the exit the strategy
        already has does not vanish while the amendment is being decided.

        Idempotent by nature (setting the same price twice is the same state), so unlike a
        submit this is safe to retry.
        """
        return self.request("PATCH", f"/v2/orders/{order_id}", json=payload) or {}

    def cancel_order(self, order_id: str) -> bool:
        """``True`` when it is now gone. A 404 is success: someone else cancelled it."""
        self.request("DELETE", f"/v2/orders/{order_id}", allow_404=True)
        return True
