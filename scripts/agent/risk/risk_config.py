"""M4 §C — RiskConfig: the ONE parser of the risk_rules additions (SignalConfig posture,
signal_config.py:76-115 — closed key sets, fail-loud ValueError at startup, parsed once).

Posture (FD-M4-6/21): the 7 `caps.*` values are committed integer whole-USD JSON numbers
at 0 (tighten-only `min()`-merge native — overlays lower, never raise); safety mechanics
are CODE CONSTANTS; `risk.universe` is strings/dicts so `tighten_only_merge` keeps base
(overlays cannot tamper); `INTRADAY_MARGIN_BUFFER_USD` is a code constant because its
polarity is INVERTED under min()-merge (raising it is a code change).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Tuple

from agent.config import rules_hash

INTRADAY_MARGIN_BUFFER_USD = Decimal("0")   # CODE CONSTANT (FD-M4-6: inverted polarity —
                                            # min()-merge would loosen it; raising = code change)
SHORTS_SUPPORTED = False                    # CODE CONSTANT (FD-M4-1): ANDed with config;
                                            # no overlay/commit can enable shorts before locate exists

_CAP_KEYS: Tuple[str, ...] = (
    "max_position_usd",
    "max_gross_exposure_usd",
    "max_net_exposure_usd",
    "max_daily_loss_usd",
    "max_drawdown_usd",
    "max_sector_exposure_usd",
    "max_abs_beta_notional_usd",
)
_RISK_KEYS = frozenset({"short_selling", "universe"})
_UNIVERSE_ENTRY_KEYS = frozenset({"sector", "beta"})


@dataclass(frozen=True)
class SymbolRisk:
    sector: str        # non-empty slug
    beta: Decimal      # finite, parsed from the committed Decimal-string


@dataclass(frozen=True)
class RiskConfig:
    max_position_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_net_exposure_usd: Decimal
    max_daily_loss_usd: Decimal
    max_drawdown_usd: Decimal
    max_sector_exposure_usd: Decimal
    max_abs_beta_notional_usd: Decimal
    short_selling_enabled: bool            # identity-strict; committed false
    universe: Mapping[str, SymbolRisk]     # FD-M4-21; committed {}
    rules_hash: str                        # of the WHOLE assembled config (config.py:17)

    @classmethod
    def from_config(cls, config: dict) -> "RiskConfig":
        """`config` = the assembled {"agent_rules": ..., "risk_rules": ...} dict.
        Caps must be JSON ints >= 0 (bool excluded); unknown/missing keys in
        risk_rules.caps / risk_rules.risk -> ValueError; universe entries must be
        exactly {"sector": <non-empty str>, "beta": <finite Decimal-string>}."""
        if not isinstance(config, dict):
            raise ValueError("config must be a dict")
        for top in ("agent_rules", "risk_rules"):
            if top not in config or not isinstance(config[top], dict):
                raise ValueError(f"config has no {top!r} dict (assembled pair required)")
        risk_rules = config["risk_rules"]

        caps = risk_rules.get("caps")
        if not isinstance(caps, dict):
            raise ValueError("risk_rules.caps: missing or not a dict")
        unknown = set(caps) - set(_CAP_KEYS)
        if unknown:
            raise ValueError(f"risk_rules.caps: unknown keys {sorted(unknown)}")
        missing = set(_CAP_KEYS) - set(caps)
        if missing:
            raise ValueError(f"risk_rules.caps: missing keys {sorted(missing)}")
        parsed_caps = {}
        for name in _CAP_KEYS:
            value = caps[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"risk_rules.caps.{name}: must be a JSON integer (whole USD), "
                    f"got {type(value).__name__}={value!r}")
            if value < 0:
                raise ValueError(f"risk_rules.caps.{name}: must be >= 0, got {value}")
            parsed_caps[name] = Decimal(value)

        risk = risk_rules.get("risk")
        if not isinstance(risk, dict):
            raise ValueError("risk_rules.risk: missing or not a dict")
        unknown = set(risk) - _RISK_KEYS
        if unknown:
            raise ValueError(f"risk_rules.risk: unknown keys {sorted(unknown)}")
        missing = _RISK_KEYS - set(risk)
        if missing:
            raise ValueError(f"risk_rules.risk: missing keys {sorted(missing)}")

        short_selling = risk["short_selling"]
        if not isinstance(short_selling, dict) or set(short_selling) != {"enabled"}:
            raise ValueError(
                "risk_rules.risk.short_selling: must be exactly {'enabled': <bool>}")
        enabled = short_selling["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(
                "risk_rules.risk.short_selling.enabled: must be a bool, "
                f"got {type(enabled).__name__}")

        raw_universe = risk["universe"]
        if not isinstance(raw_universe, dict):
            raise ValueError("risk_rules.risk.universe: must be a dict")
        universe = {}
        for symbol, entry in raw_universe.items():
            path = f"risk_rules.risk.universe.{symbol}"
            if not isinstance(symbol, str) or not symbol:
                raise ValueError(f"{path}: symbol must be a non-empty string")
            if not isinstance(entry, dict) or set(entry) != _UNIVERSE_ENTRY_KEYS:
                raise ValueError(
                    f"{path}: entry must be exactly {{'sector': <str>, 'beta': <str>}}")
            sector = entry["sector"]
            if not isinstance(sector, str) or not sector:
                raise ValueError(f"{path}.sector: must be a non-empty string")
            raw_beta = entry["beta"]
            if not isinstance(raw_beta, str):
                raise ValueError(f"{path}.beta: must be a Decimal-string, "
                                 f"got {type(raw_beta).__name__}")
            try:
                beta = Decimal(raw_beta)
            except InvalidOperation:
                raise ValueError(f"{path}.beta: not a Decimal string: {raw_beta!r}")
            if not beta.is_finite():
                raise ValueError(f"{path}.beta: non-finite Decimal not allowed: {raw_beta!r}")
            universe[symbol] = SymbolRisk(sector=sector, beta=beta)

        return cls(
            max_position_usd=parsed_caps["max_position_usd"],
            max_gross_exposure_usd=parsed_caps["max_gross_exposure_usd"],
            max_net_exposure_usd=parsed_caps["max_net_exposure_usd"],
            max_daily_loss_usd=parsed_caps["max_daily_loss_usd"],
            max_drawdown_usd=parsed_caps["max_drawdown_usd"],
            max_sector_exposure_usd=parsed_caps["max_sector_exposure_usd"],
            max_abs_beta_notional_usd=parsed_caps["max_abs_beta_notional_usd"],
            short_selling_enabled=(enabled is True) and SHORTS_SUPPORTED,
            universe=universe,
            rules_hash=rules_hash(config),
        )
