# Autonomous Paper Trader Design

**Status:** Approved for autonomous execution under the repository's standing
autonomy directive. This document does not authorize credentialed runs, gate
changes, production artifact writes, or live-money trading.

**Date:** 2026-07-09

## Goal

Turn the existing fail-closed M0-M7 spine into an unattended, evidence-producing
paper trader without weakening S1-S10, changing pinned M7 criteria, or coupling
offline strategy research to paper-runtime implementation.

## Definition Of Done

The project is an autonomous paper trader only when all of the following are
demonstrated against current code and runtime state:

1. A reviewed production artifact verifies `ok` for the exact
   `(strategy_id, rules_hash, data_pin)` triple. Staged M7d research remains a
   separate input to this gate and keeps its own predeclared go/no-go process.
2. The production paper composition can receive fresh quote, calendar, corporate
   action, halt, LULD, and SSR state without inferring unavailable status from
   price action. Unknown or stale state blocks opens.
3. Replay mode is credential- and network-inert by construction.
4. The broker remains position-of-record. Broker-only and local-only exposure
   both trigger blindness, reconciliation, and reduction safeguards.
5. Exactly one session owns a journal tree, including stale-lock reclaim and
   owner-verified release under concurrent startup.
6. Feed or status-stream loss cannot silently strand an opening order or
   position. Cleanup records cancel, reconcile, flatten/halt decisions, and the
   original failure.
7. A calendar-aware supervisor starts one idempotent session, handles bounded
   transient retry, writes append-only run evidence, and produces daily and
   weekly paper-phase reports.
8. Credentialed drills prove account access, order submit/cancel finality,
   status-feed coverage, quote-feed continuity, restart recovery, kill-switch
   behavior, and broker/journal reconciliation without live money.
9. The pinned paper edge criteria pass on complete live-like paper evidence.
10. Committed defaults remain `enabled=false`, `paper_trading.enabled=false`,
    `live_trading.enabled=false`, with all committed caps at zero. Runtime arming
    remains a separately authorized operator action.

## Program Decomposition

### Track R: Offline Research

M7d remains staged-only and independent of runtime hardening. It may run only
after the predeclared holdout is complete, Robin chooses M7d, and Robin gives the
separate credentialed-run go. A staged result never writes to
`artifacts/backtests/` and never arms paper mode.

### Track A: Correctness And Recovery

Close the review-proven correctness gaps before relying on unattended paper
operation:

- recompute semantic artifact criteria at verification/promotion;
- make run-lock reclaim and release owner-safe;
- force replay composition to omit broker credentials and network adapters;
- execute EOD/cleanup on feed exceptions;
- include broker-only exposure in blindness handling;
- require terminal cancel evidence from the Alpaca paper drill;
- preserve incremental journal replay equivalence under mutation and same-size
  replacement.

### Track B: Status Data Plane

`EQUS.MINI` remains the primary quote feed but has no `status` schema. A separate
vendor-neutral `StatusProvider` data plane supplies per-symbol halt, LULD, and SSR
state. The first production adapter targets Alpaca's market-data `statuses` and
`lulds` channels because Alpaca is already the selected paper broker.

The adapter is not the trading client. It owns its own subscription state,
freshness, reconnect epoch, symbol coverage, and normalization. A future
status-capable Databento dataset can implement the same interface without
changing the orchestrator.

Status rules are fail-closed:

- no status observation for a symbol means `UNKNOWN`;
- stale status or a disconnected status stream blocks new opens;
- conflicting sources resolve to the most restrictive state;
- calendar state never substitutes for symbol halt/LULD/SSR state;
- reductions remain available when opening is denied;
- status transition rows are journaled with source and timestamps.

### Track C: Autonomous Operations And Evidence

A calendar-aware session supervisor composes the validated quote feed, status
provider, orchestrator, and report writer. It starts at most one session per ET
trading date, retries only explicitly classified transient startup/feed errors,
and never retries safety, corruption, reconcile, or kill-switch failures.

Each attempt receives a unique run id and append-only report. A daily aggregate
records completeness, feed/status gaps, SOD/EOD results, broker/local divergence,
orders, fills, PnL split, kills, and attention markers. A weekly aggregator feeds
the existing pinned paper criteria and never converts missing/null evidence to
zero.

### Track D: Credentialed Validation And Arming

Credentials and subscriptions are external prerequisites, not committed code.
The final readiness sequence is:

1. read-only Alpaca paper account verification;
2. non-marketable submit/cancel drill with terminal-state proof;
3. Databento live quote verification;
4. Alpaca status/LULD coverage and disconnect verification;
5. replay and synthetic recovery drills;
6. reviewed paper caps commit;
7. runtime-only paper arming;
8. bounded paper canary, then full autonomous paper evidence collection.

No step authorizes live money.

## Runtime Architecture

```text
Databento EQUS.MINI quotes ----+
                               +--> readiness barrier --> orchestrator --> Alpaca paper
Alpaca status/LULD sidecar ----+          |                   |
                                          |                   +--> broker ledger
calendar + CA providers ------------------+                   +--> reduction/kill path
                                          |
                                          +--> append-only status and evidence journals

session supervisor --> run lock --> one session attempt --> daily aggregate --> weekly criteria
```

The readiness barrier is evaluated on every open decision. It requires fresh
quote, status, calendar, corporate-action, account, artifact, and risk state.
It is not cached across reconnect epochs.

## Failure Semantics

| Failure | New opens | Reductions | Required outcome |
| --- | --- | --- | --- |
| Quote/status stale or disconnected | Denied | Allowed | Journal attention; reconnect boundedly |
| Feed exception while flat | Denied | N/A | Cleanup, incomplete report, non-zero exit |
| Feed exception with exposure | Denied | Required | Cancel openings, reconcile, flatten/halt decision |
| Broker/account freshness lost | Denied | Allowed if broker reachable | Blindness timer uses broker and local exposure |
| Journal corruption | Denied | Operator-attended | No automatic retry or overwrite |
| Lock ownership conflict | Denied | N/A | One owner proceeds; loser exits distinctly |
| Artifact semantic mismatch | Denied | N/A | Promotion/readiness fails closed |
| Drill order unresolved or filled | Denied | Operator-attended | Failure/attention, never `ok` |

## Artifact Trust Boundary

The reviewed writer remains the primary builder and already evaluates the pinned
criteria. `verify_artifact` becomes an independent promotion check: after schema,
hash, and triple validation it re-runs the canonical semantic evaluator over the
actual metrics. Hash equality proves payload consistency, not provenance.

Cross-sectional family gates remain strategy-specific. Before M7d promotion,
the artifact schema must carry sufficient hash-bound evidence to verify the
equal-weight benchmark and breadth/concentration rules independently.

## Testing Strategy

Every behavior change follows red-green-refactor. Required layers are:

- unit regressions for each reproduced defect;
- deterministic concurrency interleavings for lock reclaim/release;
- production-composition tests proving replay cannot construct network clients;
- exception-path tests proving cleanup and evidence emission;
- broker-only/local-only/divergent exposure cases;
- status freshness, reconnect, conflict, and missing-symbol state-machine tests;
- full-session replay and synthetic paper E2E;
- credentialed drills kept outside the offline suite;
- the full offline suite after every implementation wave.

## Rollout And Stop Conditions

Track A and M7d may proceed independently. Track B can be implemented without
credentials by using scripted fakes, but paper opening remains blocked until the
real subscription is verified. Track C may run in observe/replay mode before
arming.

Promotion requires Track A plus a passing reviewed artifact. Autonomous paper
arming additionally requires Tracks B-C and the credentialed Track D drills.
M8 remains blocked until the pinned realized paper edge criteria pass.

Any change that weakens committed gates, pinned thresholds, replay isolation,
broker-as-position-of-record, or reduction availability is out of scope and must
stop for a separate explicit decision.
