"""M5 §L.1 — synthetic strategy family: offline E2E drivers (S9; FD-M5-8/28).

Wall 3 (FD-M5-8): this module imports ONLY stdlib + `agent.candidate` +
`agent.strategy` (§3 import row) — it cannot even name a broker or token type.
Consequences, documented per the contract:

- It CANNOT import `agent.exec_reasons`, so `ExitInstruction.reason` is validated
  here only as a non-empty string; the CLOSE_REASONS closed-vocabulary enforcement
  happens downstream at the exec_ledger seam (§3; §P.2 `position_close` pins
  `reason ∈ CLOSE_REASONS`). The scripted close emits the literal
  `"synthetic_script"`, a CLOSE_REASONS member (exec_reasons.py §2.4).
- It CANNOT import `agent.bar_series`/`agent.signal_snapshot` helpers, so the M3
  bar-key derivation is re-implemented locally as the trivial string functions it
  is: `_parse_utc`/`_canonical_utc` mirror signal_snapshot.py:38-52 and `_bar_key`
  mirrors calibration_probe.py:99-100 (`f"{symbol}|{interval}|{bucket_end_utc}"`,
  the M3 frozen bar-key format). The bar end is re-minted in the ONE canonical
  surface form before keying (the M3-02 precedent), so a whole-second-form
  timestamp can never fork the key.

Synthetic candidates MAY carry `paper_eligible=True` — that means only "may pass
the M4 ladder stage 6 under a fixture config"; reaching a REAL broker is blocked
by walls 1+2 and the artifact gate, which synthetics can never present (§L.1).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from agent.candidate import Candidate, Leg
from agent.strategy import ScanContext

UTC = timezone.utc

# M3 FD-1: "1m" is the ONLY interval built (signal_config.py:117-119 rejects any
# other value at parse), so the synthetic bar key pins it as a local constant
# rather than importing SignalConfig (outside the wall-3 import set).
_INTERVAL = "1m"

_SCRIPT_ROW_KEYS = frozenset({"on_bar", "action", "symbol", "qty", "limit"})
_SCRIPT_ACTIONS = frozenset({"open", "close"})

# The scripted close reason: a CLOSE_REASONS member by construction (§2.4), but the
# membership check itself lives downstream at the exec_ledger seam (wall 3, §3).
_SCRIPTED_CLOSE_REASON = "synthetic_script"


def _parse_utc(ts: str) -> datetime:
    """Local mirror of signal_snapshot._parse_utc (wall 3 forbids importing it)."""
    raw = ts
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        raise ValueError(f"cannot parse UTC timestamp: {raw!r}")
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(UTC)


def _canonical_utc(dt: datetime) -> str:
    """Local mirror of signal_snapshot._canonical_utc (the §0 canonical form)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _bar_key(symbol: str, interval: str, bucket_end_utc: str) -> str:
    """Local mirror of calibration_probe._bar_key (M3 frozen bar-key format)."""
    return f"{symbol}|{interval}|{bucket_end_utc}"


@dataclass(frozen=True)
class ExitInstruction:
    """§L.1 scripted-close row. `qty <= held` is validated downstream by the
    reduce mint; `reason ∈ CLOSE_REASONS` is enforced downstream at the
    exec_ledger seam (§3 — wall 3 forbids importing exec_reasons here)."""

    symbol: str
    instrument_id: int
    qty: Decimal                  # > 0 (validated here); <= held (downstream)
    reason: str                   # ∈ CLOSE_REASONS (downstream); non-empty str here

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError(f"symbol: non-empty str required, got {self.symbol!r}")
        if isinstance(self.instrument_id, bool) or not isinstance(self.instrument_id, int):
            raise ValueError(
                f"instrument_id: int required, got {type(self.instrument_id).__name__}")
        if isinstance(self.qty, bool) or isinstance(self.qty, float) or not isinstance(
                self.qty, Decimal):
            raise ValueError(f"qty: must be a Decimal, got {type(self.qty).__name__}")
        if not self.qty.is_finite():
            raise ValueError(f"qty: non-finite Decimal not allowed: {self.qty!r}")
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError(f"reason: non-empty str required, got {self.reason!r}")


@runtime_checkable
class ExitProvider(Protocol):
    """Optional capability: the orchestrator polls `exits` when present (§L.1)."""

    def exits(self, ctx: ScanContext) -> Sequence[ExitInstruction]: ...


class SyntheticStrategy:
    """Base for offline E2E drivers. Structural facts (FD-M5-8/28):
    - synthetic = True (class attr, identity-checked);
    - __init_subclass__ raises unless strategy_id.startswith("synthetic.");
    - emitted Candidates MAY carry paper_eligible=True — that means only 'may pass
      the M4 ladder stage 6 under a fixture config'; reaching a REAL broker is
      blocked by walls 1+2 and the artifact gate, which synthetics can never
      present."""

    synthetic = True
    strategy_id: str                       # MUST start "synthetic." (FD-M5-28)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        strategy_id = getattr(cls, "strategy_id", None)
        if not isinstance(strategy_id, str) or not strategy_id.startswith("synthetic."):
            raise ValueError(
                f"SyntheticStrategy subclass {cls.__name__!r} requires a strategy_id "
                f"str starting with 'synthetic.' (FD-M5-28), got {strategy_id!r}")

    def scan(self, ctx: ScanContext) -> Sequence[Candidate]:
        raise NotImplementedError


def _validate_script_row(row, *, index: int) -> dict:
    """Frozen §L.1 script-row shape: {"on_bar": <bar_key|ordinal>, "action":
    "open"|"close", "symbol", "qty": "<int-str>", "limit": "<Decimal-str>"|None}.
    Fail-loud at construction; returns a normalized internal row."""
    where = f"script[{index}]"
    if not isinstance(row, Mapping):
        raise ValueError(f"{where}: mapping required, got {type(row).__name__}")
    if set(row) != _SCRIPT_ROW_KEYS:
        missing = _SCRIPT_ROW_KEYS - set(row)
        extra = set(row) - _SCRIPT_ROW_KEYS
        raise ValueError(
            f"{where}: frozen row shape violated: missing={sorted(missing)}, "
            f"extra={sorted(extra)}")
    on_bar = row["on_bar"]
    if isinstance(on_bar, bool) or not isinstance(on_bar, (str, int)):
        raise ValueError(
            f"{where}: on_bar must be a bar_key str or a 1-based int ordinal, "
            f"got {type(on_bar).__name__}")
    if isinstance(on_bar, str) and not on_bar:
        raise ValueError(f"{where}: on_bar bar_key must be non-empty")
    if isinstance(on_bar, int) and on_bar < 1:
        raise ValueError(f"{where}: on_bar ordinal is 1-based, got {on_bar}")
    action = row["action"]
    if action not in _SCRIPT_ACTIONS:
        raise ValueError(
            f"{where}: action must be one of {sorted(_SCRIPT_ACTIONS)}, got {action!r}")
    symbol = row["symbol"]
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f"{where}: symbol must be a non-empty str, got {symbol!r}")
    qty_raw = row["qty"]
    if not isinstance(qty_raw, str) or not (qty_raw.isascii() and qty_raw.isdigit()):
        raise ValueError(f"{where}: qty must be an '<int-str>', got {qty_raw!r}")
    qty = Decimal(qty_raw)
    if qty < 1:
        raise ValueError(f"{where}: qty must be >= 1, got {qty_raw!r}")
    limit_raw = row["limit"]
    if limit_raw is None:
        limit = None
    else:
        if not isinstance(limit_raw, str):
            raise ValueError(
                f"{where}: limit must be a '<Decimal-str>' or None, "
                f"got {type(limit_raw).__name__}")
        try:
            limit = Decimal(limit_raw)
        except InvalidOperation:
            raise ValueError(f"{where}: limit does not parse as Decimal: {limit_raw!r}")
        if not limit.is_finite() or limit <= 0:
            raise ValueError(f"{where}: limit must be a finite Decimal > 0, got {limit_raw!r}")
    return {"on_bar": on_bar, "action": action, "symbol": symbol,
            "qty": qty, "limit": limit}


class ScriptedSyntheticStrategy(SyntheticStrategy):
    """Deterministic script-driven synthetic — drives the first open→mark→close
    E2E (S9) with zero randomness.

    FROZEN MATCHING RULE (M5C-B2): a str "on_bar" matches the M3 bar_key derived
    from ctx.snapshot.event_start_bar_end_utc; an int "on_bar" matches the ORDINAL
    of successful scan() invocations (1-based, counted only when a SignalSnapshot
    was assembled — GateFail ticks never reach scan(), so the ordinal counts
    scan() calls). exits() consults the CURRENT ordinal (the most recent scan
    tick's) and never advances it. Rows are NOT consumed: a due row re-emits on
    every matching call (the orchestrator's global in-flight guard dedupes —
    RC-1/FD-M5-21)."""

    strategy_id = "synthetic.scripted_v1"

    def __init__(self, script: Sequence[Mapping]) -> None:
        self._script = tuple(
            _validate_script_row(row, index=i) for i, row in enumerate(script))
        self._scan_count = 0

    # --- the M5C-B2 matching rule ---

    def _ctx_bar_key(self, ctx: ScanContext) -> str:
        # Re-mint the canonical surface form before keying (M3-02 precedent).
        bar_end = _canonical_utc(_parse_utc(ctx.snapshot.event_start_bar_end_utc))
        return _bar_key(ctx.snapshot.symbol, _INTERVAL, bar_end)

    def _due(self, on_bar, bar_key: str) -> bool:
        if isinstance(on_bar, str):
            return on_bar == bar_key
        return on_bar == self._scan_count

    def scan(self, ctx: ScanContext) -> Sequence[Candidate]:
        self._scan_count += 1                      # ordinal: counts scan() calls
        bar_key = self._ctx_bar_key(ctx)
        candidates = []
        for row in self._script:
            if row["action"] != "open":
                continue
            if row["symbol"] != ctx.snapshot.symbol:
                continue
            if not self._due(row["on_bar"], bar_key):
                continue
            candidates.append(Candidate(
                strategy_id=self.strategy_id,
                legs=(Leg(
                    symbol=row["symbol"],
                    instrument_id=ctx.snapshot.instrument_id,
                    side="buy",                    # long-only opens (FD-M4-1; §2.2 stage 4)
                    qty=row["qty"],
                    limit_price=row["limit"],
                ),),
                paper_eligible=True,               # legal per §L.1 (walls confine it)
                score=None,
            ))
        return tuple(candidates)

    def exits(self, ctx: ScanContext) -> Sequence[ExitInstruction]:
        bar_key = self._ctx_bar_key(ctx)
        instructions = []
        for row in self._script:
            if row["action"] != "close":
                continue
            if row["symbol"] != ctx.snapshot.symbol:
                continue
            if not self._due(row["on_bar"], bar_key):
                continue
            instructions.append(ExitInstruction(
                symbol=row["symbol"],
                instrument_id=ctx.snapshot.instrument_id,
                qty=row["qty"],
                reason=_SCRIPTED_CLOSE_REASON,
            ))
        return tuple(instructions)
