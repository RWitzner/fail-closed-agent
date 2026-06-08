# M0 — Skeleton + Abstractions Implementation Plan

- **Date:** 2026-06-08
- **Status:** Draft plan for review (reconciled with adversarial multi-lens review 2026-06-08)
- **Parent spec:** `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`
- **Milestone goal:** create the repo skeleton + safety spine with committed fail-closed defaults. **No real broker order may be submitted.** Every acceptance command must actually run on a bare checkout.

## Scope

Asset-agnostic foundations only:

- Build/import bootstrap so the printed unittest commands resolve on a bare repo (no feature code without this).
- `Broker` interface + spy/no-op `AlpacaPaperBroker` stub; no credential use, no network, no `alpaca-py` import at module load.
- `MarketDataTransport` interface + deterministic fake transport. (Canonical name: `MarketDataTransport`, used in all docs.)
- Deterministic JSONL writer: row hashes, Decimal-as-string, per-stream writer lock, correlation IDs (`run_id`/`decision_id`/`order_id`) + monotonic `seq`, partial-line replay behavior.
- Config/gates: identity-strict booleans, tighten-only merge, `rules_hash`, committed fail-closed defaults.
- `PreflightToken` typed contract (reject-all stub; full preflight logic deferred to M5).
- `kill_switch.py`: reduce-only state machine (contract-level; live flatten proof in M8).
- Dashboard stub on `127.0.0.1` only + path-traversal sandbox test.
- Charter files restating the §12 hard boundaries so any future coding agent inherits fail-closed defaults.
- `.secrets/` layout doc + a test enforcing no-network / no-creds in the suite.
- Two-key arming seam stub (construct-with-one-key fails).

**Out of scope (later milestones):** real Databento/Alpaca calls (M1/M5), full `execution_preflight` logic + S4 (M5), session/calendar (M2), risk sub-gates (M4).

## Dependencies / environment

- `requirements.txt` with **exact pins** (not floors). M0 surface is stdlib-only for journal/config; pin `alpaca-py==<X.Y.Z>` for the broker adapter **but the M0 stub must not import it at module load** (import lazily inside the live path only, which M0 never exercises). Databento + `exchange_calendars` are introduced in M1, not pinned here.
- Install step mirrors Polymarket: `python3 -m pip install -r requirements.txt`.

## Import / path convention (build-blocker fix)

The dotted-path unittest commands require import resolution on a bare repo. Adopt the Polymarket convention explicitly:

- `tests/conftest.py` (or a `sitecustomize`/bootstrap) inserts repo `ROOT` and `ROOT/scripts` onto `sys.path` (`ROOT = Path(__file__).resolve().parents[1]`).
- Add `__init__.py` to: `scripts/agent/`, `scripts/agent/broker/`, `scripts/agent/marketdata/`, `tests/`, `tests/agent/`, `tests/lib/`.
- Document the import-root contract (sys.path bootstrap vs editable install) so M1 (`scripts/recorder/`) follows the same rule.

## Files to create

**Charter / governance**
- `AGENTS.md`, `CLAUDE.md`, `PLAN.md`, `MEMORY.md` (seeded from Polymarket, rewritten for the stocks posture; CLAUDE.md must restate §12 hard boundaries, live-gate OFF, two-key arming, kill-switch drill).

**Config**
- `config/agent_rules.json`, `config/risk_rules.json`, `config/data_retention.json`
- `requirements.txt`
- `tests/conftest.py`, `__init__.py` set (above)

**Agent skeleton**
- `scripts/agent/broker/base.py` (`Broker` Protocol; `submit_order` requires a `PreflightToken`)
- `scripts/agent/broker/alpaca.py` (spy/no-op `AlpacaPaperBroker`; lazy `alpaca-py` import; live broker absent until M8)
- `scripts/agent/marketdata/base.py` (`MarketDataTransport` Protocol)
- `scripts/agent/config.py`, `scripts/agent/gates.py`
- `scripts/agent/journal.py`, `scripts/agent/serializer.py` (Decimal-strict; `BrokerUSD`/`ModeledUSD` newtypes)
- `scripts/agent/execution_preflight.py` (typed `PreflightToken` + reject-all stub; raises until M5)
- `scripts/agent/kill_switch.py` (reduce-only state machine)
- `dashboard/app.py` (stub; `_safe_workspace_path` sandbox; binds 127.0.0.1)
- `.secrets/README.md` (layout: exact filenames for Alpaca key/secret, Databento key)

**Tests**
- `tests/agent/test_config_canary.py`, `tests/agent/test_config_merge.py`
- `tests/agent/test_serializer_decimal_strict.py`, `tests/agent/test_journal_replay.py`
- `tests/agent/test_kill_switch.py`, `tests/agent/test_preflight_token.py`
- `tests/agent/test_dashboard_sandbox.py`, `tests/agent/test_no_network_no_creds.py`
- `tests/agent/test_two_key_arming.py`
- `tests/lib/fakes.py` (shared spy broker, fake transport, fake clock)

## Fixtures

- `tests/fixtures/config/committed_fail_closed_agent_rules.json`
- `tests/fixtures/config/committed_fail_closed_risk_rules.json`
- `tests/fixtures/config/malformed_live_block.json`
- `tests/fixtures/config/local_armed_overlay.json` (documents what an armed working-tree overlay looks like; never committed as live)
- `tests/fixtures/journal/truncated_tail.positions.jsonl`, `tests/fixtures/journal/valid_positions.jsonl`

## Safety invariants covered

| Invariant | Test target |
|-----------|-------------|
| S1 nothing opens — `submit_order` never called (open/close/cancel/flatten) | `tests/agent/test_config_canary.py::TestCommittedConfigCanary::test_zero_submits_at_broker_boundary_all_paths` |
| S2 Decimal / no float NaN/Inf (+ newtype separation) | `tests/agent/test_serializer_decimal_strict.py` (incl. `test_modeled_price_cannot_write_broker_field`) |
| S3 replay + partial tail | `tests/agent/test_journal_replay.py` |
| S6 correlation IDs + monotonic seq | `tests/agent/test_journal_replay.py::test_rows_require_correlation_and_monotonic_seq` |
| S8 kill-switch reduce-only state machine | `tests/agent/test_kill_switch.py` (incl. `test_kill_switch_never_emits_opening_order`) |
| Preflight chokepoint (contract) | `tests/agent/test_preflight_token.py::test_submit_requires_valid_token_and_disabled_rejects_all` |
| Config integrity (§4.7 tighten-only + rules_hash) | `tests/agent/test_config_merge.py` |
| Dashboard sandbox (security) | `tests/agent/test_dashboard_sandbox.py` (traversal, absolute, symlink, MAX_FILE_BYTES, 127.0.0.1 bind) |
| No-network / no-creds | `tests/agent/test_no_network_no_creds.py` (suite green with `.secrets/` absent; stub does zero I/O) |
| Two-key arming seam | `tests/agent/test_two_key_arming.py::test_live_broker_unconstructable_with_one_key` |

## Acceptance commands

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest \
  tests.agent.test_config_canary \
  tests.agent.test_config_merge \
  tests.agent.test_serializer_decimal_strict \
  tests.agent.test_journal_replay \
  tests.agent.test_kill_switch \
  tests.agent.test_preflight_token \
  tests.agent.test_dashboard_sandbox \
  tests.agent.test_no_network_no_creds \
  tests.agent.test_two_key_arming
```

## Stop condition

M0 is complete only when the command above exits 0 **on a bare checkout** and the committed config still has:

- `agent_rules.enabled = false`
- `agent_rules.paper_trading.enabled = false`
- `risk_rules.live_trading.enabled = false`

and `execution_preflight.py` is a typed reject-all stub (full S4 verification remains in M5).
