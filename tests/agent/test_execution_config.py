"""M5 §B / §R 1 — committed `agent_rules.execution` block + `ExecutionConfig` parsing,
plus the §2 frozen vocabularies in `exec_reasons.py` (S1-config, FD-M5-29).

`ExecutionConfig.from_config` takes the ASSEMBLED {"agent_rules": ..., "risk_rules": ...}
dict (the M4 RiskConfig shape — NOT SignalConfig's bare agent_rules dict) and is the ONE
parser of `agent_rules.execution` + `latency_budget_ms`. The two §B polarity traps are
pinned here (M5C-T2): `tighten_only_merge` min()s non-bool numerics, so a hostile
LOWERING overlay DOES take effect at merge — the defense is the code FLOOR
(`LATENCY_BUDGET_MIN_MS`) for values that parse, and a fail-loud `ValueError` for
values that don't (0 / negative), never the merge.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent import config as agent_config
from agent.exec_reasons import (
    BROKER_KINDS,
    CANCEL_CAUSES,
    CANCEL_OUTCOMES,
    CLOSE_REASONS,
    COLLECTED_PREFLIGHT_REASONS,
    CONSUME_REASONS,
    DIVERGENCE_FLAGS,
    EXTRA_REJECT_REASONS,
    ExecError,
    FILL_POLICIES,
    FILL_SOURCES,
    MODELED_FILL_MODELS,
    ORDER_STATES,
    PREFLIGHT_REASONS,
    PREFLIGHT_STAGES,
    REALISM_CLASSES,
    REJECT_STAGES,
    RESERVED_PREFLIGHT_REASONS,
    STRATEGY_DECISION_ACTIONS,
    SUBMIT_RESOLUTIONS,
    TERMINAL_PREFLIGHT_REASONS,
    TERMINAL_STATES,
    require_member,
    require_reason,
    require_stage,
)
from agent.execution_config import (
    DEPTH_FRESHNESS_TTL_MS,
    DIVERGENCE_ALERT_BPS,
    ExecutionConfig,
    FLATTEN_CAP_BPS,
    LATENCY_BUDGET_MIN_MS,
    MIN_REQUOTE_DELTA_MS,
    OPEN_TOKEN_TTL_MS,
    ORDER_POLL_INTERVAL_MS_MAX,
    RISK_VERDICT_TTL_MS,
    SUBMIT_RECOVERY_ATTEMPTS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _committed_config() -> dict:
    """The real committed files, freshly loaded (independent dicts per call)."""
    return {
        "agent_rules": agent_config.load(CONFIG_DIR / "agent_rules.json"),
        "risk_rules": agent_config.load(CONFIG_DIR / "risk_rules.json"),
    }


class TestCommittedExecutionBlock(unittest.TestCase):
    def setUp(self):
        self.config = _committed_config()

    def test_committed_execution_values_as_committed(self):
        # The four §B values, byte-as-committed: JSON ints (never bool), min()-merge
        # polarity correct for all four (FD-M5-29).
        execution = self.config["agent_rules"]["execution"]
        self.assertEqual(
            execution,
            {
                "slippage_cap_bps": 25,
                "order_poll_interval_ms": 500,
                "account_refresh_interval_ms": 2500,
                "max_open_orders": 1,
            },
        )
        for key, value in execution.items():
            self.assertIsInstance(value, int, key)
            self.assertNotIsInstance(value, bool, key)

    def test_committed_config_parses_to_typed_values(self):
        parsed = ExecutionConfig.from_config(self.config)
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertIsInstance(parsed.slippage_cap_bps, Decimal)
        self.assertEqual(parsed.order_poll_interval_ms, 500)
        self.assertEqual(parsed.account_refresh_interval_ms, 2500)
        self.assertEqual(parsed.max_open_orders, 1)
        self.assertEqual(parsed.effective_latency_budget_ms, 250)
        self.assertEqual(parsed.quote_staleness_ms_max, 2000)
        self.assertEqual(parsed.spread_bps_max, Decimal("50"))
        self.assertIsInstance(parsed.spread_bps_max, Decimal)

    def test_run_gates_still_identity_false_with_execution_block_present(self):
        # Extends, never replaces, the M0 canary: adding the execution block must
        # not touch the run gates.
        self.assertIs(self.config["agent_rules"]["enabled"], False)
        self.assertIs(self.config["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(self.config["risk_rules"]["live_trading"]["enabled"], False)

    def test_latency_budget_committed_as_top_level_json_int_250(self):
        latency = self.config["agent_rules"]["latency_budget_ms"]
        self.assertIsInstance(latency, int)
        self.assertNotIsInstance(latency, bool)
        self.assertEqual(latency, 250)

    def test_rules_hash_matches_agent_config_over_assembled_dict(self):
        parsed = ExecutionConfig.from_config(self.config)
        self.assertEqual(parsed.rules_hash, agent_config.rules_hash(self.config))

    def test_hostile_raising_overlay_merges_back_ineffective(self):
        # §B canary obligation (here for the parser's view; the canary file extends
        # separately): huge slippage / max_open_orders 99 merge back via min(),
        # injected keys are dropped.
        overlay = {
            "agent_rules": {
                "execution": {
                    "slippage_cap_bps": 9999,
                    "order_poll_interval_ms": 999999,
                    "account_refresh_interval_ms": 999999,
                    "max_open_orders": 99,
                    "smuggled_knob": 1,
                }
            }
        }
        merged = agent_config.tighten_only_merge(self.config, overlay)
        self.assertEqual(
            merged["agent_rules"]["execution"],
            self.config["agent_rules"]["execution"],
        )
        parsed = ExecutionConfig.from_config(merged)
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertEqual(parsed.max_open_orders, 1)


class TestExecutionConfigParsing(unittest.TestCase):
    _INT_KEYS = (
        "slippage_cap_bps",
        "order_poll_interval_ms",
        "account_refresh_interval_ms",
        "max_open_orders",
    )

    def test_ints_must_be_json_ints_bool_float_string_rejected(self):
        for key in self._INT_KEYS:
            for bad in (True, False, 500.0, 25.5, "500", None, [500]):
                cfg = _committed_config()
                cfg["agent_rules"]["execution"][key] = bad
                with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                    ExecutionConfig.from_config(cfg)

    def test_ints_must_be_positive(self):
        for key in self._INT_KEYS:
            for bad in (0, -1, -500):
                cfg = _committed_config()
                cfg["agent_rules"]["execution"][key] = bad
                with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                    ExecutionConfig.from_config(cfg)

    def test_unknown_key_raises(self):
        cfg = _committed_config()
        cfg["agent_rules"]["execution"]["surprise"] = 1
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)

    def test_missing_key_raises(self):
        for key in self._INT_KEYS:
            cfg = _committed_config()
            del cfg["agent_rules"]["execution"][key]
            with self.assertRaises(ValueError, msg=key):
                ExecutionConfig.from_config(cfg)

    def test_missing_execution_block_raises(self):
        cfg = _committed_config()
        del cfg["agent_rules"]["execution"]
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)

    def test_execution_block_must_be_dict(self):
        cfg = _committed_config()
        cfg["agent_rules"]["execution"] = "25"
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)

    def test_missing_agent_rules_raises(self):
        cfg = _committed_config()
        del cfg["agent_rules"]
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config("not-a-dict")

    def test_max_open_orders_must_be_exactly_one(self):
        # FD-M5-21: one in-flight order at a time; the parser REJECTS != 1 in M5.
        for bad in (2, 5, 99):
            cfg = _committed_config()
            cfg["agent_rules"]["execution"]["max_open_orders"] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                ExecutionConfig.from_config(cfg)

    def test_account_refresh_interval_must_be_under_5000(self):
        # Must outrun ACCOUNT_FRESHNESS_TTL_MS at parse (§B): 5000 itself rejects.
        for bad in (5000, 5001, 999999):
            cfg = _committed_config()
            cfg["agent_rules"]["execution"]["account_refresh_interval_ms"] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                ExecutionConfig.from_config(cfg)
        cfg = _committed_config()
        cfg["agent_rules"]["execution"]["account_refresh_interval_ms"] = 4999
        parsed = ExecutionConfig.from_config(cfg)
        self.assertEqual(parsed.account_refresh_interval_ms, 4999)

    def test_order_poll_interval_ceiling_clamped_at_1000(self):
        for raw, effective in ((1, 1), (500, 500), (999, 999), (1000, 1000),
                               (1001, 1000), (5000, 1000)):
            cfg = _committed_config()
            cfg["agent_rules"]["execution"]["order_poll_interval_ms"] = raw
            parsed = ExecutionConfig.from_config(cfg)
            self.assertEqual(parsed.order_poll_interval_ms, effective, str(raw))

    def test_latency_budget_must_be_json_int(self):
        for bad in (True, False, 250.0, "250", None):
            cfg = _committed_config()
            cfg["agent_rules"]["latency_budget_ms"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                ExecutionConfig.from_config(cfg)
        cfg = _committed_config()
        del cfg["agent_rules"]["latency_budget_ms"]
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)

    def test_latency_budget_zero_or_negative_raises(self):
        # M5C-T2: the floor only applies to values that PARSE; 0/negative is a
        # startup ValueError, never silently floored.
        for bad in (0, -1, -250):
            cfg = _committed_config()
            cfg["agent_rules"]["latency_budget_ms"] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                ExecutionConfig.from_config(cfg)

    def test_effective_latency_budget_is_max_of_parsed_and_floor(self):
        for raw, effective in ((1, 250), (100, 250), (249, 250), (250, 250),
                               (251, 251), (1000, 1000)):
            cfg = _committed_config()
            cfg["agent_rules"]["latency_budget_ms"] = raw
            parsed = ExecutionConfig.from_config(cfg)
            self.assertEqual(parsed.effective_latency_budget_ms, effective, str(raw))
            self.assertEqual(parsed.effective_latency_budget_ms,
                             max(raw, LATENCY_BUDGET_MIN_MS))


class TestLatencyPolarityTraps(unittest.TestCase):
    """The TWO pinned M5C-T2 cases: `latency_budget_ms` min()-merge polarity is
    INVERTED (lowering it loosens realism), and `tighten_only_merge` min()s non-bool
    numerics — so the hostile overlay DOES take effect at merge. The defense is the
    floor (values that parse) or the parse error (values that don't), NEVER the merge."""

    def test_hostile_latency_1_overlay_merges_to_1_then_floored_to_250(self):
        committed = _committed_config()
        overlay = {"agent_rules": {"latency_budget_ms": 1}}
        merged = agent_config.tighten_only_merge(committed, overlay)
        self.assertEqual(merged["agent_rules"]["latency_budget_ms"], 1)  # min() took it
        parsed = ExecutionConfig.from_config(merged)
        self.assertEqual(parsed.effective_latency_budget_ms, 250)        # the floor

    def test_hostile_latency_0_overlay_merges_to_0_then_parser_raises(self):
        committed = _committed_config()
        overlay = {"agent_rules": {"latency_budget_ms": 0}}
        merged = agent_config.tighten_only_merge(committed, overlay)
        self.assertEqual(merged["agent_rules"]["latency_budget_ms"], 0)  # min() took it
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(merged)                          # fail-loud


class TestRulesHashProvenance(unittest.TestCase):
    def test_changing_any_execution_leaf_changes_rules_hash(self):
        committed = _committed_config()
        committed_hash = agent_config.rules_hash(committed)
        parsed_committed = ExecutionConfig.from_config(committed)
        self.assertEqual(parsed_committed.rules_hash, committed_hash)
        # max_open_orders != 1 cannot parse (FD-M5-21), so its hash divergence is
        # asserted on the assembled dict directly; the three others through the parser.
        for key, new_value in (("slippage_cap_bps", 26),
                               ("order_poll_interval_ms", 501),
                               ("account_refresh_interval_ms", 2501),
                               ("max_open_orders", 2)):
            mutated = _committed_config()
            mutated["agent_rules"]["execution"][key] = new_value
            self.assertNotEqual(agent_config.rules_hash(mutated), committed_hash, key)
            if key != "max_open_orders":
                parsed = ExecutionConfig.from_config(mutated)
                self.assertNotEqual(parsed.rules_hash, committed_hash, key)
                self.assertEqual(parsed.rules_hash, agent_config.rules_hash(mutated), key)

    def test_rules_hash_is_over_the_dict_received_pre_substitution(self):
        # M5C-S4: this module hashes exactly what the caller passes (the
        # PRE-substitution assembly); same dict in => same hash out, deterministically.
        cfg = _committed_config()
        first = ExecutionConfig.from_config(cfg)
        second = ExecutionConfig.from_config(json.loads(json.dumps(cfg)))
        self.assertEqual(first.rules_hash, second.rules_hash)
        self.assertEqual(first, second)


class TestSignalOneSourceReread(unittest.TestCase):
    """`quote_staleness_ms_max` / `spread_bps_max` are re-read from the signal block
    (ONE source, §B) — committed as STRINGS (FD-7), parsed like SignalConfig does."""

    def test_values_re_read_from_committed_signal_strings(self):
        cfg = _committed_config()
        signal = cfg["agent_rules"]["signal"]
        self.assertIsInstance(signal["quote_staleness_ms_max"], str)
        self.assertIsInstance(signal["spread_bps_max"], str)
        parsed = ExecutionConfig.from_config(cfg)
        self.assertEqual(parsed.quote_staleness_ms_max,
                         int(signal["quote_staleness_ms_max"]))
        self.assertEqual(parsed.spread_bps_max, Decimal(signal["spread_bps_max"]))

    def test_missing_signal_block_raises(self):
        cfg = _committed_config()
        del cfg["agent_rules"]["signal"]
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(cfg)

    def test_malformed_signal_values_raise(self):
        for key, bad in (("quote_staleness_ms_max", "abc"),
                         ("quote_staleness_ms_max", 2000),   # non-string (FD-7)
                         ("quote_staleness_ms_max", "0"),
                         ("spread_bps_max", "NaN"),
                         ("spread_bps_max", 50)):             # non-string (FD-7)
            cfg = _committed_config()
            cfg["agent_rules"]["signal"][key] = bad
            with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                ExecutionConfig.from_config(cfg)


class TestCodeConstants(unittest.TestCase):
    def test_code_constants_pinned_to_contract_values(self):
        # FD-M5-29: inverted-polarity / safety-quantum values are CODE CONSTANTS,
        # never config knobs.
        self.assertEqual(LATENCY_BUDGET_MIN_MS, 250)
        self.assertEqual(MIN_REQUOTE_DELTA_MS, 1)
        self.assertEqual(OPEN_TOKEN_TTL_MS, 2000)
        self.assertEqual(RISK_VERDICT_TTL_MS, 2000)
        self.assertEqual(ORDER_POLL_INTERVAL_MS_MAX, 1000)
        self.assertEqual(SUBMIT_RECOVERY_ATTEMPTS, 3)
        self.assertEqual(DIVERGENCE_ALERT_BPS, Decimal("10"))
        self.assertIsInstance(DIVERGENCE_ALERT_BPS, Decimal)
        self.assertEqual(FLATTEN_CAP_BPS, Decimal("100"))
        self.assertIsInstance(FLATTEN_CAP_BPS, Decimal)
        self.assertEqual(DEPTH_FRESHNESS_TTL_MS, 2000)


# ---------------------------------------------------------------------------
# exec_reasons.py — §2 frozen vocabularies (literals copied from the contract)
# ---------------------------------------------------------------------------

_TERMINAL = frozenset({"run_gates_off", "kill_switch_halted", "missing_decision_stamp"})

_COLLECTED = frozenset({
    # candidate
    "order_matrix_unsupported", "invalid_lot",
    # strategy_gate
    "strategy_not_paper_eligible", "backtest_artifact_missing",
    "artifact_key_mismatch", "artifact_hash_invalid",
    "synthetic_requires_fake_broker", "fake_broker_requires_synthetic",
    # inflight
    "open_order_in_flight",
    # latency
    "latency_not_elapsed", "requote_not_later", "epoch_changed",
    # quote (M3 strings verbatim)
    "quote_missing", "quote_stale", "quote_crossed", "quote_locked",
    "quote_one_sided", "quote_nonfinite", "quote_nonpositive", "spread_too_wide",
    # market_state
    "market_state_not_tradable", "market_state_stale_default",
    "market_state_not_rth", "halt_luld_auction", "ca_blackout",
    # order
    "unpriceable_candidate", "invalid_tick", "not_marketable", "latency_lost_edge",
    # risk
    "risk_verdict_missing", "risk_verdict_stale", "risk_verdict_mismatch",
    "can_open_denied", "kill_generation_changed",
})

_CONSUME = frozenset({
    "preflight_runtime_unbound", "open_token_expired", "kill_generation_changed"})

_RESERVED = frozenset({"ssr_short_blocked", "locate_unavailable", "extended_hours_blocked"})

_STAGES = ("run_gates", "kill", "stamp", "candidate", "strategy_gate", "inflight",
           "latency", "quote", "market_state", "order", "risk")

_OTHER_VOCABS = {
    "ORDER_STATES": (ORDER_STATES, frozenset({
        "accepted", "partially_filled", "filled", "canceled", "expired",
        "rejected", "done_for_day", "pending_cancel", "unknown"})),
    "TERMINAL_STATES": (TERMINAL_STATES, frozenset({
        "filled", "canceled", "expired", "rejected", "done_for_day"})),
    "CANCEL_CAUSES": (CANCEL_CAUSES, frozenset({
        "epoch_changed", "halt_luld_auction", "market_state_not_tradable",
        "market_state_stale_default", "session_end", "kill_trip",
        "unexpected_status", "restart_unknown_state"})),
    "CANCEL_OUTCOMES": (CANCEL_OUTCOMES, frozenset({
        "cancel_submitted", "already_terminal", "cancel_rejected", "error"})),
    "CLOSE_REASONS": (CLOSE_REASONS, frozenset({
        "strategy_exit", "kill_flatten", "session_end", "synthetic_script",
        "operator"})),
    "REALISM_CLASSES": (REALISM_CLASSES, frozenset({
        "modeled_full", "modeled_partial", "modeled_unfillable"})),
    "MODELED_FILL_MODELS": (MODELED_FILL_MODELS, frozenset({
        "tob_l1_v1", "depth_vwap_l2_v2"})),
    "DIVERGENCE_FLAGS": (DIVERGENCE_FLAGS, frozenset({
        "aligned", "broker_optimistic", "broker_conservative", "unassessed"})),
    "FILL_POLICIES": (FILL_POLICIES, frozenset({
        "immediate_full", "partial_then_full", "never_fill", "reject_all"})),
    "BROKER_KINDS": (BROKER_KINDS, frozenset({
        "spy", "fake", "alpaca_paper", "alpaca_live"})),
    "STRATEGY_DECISION_ACTIONS": (STRATEGY_DECISION_ACTIONS, frozenset({
        "would_open", "would_close"})),
    "FILL_SOURCES": (FILL_SOURCES, frozenset({"alpaca_paper", "fake"})),
    "SUBMIT_RESOLUTIONS": (SUBMIT_RESOLUTIONS, frozenset({
        "adopted", "not_found", "offline_orphan"})),
}


class TestExecReasonVocabularies(unittest.TestCase):
    def test_exec_error_is_a_value_error(self):
        self.assertTrue(issubclass(ExecError, ValueError))

    def test_component_sets_match_contract_verbatim(self):
        self.assertEqual(TERMINAL_PREFLIGHT_REASONS, _TERMINAL)
        self.assertEqual(COLLECTED_PREFLIGHT_REASONS, _COLLECTED)
        self.assertEqual(CONSUME_REASONS, _CONSUME)
        self.assertEqual(RESERVED_PREFLIGHT_REASONS, _RESERVED)

    def test_union_arithmetic_is_42(self):
        # §2.1 frozen arithmetic: 3 terminal + 34 collected + 2 consume-only
        # (kill_generation_changed is SHARED with collected) + 3 reserved = 42.
        self.assertEqual(len(TERMINAL_PREFLIGHT_REASONS), 3)
        self.assertEqual(len(COLLECTED_PREFLIGHT_REASONS), 34)
        self.assertEqual(len(CONSUME_REASONS), 3)
        self.assertEqual(len(RESERVED_PREFLIGHT_REASONS), 3)
        self.assertEqual(COLLECTED_PREFLIGHT_REASONS & CONSUME_REASONS,
                         frozenset({"kill_generation_changed"}))
        self.assertEqual(TERMINAL_PREFLIGHT_REASONS & COLLECTED_PREFLIGHT_REASONS,
                         frozenset())
        self.assertEqual(TERMINAL_PREFLIGHT_REASONS & CONSUME_REASONS, frozenset())
        self.assertEqual(
            RESERVED_PREFLIGHT_REASONS
            & (TERMINAL_PREFLIGHT_REASONS | COLLECTED_PREFLIGHT_REASONS | CONSUME_REASONS),
            frozenset())
        self.assertEqual(len(PREFLIGHT_REASONS), 42)
        self.assertEqual(
            PREFLIGHT_REASONS,
            TERMINAL_PREFLIGHT_REASONS | COLLECTED_PREFLIGHT_REASONS
            | CONSUME_REASONS | RESERVED_PREFLIGHT_REASONS)
        self.assertEqual(PREFLIGHT_REASONS, _TERMINAL | _COLLECTED | _CONSUME | _RESERVED)

    def test_stage_tuple_frozen_order(self):
        self.assertEqual(PREFLIGHT_STAGES, _STAGES)
        self.assertIsInstance(PREFLIGHT_STAGES, tuple)
        self.assertEqual(len(PREFLIGHT_STAGES), 11)

    def test_extra_reject_reasons_and_reject_stages(self):
        self.assertEqual(EXTRA_REJECT_REASONS,
                         frozenset({"no_price_for_cap", "broker_rejected"}))
        # §2.1: reject-row supplements are NOT in PREFLIGHT_REASONS.
        self.assertEqual(EXTRA_REJECT_REASONS & PREFLIGHT_REASONS, frozenset())
        self.assertEqual(
            REJECT_STAGES,
            frozenset(_STAGES) | {"consume", "broker", "reduce_pricing"})
        self.assertEqual(len(REJECT_STAGES), 14)

    def test_other_vocabularies_match_contract_verbatim(self):
        for name, (actual, expected) in _OTHER_VOCABS.items():
            self.assertEqual(actual, expected, name)
            self.assertIsInstance(actual, frozenset, name)

    def test_terminal_states_subset_and_unknown_never_terminal(self):
        self.assertTrue(TERMINAL_STATES <= ORDER_STATES)
        for non_terminal in ("unknown", "accepted", "partially_filled", "pending_cancel"):
            self.assertIn(non_terminal, ORDER_STATES)
            self.assertNotIn(non_terminal, TERMINAL_STATES)

    def test_require_reason_accepts_every_emittable_member(self):
        for code in sorted(_TERMINAL | _COLLECTED | _CONSUME):
            self.assertEqual(require_reason(code), code)

    def test_require_reason_raises_on_unknown(self):
        for bad in ("bogus", "", "RUN_GATES_OFF", "quote_stale "):
            with self.assertRaises(ExecError, msg=repr(bad)):
                require_reason(bad)

    def test_require_reason_raises_on_every_reserved_member(self):
        # M5C-T8: reserved-in-M5 strings are IN the frozenset but may never be
        # emitted; require_reason refuses them.
        for code in sorted(_RESERVED):
            self.assertIn(code, PREFLIGHT_REASONS)
            with self.assertRaises(ExecError, msg=code):
                require_reason(code)

    def test_require_stage_accepts_every_legal_reject_stage(self):
        for name in sorted(REJECT_STAGES):
            self.assertEqual(require_stage(name), name)

    def test_require_stage_raises_on_unknown(self):
        for bad in ("bogus", "", "preflight", "RUN_GATES"):
            with self.assertRaises(ExecError, msg=repr(bad)):
                require_stage(bad)

    def test_require_member_membership_and_error_message(self):
        self.assertEqual(require_member(ORDER_STATES, "filled", what="order state"),
                         "filled")
        with self.assertRaises(ExecError) as ctx:
            require_member(ORDER_STATES, "exploded", what="order state")
        self.assertIn("order state", str(ctx.exception))
        self.assertIn("exploded", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
