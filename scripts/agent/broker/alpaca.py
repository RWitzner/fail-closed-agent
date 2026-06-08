"""M0 Alpaca paper broker — a spy/no-op stub (spec §5 Tier 6, invariant S1).

Extends `BrokerBase`, so `submit_order` is the non-bypassable preflight gateway and
this class only implements `_place`. It imports **no** `alpaca-py`, makes **no**
network calls, and records accepted submissions. The real REST/WS adapter and the
live broker land in M5/M8.
"""
from decimal import Decimal

from agent.broker.base import BrokerBase


class AlpacaPaperBroker(BrokerBase):
    def __init__(self):
        self.submitted = []
        self._positions = {}

    def _place(self, intent):
        self.submitted.append(intent)
        return {"order_id": intent.intent_id, "status": "accepted_paper_stub"}

    def positions(self):
        return dict(self._positions)

    def account(self):
        return {"equity": Decimal("0"), "buying_power": Decimal("0")}
