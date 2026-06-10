"""Alpaca paper broker — three-mode adapter (M5 contract §G; FD-M5-5/7/8/28).

Modes (keyword-only ctor; NEITHER argument set => the M0 spy/no-op, byte-identical
behavior — records to `self.submitted`, no network, no SDK — plus the wall-2
synthetic refusal below, unreachable by any M0 test):

- ``order_api=``            offline fixture-shaped adapter: the FULL lifecycle code
                            path (payload build, parse chokepoint upstream, polling,
                            cancel) with ZERO network — tests drive it via
                            `ScriptedOrderApi`.
- ``credentials_loader=``   `_build_real_client()`: the ONLY function body in the
                            codebase that may ``import alpaca`` (alpaca-py 0.43.4,
                            FD-M5-5) and call the loader. ``base_url`` is PINNED to
                            `_PAPER_HOST` — any other value raises ``ValueError`` at
                            construction, BEFORE the SDK import (this class is
                            structurally paper-only; `AlpacaLiveBroker` is a separate
                            M8 class behind `construct_live_broker`).

Frozen semantics:

- **Wall 2 (FD-M5-8/28)** — FIRST line of `_place` in ALL modes (spy included): any
  intent whose ``intent_id`` starts with ``"synthetic-"`` raises
  `SyntheticConfinementError`; the position-of-record seam itself refuses synthetic
  flow even against a forged-path token. `SyntheticConfinementError` is DEFINED HERE
  (the §G pinned home); `broker/fake.py` raises the SAME class via a lazy import
  inside its `_place` raise branch (its §3 module-scope import row stays intact).
- **Wire payload (A8 frozen shape)** — ``{"symbol", "qty": "<int-str>", "side",
  "type": "limit", "time_in_force": "day", "limit_price": "<Decimal-str>",
  "extended_hours": false, "client_order_id": "<order_id>"}``. There is NO
  market-order payload shape in the codebase (FD-M5-1 structural): ``limit_price is
  None`` yields a `BrokerHttpError`-shaped LOCAL rejection (``status_code=0``,
  ``message="unpriceable"``) BEFORE any wire attempt.
- **Networking discipline (order-api mode)** — every call site catches
  `BrokerHttpError` and returns it as DATA to the orchestrator (which journals
  `broker_reject`); ambiguous failures (timeouts) PROPAGATE — the FD-M5-17
  query-by-client_order_id recovery is the orchestrator's, never a blind resubmit.
- `cancel_order`/`order_status` take the ``client_order_id`` (= our deterministic
  ``order_id``, FD-M5-7) and return RAW dicts; cancel is risk-reducing-or-neutral
  and is NEVER token-gated (FD-M5-23).
- `positions()`/`account()` return RAW payloads, fed straight to the M4
  `parse_account_payload`/`parse_positions_payload` with ``source="alpaca_paper"``.
"""
from decimal import Decimal
from typing import Callable, Mapping, Optional

from agent.broker.base import BrokerBase, OrderIntent
from agent.broker.order_state import OrderApi
from agent.exec_reasons import ExecError

_PAPER_HOST = "https://paper-api.alpaca.markets"     # CODE CONSTANT (§0.2 A7)


class BrokerHttpError(Exception):
    """An HTTP-level broker rejection as an exception/data hybrid (FD-M5-17):
    raised by the wire seam, caught at every order-api-mode call site and
    returned as DATA. ``status_code == 0`` means LOCAL — no HTTP exchange
    happened (the FD-M5-1 ``"unpriceable"`` rejection)."""

    def __init__(self, status_code: int, code: Optional[int], message: str):
        super().__init__(f"broker http {status_code} code={code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class SyntheticConfinementError(ExecError):
    """Wall 2 (FD-M5-8): synthetic-namespaced flow refused at a broker seam.
    Defined HERE per contract §G; `FakeBroker` raises this SAME class (lazily
    imported) for its REV-2 reverse wall."""


def _wire_payload(intent: OrderIntent) -> dict:
    """The frozen §G/A8 wire dict. A ``limit_price=None`` intent is structurally
    unserializable (no market-order shape exists — FD-M5-1); callers reject it
    locally BEFORE reaching this builder."""
    if intent.limit_price is None:
        raise ExecError(
            "no market-order payload shape exists (FD-M5-1): limit_price required")
    if intent.qty != intent.qty.to_integral_value():
        raise ExecError("wire payload requires whole-share qty (S2/A1)")
    return {
        "symbol": intent.symbol,
        "qty": str(int(intent.qty)),
        "side": intent.side,
        "type": "limit",
        "time_in_force": "day",
        "limit_price": str(intent.limit_price),
        "extended_hours": False,
        "client_order_id": intent.intent_id,   # = our deterministic order_id (FD-M5-7)
    }


class AlpacaPaperBroker(BrokerBase):
    kind = "alpaca_paper"

    def __init__(self, *, order_api: Optional[OrderApi] = None,
                 credentials_loader: Optional[Callable[[], Mapping]] = None) -> None:
        # M0 spy state — preserved byte-identically for the default mode.
        self.submitted = []
        self._positions = {}
        if order_api is not None and credentials_loader is not None:
            raise ValueError(
                "AlpacaPaperBroker takes order_api OR credentials_loader, not both "
                "(the three modes are exclusive — contract §G)")
        if order_api is None and credentials_loader is not None:
            order_api = self._build_real_client(credentials_loader)
        self._order_api = order_api

    # ------------------------------------------------------------------ place

    def _place(self, intent: OrderIntent):
        # Wall 2 (FD-M5-8/28): FIRST LINE, ALL modes — before any record, any
        # pricing check, any wire attempt.
        if intent.intent_id.startswith("synthetic-"):
            raise SyntheticConfinementError(
                "synthetic-namespaced intent refused at the position-of-record "
                "seam (wall 2, FD-M5-8)")
        if self._order_api is None:
            # M0 spy/no-op mode: byte-identical behavior.
            self.submitted.append(intent)
            return {"order_id": intent.intent_id, "status": "accepted_paper_stub"}
        if intent.limit_price is None:
            # FD-M5-1 structural: unserializable -> local BrokerHttpError-shaped
            # rejection as DATA, BEFORE any wire attempt.
            return BrokerHttpError(status_code=0, code=None, message="unpriceable")
        payload = _wire_payload(intent)
        try:
            return self._order_api.submit(payload)
        except BrokerHttpError as err:
            return err          # DATA: the orchestrator journals broker_reject

    # ------------------------------------------------- tokenless order surface

    def cancel_order(self, order_id: str):
        """Best-effort cancel by client_order_id: resolve the broker id, then
        DELETE. NO token, NO mint — cancel is never gated (FD-M5-23)."""
        if self._order_api is None:
            return super().cancel_order(order_id)   # spy mode: NotImplementedError
        try:
            current = self._order_api.get_by_client_order_id(order_id)
            broker_order_id = (current.get("id")
                               if isinstance(current, Mapping) else None)
            if not isinstance(broker_order_id, str) or not broker_order_id:
                return current   # raw answer; the §F chokepoint classifies upstream
            return self._order_api.cancel(broker_order_id)
        except BrokerHttpError as err:
            return err

    def order_status(self, order_id: str):
        if self._order_api is None:
            return super().order_status(order_id)
        try:
            return self._order_api.get_by_client_order_id(order_id)
        except BrokerHttpError as err:
            return err

    # ----------------------------------------------------------- read surface

    def positions(self):
        if self._order_api is None:
            return dict(self._positions)            # M0 byte-identical
        try:
            return self._order_api.list_positions()
        except BrokerHttpError as err:
            return err

    def account(self):
        if self._order_api is None:
            return {"equity": Decimal("0"), "buying_power": Decimal("0")}
        try:
            return self._order_api.get_account()
        except BrokerHttpError as err:
            return err

    # ----------------------------------------------------------- real client

    def _build_real_client(self, credentials_loader: Callable[[], Mapping]):
        """Build the real SDK-backed OrderApi. The ONLY function body in the
        codebase that may import the alpaca SDK (FD-M5-5); never reached
        offline. The host pin is validated BEFORE the import, so a bad loader
        fails loudly with no SDK present. SDK models surface as RAW dicts
        (``raw_data=True``) so the SAME §F parser chokepoint runs on fixture
        and live payloads alike (verified at M5-2a)."""
        creds = credentials_loader()
        base_url = creds.get("base_url") if isinstance(creds, Mapping) else None
        if base_url != _PAPER_HOST:
            raise ValueError(
                "AlpacaPaperBroker is structurally paper-only: credentials must "
                f"pin base_url == {_PAPER_HOST!r} (got {base_url!r})")

        from alpaca.common.exceptions import APIError
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

        client = TradingClient(api_key=creds["key_id"], secret_key=creds["secret_key"],
                               paper=True, raw_data=True)

        def _wire(call):
            try:
                return call()
            except APIError as err:   # SDK HTTP error -> the ONE adapter error type
                raise BrokerHttpError(
                    status_code=int(getattr(err, "status_code", 0) or 0),
                    code=getattr(err, "code", None),
                    message=str(err)) from err

        class _RealOrderApi:
            """SDK -> raw-dict OrderApi (the §F seam; live-verified at M5-2a)."""

            def submit(self, payload):
                request = LimitOrderRequest(
                    symbol=payload["symbol"], qty=int(payload["qty"]),
                    side=OrderSide(payload["side"]),
                    time_in_force=TimeInForce(payload["time_in_force"]),
                    limit_price=payload["limit_price"],
                    extended_hours=payload["extended_hours"],
                    client_order_id=payload["client_order_id"])
                return _wire(lambda: client.submit_order(order_data=request))

            def get_by_client_order_id(self, client_order_id):
                return _wire(lambda: client.get_order_by_client_id(client_order_id))

            def cancel(self, broker_order_id):
                _wire(lambda: client.cancel_order_by_id(broker_order_id))
                # DELETE returns 204 no-content; cancel is ASYNC (A10) — surface
                # the pending state as a raw dict for the §F chokepoint.
                return {"id": broker_order_id, "status": "pending_cancel"}

            def get_account(self):
                return _wire(lambda: client.get_account())

            def list_positions(self):
                return _wire(lambda: client.get_all_positions())

            def list_open_orders(self):
                return _wire(lambda: client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.OPEN)))

        return _RealOrderApi()


class AlpacaAccountProvider:
    """The M4 `AccountReadProvider` adapter (LD8 near-passthrough): RAW payloads
    out; parsing stays at the M4 §B chokepoint with ``source="alpaca_paper"``."""

    def __init__(self, api: OrderApi) -> None:
        self._api = api

    def account_payload(self) -> Mapping:
        return self._api.get_account()           # raw GET /v2/account (M4 §B names)

    def positions_payload(self) -> list:
        return self._api.list_positions()        # raw GET /v2/positions
