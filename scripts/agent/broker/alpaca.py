"""M0 Alpaca paper broker — a spy/no-op stub (spec §5 Tier 6, invariant S1).

This is intentionally a no-op: it imports **no** `alpaca-py`, makes **no** network
calls, and records accepted submissions. The real REST/WS adapter (and the live
broker) land in M5. `submit_order` is reachable only with a valid preflight token
(`require_token`), so on the committed config nothing can be opened.
"""
from decimal import Decimal

from agent.broker.base import require_token


class AlpacaPaperBroker:
    def __init__(self):
        self.submitted = []
        self._positions = {}

    def submit_order(self, intent, token):
        require_token(intent, token)
        self.submitted.append(intent)
        return {"order_id": intent.intent_id, "status": "accepted_paper_stub"}

    def positions(self):
        return dict(self._positions)

    def account(self):
        return {"equity": Decimal("0"), "buying_power": Decimal("0")}
