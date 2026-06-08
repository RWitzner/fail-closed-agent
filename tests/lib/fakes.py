"""Deterministic test doubles (spec §9 testing strategy).

No network, no clocks, no randomness — everything is scripted/injected so the
suite is byte-stable and runs offline.
"""
from agent.broker.base import require_token


class FakeTransport:
    """A MarketDataTransport that replays a scripted list of messages."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def stream(self, symbols):
        for message in self._messages:
            yield message


class FakeClock:
    """Injected monotonic clock for latency/freshness tests."""

    def __init__(self, start_ms=0):
        self._ms = int(start_ms)

    def now_ms(self) -> int:
        return self._ms

    def advance(self, ms) -> int:
        self._ms += int(ms)
        return self._ms


class SpyBroker:
    """Records every `submit_order` attempt at entry (for the S1 canary)."""

    def __init__(self):
        self.calls = []  # every attempt, before token validation
        self.submitted = []  # accepted submissions

    def submit_order(self, intent, token):
        self.calls.append(intent)
        require_token(intent, token)
        self.submitted.append(intent)
        return {"order_id": getattr(intent, "intent_id", ""), "status": "accepted_spy"}

    def positions(self):
        return {}

    def account(self):
        return {}
