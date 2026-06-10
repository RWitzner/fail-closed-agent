"""M5 §L.2 — backtest-artifact gate (FD-M5-27; S9).

The artifact is committed + reviewed + hash-bound (no crypto in M5):
`artifacts/backtests/<strategy_id>.json` = `{v, strategy_id, rules_hash, data_pin,
metrics{..., basis: "execution_realistic_pnl"}, created_utc, artifact_hash}` with
`artifact_hash = serializer.row_hash(payload minus artifact_hash)`. M5 ships the
verifier + an EMPTY dir (`.gitkeep`) ⇒ every real strategy rejects fail-closed
(`backtest_artifact_missing`, FD-M5-8); M7 owns artifact production.

`data_pin` uses the M3 frozen format `"{dataset}:{schema}:{interval}:{source_id}"`
(M3 contract §I) and is compared byte-exact — any drift re-closes the gate.

Fail-closed posture: `verify_artifact` NEVER raises on data — an absent file is
`missing`; unreadable / malformed JSON / wrong shape / tampered hash / wrong
metric basis all degrade to `hash_invalid`; a verified-but-stale key triple is
`key_mismatch`. Imports: stdlib + `agent.serializer` ONLY (§3).
"""
import json
import os
from dataclasses import dataclass
from typing import Optional

from agent.serializer import row_hash

ARTIFACTS_DIR = "artifacts/backtests"     # shipped EMPTY (.gitkeep) — FD-M5-8

_STATUSES = frozenset({"ok", "missing", "key_mismatch", "hash_invalid"})

# FD-M5-27 frozen payload shape — exactly these keys, nothing else.
_PAYLOAD_KEYS = frozenset({
    "v", "strategy_id", "rules_hash", "data_pin", "metrics", "created_utc",
    "artifact_hash"})

_REQUIRED_BASIS = "execution_realistic_pnl"    # the S9 metric pin


@dataclass(frozen=True)
class ArtifactCheck:
    status: str                  # ∈ {"ok", "missing", "key_mismatch", "hash_invalid"}
    artifact_path: Optional[str]
    artifact_hash: Optional[str]

    def __post_init__(self):
        if self.status not in _STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_STATUSES)}, got {self.status!r}")


def _missing() -> ArtifactCheck:
    return ArtifactCheck(status="missing", artifact_path=None, artifact_hash=None)


def _hash_invalid(path: str, claimed_hash: Optional[str]) -> ArtifactCheck:
    return ArtifactCheck(status="hash_invalid", artifact_path=path,
                         artifact_hash=claimed_hash)


def verify_artifact(strategy_id: str, *, rules_hash: str, data_pin: str,
                    artifacts_dir: str = ARTIFACTS_DIR) -> ArtifactCheck:
    """FD-M5-27 verdict for `(strategy_id, rules_hash, data_pin)` against
    `<artifacts_dir>/<strategy_id>.json`. Production keeps the `ARTIFACTS_DIR`
    default; test BUILDERS take a mandatory artifacts_dir (M5C-S12)."""
    # Defensive: a path-traversal strategy_id cannot escape artifacts_dir; no
    # legitimate artifact can exist under such a name -> missing (fail-closed).
    if (not isinstance(strategy_id, str) or not strategy_id
            or "/" in strategy_id or "\\" in strategy_id or ".." in strategy_id):
        return _missing()
    path = os.path.join(artifacts_dir, strategy_id + ".json")
    if not os.path.isfile(path):
        return _missing()

    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        # unreadable or malformed JSON (UnicodeDecodeError/JSONDecodeError are
        # ValueError subclasses) -> hash_invalid, never a raise (fail-closed)
        return _hash_invalid(path, None)

    if not isinstance(payload, dict):
        return _hash_invalid(path, None)
    claimed_hash = payload.get("artifact_hash")
    if not isinstance(claimed_hash, str) or not claimed_hash:
        claimed_hash = None
    if set(payload) != _PAYLOAD_KEYS:
        return _hash_invalid(path, claimed_hash)
    if claimed_hash is None:
        return _hash_invalid(path, None)

    body = {key: value for key, value in payload.items() if key != "artifact_hash"}
    try:
        computed_hash = row_hash(body)
    except (TypeError, ValueError):
        # floats / non-serializable content in the payload -> hash_invalid
        return _hash_invalid(path, claimed_hash)
    if computed_hash != claimed_hash:
        return _hash_invalid(path, claimed_hash)

    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or metrics.get("basis") != _REQUIRED_BASIS:
        return _hash_invalid(path, claimed_hash)   # the S9 metric pin

    if (payload["strategy_id"], payload["rules_hash"], payload["data_pin"]) != (
            strategy_id, rules_hash, data_pin):
        return ArtifactCheck(status="key_mismatch", artifact_path=path,
                             artifact_hash=claimed_hash)

    return ArtifactCheck(status="ok", artifact_path=path, artifact_hash=claimed_hash)
