# M0 — Skeleton + Abstractions Implementation Plan

- **Date:** 2026-06-08
- **Status:** **DONE** — implemented & hardened 2026-06-08 (`4230c8f` feat, `5e45bf4` harden; 113 tests; marked done in `c1307ae`)
- **Parent spec:** `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`
- **Milestone goal:** create the repo skeleton + safety spine with committed fail-closed defaults. **No real broker order may be submitted.** Every acceptance command must actually run on a bare checkout.

## Scope

Asset-agnostic foundations only:

- Build/import bootstrap so the printed unittest commands resolve on a bare repo (no feature code without this).
- `Broker` interface + spy/no-op `AlpacaPaperBroker` stub; no credential use, no network, no `alpaca-py` import at module load.
- `MarketDataTransport` interface + deterministic fake transport. (Canonical name: `MarketDataTransport`, used in all docs.)
- Deterministic JSONL writer: row hashes, Decimal-as-string, per-stream writer lock, correlation IDs (`run_id`/`decision_id`/`order_id`) + monotonic `seq`, partial-line replay behavior.
- Config/gates: identity-strict booleans, tighten-only merge, `rules_hash`, committed fail-closed defaults.
- `OpenPreflightToken` / `ReduceOnlyPreflightToken` typed contracts (reject-all stubs; full preflight logic deferred to M5). S1 forbids *opening/increasing* submits; the reduce-only path stays reachable for held positions (kill-switch/halt/close).
- `kill_switch.py`: reduce-only state machine (contract-level; live flatten proof in M8).
- Dashboard stub on `127.0.0.1` only + path-traversal sandbox test.
- Charter files restating the §12 hard boundaries so any future coding agent inherits fail-closed defaults.
- `.secrets/` layout doc + a test enforcing no-network / no-creds in the suite.
- Two-key arming seam stub (construct-with-one-key fails).

**Out of scope (later milestones):** real Databento/Alpaca calls (M1/M5), full `execution_preflight` logic + S4 (M5), session/calendar (M2), risk sub-gates (M4).

## Dependencies / environment

- `requirements.txt` is created at M0 but **M0 is stdlib-only** — journal/config/gates/dashboard + the spy/no-op broker need no third-party packages, and the spy broker imports **no** `alpaca-py`. Third-party deps are pinned to **exact versions in the milestone that first imports them**: `databento` + `exchange_calendars` in **M1**, `alpaca-py` in **M5** (the real Alpaca adapter). This keeps M0's bare-checkout acceptance commands dependency-free.
- Install step mirrors Polymarket: `python3 -m pip install -r requirements.txt`.

## Import / path convention (build-blocker fix)

The dotted-path unittest commands require import resolution on a bare repo. Adopt the Polymarket convention explicitly:

- `tests/__init__.py` (for `python3 -m unittest`) and a repo-root `conftest.py` (pytest shim) each insert only `<repo>/scripts` onto `sys.path`; the repo root itself is not added.
- Add `__init__.py` to: `scripts/agent/`, `scripts/agent/broker/`, `scripts/agent/marketdata/`, `tests/`, `tests/agent/`, `tests/lib/`.
- Document the import-root contract (sys.path bootstrap vs editable install) so M1 (`scripts/recorder/`) follows the same rule.

## Files to create

**Charter / governance** — **seeded 2026-06-08** (ahead of M0 code), maintained through M0
- `AGENTS.md`, `CLAUDE.md`, `PLAN.md`, `MEMORY.md` (rewritten from Polymarket for the stocks posture; CLAUDE.md restates the §12 hard boundaries, live-gate OFF, two-key arming, kill-switch). Keep in sync as M0 lands real config/code.

**Config**
- `config/agent_rules.json`, `config/risk_rules.json`, `config/data_retention.json`
- `requirements.txt`
- `tests/__init__.py` (bootstrap) + repo-root `conftest.py` (pytest shim) + the `__init__.py` set (above)

**Agent skeleton**
- `scripts/agent/broker/base.py` (`Broker` Protocol; `submit_order` requires a `PreflightToken` — the base type, with concrete `OpenPreflightToken` / `ReduceOnlyPreflightToken` kinds)
- `scripts/agent/broker/alpaca.py` (spy/no-op `AlpacaPaperBroker`; lazy `alpaca-py` import; live broker absent until M8)
- `scripts/agent/marketdata/base.py` (`MarketDataTransport` Protocol)
- `scripts/agent/config.py`, `scripts/agent/gates.py`
- `scripts/agent/journal.py`, `scripts/agent/serializer.py` (Decimal-strict; `BrokerUSD`/`ModeledUSD` newtypes)
- `scripts/agent/execution_preflight.py` (typed `OpenPreflightToken` + `ReduceOnlyPreflightToken`; reject-all stubs; raise until M5)
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
- (journal crash / partial-write cases are built at runtime in a tempdir by `test_journal_replay.py` — no committed journal fixtures)

## Safety invariants covered

| Invariant | Test target |
|-----------|-------------|
| S1 nothing opens — no opening/increasing order minted; zero total submits on committed config (no positions → nothing to flatten) | `tests/agent/test_config_canary.py::TestCommittedConfigCanary::test_no_opening_submit_and_zero_total_on_committed_config` |
| S2 Decimal / no float NaN/Inf (+ newtype separation) | `tests/agent/test_serializer_decimal_strict.py` (incl. `test_modeled_price_cannot_write_broker_field`) |
| S3 replay + partial tail | `tests/agent/test_journal_replay.py` |
| S6 correlation IDs + monotonic seq | `tests/agent/test_journal_replay.py::test_rows_require_correlation_and_monotonic_seq` |
| S8 kill-switch reduce-only (mints only `ReduceOnlyPreflightToken`, never an open) | `tests/agent/test_kill_switch.py` (incl. `test_all_flatten_orders_are_reducing`, `test_one_bad_position_does_not_freeze_or_skip_the_rest`) |
| Preflight chokepoint (contract) | `tests/agent/test_preflight_token.py` (`submit_order` needs a valid token; `OpenPreflightToken` reject-all when disabled; `ReduceOnlyPreflightToken` only for a held position + decreasing order) |
| Config integrity (§4.7 tighten-only + rules_hash) | `tests/agent/test_config_merge.py` |
| Dashboard sandbox (security) | `tests/agent/test_dashboard_sandbox.py` (traversal, absolute, symlink, MAX_FILE_BYTES, 127.0.0.1 bind) |
| No-network / no-creds | `tests/agent/test_no_network_no_creds.py` (suite green with `.secrets/` absent; stub does zero I/O) |
| Two-key arming seam | `tests/agent/test_two_key_arming.py::TestConstructLiveBroker::test_one_key_cannot_construct_live_broker` |

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
  tests.agent.test_two_key_arming \
  tests.agent.test_gates \
  tests.agent.test_alpaca_spy \
  tests.agent.test_marketdata_transport
```

(Or simply `python3 -m unittest discover -s tests -p 'test_*.py' -t .` — 113 tests.)

## Stop condition

M0 is complete only when the command above exits 0 **on a bare checkout** and the committed config still has:

- `agent_rules.enabled = false`
- `agent_rules.paper_trading.enabled = false`
- `risk_rules.live_trading.enabled = false`

and `execution_preflight.py` is a typed reject-all stub (full S4 verification remains in M5).
