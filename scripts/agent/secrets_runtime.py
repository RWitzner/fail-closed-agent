"""M5 §O.2 — runtime-secret loaders: run-gates file + Alpaca paper credentials.
[stdlib only]

``load_run_gates(path)`` reads the ONE new gate-reading surface (FD-M5-2):
``.secrets/run_gates.json``, frozen shape::

    { "enabled": true, "paper_trading": { "enabled": true } }

Frozen semantics (§O.2.1-8):

- ``path`` is a REQUIRED explicit argument — there is NO ``.secrets/`` default
  here (tests pin the signature); only the paper-mode startup path passes the
  real location. Observe/synthetic NEVER call this loader.
- Absent file, malformed JSON, wrong shape, or ANY non-identity-``True`` value
  ⇒ BOTH gates read False — ONE rule, fail-closed (§O.2.3: "Malformed JSON,
  wrong shape, or any non-identity-True value ⇒ both read False"). The two
  booleans are COUPLED: the file is a single switch, open only when BOTH paths
  are identity-True. Hostile extra keys are never read out — only the two gate
  paths are ever walked.
- The return value is the provenance reading for the §O.2.3 startup status row:
  ``{"present", "parse_ok", "enabled", "paper_enabled"}`` (journaled by the
  orchestrator as ``run_gates_file{...}`` on every paper run, all-False default
  included).

``assemble_gates_view(committed_config, gates_reading)`` returns the committed
(+overlay) dict with ONLY ``agent_rules.enabled`` and
``agent_rules.paper_trading.enabled`` replaced by the reading's booleans
(identity-strict re-read; defense-in-depth). The input dict is NEVER mutated
(deep copy), so ``rules_hash`` over the pre-substitution dict is untouched
(§O.2.7: the substitution never feeds ``rules_hash`` or the parsers). Everything
else — caps, universe, signal, execution, risk — stays committed-only (§O.2.2).

``load_alpaca_paper_credentials(path)`` is fail-LOUD (the opposite posture): a
missing/unreadable file, malformed JSON, missing/extra keys, or a non-string
value ⇒ ``ValueError``. Exactly ``{key_id, secret_key, base_url}`` is returned;
the paper-host pin on ``base_url`` is enforced by the broker ctor (§G), not
duplicated here.

This module never touches ``.secrets/`` on its own and performs no I/O beyond
the explicit path it is handed (R11: tests only ever inject tmp paths).
"""
import copy
import json
from pathlib import Path
from typing import Mapping

__all__ = [
    "assemble_gates_view",
    "load_alpaca_paper_credentials",
    "load_run_gates",
]

_CREDENTIAL_KEYS = ("key_id", "secret_key", "base_url")


def load_run_gates(path) -> dict:
    """Read the run-gates file; ALWAYS returns the 4-key provenance reading
    ``{present, parse_ok, enabled, paper_enabled}`` (never raises on bad data —
    fail-closed all-False is the answer, the reading is the status-row record)."""
    reading = {"present": False, "parse_ok": False,
               "enabled": False, "paper_enabled": False}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return reading                      # absent/unreadable -> both False
    reading["present"] = True
    try:
        data = json.loads(text)
    except ValueError:
        return reading                      # malformed JSON -> both False
    reading["parse_ok"] = True

    # ONE rule (§O.2.3): open iff the frozen shape holds with BOTH values
    # identity-True. Only the two gate paths are ever read out of the file.
    enabled_raw = data.get("enabled") if isinstance(data, dict) else None
    paper_node = data.get("paper_trading") if isinstance(data, dict) else None
    paper_raw = paper_node.get("enabled") if isinstance(paper_node, dict) else None
    both_open = (enabled_raw is True) and (paper_raw is True)
    reading["enabled"] = both_open
    reading["paper_enabled"] = both_open
    return reading


def assemble_gates_view(committed_config, gates_reading) -> dict:
    """Committed(+overlay) config with ONLY the two run-gate keys replaced by the
    file reading (§O.2.2). The input dict is NOT mutated; the view exists solely
    as the ``gates.opening_allowed`` argument (§O.2.7)."""
    if not isinstance(committed_config, Mapping):
        raise ValueError("committed_config must be a mapping (the assembled "
                         "{'agent_rules':..., 'risk_rules':...} dict)")
    if not isinstance(gates_reading, Mapping):
        raise ValueError("gates_reading must be the load_run_gates mapping")
    enabled = gates_reading.get("enabled") is True          # identity-strict
    paper_enabled = gates_reading.get("paper_enabled") is True

    view = copy.deepcopy(dict(committed_config))
    agent_rules = view.setdefault("agent_rules", {})
    if not isinstance(agent_rules, dict):
        raise ValueError("committed_config['agent_rules'] must be a dict")
    agent_rules["enabled"] = enabled
    paper_trading = agent_rules.setdefault("paper_trading", {})
    if not isinstance(paper_trading, dict):
        raise ValueError(
            "committed_config['agent_rules']['paper_trading'] must be a dict")
    paper_trading["enabled"] = paper_enabled
    return view


def load_alpaca_paper_credentials(path) -> dict:
    """Fail-loud loader for ``.secrets/alpaca_paper.json`` (§O.2 layout):
    exactly ``{key_id, secret_key, base_url}``, every value a non-empty string."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read credentials file {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"credentials file {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"credentials file {p} must be a JSON object")
    missing = sorted(set(_CREDENTIAL_KEYS) - set(data))
    if missing:
        raise ValueError(f"credentials file {p} missing keys: {missing}")
    extra = sorted(set(data) - set(_CREDENTIAL_KEYS))
    if extra:
        raise ValueError(f"credentials file {p} has unexpected keys: {extra}")
    for key in _CREDENTIAL_KEYS:
        value = data[key]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"credentials file {p}: {key!r} must be a non-empty string")
    return {key: data[key] for key in _CREDENTIAL_KEYS}
