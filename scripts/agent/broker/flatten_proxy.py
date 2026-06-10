"""M5 §H.2 — `PriceCappedFlattenBroker`: the FD-M5-1 actuator (rev 2).

Deliberately NOT a `BrokerBase` and NOT a `Broker`-Protocol member (M5C-S7): a
submit-only shim handed EXCLUSIVELY to `RiskKillSwitch.trigger`/`retry_residual`,
which call ``submit_order``/``positions``/``account`` only — it implements no
``cancel_order``/``order_status`` and carries NO ``kind`` attribute
(`BROKER_KINDS` stays ``{spy, fake, alpaca_paper, alpaca_live}``; the proxy never
appears as a journaled fill source).

`kill_switch.py:34-40` builds reduce intents with ``limit_price=None`` (and stays
unedited — FD-M4-4). This proxy adds a PRICE, never a gate:

- unmapped symbol            -> ``FlattenUnpriced("no_price_for_cap")`` (M5C-S8:
                                resolves like an unpriceable quote — never a gate)
- ``quote_view.latest`` None -> ``FlattenUnpriced("no_price_for_cap")``
- ``reduce_cap(...)`` None   -> ``FlattenUnpriced("no_price_for_cap")``
- else                       -> rebuild the intent with ``limit_price=cap`` and
                                forward it WITH THE SAME TOKEN — the reduce
                                authorization binds symbol/side/qty only, so the
                                token stays valid and the inner broker consumes it.

Staleness is ACCEPTED: a stale quote still prices (staleness never blocks a
reduce — FD-M4-3). A raised `FlattenUnpriced` is isolated per-position by the M0
kill loop into ``failed[]`` -> ``residual`` -> ``retry_residual`` once quotable;
NEVER a bare market order (FD-M5-1). The exception's message string IS the
journaled reason. ``cap_bps`` is injected by the orchestrator (FLATTEN_CAP_BPS —
M5C-B6 keeps this module free of `execution_config`).
"""
from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from agent.broker.base import OrderIntent
from agent.exec_reasons import ExecError
from agent.order_pricing import reduce_cap


class FlattenUnpriced(Exception):
    """An unpriceable flatten attempt; the message string IS the journaled
    reason (always ``"no_price_for_cap"`` in M5)."""


class PriceCappedFlattenBroker:
    def __init__(self, *, inner, quote_view,
                 instrument_ids: Mapping[str, int],   # symbol -> instrument_id,
                 # injected by the orchestrator from universe ∪ held positions
                 # (M5C-S8: QuoteView.latest needs both; kill intents carry symbol only)
                 cap_bps: Decimal) -> None:
        if (isinstance(cap_bps, float) or not isinstance(cap_bps, Decimal)
                or not cap_bps.is_finite()):
            raise ExecError("cap_bps must be a finite Decimal (S2)")
        self._inner = inner
        self._quote_view = quote_view
        self._instrument_ids = dict(instrument_ids)
        self._cap_bps = cap_bps

    def submit_order(self, intent: OrderIntent, token):
        instrument_id = self._instrument_ids.get(intent.symbol)
        if instrument_id is None:
            raise FlattenUnpriced("no_price_for_cap")   # M5C-S8: never a gate
        quote = self._quote_view.latest(intent.symbol, instrument_id)  # STALE ACCEPTED
        if quote is None:
            raise FlattenUnpriced("no_price_for_cap")
        cap = reduce_cap(side=intent.side, quote=quote, cap_bps=self._cap_bps)
        if cap is None:
            raise FlattenUnpriced("no_price_for_cap")
        # The reduce authorization binds symbol/side/qty ONLY, so the SAME token
        # stays valid; the inner broker's submit_order consumes it.
        priced = replace(intent, limit_price=cap)
        return self._inner.submit_order(priced, token)

    def positions(self):
        return self._inner.positions()

    def account(self):
        return self._inner.account()
