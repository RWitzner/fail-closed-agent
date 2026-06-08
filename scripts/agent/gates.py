"""Identity-strict, fail-closed run gates (spec §4.7, §12).

A gate is open only when its value is exactly `True`. A truthy non-bool, a missing
key, or a typo all read False. Opening requires BOTH run gates
(`agent_rules.enabled` and `agent_rules.paper_trading.enabled`); live trading
requires `risk_rules.live_trading.enabled`. The committed config opens nothing.
"""


def strict_bool(value) -> bool:
    return value is True


def gate(config, *path) -> bool:
    node = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return strict_bool(node)


def opening_allowed(config) -> bool:
    return gate(config, "agent_rules", "enabled") and gate(
        config, "agent_rules", "paper_trading", "enabled"
    )


def live_allowed(config) -> bool:
    return gate(config, "risk_rules", "live_trading", "enabled")
