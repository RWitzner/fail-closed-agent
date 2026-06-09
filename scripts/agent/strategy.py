"""M3 §E — `Strategy` Protocol + frozen `ScanContext`: PURE types for M5+ (FD-12).

The M3 `CalibrationProbe` deliberately does NOT implement this Protocol (it has no
`scan` and no `strategy_id` attribute) — asserted by test. The M5 runner is the
first consumer.
"""
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from agent.candidate import Candidate
from agent.signal_snapshot import SignalSnapshot


@dataclass(frozen=True)
class ScanContext:
    snapshot: SignalSnapshot
    rules_hash: str
    now_ms: int


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str

    def scan(self, ctx: ScanContext) -> Sequence[Candidate]: ...
