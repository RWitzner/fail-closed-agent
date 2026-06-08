"""Two-key arming seam for live trading (spec §12).

Live capital requires BOTH keys, independently:
- Key A: a committed, git-visible, code-reviewable config flag
  (`risk_rules.live_trading.enabled` is identity-`True`).
- Key B: a runtime credential/token (in `.secrets/` or operator env) that is
  never committed.

No single commit or process can supply both. `construct_live_broker` enforces the
seam; the live broker itself is built in M8.
"""
from agent.gates import live_allowed


class ArmingError(Exception):
    """An attempt to go live without both arming keys present."""


def two_key_armed(config, runtime_secret) -> bool:
    key_a = live_allowed(config)  # committed flag, identity-strict
    key_b = bool(runtime_secret)  # runtime secret, never committed
    return key_a and key_b


def construct_live_broker(config, runtime_secret):
    if not two_key_armed(config, runtime_secret):
        raise ArmingError(
            "live requires BOTH key A (committed config flag) and key B (runtime secret)"
        )
    raise NotImplementedError("AlpacaLiveBroker lands in M8")
