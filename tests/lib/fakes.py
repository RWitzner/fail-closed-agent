"""Deterministic test doubles (spec §9 testing strategy).

No network, no clocks, no randomness — everything is scripted/injected so the
suite is byte-stable and runs offline.
"""
from agent.broker.base import require_token
from agent.serializer import dumps


class FakeTransport:
    """A MarketDataTransport that replays a scripted list of messages."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def stream(self, symbols):
        for message in self._messages:
            yield message


class TransportDisconnected(Exception):
    """Raised by FlakyTransport.stream to signal a (recoverable) disconnect to the
    recorder loop. The recorder CATCHES this, increments reconnect_epoch, sleeps
    the injected backoff, and re-calls stream() (a fresh async generator from the
    next segment). It is NEVER a fatal/silent exit (M1 §F2)."""


class FlakyTransport:
    """A MarketDataTransport that replays scripted frames and injects faults via
    inline ``_control`` rows (M1 §F2). Mirrors ``FakeTransport`` structurally:
    ``async def stream`` + ``yield``. The PARSER never sees a ``_control`` row —
    ``FlakyTransport`` consumes/strips them.

    ``frames`` is an ordered list of dicts. Two kinds:
      - DATA frame: a normal vendor record dict (yielded as json bytes).
      - CONTROL frame: ``{'_control': <verb>, ...}``. Honored verbs (frozen):
          ``{'_control':'disconnect', 'after_seq': <int>}`` -> on reaching it,
              RAISE ``TransportDisconnected`` (the recorder bumps reconnect_epoch
              and may alert).
          ``{'_control':'reconnect'}`` -> marks the start of the next live segment
              (the next ``stream()`` call resumes here).

    ``control_aware=True`` (default) honors ``_control`` verbs; ``control_aware=
    False`` yields data frames only (ignoring control rows, like FakeTransport).

    ``stream`` is STATEFUL across calls: it yields data frames until a
    ``'disconnect'`` control row, then raises ``TransportDisconnected``; the next
    ``stream()`` call resumes AFTER the matching ``'reconnect'`` row (or after the
    disconnect row if none), so the recorder's reconnect loop advances through
    segments deterministically.
    """

    def __init__(self, frames, *, control_aware: bool = True):
        self._frames = list(frames)
        self._control_aware = control_aware
        self._pos = 0  # cursor across stream() calls (stateful)

    @staticmethod
    def _is_control(frame) -> bool:
        return isinstance(frame, dict) and "_control" in frame

    @staticmethod
    def _encode(frame) -> bytes:
        return dumps(frame).encode("utf-8")

    async def stream(self, symbols):
        while self._pos < len(self._frames):
            frame = self._frames[self._pos]
            self._pos += 1
            if self._is_control(frame):
                if not self._control_aware:
                    continue  # ignore control rows; treat file as plain script
                verb = frame["_control"]
                if verb == "disconnect":
                    raise TransportDisconnected(frame)
                if verb == "reconnect":
                    # start of the next live segment; resume yielding from here
                    continue
                raise ValueError(f"unknown _control verb: {verb!r}")
            yield self._encode(frame)


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
    """Records every `submit_order` attempt at entry (for the S1 canary).

    M5 growth (contract §3 table, §E Broker Protocol): `kind="spy"` plus
    `cancel_order`/`order_status` recorders mirroring the entry-recording
    semantics — record the client_order_id at entry, then return a raw (empty)
    payload dict. `self.calls` keeps its M0 meaning: SUBMIT attempts only
    (the S1 canary asserts `calls == []` for zero submits of any kind);
    cancel/status queries land in their own lists.
    """

    kind = "spy"  # ∈ exec_reasons.BROKER_KINDS

    def __init__(self):
        self.calls = []  # every submit attempt, before token validation
        self.submitted = []  # accepted submissions
        self.cancel_calls = []  # every cancel_order(client_order_id), at entry
        self.status_calls = []  # every order_status(client_order_id), at entry

    def submit_order(self, intent, token):
        self.calls.append(intent)
        require_token(intent, token)
        self.submitted.append(intent)
        return {"order_id": getattr(intent, "intent_id", ""), "status": "accepted_spy"}

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {}

    def order_status(self, order_id):
        self.status_calls.append(order_id)
        return {}

    def positions(self):
        return {}

    def account(self):
        return {}
