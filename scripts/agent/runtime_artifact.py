"""Explicit offline binding of passing historical evidence to a paper runtime.

The v2 research artifact is preserved in full inside a v3 envelope. Writing
requires a separate reviewed-runtime flag and never overwrites an artifact.
No config, credentials, gates, market data or broker are accessed implicitly.
"""
import argparse
import copy
import json
from pathlib import Path

from agent.backtest_gate import verify_artifact_payload
from agent.execution_config import ExecutionConfig
from agent.risk.risk_config import RiskConfig
from agent.serializer import dumps, row_hash


def build_runtime_artifact(*, research_artifact: dict, config: dict,
                           live_data_pin: str, created_utc: str) -> dict:
    """Pure, fail-closed builder; does not promote or modify research evidence."""
    RiskConfig.from_config(config)
    execution = ExecutionConfig.from_config(config)
    if not isinstance(research_artifact, dict):
        raise ValueError("a v2 historical research artifact is required")
    strategy_id = research_artifact.get("strategy_id")
    if (not isinstance(strategy_id, str) or not strategy_id
            or "/" in strategy_id or "\\" in strategy_id or ".." in strategy_id
            or strategy_id.startswith("synthetic.")):
        raise ValueError("invalid real strategy id")
    if not isinstance(created_utc, str) or not created_utc:
        raise ValueError("created_utc is required")
    payload = {
        "v": 3, "strategy_id": strategy_id,
        "rules_hash": execution.rules_hash, "data_pin": live_data_pin,
        "research_artifact": copy.deepcopy(research_artifact),
        "created_utc": created_utc,
    }
    payload["artifact_hash"] = row_hash(payload)
    verdict = verify_artifact_payload(payload, strategy_id=strategy_id,
        rules_hash=execution.rules_hash, data_pin=live_data_pin,
        runtime_config=config)
    if verdict.status != "ok":
        raise ValueError(f"runtime binding refused: {verdict.status}")
    return payload


def write_runtime_artifact(*, artifacts_dir, allow_reviewed_runtime: bool = False,
                           **kwargs) -> Path:
    if allow_reviewed_runtime is not True:
        raise ValueError("writing requires --allow-reviewed-runtime")
    payload = build_runtime_artifact(**kwargs)
    directory = Path(artifacts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (payload["strategy_id"] + ".json")
    # A review must choose a separate output directory when preserving an
    # existing artifact at this path. Never overwrite the research source.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(dumps(payload))
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-artifact", required=True)
    parser.add_argument("--agent-rules", required=True)
    parser.add_argument("--risk-rules", required=True)
    parser.add_argument("--live-data-pin", required=True)
    parser.add_argument("--created-utc", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--allow-reviewed-runtime", action="store_true")
    args = parser.parse_args(argv)
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        path = write_runtime_artifact(
            artifacts_dir=args.artifacts_dir,
            allow_reviewed_runtime=args.allow_reviewed_runtime,
            research_artifact=read(args.research_artifact),
            config={"agent_rules": read(args.agent_rules), "risk_rules": read(args.risk_rules)},
            live_data_pin=args.live_data_pin, created_utc=args.created_utc)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
