"""M5 §Q — Alpaca payload builders + `ScriptedOrderApi` (offline wire doubles).

Builders load the COMMITTED `tests/fixtures/alpaca/*.json` files and apply
keyword overrides, so every test payload stays anchored to the documented REST
field names (§0.2 A4/A8). `ScriptedOrderApi` implements the §F `OrderApi`
Protocol over per-method response queues: a step that is an Exception instance
is RAISED (BrokerHttpError fixtures, `BrokerTimeout` injection — FD-M5-17),
anything else is returned RAW. No network, no randomness, byte-stable.

`BrokerTimeout` is defined HERE (§Q): it models a lost submit/poll response —
an AMBIGUOUS outcome the adapter must NEVER swallow (only `BrokerHttpError` is
returned as data); the orchestrator owns the FD-M5-17 query-by-client_order_id
recovery, driven by the `submit_then_found_script` / `submit_then_not_found_script`
helpers below.
"""
import json
from pathlib import Path

from agent.broker.alpaca import BrokerHttpError

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"


class BrokerTimeout(Exception):
    """Injected wire timeout: the response is LOST, the outcome is ambiguous.
    The adapter lets it propagate (never blind-resubmit, FD-M5-17)."""


def load_fixture(name: str):
    """Raw committed fixture by stem name (e.g. 'order_accepted')."""
    return json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def order_payload(**overrides) -> dict:
    """`order_accepted.json` (status 'new') with keyword overrides."""
    payload = load_fixture("order_accepted")
    payload.update(overrides)
    return payload


def order_canceled_payload(**overrides) -> dict:
    payload = load_fixture("order_canceled")
    payload.update(overrides)
    return payload


def order_pending_cancel_payload(**overrides) -> dict:
    payload = load_fixture("order_pending_cancel")
    payload.update(overrides)
    return payload


def order_unknown_status_payload(**overrides) -> dict:
    payload = load_fixture("order_unknown_status")
    payload.update(overrides)
    return payload


def order_fill_sequence() -> list:
    """The FD-M5-18 cumulative-aggregate avg-drift sequence (4 snapshots)."""
    return load_fixture("order_fill_sequence")


def account_payload(**overrides) -> dict:
    payload = load_fixture("account_paper")
    payload.update(overrides)
    return payload


def positions_payload() -> list:
    return load_fixture("positions_paper")


def rejection_fixture(name: str) -> dict:
    """Raw committed HTTP-rejection shape {'status_code','code','message'}."""
    return load_fixture(name)


def http_error(name: str) -> BrokerHttpError:
    """A raisable `BrokerHttpError` built from a committed rejection fixture
    ('order_rejected_subpenny' | 'order_rejected_insufficient_bp' |
    'order_rejected_pdt')."""
    return BrokerHttpError(**rejection_fixture(name))


def not_found_error() -> BrokerHttpError:
    """404 by-client-order-id miss (the FD-M5-17 'not found' recovery branch)."""
    return BrokerHttpError(status_code=404, code=40410000, message="order not found")


def submit_then_found_script(*, client_order_id: str, **overrides) -> dict:
    """FD-M5-17 recovery: submit response LOST, the later by-client-order-id
    query FINDS the order (adopt, never blind-resubmit)."""
    found = order_payload(client_order_id=client_order_id, **overrides)
    return {
        "submit": [BrokerTimeout("submit response lost (injected)")],
        "get_by_client_order_id": [found],
    }


def submit_then_not_found_script(*, attempts: int = 3) -> dict:
    """FD-M5-17 recovery: submit response LOST and the order is NOT found after
    `attempts` queries (order_submit_unconfirmed + open-deny is the caller's)."""
    return {
        "submit": [BrokerTimeout("submit response lost (injected)")],
        "get_by_client_order_id": [not_found_error() for _ in range(attempts)],
    }


class ScriptedOrderApi:
    """A scripted §F `OrderApi`: RAW wire dicts in/out, per-method FIFO queues.

    `script` maps method name -> list of steps; a step is:
      - an Exception instance  -> RAISED (BrokerHttpError / BrokerTimeout injection)
      - a callable             -> called with the request args, result returned
      - anything else          -> returned as the raw response
    An unscripted/exhausted method RAISES AssertionError (a test must script every
    wire interaction it expects — and gets a loud failure on any it does not).

    Recorders: `calls` = every (method, args) at entry, in order; plus per-method
    convenience lists (`submit_calls`, `get_calls`, `cancel_calls`) so "zero submit
    calls" assertions are direct.
    """

    def __init__(self, script=None):
        self.script = {method: list(steps) for method, steps in (script or {}).items()}
        self.calls = []
        self.submit_calls = []
        self.get_calls = []
        self.cancel_calls = []

    def _step(self, method, *args):
        self.calls.append((method, args))
        queue = self.script.get(method)
        if not queue:
            raise AssertionError(f"unscripted OrderApi call: {method}({args!r})")
        step = queue.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(*args)
        return step

    def submit(self, payload):
        self.submit_calls.append(payload)
        return self._step("submit", payload)

    def get_by_client_order_id(self, client_order_id):
        self.get_calls.append(client_order_id)
        return self._step("get_by_client_order_id", client_order_id)

    def cancel(self, broker_order_id):
        self.cancel_calls.append(broker_order_id)
        return self._step("cancel", broker_order_id)

    def get_account(self):
        return self._step("get_account")

    def list_positions(self):
        return self._step("list_positions")

    def list_open_orders(self):
        return self._step("list_open_orders")
