"""M4 §G — daily-loss + HWM drawdown monitor (FD-M4-18).

`daily_loss = last_equity − equity` (both broker-reported — the purest LD2 read);
`drawdown = hwm_equity − equity` where the HWM is the running max of FRESH equity
reads within the run lineage (journaled `loss_hwm_update`, rehydrated; run-lifetime
horizon). Breach iff value strictly `>` its cap; **cap 0 = zero budget** (any positive
loss breaches; 0 NEVER means "disabled"). `observe` acts ONLY on a fresh read —
stale/missing/invalid/skew reads update nothing and can never trip (FD-M4-3). The
monitor holds no broker and submits nothing — the caller feeds the TripSignal to
`RiskKillSwitch.trigger`.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Optional, Tuple

from agent.risk.account_state import AccountRead
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_ledger import rehydrate_risk_state
from agent.serializer import BrokerUSD

_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class TripSignal:
    cause: str                    # ∈ {"daily_loss_cap","drawdown_cap"}
    measured_usd: Decimal         # the breaching value (exact)
    cap_usd: Decimal
    equity: BrokerUSD
    basis: str                    # ∈ {"last_equity","high_water_mark"}
    session_date_et: str
    account_snapshot_id: str


@dataclass(frozen=True)
class LossRead:
    hwm_equity: Optional[BrokerUSD]    # None until the first FRESH observation this run
    daily_loss_usd: Optional[Decimal]  # last_equity - equity at last observation
    drawdown_usd: Optional[Decimal]    # hwm - equity at last observation
    breaches: Tuple[str, ...]          # sorted ⊆ {"daily_loss_breached","drawdown_breached"}


class LossLimitsMonitor:
    def __init__(self, *, cfg: RiskConfig, ledger=None) -> None:
        self._cfg = cfg
        self._ledger = ledger
        self._hwm: Optional[BrokerUSD] = None
        self._daily_loss: Optional[Decimal] = None
        self._drawdown: Optional[Decimal] = None
        self._breaches: Tuple[str, ...] = ()

    def observe(self, account: AccountRead, *, session_date_et: str) -> Optional[TripSignal]:
        """Acts ONLY on `account.status == "fresh"` (FD-M4-3; the caller's edge-
        triggered `account_alert` row is the degraded-read visibility). On a fresh
        read: HWM = max(hwm, equity) (a new high journals `loss_hwm_update` — edge-
        triggered, sparse); daily-loss checked first; one TripSignal per call."""
        if account.status != "fresh":
            return None
        read = account.read
        with localcontext(_DECIMAL_CTX):
            equity = read.equity
            if self._hwm is None or equity > self._hwm:
                self._hwm = equity
                if self._ledger is not None:
                    self._ledger.record_loss_hwm_update(
                        session_date_et=session_date_et,
                        hwm_equity=self._hwm,
                        equity=equity,
                        account_snapshot_id=read.account_snapshot_id)
            daily_loss = Decimal(read.last_equity) - Decimal(equity)
            drawdown = Decimal(self._hwm) - Decimal(equity)
        self._daily_loss = daily_loss
        self._drawdown = drawdown
        breaches = []
        if daily_loss > self._cfg.max_daily_loss_usd:
            breaches.append("daily_loss_breached")
        if drawdown > self._cfg.max_drawdown_usd:
            breaches.append("drawdown_breached")
        self._breaches = tuple(sorted(breaches))

        if daily_loss > self._cfg.max_daily_loss_usd:
            return TripSignal(cause="daily_loss_cap", measured_usd=daily_loss,
                              cap_usd=self._cfg.max_daily_loss_usd, equity=equity,
                              basis="last_equity", session_date_et=session_date_et,
                              account_snapshot_id=read.account_snapshot_id)
        if drawdown > self._cfg.max_drawdown_usd:
            return TripSignal(cause="drawdown_cap", measured_usd=drawdown,
                              cap_usd=self._cfg.max_drawdown_usd, equity=equity,
                              basis="high_water_mark", session_date_et=session_date_et,
                              account_snapshot_id=read.account_snapshot_id)
        return None

    def read(self) -> LossRead:
        return LossRead(hwm_equity=self._hwm, daily_loss_usd=self._daily_loss,
                        drawdown_usd=self._drawdown, breaches=self._breaches)

    def rehydrate(self, rows) -> None:
        """Re-seed the HWM from the journal's loss slice (run lineage, FD-M4-18)."""
        slice_ = rehydrate_risk_state(rows)["loss"]
        hwm = slice_["hwm_equity"]
        self._hwm = BrokerUSD(hwm) if hwm is not None else None
        self._daily_loss = None
        self._drawdown = None
        self._breaches = ()
