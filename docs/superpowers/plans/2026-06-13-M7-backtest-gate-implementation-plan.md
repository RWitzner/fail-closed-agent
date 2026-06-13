# M7 - Backtest Gate Implementation Plan

- **Date:** 2026-06-13
- **Status:** Offline build complete (2026-06-13): Waves 1-6 green, full suite 1669 tests.
- **Parent contract:** `docs/superpowers/specs/2026-06-13-M7-backtest-gate-contract.md`
- **Milestone goal:** build the anti-lookahead historical backtest gate and the first real paper-eligible
  directional strategy without weakening committed fail-closed defaults.

## Ground rules

- Follow TDD: write each failing test first, verify it fails for the expected reason, then implement.
- Do not flip committed run gates or risk caps.
- No network/credentials in the normal suite.
- Keep `artifacts/backtests/` production default, but all tests write artifacts to temp dirs unless a reviewed
  production artifact is intentionally added.
- Verify S1 after every wave that touches strategy, preflight, orchestrator, or artifacts.

## Wave 1 - Artifact gate v2 and cache-key hardening

Files:

- `scripts/agent/backtest_gate.py`
- `scripts/agent/orchestrator.py`
- `tests/agent/test_backtest_gate_m7.py`
- `tests/agent/test_orchestrator_m7_artifact_cache.py`
- update existing M5 tests only where expectations intentionally evolve.

TDD tests:

- v2 valid artifact returns `ok`.
- `metrics.basis != "execution_realistic_pnl"` returns `hash_invalid`.
- `metrics.pass is not True` returns `hash_invalid`.
- float anywhere in artifact returns `hash_invalid`.
- key mismatch still returns `key_mismatch`.
- artifact cache key is `(strategy_id, rules_hash, data_pin)`; a data-pin change calls verifier again.
- committed config plus valid artifact still submits zero orders.

Verification:

```bash
python3 -m unittest tests.agent.test_backtest_gate_m7 tests.agent.test_orchestrator_m7_artifact_cache
python3 -m unittest tests.agent.test_execution_preflight_m5 tests.agent.test_config_canary
```

## Wave 2 - Pure anti-lookahead backtest engine

Files:

- `scripts/agent/backtest_engine.py`
- `scripts/agent/backtest_metrics.py`
- `tests/agent/test_backtest_engine_m7.py`
- no committed historical fixture data in offline tier; tests build in-memory bars.

TDD tests:

- future receipt bars are rejected.
- equal watermark and as-of instants are accepted even with mixed timestamp surface forms.
- lexicographic timestamp trap is covered.
- horizon crossing RTH close is skipped, not filled.
- quote B must be eligible after latency and strictly uses the modeled basis.
- identical fixture run produces byte-identical artifact body with pinned `created_utc`.

Verification:

```bash
python3 -m unittest tests.agent.test_backtest_engine_m7
python3 -m unittest tests.agent.test_bar_series tests.agent.test_signal_snapshot tests.agent.test_calibration
```

## Wave 3 - `directional.momentum_v1`

Files:

- `scripts/agent/strategies/directional_momentum.py`
- `tests/agent/test_directional_momentum_m7.py`

TDD tests:

- emits one long-only BUY candidate when all feature/edge predicates pass.
- emits no candidate for weak momentum, negative z-return, zero realized vol, missing mid, or non-positive edge.
- candidate qty is whole-share Decimal and limit is on the M5 grid.
- strategy imports no broker/preflight/token/arming modules and performs no I/O.
- `paper_eligible=True` only reaches broker submission after the downstream S9 artifact gate verifies the scanned
  `(strategy_id,rules_hash,data_pin)`.

Verification:

```bash
python3 -m unittest tests.agent.test_directional_momentum_m7
python3 -m unittest tests.agent.test_synthetic_isolation tests.agent.test_config_canary
```

## Wave 4 - Artifact builder CLI and report/runbook

Files:

- `scripts/agent/__main__.py`
- `scripts/agent/backtest_builder.py`
- `scripts/agent/paper_phase_criteria.py`
- `docs/runbooks/m7-paper-edge-validation.md`
- `tests/agent/test_m7_backtest_cli.py`
- `tests/agent/test_m7_paper_phase_criteria.py`

TDD tests:

- CLI writes v2 artifact only when thresholds pass.
- CLI refuses to write into production `artifacts/backtests/` unless an explicit `--write-reviewed-artifact` flag
  is provided.
- criteria evaluator blocks each missing/failed paper-phase threshold.
- runbook names the exact post-M7 paper evidence required before M8.

Verification:

```bash
python3 -m unittest tests.agent.test_m7_backtest_cli tests.agent.test_m7_paper_phase_criteria
python3 -m unittest tests.agent.test_no_network_no_creds
```

## Wave 5 - End-to-end S9 integration

Files:

- `tests/agent/test_m7_s9_integration.py`
- targeted updates to `tests/agent/test_synthetic_isolation.py`, `tests/agent/test_synthetic_e2e.py`, and
  `tests/agent/test_config_canary.py` if artifact allowlist behavior changes.

TDD tests:

- real strategy + missing artifact rejects before broker submit.
- real strategy + mismatched artifact rejects with `artifact_key_mismatch`.
- real strategy + valid artifact reaches later gates under permissive fixture config.
- committed config + valid artifact still produces zero broker submits.
- synthetic path remains confined to `FakeBroker` and does not consult real artifacts.

Verification:

```bash
python3 -m unittest \
  tests.agent.test_m7_s9_integration \
  tests.agent.test_synthetic_isolation \
  tests.agent.test_synthetic_e2e \
  tests.agent.test_config_canary
```

## Wave 6 - Closeout and full verification

Tasks:

- Update `PLAN.md` with exact M7 build status, test count, and artifact state.
- If a reviewed artifact is committed, replace the M5 `.gitkeep`-only assertion with an allowlist assertion.
- If historical credentials/data are unavailable, document tier-2 deferral and keep no real artifact committed.
- Run affected block, compile, diff check, and full suite.

Verification:

```bash
python3 -m unittest \
  tests.agent.test_backtest_gate_m7 \
  tests.agent.test_orchestrator_m7_artifact_cache \
  tests.agent.test_backtest_engine_m7 \
  tests.agent.test_directional_momentum_m7 \
  tests.agent.test_m7_paper_phase_criteria \
  tests.agent.test_m7_backtest_cli \
  tests.agent.test_m7_s9_integration
python3 -m unittest discover -s tests -p 'test_*.py' -t .
python3 -m compileall scripts tests
git diff --check
```

## Stop condition

M7 is complete only when one of these is true:

- **Artifact path complete:** reviewed v2 artifact for `directional.momentum_v1` is committed, full suite is green,
  committed config canary is still zero-submit, and paper-phase runbook criteria are pinned.
- **Artifact deferred:** offline engine/gate/strategy are built and green, but no artifact is committed because
  historical data/credentials are unavailable or metrics fail. In that case the real strategy remains unable to
  open and `PLAN.md` records the deferral explicitly.

Current M7 outcome: artifact deferred. No reviewed production artifact is committed; `PLAN.md` records the
fail-closed default artifact state and the separate historical reviewed-artifact tier.
