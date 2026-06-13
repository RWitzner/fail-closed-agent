# M7b Strategy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second reviewed directional momentum strategy version and replay it through the existing M7 historical artifact gate without relaxing any pinned criteria.

**Architecture:** Keep `directional.momentum_v1` byte-compatible for existing artifacts and tests. Add `directional.momentum_v2` in the same pure strategy module with stricter trend, spread, and edge predicates, then make the historical artifact builder select an explicit strategy id so artifacts remain hash-bound to the real strategy version.

**Tech Stack:** Python stdlib, `unittest`, existing M3/M7 signal/backtest/artifact modules.

---

## Files

- Modify: `scripts/agent/strategies/directional_momentum.py`
  - Preserve `MomentumV1Strategy`.
  - Add `MomentumV2Strategy` with no I/O, no broker/preflight imports, long-only BUY output, and `strategy_id = "directional.momentum_v2"`.
  - Add `strategy_for_id(strategy_id)` to centralize allowed strategy ids.
- Modify: `scripts/agent/backtest_historical.py`
  - Accept `strategy_id` in `run_historical_backtest()` and `write_m7_historical_artifact()`.
  - Use `strategy_for_id(strategy_id)` and write `<strategy_id>.json`.
- Modify: `scripts/agent/__main__.py`
  - Add `--strategy-id` to `m7-historical-artifact`, defaulting to v1 for compatibility.
- Modify: `tests/agent/test_directional_momentum_m7.py`
  - Add RED tests for v2 positive path, weak/trend/wide-spread rejections, and pure import surface.
- Modify: `tests/agent/test_m7_historical_artifact.py`
  - Add RED tests proving the historical flow writes verifier-compatible v2 artifacts and rejects unknown strategy ids.
- Add: `docs/superpowers/specs/2026-06-13-M7b-strategy-hardening-contract.md`
  - Pin the v2 hypothesis and the stop rule: no production artifact unless a reviewed historical run verifies `ok`.

## Tasks

### Task 1: Contract

- [x] **Step 1:** Write the M7b contract documenting that thresholds stay pinned, v1 remains compatible, v2 is a stricter new strategy version, and M8/paper remain blocked unless the reviewed artifact verifies `ok`.

- [x] **Step 2:** Re-read the contract and remove any wording that permits threshold tuning, cherry-picking, production artifact writes, or live/paper gate flips.

### Task 2: RED Tests

- [x] **Step 1:** Add `MomentumV2Strategy` tests before implementation.

- [x] **Step 2:** Add historical builder tests for explicit v2 strategy selection and unknown strategy rejection.

- [x] **Step 3:** Run:

```bash
python3 -m unittest tests.agent.test_directional_momentum_m7 tests.agent.test_m7_historical_artifact
```

Expected: fail because v2/strategy selector does not exist yet.

### Task 3: GREEN Implementation

- [x] **Step 1:** Implement `MomentumV2Strategy` minimally.

- [x] **Step 2:** Wire `strategy_for_id()` into `run_historical_backtest()` and `write_m7_historical_artifact()`.

- [x] **Step 3:** Add `--strategy-id` to the historical artifact CLI.

- [x] **Step 4:** Re-run:

```bash
python3 -m unittest tests.agent.test_directional_momentum_m7 tests.agent.test_m7_historical_artifact
```

Expected: pass.

### Task 4: Historical Replay

- [x] **Step 1:** Run `m7-historical-artifact --strategy-id directional.momentum_v2` against each existing local broader and holdout manifest under `reports/m7_historical_runs/`.

- [x] **Step 2:** Write a failure/success review markdown file summarizing symbol-level metrics and whether a production artifact was written.

### Task 5: Verification

- [x] **Step 1:** Run targeted M7 strategy/artifact tests.

- [x] **Step 2:** Run:

```bash
python3 -m compileall scripts tests
git diff --check
python3 -m unittest discover -s tests -p 'test_*.py' -t .
```

- [x] **Step 3:** Report the exact artifact outcome. If criteria fail, leave `artifacts/backtests/` unchanged and state that paper/M8 remain blocked.
