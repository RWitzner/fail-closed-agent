# Seven patterns for an agent that can spend money

This document is written for someone building an autonomous agent that touches something irreversible — money,
a production deploy, a customer's data, an API with a bill attached. It is not written for traders. Trading is
just the domain that forced these patterns; none of them are about markets.

The context that produced them: an agent that runs unattended, all day, holding credentials that can move
money, in an environment where being wrong is not a stack trace but a position. The design question was never
"how do we make it trade well" — it was **"how do we make certain it cannot do the thing we did not authorise,
even when it is broken, even when its inputs are lying, even when we are asleep"**.

Each pattern below has the same shape: the failure it prevents, how it is implemented here, where the code is,
and the test that pins it. **Where a pattern is weaker than it sounds, that is stated.** An architecture
document that only lists strengths is marketing.

A note on what "verified" means here: every file and symbol referenced below exists in this repository and every
named test runs in the offline suite (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`, 2000 tests,
no install, no network, no credentials).

---

## 1. Two-key arming

**The failure it prevents.** A single edit — one commit, one environment variable, one merged pull request —
turning a safe system into a live one. This is the most common way agent systems go wrong: not a dramatic
exploit, but somebody flipping a boolean that turned out to be the only thing standing between "simulation" and
"production".

**The pattern.** Authority to touch real money requires **two independent keys that cannot both be supplied by
the same actor in the same act**:

- **Key A** is a flag in the committed configuration — visible in git, visible in review, visible in a diff.
- **Key B** is a runtime secret that lives outside the repository and is never committed.

A commit alone cannot arm the system, because it cannot produce key B. A leaked secret alone cannot arm the
system, because it cannot produce key A without a visible commit. Arming becomes an event that leaves evidence
in two places under two different kinds of control.

**Where it is.** `scripts/agent/arming.py` — `two_key_armed()` (line 19) computes `key_a and key_b`, and
`construct_live_broker()` (line 25) raises `ArmingError` unless both are present. Key A is read
identity-strictly: the config value must be the boolean `True`, not a truthy string, not `1`, not `"true"`.
That detail matters more than it looks — most accidental arming in configuration systems comes from a truthy
value sliding through a loose check.

**The test.** `tests/agent/test_two_key_arming.py`: `test_committed_config_is_not_armed`,
`test_key_a_only_is_not_armed`, `test_key_b_only_is_not_armed`, `test_both_keys_arms`,
`test_config_flag_must_be_identity_true`, `test_one_key_cannot_construct_live_broker`.

**Where it is weaker than it sounds.** The AND only governs *live* capital. For *paper* mode, the runtime
secret file alone opens the run gates — `assemble_gates_view()` in `scripts/agent/secrets_runtime.py` (line 84)
*replaces* the committed `false` rather than AND-ing with it. So paper is single-key. That was a deliberate
choice (paper cannot lose money) but it is an asymmetry worth knowing about if you copy the pattern into a
domain where the "safe" mode is not actually safe.

---

## 2. Chokepoint plus capability token

**The failure it prevents.** Safety checks that live *next to* the dangerous call instead of *in front of* it.
If your validation is a function that callers are expected to call first, then every new code path is a new
opportunity to forget, and the safety property degrades quietly with every refactor.

**The pattern.** There is exactly one function through which the agent can open or increase a position —
`submit_order()` — and its signature makes it **impossible to call without proof that the gates ran**. The proof
is an unforgeable capability token:

- Tokens cannot be constructed directly. The constructor requires a module-private sentinel (`_MINT`,
  `scripts/agent/execution_preflight.py` line 53); anything else raises `PreflightForgery` (line 96).
- Tokens cannot be copied, deep-copied, or pickled — `__copy__`, `__deepcopy__` and `__reduce__` all raise
  (lines 101, 104, 107). You cannot smuggle authority by duplicating an object you were legitimately handed.
- The token object does not *carry* its authority; authority lives in a module-private registry, so mutating
  the token's attributes does not rebind what it is allowed to do.
- Tokens are **typed by capability**: `OpenPreflightToken` (line 110) requires the full gate stack and rejects
  everything while the open run-gates are off; `ReduceOnlyPreflightToken` (line 114) requires a held position
  and only permits position-*decreasing* orders. Reducing risk is never blocked by the machinery that blocks
  increasing it — a distinction worth stealing for any domain with a "make it safe now" path.
- Tokens are single-use and consumed at the boundary (`consume`, line 149), with a re-check at consumption
  time so that state which changed between minting and submitting invalidates the token.

The generalisation: **make the dangerous operation take an argument that only the safety system can produce.**
Then "did we check?" is answered by the type system rather than by discipline.

**The test.** `tests/agent/test_preflight_token.py`: `test_token_cannot_be_constructed_with_wrong_mint`,
`test_subclass_cannot_be_constructed_with_wrong_mint`, `test_open_token_built_with_real_mint_is_unauthorized`,
`test_reduce_token_attributes_cannot_be_mutated_to_rebind`,
`test_mint_open_token_rejects_at_run_gates_on_committed_config`. Two canaries in
`tests/agent/test_config_canary.py` carry the composed property on the committed configuration:
`test_no_opening_submit_and_zero_total_on_committed_config` pins the mint path (every open preflight terminates
at `run_gates`, so the broker is never reached), and
`test_s1_canary_a_committed_config_spybroker_never_reached` runs the real orchestrator with an injected
`SpyBroker` and asserts zero submits of any kind at the Broker protocol boundary while the decision loop
actually ran. Two tested compositions — not a proof over all paths.

**Where it is weaker than it sounds.** Non-bypassability rests on inheritance from `BrokerBase`
(`scripts/agent/broker/base.py`) plus code review. Three wrappers legitimately override `submit_order`, and each
was checked to still require a token — but there is no static guard that would stop a fourth from not requiring
one. A stricter version of this pattern would enforce the signature with an AST check in the test suite.

Two other calls reach the broker and are deliberately **not** token-gated, so "one chokepoint" is a claim about
opening risk, not about all outside contact. `cancel_order` (`scripts/agent/broker/base.py`,
`scripts/agent/broker/alpaca.py`) sends a real cancel with no token, and the agent's own post-submit watcher
calls it — gating it would let the machinery that blocks *increasing* risk also block *reducing* it, which is
the failure this pattern exists to prevent. And the operator verifier `scripts/agent/verify_alpaca_paper.py`
submits a real one-share, intentionally non-marketable limit order and cancels it, outside the agent loop and
without a token; it is off by default (`--allow-order-drill`), pinned to the paper endpoint before the SDK is
even imported, treats a fill as a hard failure, and was run once. Neither can open or increase a position — but
both are writes to the outside world, and they belong in this list rather than only in the source.

---

## 3. Fail-closed on unknown state

**The failure it prevents.** Treating "I don't know" as "fine". Most agent incidents are not caused by an agent
acting on bad information; they are caused by an agent acting on *missing* information as though absence were
permission.

**The pattern.** Every external state has an explicit `UNKNOWN` member, and `UNKNOWN` is wired to the
**restrictive** branch — not as a convention, but as the enum's documented meaning:

```
scripts/agent/market_state.py
  line 45  HaltState.UNKNOWN     status feed unavailable/stale -> FAIL-CLOSED == HALTED-equivalent
  line 52  LuldState.UNKNOWN     band unknown/stale            -> FAIL-CLOSED restrictive
  line 58  SsrState.UNKNOWN      -> FAIL-CLOSED: treat as ACTIVE for short-side decisions
```

The decider is a one-way ladder that can only *tighten*: unknown session phase tightens to not-tradable
(line 280), `UNKNOWN` halt state is grouped with `HALTED` (line 293), `UNKNOWN` limit state is grouped with
`PAUSED` (line 301), an unknown price band tightens (line 320). There is no path back up. A stale data feed and
a genuinely halted market produce the same decision, which is the correct decision.

The same posture applies at seam level. An integration that has not been verified against the real vendor API
**raises** rather than proceeding: unverified seams are dead until a human has run the verification and
committed the result. When the status feed disconnects, the status plane composes to `DISCONNECTED`, which
means the agent keeps running and keeps journalling but **cannot open anything** — visible degradation instead
of silent capability loss.

**The test.** `tests/agent/test_market_state.py`: `test_vendor_halt_and_unknown_halt_block`,
`test_luld_paused_or_unknown_blocks`, `test_ssr_active_or_unknown_blocks_short`,
`test_closed_and_unknown_phase_block`. `tests/agent/test_status_plane.py`:
`test_band_decay_degrades_luld_to_unknown`, `test_alpaca_source_is_unverified_fail_closed`.

**Where it is weaker than it sounds.** One seam has had its fail-closed default deliberately inverted after
being verified against the live API: `scripts/agent/marketdata/alpaca_feed.py` line 129 defaults
`allow_unverified_live=True`. That is a documented, reviewed decision, but it is an example of how this pattern
erodes — the flip happens once, for a good reason, and then the default is no longer safe for the *next* person
who reuses the module.

---

## 4. Bounded blindness

**The failure it prevents.** An agent that loses its view of the authoritative state and keeps operating on its
last known picture. This is the failure mode that turns a small outage into a large loss: the agent is not
wrong, it is *blind*, and blindness has no error message.

**The pattern.** Blindness is treated as a **clock, not a condition**. Any read of authoritative state is either
*fresh* or it is not. A fresh read resets the clock. A non-fresh read — missing, stale, malformed, or with
implausible clock skew — starts it and keeps it running. Past a hard bound, if the agent still holds a position,
it **flattens and halts** rather than continuing to act blind.

Two design details that make this work:

- **The bound is on continuous blindness, not on a single read.** One dropped poll is not an incident; ninety
  seconds of dropped polls is.
- **Numeric limits are SKIPPED while blind, never evaluated.** The agent does not trip a drawdown limit using
  numbers it does not trust — it does not pretend the stale number is real in *either* direction. Blindness is
  its own failure mode with its own response, rather than being laundered into a different one.

**Where it is.** `scripts/agent/risk/risk_kill.py` line 42 (`MAX_ACCOUNT_BLIND_MS = 120_000`) and
`_evaluate_kill` in `scripts/agent/orchestrator.py` (around line 1931).

**The test.** `tests/agent/test_orchestrator.py::test_account_blind_beyond_cap_with_position_flattens_and_halts`,
plus `test_account_blind_new_ack_keeps_residual_and_retry_never_resubmits` for the ugly case: flattening while
blind must not double-submit if the first attempt's acknowledgement is the thing that went missing.

**The generalisation.** Any agent with an external source of truth should have an answer to "how long may I go
without confirming reality before I stop?" — and that answer should be a number in the code, not an assumption.

---

## 5. External source of truth; the local model is only a label

**The failure it prevents.** The agent's own belief about the world quietly becoming the world. Every system
that models external state eventually finds a code path where the model gets written back as fact.

**The pattern.** The broker's ledger is the position of record — always, on every conflict, without exception.
The agent also computes its own *modelled* view (what it thinks the fill should have been, what it thinks the
P&L is), because that is how strategy quality is measured. But the two are **different types**:

```
scripts/agent/serializer.py
  line 15  class BrokerUSD(Decimal)    — ledger truth
  line 21  class ModeledUSD(Decimal)   — strategy evaluation
```

They are sibling subclasses, so neither is assignable to the other. Guards at each boundary enforce direction:
`as_broker_usd` (line 58) raises `TypeError` on anything that is not `BrokerUSD`; `as_modeled_usd` in
`scripts/agent/exec_ledger.py` mirrors it; and `scripts/agent/broker_reconcile.py` line 190 raises outright if a
`ModeledUSD` ever reaches a reconciliation slot. Confusing the two is not a bug you find in production — it is a
type error you find in the test suite.

**The generalisation.** If your agent both *observes* an external system and *predicts* it, make observations
and predictions different types. Then the compiler, or in Python the guard at the boundary, refuses the
conflation that a human reviewer would miss. Naming a variable `estimated_balance` is not the same as making it
impossible to write into `balance`.

**The test.** `tests/agent/test_broker_reconcile.py` and `tests/agent/test_exec_ledger.py` pin both directions.

---

## 6. Append-only journal with per-row content hashes

**The failure it prevents.** Not being able to answer "what did it do, and why, at 14:32?" — and the subtler
one: an answer you cannot trust because the log is mutable.

**The pattern.** Every decision, order, fill, risk evaluation and state transition is appended as one JSONL row
that is never edited. Each row carries:

- a **content hash** over its canonical serialisation (`row_hash`, `scripts/agent/serializer.py` line 53, over
  `dumps` at line 47 — sorted keys, no whitespace, Decimals as strings, so the same logical row always hashes
  identically);
- correlation IDs (`run_id`, `decision_id`, `order_id`) so a decision can be followed to its consequences;
- a **per-stream monotonic sequence number**, with a single writer lock per stream so two writer instances on
  one path share one sequence.

Replay verifies every row's hash and treats a mismatch as fatal (`JournalCorruption`,
`scripts/agent/journal.py` line 26; verification at lines 63 and 156). One exception is allowed and it is the
right one: a **truncated final line with no trailing newline** — a crash mid-write — is dropped, because that
is a partial record rather than a corrupted one. A corrupt line that *is* newline-terminated is a complete,
wrong record and raises. That distinction is the difference between a log that survives a power cut and a log
that silently eats evidence.

**The test.** `tests/agent/test_journal_replay.py`: `test_seq_is_monotonic`,
`test_rows_require_correlation_and_monotonic_seq`, `test_replay_drops_truncated_trailing_line`,
`test_reserved_field_collision_rejected`.

**Where it is weaker than it sounds — read this one carefully.** This journal is **not hash-chained**. Each row
hashes only its own content; there is no `prev_hash`, no link between rows, and `replay()` does not check
sequence continuity. The practical consequence, verified rather than assumed: **deleting a row from the middle
of a stream, or reordering a stream entirely, replays clean.** The hashes detect *corruption* and *mutation of
a row*; they do not detect *deletion* or *reordering*.

If you need tamper-evidence rather than corruption-detection — and an agent that spends money plausibly does —
you want each row to commit to its predecessor's hash, which turns any deletion or reordering into a broken
chain. That is a small change to `journal.py` and it is the single most valuable thing on this list that this
project did not build.

(An earlier draft of this document, and several internal notes, described this journal as "hash-chained". It
was wrong. It is called out here rather than quietly corrected because a safety claim that is one word stronger
than the implementation is exactly the kind of error this project exists to argue against.)

---

## 7. Predeclaration, a search budget, and a stop rule

**The failure it prevents.** The most expensive failure in this repository, and the one least likely to be
called an engineering problem: **an agent — or a person — that keeps searching until it finds a result it
likes.** Given enough configurations, something always passes. If the criteria can move after you see the
numbers, then "it passed" carries no information.

**The pattern.** Three commitments, made before the run:

1. **Predeclaration.** The hypothesis, the universe, the horizon, the data substrate and the exact pass/fail
   thresholds are written down and committed *before* the data is pulled. In this repository those documents
   are `docs/superpowers/specs/*-research-packet.md`, and their commit timestamps are the evidence.
2. **A search budget.** Before starting, decide how many attempts the idea gets. Here: at most two strategy
   families on one data substrate, then at most two substrate changes. The budget exists because the number of
   things you *can* try is unbounded, and an unbounded search finds noise.
3. **A stop rule.** Write down, in advance, what result ends the line — and what it specifically does *not*
   authorise. The rule used here named the follow-ups that were **forbidden** after a null, which is the half
   people leave out: a null result is otherwise infinitely re-interpretable as "we just need one more variant".

**What is enforced in code.** Artifact writers fail closed unless the `rules_hash` supplied equals the hash
*derived* from the committed configuration — you cannot promote a result that was produced under different
rules than the ones you declared (`_require_config_derived_rules_hash`, `scripts/agent/backtest_historical.py`
line 635, called by both writers at lines 2173 and 2265). The verifier independently recomputes the pass
criteria rather than trusting a `pass: true` field in the artifact (`verify_artifact`,
`scripts/agent/backtest_gate.py` line 280). And the whole promotion gate is wired into the trading path: with
no passing artifact, the preflight adds `backtest_artifact_missing` (`execution_preflight.py` line 369) and
every real-strategy open is refused. **That is why this agent has never traded — not restraint, but a gate that
was never satisfied.**

**Where it is weaker than it sounds.** The search budget and the stop rule are enforced by *documentation and
human review*, not by code. No module counts strategy families or refuses a third one. The code enforces the
narrower property — that a promoted result matches its declared rules. Whether the *decision* to stop is honoured
is a matter of character, and the honest thing to say is that this is the one pattern here with no technical
enforcement at all.

**How it actually played out.** Two families were predeclared, both were nulled on their own criteria, and the
stop rule was applied instead of being argued around. The result is `docs/RESULTS.md`, and the reason this
repository exists.

---

## What this buys you, concretely

The patterns above are why the following is true. Most of it you can check for yourself in a clone; one item
you cannot, and it is filed separately — a document arguing that you must not fool yourself does not get to
blur that line.

**Checkable in this repository:**

- The three run gates are `false` in the committed configuration — and were `false` in **every committed
  version of `config/agent_rules.json` and `config/risk_rules.json` reachable from any ref**, across all
  branches (`git rev-list --all` piped through `git cat-file`). Blobs *elsewhere* in the tree do contain
  `"enabled": true` — the armed overlays under `tests/fixtures/config/` that exist precisely so the arming
  path can be tested, plus documentation examples — so check the two files under `config/`, not the string.
- `artifacts/backtests/` has only ever contained `.gitkeep`. No strategy was ever promoted
  (`git log --all --name-only -- artifacts/`).
- The refusal machinery itself runs offline, with no credentials and no vendor account:
  `tests/agent/test_observe_e2e.py` drives the real orchestrator over the committed replay fixture
  `tests/fixtures/execution/observe_session_tbbo.jsonl` under the real committed config, and byte-compares the
  output against a committed golden — 98 decisions (50 `do_nothing`, 48 observe-only forecasts), not one of
  them an open.

**Recorded here, not checkable in this repository:**

- In the agent's only live session (2026-07-10, real market data, real paper-broker credentials, both gates
  off), it ran for 3 h 39 m — 15:29 to 19:08 UTC, stopped before the close, so no end-of-day report was
  written — and made **438 decisions (219 one-minute bars x two symbols), every one of them `do_nothing`**,
  submitting zero orders. Its reasons were the ones designed above: `market_state_not_tradable`,
  `feature_cutoff_mismatch`, `features_unavailable`, `quote_stale`, `spread_too_wide` — though the binding one
  was the first: all 438 carry `session_state: halted`, because the free feed's missing limit-band data left
  the status plane at `UNKNOWN` and pattern 3 wires `UNKNOWN` to `halted`. This is therefore a demonstration of
  one pattern holding for a whole session, not of five gates each being exercised. **That journal is not
  published.** Its rows carry vendor-derived market-data provenance — dataset, schema, instrument id and
  exchange timestamps — so `journal/` is git-ignored, and this bullet rests on the author's word rather than on
  evidence you can re-derive. The safe outcome was produced by the machinery, not by luck; the machinery is the
  part you can check.

An agent that does nothing is easy to build. An agent that does nothing **for auditable reasons, while fully
connected to live data with the whole order path wired to a real broker API**, is the actual engineering
result. The account was a paper account holding simulated money, and the adapter is structurally paper-only:
`AlpacaPaperBroker` raises at construction unless the credentials pin `base_url` to the paper host, and this
repository contains no live broker class at all.
