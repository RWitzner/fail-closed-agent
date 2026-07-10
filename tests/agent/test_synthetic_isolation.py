"""M5 §R test 14 — MODULE-LEVEL subset only (S9, S1).

Owned here: `__init_subclass__` namespace enforcement (FD-M5-28); the frozen
ScriptedSyntheticStrategy matching rule (M5C-B2: str bar_key + int scan-ordinal);
ExitInstruction validation; the `verify_artifact` matrix over inline builders with
a MANDATORY tmp `artifacts_dir` (M5C-S12); the committed `artifacts/backtests/`
contains ONLY `.gitkeep`; the wall-3 AST import guard over scripts/agent/strategies/
(FD-M5-8; mirrors the test_no_network_no_creds FD-M4-24 pattern).

LATER WAVES own (deliberately NOT here): wall 1 (orchestrator ctor type-identity
check ⇒ SyntheticConfinementError), wall 2 (broker-side namespace refusal, §R 9 +
FakeBroker reverse wall), the `backtest_artifact_missing` ladder integration, and
the synthetic open→mark→close E2E + golden + GateFail-tick ordinal case
(`test_synthetic_e2e.py`) — §R 14's orchestrator-dependent cases.
"""
import ast
import dataclasses
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.backtest_gate import ARTIFACTS_DIR, ArtifactCheck, verify_artifact
from agent.serializer import row_hash
from agent.signal_snapshot import SignalSnapshot
from agent.strategies.synthetic import (
    ExitInstruction,
    ExitProvider,
    ScriptedSyntheticStrategy,
    SyntheticStrategy,
)
from agent.strategy import ScanContext, Strategy
from tests.lib.exec_fixtures import valid_v2_metrics

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STRATEGIES_DIR = _REPO_ROOT / "scripts" / "agent" / "strategies"

_BAR_END = "2026-06-15T14:31:00.000000Z"
_BAR_KEY = f"AAPL|1m|{_BAR_END}"
_DATA_PIN = "EQUS.MINI:tbbo:1m:fixture:signal-aapl-v1"   # M3 frozen data-pin format


def _ctx(*, symbol="AAPL", instrument_id=1001, bar_end_utc=_BAR_END, now_ms=1_000):
    """Real frozen SignalSnapshot/ScanContext. The wall-3 synthetic strategy reads
    ONLY symbol / instrument_id / event_start_bar_end_utc; the heavy collaborator
    fields are None — doubling as a tripwire (any read of feature/quote/
    quote_verdict/market_state inside synthetic.py crashes loudly here)."""
    snapshot = SignalSnapshot(
        symbol=symbol,
        instrument_id=instrument_id,
        decision_ts_utc="2026-06-15T14:31:00.250000Z",
        decision_seen_at_ms=now_ms,
        rules_hash="rh-test",
        feature=None,
        quote=None,
        quote_verdict=None,
        market_state=None,
        calendar_pin="nyse-test-pin",
        session_date_et="2026-06-15",
        event_start_bar_end_utc=bar_end_utc,
        horizons=("15m",),
        threshold_k=Decimal("0.0005"),
    )
    return ScanContext(snapshot=snapshot, rules_hash="rh-test", now_ms=now_ms)


def _row(**overrides):
    """Frozen §L.1 script-row shape builder."""
    base = {"on_bar": _BAR_KEY, "action": "open", "symbol": "AAPL",
            "qty": "5", "limit": "100.25"}
    base.update(overrides)
    return base


class TestSyntheticSubclassGuard(unittest.TestCase):
    """FD-M5-28: the namespace is enforced at class creation, not at use."""

    def test_base_and_scripted_class_facts(self):
        self.assertIs(SyntheticStrategy.synthetic, True)
        self.assertIs(ScriptedSyntheticStrategy.synthetic, True)
        self.assertEqual(ScriptedSyntheticStrategy.strategy_id, "synthetic.scripted_v1")
        strategy = ScriptedSyntheticStrategy([])
        self.assertIsInstance(strategy, SyntheticStrategy)
        self.assertIsInstance(strategy, Strategy)      # satisfies the M3 Protocol
        self.assertIsInstance(strategy, ExitProvider)  # and the exits capability

    def test_rejects_strategy_id_outside_synthetic_namespace(self):
        with self.assertRaises(ValueError):
            class Sneaky(SyntheticStrategy):
                strategy_id = "real.alpha_v1"

    def test_rejects_synthetic_without_dot(self):
        with self.assertRaises(ValueError):
            class NoDot(SyntheticStrategy):
                strategy_id = "synthetic_scripted"

    def test_rejects_missing_strategy_id(self):
        with self.assertRaises(ValueError):
            class NoId(SyntheticStrategy):
                pass

    def test_rejects_non_string_strategy_id(self):
        with self.assertRaises(ValueError):
            class NotAString(SyntheticStrategy):
                strategy_id = 7

    def test_accepts_synthetic_namespace(self):
        class Fine(SyntheticStrategy):
            strategy_id = "synthetic.fine_v1"
        self.assertIs(Fine.synthetic, True)


class TestScriptedMatchingRule(unittest.TestCase):
    """M5C-B2 frozen: str on_bar == the M3 bar_key; int on_bar == the 1-based
    scan() ordinal (GateFail ticks never reach scan, so it counts scan calls)."""

    def test_str_on_bar_matches_bar_key_and_emits_candidate(self):
        strategy = ScriptedSyntheticStrategy([_row()])
        candidates = strategy.scan(_ctx())
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.strategy_id, "synthetic.scripted_v1")
        self.assertIs(candidate.paper_eligible, True)   # legal per §L.1
        self.assertIsNone(candidate.score)
        self.assertEqual(len(candidate.legs), 1)
        leg = candidate.legs[0]
        self.assertEqual(leg.symbol, "AAPL")
        self.assertEqual(leg.instrument_id, 1001)       # from ctx.snapshot
        self.assertEqual(leg.side, "buy")               # long-only opens (FD-M4-1)
        self.assertEqual(leg.qty, Decimal("5"))
        self.assertEqual(leg.limit_price, Decimal("100.25"))

    def test_whole_second_form_bar_end_still_matches_canonical_key(self):
        # The M3-02 precedent: the bar end is re-minted in the canonical surface
        # form before keying, so a recorder-derived whole-second form cannot fork
        # the key.
        strategy = ScriptedSyntheticStrategy([_row()])
        candidates = strategy.scan(_ctx(bar_end_utc="2026-06-15T14:31:00Z"))
        self.assertEqual(len(candidates), 1)

    def test_str_on_bar_not_yet_due_emits_nothing(self):
        later = "AAPL|1m|2026-06-15T14:45:00.000000Z"
        strategy = ScriptedSyntheticStrategy([_row(on_bar=later)])
        self.assertEqual(strategy.scan(_ctx()), ())

    def test_str_on_bar_re_emits_on_every_matching_scan(self):
        # Rows are NOT consumed; the orchestrator's global in-flight guard dedupes
        # (RC-1/FD-M5-21 — later wave).
        strategy = ScriptedSyntheticStrategy([_row()])
        self.assertEqual(len(strategy.scan(_ctx())), 1)
        self.assertEqual(len(strategy.scan(_ctx())), 1)

    def test_int_on_bar_matches_scan_ordinal_one_based(self):
        strategy = ScriptedSyntheticStrategy([_row(on_bar=2)])
        self.assertEqual(strategy.scan(_ctx()), ())          # ordinal 1: not due
        self.assertEqual(len(strategy.scan(_ctx())), 1)      # ordinal 2: due
        self.assertEqual(strategy.scan(_ctx()), ())          # ordinal 3: past

    def test_int_on_bar_one_fires_on_first_scan(self):
        strategy = ScriptedSyntheticStrategy([_row(on_bar=1)])
        self.assertEqual(len(strategy.scan(_ctx())), 1)

    def test_exits_never_advance_the_scan_ordinal(self):
        strategy = ScriptedSyntheticStrategy([_row(on_bar=2)])
        self.assertEqual(strategy.scan(_ctx()), ())          # ordinal -> 1
        ctx = _ctx()
        strategy.exits(ctx)
        strategy.exits(ctx)
        strategy.exits(ctx)                                   # ordinal still 1
        self.assertEqual(len(strategy.scan(_ctx())), 1)      # ordinal -> 2: due

    def test_close_row_emits_exit_instruction_with_synthetic_script_reason(self):
        strategy = ScriptedSyntheticStrategy([_row(action="close", qty="3", limit=None)])
        exits = strategy.exits(_ctx())
        self.assertEqual(len(exits), 1)
        instruction = exits[0]
        self.assertIsInstance(instruction, ExitInstruction)
        self.assertEqual(instruction.symbol, "AAPL")
        self.assertEqual(instruction.instrument_id, 1001)    # from ctx.snapshot
        self.assertEqual(instruction.qty, Decimal("3"))
        # "synthetic_script" ∈ CLOSE_REASONS (exec_reasons §2.4); the membership
        # check itself is downstream at the exec_ledger seam (§3, wall 3).
        self.assertEqual(instruction.reason, "synthetic_script")

    def test_int_close_row_matches_current_scan_ordinal(self):
        strategy = ScriptedSyntheticStrategy([_row(action="close", on_bar=1, limit=None)])
        self.assertEqual(strategy.exits(_ctx()), ())         # ordinal 0: no scan yet
        strategy.scan(_ctx())                                 # ordinal -> 1
        self.assertEqual(len(strategy.exits(_ctx())), 1)

    def test_scan_never_emits_close_rows_and_exits_never_emits_open_rows(self):
        strategy = ScriptedSyntheticStrategy([
            _row(action="open"), _row(action="close", limit=None)])
        ctx = _ctx()
        candidates = strategy.scan(ctx)
        exits = strategy.exits(ctx)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(exits), 1)
        self.assertEqual(candidates[0].legs[0].side, "buy")
        self.assertIsInstance(exits[0], ExitInstruction)

    def test_symbol_mismatched_row_never_fires(self):
        strategy = ScriptedSyntheticStrategy(
            [_row(on_bar=1, symbol="MSFT"), _row(action="close", on_bar=1,
                                                 symbol="MSFT", limit=None)])
        ctx = _ctx()   # AAPL snapshot
        self.assertEqual(strategy.scan(ctx), ())
        self.assertEqual(strategy.exits(ctx), ())

    def test_open_with_limit_none_emits_unpriced_leg(self):
        strategy = ScriptedSyntheticStrategy([_row(limit=None)])
        candidates = strategy.scan(_ctx())
        self.assertIsNone(candidates[0].legs[0].limit_price)


class TestScriptValidation(unittest.TestCase):
    """The §L.1 row shape is frozen — malformed scripts fail loudly at __init__."""

    def test_valid_script_constructs(self):
        ScriptedSyntheticStrategy([_row(), _row(action="close", on_bar=3, limit=None)])

    def test_empty_script_is_legal(self):
        self.assertEqual(ScriptedSyntheticStrategy([]).scan(_ctx()), ())

    def test_rejects_non_mapping_row(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy(["not-a-mapping"])

    def test_rejects_missing_key(self):
        bad = _row()
        del bad["limit"]
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([bad])

    def test_rejects_extra_key(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([_row(reason="strategy_exit")])

    def test_rejects_bad_action(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([_row(action="flatten")])

    def test_rejects_bool_on_bar(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([_row(on_bar=True)])

    def test_rejects_nonpositive_int_on_bar(self):
        for ordinal in (0, -1):
            with self.assertRaises(ValueError):
                ScriptedSyntheticStrategy([_row(on_bar=ordinal)])

    def test_rejects_empty_str_on_bar(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([_row(on_bar="")])

    def test_rejects_non_int_str_qty(self):
        for qty in ("0", "-5", "1.5", "5e0", "+5", "", 5, Decimal("5"), None):
            with self.assertRaises(ValueError):
                ScriptedSyntheticStrategy([_row(qty=qty)])

    def test_rejects_bad_limit(self):
        for limit in (100.25, Decimal("100.25"), "NaN", "Infinity", "0", "-1", "abc"):
            with self.assertRaises(ValueError):
                ScriptedSyntheticStrategy([_row(limit=limit)])

    def test_rejects_empty_symbol(self):
        with self.assertRaises(ValueError):
            ScriptedSyntheticStrategy([_row(symbol="")])


class TestExitInstruction(unittest.TestCase):
    def _kwargs(self, **overrides):
        base = dict(symbol="AAPL", instrument_id=1001, qty=Decimal("3"),
                    reason="strategy_exit")
        base.update(overrides)
        return base

    def test_valid_instruction(self):
        instruction = ExitInstruction(**self._kwargs())
        self.assertEqual(instruction.qty, Decimal("3"))

    def test_frozen(self):
        instruction = ExitInstruction(**self._kwargs())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            instruction.qty = Decimal("4")

    def test_qty_must_be_positive_finite_decimal(self):
        for qty in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"),
                    3.0, 3, True, "3", None):
            with self.assertRaises(ValueError):
                ExitInstruction(**self._kwargs(qty=qty))

    def test_reason_must_be_non_empty_str(self):
        # Wall 3 (§3): strategies/ cannot import exec_reasons, so ExitInstruction
        # validates reason only as a non-empty string here — the CLOSE_REASONS
        # closed-vocabulary enforcement happens downstream at the exec_ledger seam.
        for reason in ("", None, 7):
            with self.assertRaises(ValueError):
                ExitInstruction(**self._kwargs(reason=reason))

    def test_symbol_and_instrument_id_validated(self):
        with self.assertRaises(ValueError):
            ExitInstruction(**self._kwargs(symbol=""))
        with self.assertRaises(ValueError):
            ExitInstruction(**self._kwargs(instrument_id="1001"))
        with self.assertRaises(ValueError):
            ExitInstruction(**self._kwargs(instrument_id=True))

    def test_exit_provider_protocol_membership(self):
        class NoExits:
            pass
        self.assertNotIsInstance(NoExits(), ExitProvider)
        self.assertIsInstance(ScriptedSyntheticStrategy([]), ExitProvider)


# --- verify_artifact matrix (FD-M5-27) — builders take MANDATORY artifacts_dir
# (M5C-S12: no default pointing at the committed ARTIFACTS_DIR, so these tests
# structurally cannot write into the committed dir). v2-only since the S9
# hardening (verify_artifact rejects v1 outright). ---

def _artifact_payload(*, strategy_id="real.alpha_v1", rules_hash="rh-1",
                      data_pin=_DATA_PIN, basis="execution_realistic_pnl"):
    body = {
        "v": 2,
        "strategy_id": strategy_id,
        "rules_hash": rules_hash,
        "data_pin": data_pin,
        "metrics": valid_v2_metrics(strategy_id, basis=basis),
        "created_utc": "2026-06-10T12:00:00.000000Z",
    }
    payload = dict(body)
    payload["artifact_hash"] = row_hash(body)   # hash over payload sans artifact_hash
    return payload


def _write_artifact(artifacts_dir, *, filename=None, payload=None, raw=None,
                    **payload_kwargs):
    """MANDATORY artifacts_dir (M5C-S12) — no default."""
    if payload is None:
        payload = _artifact_payload(**payload_kwargs)
    name = (filename or payload["strategy_id"]) + ".json"
    path = os.path.join(artifacts_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(raw if raw is not None else json.dumps(payload))
    return path


class TestVerifyArtifact(unittest.TestCase):
    def _verify(self, artifacts_dir, *, strategy_id="real.alpha_v1",
                rules_hash="rh-1", data_pin=_DATA_PIN):
        return verify_artifact(strategy_id, rules_hash=rules_hash,
                               data_pin=data_pin, artifacts_dir=artifacts_dir)

    def test_valid_triple_is_ok(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            path = _write_artifact(tmp, payload=payload)
            check = self._verify(tmp)
            self.assertEqual(check, ArtifactCheck(
                status="ok", artifact_path=path,
                artifact_hash=payload["artifact_hash"]))

    def test_absent_file_is_missing(self):
        with TemporaryDirectory() as tmp:
            check = self._verify(tmp)
            self.assertEqual(check, ArtifactCheck(
                status="missing", artifact_path=None, artifact_hash=None))

    def test_tampered_artifact_hash_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            payload["artifact_hash"] = "0" * 64
            _write_artifact(tmp, payload=payload)
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_tampered_payload_with_stale_hash_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            payload["metrics"] = dict(payload["metrics"], sharpe="9.99")  # hash now stale
            _write_artifact(tmp, payload=payload)
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_mismatched_rules_hash_is_key_mismatch(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp)
            self.assertEqual(
                self._verify(tmp, rules_hash="rh-DRIFTED").status, "key_mismatch")

    def test_mismatched_data_pin_is_key_mismatch(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp)
            drifted = "EQUS.MINI:tbbo:1m:fixture:OTHER-v2"
            self.assertEqual(
                self._verify(tmp, data_pin=drifted).status, "key_mismatch")

    def test_payload_strategy_id_drift_is_key_mismatch(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, filename="real.alpha_v1",
                            payload=_artifact_payload(strategy_id="real.other_v1"))
            self.assertEqual(self._verify(tmp).status, "key_mismatch")

    def test_wrong_basis_is_hash_invalid_even_with_valid_hash_and_keys(self):
        # The S9 metric pin: basis is checked BEFORE the key triple, so a
        # key-exact artifact with the wrong basis is hash_invalid, not ok.
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, basis="raw_pnl")
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_malformed_json_is_hash_invalid_never_raises(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, filename="real.alpha_v1", raw="{not json")
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_non_dict_top_level_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, filename="real.alpha_v1", raw="[1,2,3]")
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_missing_payload_key_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            del payload["created_utc"]
            _write_artifact(tmp, filename="real.alpha_v1", payload=payload)
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_extra_payload_key_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            payload["blessed_by"] = "nobody"
            _write_artifact(tmp, filename="real.alpha_v1", payload=payload)
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_float_in_metrics_is_hash_invalid_never_raises(self):
        # serializer.dumps rejects floats; verify_artifact must degrade, not raise.
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            payload["metrics"] = dict(payload["metrics"], sharpe=1.10)
            _write_artifact(tmp, filename="real.alpha_v1",
                            raw=json.dumps(payload))
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_non_string_artifact_hash_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            payload["artifact_hash"] = 7
            _write_artifact(tmp, filename="real.alpha_v1", payload=payload)
            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_path_traversal_strategy_id_is_missing(self):
        with TemporaryDirectory() as tmp:
            for hostile in ("../evil", "a/b", "a\\b", ".."):
                self.assertEqual(
                    self._verify(tmp, strategy_id=hostile).status, "missing")

    def test_artifact_check_rejects_out_of_vocab_status(self):
        with self.assertRaises(ValueError):
            ArtifactCheck(status="maybe", artifact_path=None, artifact_hash=None)


class TestCommittedArtifactsDirEmpty(unittest.TestCase):
    """M5C-S12 tripwire (a): the S9 premise rests on the committed dir staying
    empty — every real strategy must reject `backtest_artifact_missing`."""

    def test_committed_dir_contains_only_gitkeep(self):
        committed = _REPO_ROOT / "artifacts" / "backtests"
        self.assertEqual(sorted(os.listdir(committed)), [".gitkeep"])
        self.assertEqual((committed / ".gitkeep").read_bytes(), b"")

    def test_artifacts_dir_constant_and_shipped_empty_verdict(self):
        self.assertEqual(ARTIFACTS_DIR, "artifacts/backtests")
        # Against the committed dir (cwd-independent), any real strategy is
        # missing ⇒ the ladder maps it to backtest_artifact_missing (FD-M5-8).
        check = verify_artifact(
            "real.any_strategy_v1", rules_hash="rh-x", data_pin=_DATA_PIN,
            artifacts_dir=str(_REPO_ROOT / "artifacts" / "backtests"))
        self.assertEqual(check.status, "missing")


class TestStrategiesAstImportGuard(unittest.TestCase):
    """Wall 3 (FD-M5-8; §3): strategies/ (including M7 directional_momentum.py)
    keeps the full M3 FD-12 closed set — no agent.broker*, no
    agent.execution_preflight, no agent.kill_switch, no agent.arming, and no
    importlib/__import__ anywhere. Mirrors the test_no_network_no_creds.py
    FD-M4-24 AST pattern."""

    _FORBIDDEN_MODULE_PREFIXES = (
        "agent.broker", "agent.execution_preflight", "agent.kill_switch",
        "agent.arming",
    )
    _FORBIDDEN_TOKENS = frozenset({"importlib", "__import__"})

    def _module_forbidden(self, module_name) -> bool:
        if not module_name:
            return False
        return any(module_name == prefix or module_name.startswith(prefix + ".")
                   for prefix in self._FORBIDDEN_MODULE_PREFIXES)

    def _strategy_files(self):
        files = sorted(_STRATEGIES_DIR.glob("*.py"))
        self.assertEqual(
            sorted(path.name for path in files),
            ["__init__.py", "calibration_probe.py", "directional_momentum.py",
             "relative_strength.py", "synthetic.py"],
            "strategies/ grew a file — extend the wall-3 guard consciously")
        return files

    def test_wall_3_ast_guard_over_every_strategies_module(self):
        for source_path in self._strategy_files():
            violations = []
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._module_forbidden(alias.name):
                            violations.append(f"import {alias.name}")
                        if alias.name == "importlib" or alias.name.startswith(
                                "importlib."):
                            violations.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._module_forbidden(module):
                        violations.append(f"from {module} import ...")
                    if module == "importlib" or module.startswith("importlib."):
                        violations.append(f"from {module} import ...")
                    if module == "agent":
                        for alias in node.names:
                            if self._module_forbidden(f"agent.{alias.name}"):
                                violations.append(f"from agent import {alias.name}")
                    for alias in node.names:
                        if alias.name in self._FORBIDDEN_TOKENS:
                            violations.append(f"imported token {alias.name}")
                        if alias.asname and alias.asname in self._FORBIDDEN_TOKENS:
                            violations.append(f"alias token {alias.asname}")
                elif isinstance(node, ast.Name):
                    if node.id in self._FORBIDDEN_TOKENS:
                        violations.append(f"name {node.id}")
                elif isinstance(node, ast.Attribute):
                    if node.attr in self._FORBIDDEN_TOKENS:
                        violations.append(f"attribute .{node.attr}")
            self.assertEqual(
                violations, [],
                f"{source_path.name}: wall-3 violations: {violations}")

    def test_synthetic_imports_only_candidate_and_strategy_from_agent(self):
        # §3 import row, the POSITIVE whitelist: strategies/synthetic.py imports
        # ONLY stdlib + agent.candidate + agent.strategy.
        allowed = {"agent.candidate", "agent.strategy"}
        tree = ast.parse(
            (_STRATEGIES_DIR / "synthetic.py").read_text(encoding="utf-8"))
        agent_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "agent" or alias.name.startswith("agent."):
                        agent_imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "agent":
                    for alias in node.names:
                        agent_imports.add(f"agent.{alias.name}")
                elif module.startswith("agent."):
                    agent_imports.add(module)
        self.assertEqual(agent_imports, allowed)


if __name__ == "__main__":
    unittest.main()
