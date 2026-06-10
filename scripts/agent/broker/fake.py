"""M5 §H.1 — `FakeBroker`: the S9 actuator (offline, deterministic, rev 2).

A deterministic Alpaca-shaped lifecycle with NO randomness (A5's random partials
made scripted): a marketable buy (``limit >= ask``) fills at the ASK, sells
mirror at the BID; ``immediate_full`` fills the full qty at submit;
``partial_then_full`` fills a scripted 30%-then-remainder over the next two
`order_status` polls; ``never_fill`` rests ``accepted`` until canceled;
``reject_all`` returns ``status:"rejected"`` payloads. A symbol absent from
``instrument_ids`` — or a quote lookup returning None / an unusable touch —
rests ``accepted`` with never-fill semantics: NEVER a synthesized price
(M5C-B3). Emits Alpaca-wire-shaped order dicts (§G key forms: int-string qty,
Decimal-string prices) and M4-§L-shaped ``account()``/``positions()`` payloads
(Decimal-string money) so the M4 parsers and M5 ledgers run IDENTICAL code
paths. Extends `BrokerBase`, so even the FakeBroker is token-gated (S1 holds
inside the synthetic E2E).

**REV-2 reverse wall (FD-M5-8 — the M5C-1/M5C-S1 blocker fix):** `_place`
raises `SyntheticConfinementError` iff ``intent.is_reducing is not True`` AND
``not intent.intent_id.startswith("synthetic-")`` — a real strategy can never
OPEN against the fake, but **reductions are NEVER namespace-gated**: the M0
kill actuator's ``flatten-<symbol>`` intents pass unimpeded (§0 asymmetry,
FD-M4-3).

Import-discipline note (§3, documented resolution): module scope imports
`agent.broker.base` + stdlib per the §3 row, PLUS `agent.exec_reasons` (the
stdlib-only vocabulary home — `FILL_POLICIES` validation cannot live anywhere
else without duplicating a frozen vocabulary; exec_reasons is already an
implicit dependency via `broker.order_state`). `SyntheticConfinementError`
stays defined in `agent.broker.alpaca` (the §G pinned home) and is imported
LAZILY inside the `_place` raise branch — one class, no module-scope coupling,
and `agent.broker.alpaca` never enters ``sys.modules`` on a healthy synthetic
run.
"""
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from agent.broker.base import BrokerBase, OrderIntent
from agent.exec_reasons import ExecError, FILL_POLICIES, require_member

_ZERO = Decimal("0")
_THIRTY_PCT = Decimal("0.3")


def _usable(price) -> bool:
    """A touch is usable iff present, Decimal, finite, > 0 (order_pricing mirror)."""
    return isinstance(price, Decimal) and price.is_finite() and price > 0


def _qty_str(value: Decimal) -> str:
    """§G int-string form for whole-share quantities (Decimal-string otherwise)."""
    if value == value.to_integral_value():
        return str(int(value))
    return str(value)


class FakeBroker(BrokerBase):
    kind = "fake"

    def __init__(self, *, quote_view, clock, instrument_ids: Mapping[str, int],
                 starting_cash: Decimal = Decimal("100000"),
                 fill_policy: str = "immediate_full") -> None:
        require_member(FILL_POLICIES, fill_policy, what="fill_policy")
        if (isinstance(starting_cash, float)
                or not isinstance(starting_cash, Decimal)
                or not starting_cash.is_finite() or starting_cash < 0):
            raise ExecError("starting_cash must be a non-negative finite Decimal (S2)")
        self._quote_view = quote_view
        self._clock = clock
        self._instrument_ids = dict(instrument_ids)
        self._starting_cash = starting_cash
        self._cash = starting_cash
        self._fill_policy = fill_policy
        self._orders = {}     # client_order_id -> mutable lifecycle record
        self._book = {}       # symbol -> {"qty": Decimal, "cost": Decimal}
        self._seq = 0

    # ------------------------------------------------------------------ place

    def _place(self, intent: OrderIntent) -> Mapping:
        # REV-2 reverse wall (FD-M5-8): FIRST LINE. Refuse iff NOT reducing AND
        # not synthetic-namespaced — reductions are NEVER namespace-gated.
        if (intent.is_reducing is not True
                and not intent.intent_id.startswith("synthetic-")):
            # ONE class, defined at the §G home; lazy so module scope stays per §3.
            from agent.broker.alpaca import SyntheticConfinementError
            raise SyntheticConfinementError(
                "FakeBroker refuses non-synthetic opening intents "
                "(reverse wall, FD-M5-8 rev 2)")

        self._seq += 1
        order = {
            "broker_id": f"fake-{self._seq:04d}",
            "client_order_id": intent.intent_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "qty": intent.qty,
            "limit": intent.limit_price,
            "filled_qty": _ZERO,
            "avg": None,            # deterministic single-price fills: avg == touch
            "status": "new",        # raw Alpaca string -> maps "accepted" (§F)
            "pending": (),          # (qty, price) slices applied on future polls
            "terminal": False,
        }
        self._orders[intent.intent_id] = order

        if self._fill_policy == "reject_all":
            order["status"] = "rejected"
            order["terminal"] = True
            return self._payload(order)

        touch = self._touch(intent)
        marketable = (
            touch is not None and intent.limit_price is not None
            and (intent.limit_price >= touch if intent.side == "buy"
                 else intent.limit_price <= touch))   # boundary-equal IS marketable
        if not marketable or self._fill_policy == "never_fill":
            # Rests accepted; unmapped/unquotable orders NEVER synthesize a price.
            return self._payload(order)

        if self._fill_policy == "immediate_full":
            self._apply_fill(order, intent.qty, touch)
            order["status"] = "filled"
            order["terminal"] = True
            return self._payload(order)

        # partial_then_full: scripted 30%-then-remainder over the next two polls
        # (A5's random partials made deterministic).
        first = (intent.qty * _THIRTY_PCT).to_integral_value(rounding=ROUND_FLOOR)
        if first < 1 or first >= intent.qty:
            order["pending"] = ((intent.qty, touch),)   # degenerate tiny order
        else:
            order["pending"] = ((first, touch), (intent.qty - first, touch))
        return self._payload(order)

    # ------------------------------------------------- tokenless order surface

    def order_status(self, order_id: str) -> Mapping:
        """Poll-able: each call advances at most ONE pending scripted slice.
        An unknown client_order_id is a composition bug -> KeyError (loud)."""
        order = self._orders[order_id]
        if not order["terminal"] and order["pending"]:
            (delta_qty, price), rest = order["pending"][0], order["pending"][1:]
            order["pending"] = tuple(rest)
            self._apply_fill(order, delta_qty, price)
            if order["filled_qty"] == order["qty"]:
                order["status"] = "filled"
                order["terminal"] = True
            else:
                order["status"] = "partially_filled"
        return self._payload(order)

    def cancel_order(self, order_id: str) -> Mapping:
        """NO token (FD-M5-23). A non-terminal order cancels (keeping any fills);
        a terminal order returns its terminal payload (already_terminal upstream)."""
        order = self._orders[order_id]
        if not order["terminal"]:
            order["pending"] = ()
            order["status"] = "canceled"
            order["terminal"] = True
        return self._payload(order)

    # ----------------------------------------------------------- read surface

    def account(self) -> dict:
        """M4 §L wire shape, Decimal-string money — parses green through the
        REAL `parse_account_payload` (synthetic mode uses source='fixture')."""
        invested = _ZERO
        for entry in self._book.values():
            invested += entry["cost"]
        equity = self._cash + invested
        buying_power = self._cash if self._cash > 0 else _ZERO
        return {
            "id": "fake-account",
            "account_number": "FAKE000001",
            "status": "ACTIVE",
            "currency": "USD",
            "equity": str(equity),
            "last_equity": str(self._starting_cash),
            "cash": str(self._cash),
            "buying_power": str(buying_power),
            "maintenance_margin": "0",
            "initial_margin": "0",
            "multiplier": "1",
            "daytrading_buying_power": "0",
            "pattern_day_trader": False,
            "daytrade_count": 0,
            "shorting_enabled": False,
            "trading_blocked": False,
            "account_blocked": False,
        }

    def positions(self) -> list:
        """Alpaca-fixture-shaped rows; cost basis stands in for market value
        (deterministic — the fake never re-marks)."""
        rows = []
        for symbol in sorted(self._book):
            entry = self._book[symbol]
            qty = entry["qty"]
            avg_entry = entry["cost"] / qty if qty != 0 else None
            rows.append({
                "symbol": symbol,
                "qty": str(qty),
                "side": "long" if qty > 0 else "short",
                "avg_entry_price": None if avg_entry is None else str(avg_entry),
                "market_value": str(entry["cost"]),
                "cost_basis": str(entry["cost"]),
                "asset_class": "us_equity",
                "instrument_id": self._instrument_ids.get(symbol),
            })
        return rows

    # -------------------------------------------------------------- internals

    def _touch(self, intent: OrderIntent):
        """The fill price source: the OPPOSITE touch at submit time (buy->ask,
        sell->bid). Unmapped symbol / missing quote / unusable touch -> None."""
        instrument_id = self._instrument_ids.get(intent.symbol)
        if instrument_id is None:
            return None                            # unmapped -> rests (M5C-B3)
        quote = self._quote_view.latest(intent.symbol, instrument_id)
        if quote is None:
            return None
        touch = quote.ask if intent.side == "buy" else quote.bid
        return touch if _usable(touch) else None

    def _apply_fill(self, order, delta_qty: Decimal, price: Decimal) -> None:
        order["filled_qty"] = order["filled_qty"] + delta_qty
        order["avg"] = price       # all slices fill at ONE captured touch
        entry = self._book.setdefault(order["symbol"], {"qty": _ZERO, "cost": _ZERO})
        notional = delta_qty * price
        if order["side"] == "buy":
            entry["qty"] += delta_qty
            entry["cost"] += notional
            self._cash -= notional
        else:
            avg_entry = entry["cost"] / entry["qty"] if entry["qty"] > 0 else price
            entry["qty"] -= delta_qty
            entry["cost"] -= delta_qty * avg_entry
            self._cash += notional
        if entry["qty"] == 0:
            del self._book[order["symbol"]]        # flat is not held (LD-R4 mirror)

    def _payload(self, order) -> dict:
        """One §G/A4-shaped raw order dict (the §F chokepoint's input)."""
        return {
            "id": order["broker_id"],
            "client_order_id": order["client_order_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": _qty_str(order["qty"]),
            "filled_qty": _qty_str(order["filled_qty"]),
            "filled_avg_price": (None if order["avg"] is None
                                 else str(order["avg"])),
            "limit_price": (None if order["limit"] is None
                            else str(order["limit"])),
            "status": order["status"],
            "type": "limit",
            "time_in_force": "day",
            "extended_hours": False,
            "asset_class": "us_equity",
        }
