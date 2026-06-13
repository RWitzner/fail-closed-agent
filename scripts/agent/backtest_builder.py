"""M7 fixture artifact builder.

Historical data ingestion remains a separate tier. This builder produces the
small deterministic fixture artifact used to prove the M7 v2 gate, criteria
evaluator, and CLI write guards without credentials or network access.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

from agent.backtest_engine import BacktestTrade
from agent.backtest_metrics import build_v2_artifact_payload
from agent.paper_phase_criteria import CriteriaVerdict, evaluate_paper_phase_criteria
from agent.serializer import dumps

STRATEGY_ID = "directional.momentum_v1"
PRODUCTION_ARTIFACTS_DIR = (
    Path(__file__).resolve().parents[2] / "artifacts" / "backtests"
).resolve()
_ENTRY_MID = Decimal("100.000000")
_QTY = Decimal("2")
_FEES = Decimal("0.100000")
_USD_QUANTUM = Decimal("0.000001")


class ArtifactWriteRefused(ValueError):
    """Raised when a builder would write to the committed artifact directory."""


@dataclass(frozen=True)
class ArtifactBuildResult:
    artifact_path: Path
    payload: dict
    criteria: CriteriaVerdict


def _decimal_from_string(value: str, *, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must parse as Decimal: {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _fixture_trade(net_pnl_usd: Decimal) -> BacktestTrade:
    gross = (net_pnl_usd + _FEES).quantize(
        _USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    exit_mid = (_ENTRY_MID + gross / _QTY).quantize(
        _USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    return BacktestTrade(
        symbol="AAPL",
        instrument_id=1001,
        qty=_QTY,
        entry_bar_end_utc="2026-06-15T14:31:00.000000Z",
        exit_bar_end_utc="2026-06-15T14:32:00.000000Z",
        entry_mid=_ENTRY_MID,
        exit_mid=exit_mid,
        gross_modeled_usd=gross,
        fees_usd=_FEES,
        net_execution_realistic_pnl_usd=net_pnl_usd.quantize(
            _USD_QUANTUM, rounding=ROUND_HALF_EVEN),
    )


def _refuse_production_write(artifacts_dir: Path,
                             allow_reviewed_artifact: bool) -> None:
    if artifacts_dir.resolve() == PRODUCTION_ARTIFACTS_DIR:
        if not allow_reviewed_artifact:
            raise ArtifactWriteRefused(
                "refusing to write committed artifacts/backtests without "
                "--write-reviewed-artifact")


def write_m7_fixture_artifact(*, artifacts_dir, rules_hash: str, data_pin: str,
                              created_utc: str, input_manifest_hash: str,
                              builder_git_commit: str, tier: str,
                              fixture_net_pnl_usd: str,
                              allow_reviewed_artifact: bool = False
                              ) -> ArtifactBuildResult:
    output_dir = Path(artifacts_dir)
    _refuse_production_write(output_dir, allow_reviewed_artifact)

    net = _decimal_from_string(fixture_net_pnl_usd, name="fixture_net_pnl_usd")
    payload = build_v2_artifact_payload(
        strategy_id=STRATEGY_ID,
        rules_hash=rules_hash,
        data_pin=data_pin,
        trades=(_fixture_trade(net),),
        skips=(),
        created_utc=created_utc,
        input_manifest_hash=input_manifest_hash,
        builder_git_commit=builder_git_commit,
        tier=tier,
    )
    criteria = evaluate_paper_phase_criteria(payload["metrics"])
    artifact_path = output_dir / f"{STRATEGY_ID}.json"
    result = ArtifactBuildResult(
        artifact_path=artifact_path, payload=payload, criteria=criteria)
    if not criteria.passed:
        return result

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(dumps(payload), encoding="utf-8")
    return result
