"""Kill switch: flatten-then-halt, reduce-only only (spec §5 Tier 5, invariant S8).

On trigger, the switch submits a closing / position-decreasing order for every
held position using a `ReduceOnlyPreflightToken`, then halts. It can never mint an
opening token, so flattening risk never violates "nothing opens" (S1). A frozen
halt (leaving open exposure) is the bug this prevents.
"""
from agent.broker.base import OrderIntent
from agent.execution_preflight import mint_reduce_only_token


class KillSwitch:
    def __init__(self):
        self.state = "active"  # active -> flattening -> halted
        self.flattened = []
        self.failed = []  # (symbol, reason) for positions that could not be reduced

    def trigger(self, broker, positions) -> None:
        """Flatten every held position with a reduce-only order, then ALWAYS halt.

        Each position is isolated: one that cannot be reduced is recorded as an
        alarm and the loop continues, and `finally` guarantees we reach `halted`
        rather than freezing in `flattening` with open exposure.
        """
        self.state = "flattening"
        self.failed = []
        try:
            for position in positions:
                try:
                    qty = position.qty
                    if qty == 0:
                        continue  # nothing to reduce
                    side = "sell" if qty > 0 else "buy"  # long -> sell, short -> buy to cover
                    intent = OrderIntent(
                        symbol=position.symbol,
                        side=side,
                        qty=abs(qty),
                        is_reducing=True,
                        intent_id=f"flatten-{position.symbol}",
                    )
                    token = mint_reduce_only_token(position, intent)  # reduce-only; never an open
                    broker.submit_order(intent, token)
                    self.flattened.append(position.symbol)
                except Exception as exc:  # noqa: BLE001 — isolate one position's failure
                    self.failed.append((getattr(position, "symbol", "?"), str(exc)))
        finally:
            self.state = "halted"

    def is_halted(self) -> bool:
        return self.state == "halted"

    def allows_opening(self) -> bool:
        return False  # a kill switch never authorizes an opening order
