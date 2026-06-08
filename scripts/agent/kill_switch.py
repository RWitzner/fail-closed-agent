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

    def trigger(self, broker, positions) -> None:
        self.state = "flattening"
        for position in positions:
            intent = OrderIntent(
                symbol=position.symbol,
                side="sell",  # long positions reduce by selling (short path: M4+)
                qty=position.qty,
                is_reducing=True,
                intent_id=f"flatten-{position.symbol}",
            )
            token = mint_reduce_only_token(position, intent)  # reduce-only; never an open
            broker.submit_order(intent, token)
            self.flattened.append(position.symbol)
        self.state = "halted"

    def is_halted(self) -> bool:
        return self.state == "halted"

    def allows_opening(self) -> bool:
        return False  # a kill switch never authorizes an opening order
