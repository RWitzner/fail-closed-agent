# Paper Correctness Wave 1 Implementation Plan

> **STATUS: COMPLETED.** This wave was built, independently reviewed, and consolidated onto `main` on
> 2026-07-10. It is published as the plan it was, with its task checkboxes left unticked as authored —
> the record of what was completed is in `PLAN.md` and in the commit history, not in these boxes.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the review-proven correctness gaps that can invalidate artifact promotion or unattended paper recovery, without changing committed gates, pinned thresholds, strategy behavior, or live-money posture.

**Architecture:** Keep the existing public composition and fail-closed defaults. Add defense in depth at the artifact boundary, serialize stale-lock reclaim behind a stable advisory guard, isolate replay composition from credentials, make session cleanup exception-safe, treat last-known broker positions as exposure, require terminal order-drill evidence, and preserve incremental journal replay equivalence. Each task is an independent red-green-refactor commit.

**Tech Stack:** Python 3 stdlib, `unittest`, existing deterministic fake providers and journal fixtures.

**Authoritative design:** `docs/superpowers/specs/2026-07-09-autonomous-paper-trader-design.md`

---

## File Map

- `scripts/agent/backtest_gate.py`: independent artifact schema/hash/triple/semantic verification.
- `tests/agent/test_backtest_gate_m7.py`: forged-but-hash-valid semantic regression cases.
- `scripts/agent/run_lock.py`: one-owner journal-tree lock, serialized stale reclaim, owner-safe release.
- `tests/agent/test_run_gates_file.py`: deterministic reclaim and replacement-owner tests.
- `scripts/agent/paper_session.py`: replay composition and exception-safe session cleanup.
- `tests/agent/test_paper_session.py`: replay credential isolation and feed-exception cleanup.
- `scripts/agent/orchestrator.py`: broker/local exposure union for bounded blindness.
- `tests/agent/test_orchestrator.py`: broker-only blindness regression.
- `scripts/agent/verify_alpaca_paper.py`: terminal submit/cancel drill proof.
- `tests/agent/test_verify_alpaca_paper.py`: missing-id, unresolved, and filled-order cases.
- `scripts/agent/journal.py`: immutable incremental-reader returns and file replacement detection.
- `tests/agent/test_journal_replay.py`: nested mutation and same-size replacement regressions.
- `CLAUDE.md`, `PLAN.md`: reconcile only the facts changed by this wave after verification.

### Task 1: Recompute Artifact Semantics At Verification

**Files:**
- Modify: `scripts/agent/backtest_gate.py:120-225`
- Test: `tests/agent/test_backtest_gate_m7.py`

- [ ] **Step 1: Write failing forged-artifact tests**

Add two tests that retain `metrics.pass = True`, recompute `artifact_hash`, and
prove that invalid actual values are rejected:

```python
def test_v2_hash_valid_claimed_pass_rejects_failed_actual_metrics(self):
    with TemporaryDirectory() as tmp:
        metrics = _v2_metrics()
        metrics["pnl"] = dict(
            metrics["pnl"],
            net_execution_realistic_pnl_usd="-999.00",
            avg_trade_bps="-10.00",
            profit_factor="0.10",
        )
        metrics["benchmark"] = dict(
            metrics["benchmark"], active_pnl_usd="-888.00")
        _write_artifact(tmp, _artifact_payload(metrics=metrics))

        self.assertEqual(self._verify(tmp).status, "hash_invalid")

def test_v2_hash_valid_claimed_pass_rejects_quality_breach(self):
    with TemporaryDirectory() as tmp:
        metrics = _v2_metrics()
        metrics["quality"] = dict(
            metrics["quality"], s1_canary_breach_count=1)
        _write_artifact(tmp, _artifact_payload(metrics=metrics))

        self.assertEqual(self._verify(tmp).status, "hash_invalid")
```

- [ ] **Step 2: Run the targeted test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.agent.test_backtest_gate_m7.TestBacktestGateV2.test_v2_hash_valid_claimed_pass_rejects_failed_actual_metrics \
  tests.agent.test_backtest_gate_m7.TestBacktestGateV2.test_v2_hash_valid_claimed_pass_rejects_quality_breach
```

Expected: both tests fail because `verify_artifact` returns `ok`.

- [ ] **Step 3: Invoke the canonical evaluator from `_valid_v2_metrics`**

Import the existing evaluator and add the semantic check only after the closed
schema, threshold floors, and provenance types have validated:

```python
from agent.paper_phase_criteria import evaluate_paper_phase_criteria

# at the end of _valid_v2_metrics
if not evaluate_paper_phase_criteria(metrics).passed:
    return False
return True
```

Do not duplicate thresholds and do not change `metrics.pass`, the writer, or any
pinned constant.

- [ ] **Step 4: Run targeted and module tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_backtest_gate_m7 tests.agent.test_m7_paper_phase_criteria
```

Expected: all tests pass.

- [ ] **Step 5: Commit the isolated fix**

```bash
git add scripts/agent/backtest_gate.py tests/agent/test_backtest_gate_m7.py
git commit -m "fix: recompute artifact criteria during verification"
```

### Task 2: Serialize Stale Run-Lock Reclaim And Verify Release Ownership

**Files:**
- Modify: `scripts/agent/run_lock.py`
- Test: `tests/agent/test_run_gates_file.py`

- [ ] **Step 1: Write failing owner and reclaim tests**

Import `threading` and `mock`. Add a two-contender regression and a
replacement-inode release test. Patch `agent.run_lock._pid_alive` with a probe
that waits at a two-party barrier only for the reaped child PID, with a bounded
barrier timeout; return `True` for every other PID. This forces both contenders
past the stale-owner decision in the old code. After serialization is added,
the first contender times out alone inside the guard and the second sees the
new live owner:

```python
def test_concurrent_stale_reclaim_has_exactly_one_winner(self):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    dead_pid = child.pid
    journal = self._journal_dir()
    journal.mkdir(parents=True)
    (journal / LOCK_FILENAME).write_text(f"{dead_pid}\n", encoding="ascii")
    barrier = threading.Barrier(2)
    winners, failures = [], []

    def liveness(pid):
        if pid != dead_pid:
            return True
        try:
            barrier.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            pass
        return False

    def contend():
        lock = RunLock(journal)
        try:
            lock.acquire()
            winners.append(lock)
        except Exception as exc:
            failures.append(exc)

    with mock.patch("agent.run_lock._pid_alive", side_effect=liveness):
        threads = [threading.Thread(target=contend) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
    self.assertTrue(all(not thread.is_alive() for thread in threads))
    self.assertEqual(len(winners), 1)
    self.assertEqual(len(failures), 1)
    self.assertIsInstance(failures[0], RunLockHeld)
    winners[0].release()

def test_release_does_not_unlink_replacement_owner(self):
    lock = RunLock(self._journal_dir())
    lock.acquire()
    lock.path.unlink()
    lock.path.write_text(f"{os.getpid()}\n", encoding="ascii")

    lock.release()

    self.assertTrue(lock.path.exists())
```

Assert that both threads terminated. Do not introduce sleeps; events, barriers,
and bounded joins are the synchronization mechanism.

- [ ] **Step 2: Run the targeted tests and verify RED**

```bash
python3 -m unittest tests.agent.test_run_gates_file.TestRunLock
```

Expected: the replacement-owner test fails; the forced interleaving produces
either two winners or a raw filesystem race instead of exactly one winner and
one `RunLockHeld` loser.

- [ ] **Step 3: Add a stable advisory reclaim guard and inode ownership**

Use `fcntl.flock` on a persistent sibling guard file for the entire
inspect/reclaim/create critical section. Record `(st_dev, st_ino)` immediately
after `_create()`. `release()` acquires the same guard and unlinks only if the
current lock identity equals the stored identity:

```python
import fcntl

GUARD_FILENAME = ".lock.guard"

def _identity(path: Path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino

@contextmanager
def _guard(self):
    handle = open(self._dir / GUARD_FILENAME, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
```

Keep the existing numeric PID lock-file format, stale reporting, malformed-file
fail-closed behavior, and idempotent release. Close/unlock the guard in `finally`.

- [ ] **Step 4: Run targeted tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_run_gates_file.TestRunLock tests.agent.test_paper_session.TestMainExitCodes
```

Expected: all tests pass with exactly one reclaim winner.

- [ ] **Step 5: Commit the isolated fix**

```bash
git add scripts/agent/run_lock.py tests/agent/test_run_gates_file.py
git commit -m "fix: make run lock reclaim owner safe"
```

### Task 3: Make Replay Credential-Inert And Session Cleanup Exception-Safe

**Files:**
- Modify: `scripts/agent/paper_session.py:105-166,244-294`
- Test: `tests/agent/test_paper_session.py`

- [ ] **Step 1: Write the feed-exception cleanup regression**

Use a small fake orchestrator/feed so the test asserts behavior rather than the
implementation details:

```python
def test_feed_exception_still_attempts_eod_cleanup_and_reraises(self):
    class ExplodingOrchestrator:
        mode = "paper"
        run_id = "run-explodes"
        ticks_run = 0
        drift_latched = False
        risk_kill = type("Kill", (), {"state": "monitoring"})()

        def __init__(self):
            self.eod_calls = 0

        def run_reconcile(self, **kwargs):
            return type("Result", (), {"clean": True})()

        def run_with_feed(self, feed):
            raise RuntimeError("feed exploded")

        def ensure_eod_reconcile(self, *args, **kwargs):
            self.eod_calls += 1
            return type("Result", (), {"clean": True})()

    orch = ExplodingOrchestrator()
    tm = _TimeMachine("2026-07-06T19:58:00.000000Z")
    feed = _live_feed(
        tm, [], stop_at="2026-07-06T20:15:00.000000Z")
    with self.assertRaisesRegex(RuntimeError, "feed exploded"):
        run_paper_session(
            orchestrator=orch, feed=feed, journal_dir=Path("unused"),
            session_date_et="2026-07-06", report_dir=None,
            utc_now_iso_fn=lambda: "2026-07-06T20:00:00.000000Z")
    self.assertEqual(orch.eod_calls, 1)
```

The existing `_TimeMachine` and `_live_feed` fixtures provide the exact clock
surface required by the cleanup call.

- [ ] **Step 2: Write a production-composition replay isolation test**

Extract a pure helper for runtime paths and pin its desired API:

```python
def test_replay_runtime_paths_never_expose_credentials_or_gates(self):
    self.assertEqual(
        paper_session.runtime_paths_for(replay=True),
        {"credentials_path": None, "run_gates_path": None},
    )
```

Add a live counterpart proving the existing `.secrets` paths remain selected for
`replay=False`. The CLI must consume this helper when constructing Orchestrator.

- [ ] **Step 3: Run targeted tests and verify RED**

```bash
python3 -m unittest tests.agent.test_paper_session.TestPaperSession tests.agent.test_paper_session.TestMainExitCodes
```

Expected: the exception test shows `eod_calls == 0`; the runtime helper is absent.

- [ ] **Step 4: Implement the minimal isolation and cleanup**

Add the pure helper:

```python
def runtime_paths_for(*, replay: bool) -> dict:
    if replay:
        return {"credentials_path": None, "run_gates_path": None}
    return {
        "credentials_path": _SECRETS / "alpaca_paper.json",
        "run_gates_path": _SECRETS / "run_gates.json",
    }
```

Wrap only `orch.run_with_feed(feed)` so the original exception remains primary
while `ensure_eod_reconcile` is attempted exactly once. If cleanup also raises,
attach it as context or an exception note; do not replace the original feed
failure. The normal path must retain current report and exit semantics.

- [ ] **Step 5: Run targeted tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_paper_session tests.agent.test_config_canary
```

Expected: all tests pass and replay still submits zero orders.

- [ ] **Step 6: Commit the isolated fix**

```bash
git add scripts/agent/paper_session.py tests/agent/test_paper_session.py
git commit -m "fix: isolate replay and clean up feed failures"
```

### Task 4: Include Broker-Only Positions In Account-Blind Protection

**Files:**
- Modify: `scripts/agent/orchestrator.py:1685-1720`
- Test: `tests/agent/test_orchestrator.py:834-948`

- [ ] **Step 1: Write the broker-only exposure regression**

Extend `_blind_pipeline` with `broker_positions=False`, and feed
`positions_row` when either `seed_position` or `broker_positions` is true. This
lets local and broker seeding be selected separately. Then add:

```python
def test_account_blind_beyond_cap_with_broker_only_position_halts(self):
    pipeline, api = self._blind_pipeline(
        "blind-broker-only", seed_position=False,
        broker_positions=True)
    pipeline.tick_on_bar(1)
    for bar in range(2, 9):
        pipeline.tick_on_bar(bar, advance_ms=30_000)

    self.assertEqual(pipeline.orch.risk_kill.state, "halted")
    transitions = pipeline.rows_of("risk", "kill_switch_transition")
    self.assertEqual({row["cause"] for row in transitions},
                     {"account_blind_cap"})
```

The fake provider must expose the broker position on the last valid portfolio
read while the local `PaperBook` remains flat.

- [ ] **Step 2: Run the targeted test and verify RED**

```bash
python3 -m unittest \
  tests.agent.test_orchestrator.OrchestratorCase.test_account_blind_beyond_cap_with_broker_only_position_halts
```

Expected: state remains `monitoring` because `_has_open_positions()` consults
only the local book.

- [ ] **Step 3: Union local and last-known broker exposure**

Keep the method side-effect free:

```python
def _has_open_positions(self) -> bool:
    local_open = any(
        pos.status == "open" for pos in self._book._positions.values())
    broker_open = (
        self._portfolio is not None and bool(self._portfolio.positions))
    return local_open or broker_open
```

Do not require the cached portfolio to be fresh: the blindness path exists
precisely because freshness is lost, and last-known non-zero broker exposure must
remain conservative.

- [ ] **Step 4: Run all blindness tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_orchestrator
```

Expected: broker-only and existing local/flat cases pass.

- [ ] **Step 5: Commit the isolated fix**

```bash
git add scripts/agent/orchestrator.py tests/agent/test_orchestrator.py
git commit -m "fix: include broker positions in blindness guard"
```

### Task 5: Require Terminal Evidence From The Alpaca Paper Drill

**Files:**
- Modify: `scripts/agent/verify_alpaca_paper.py:88-167`
- Test: `tests/agent/test_verify_alpaca_paper.py`

- [ ] **Step 1: Write failing unresolved and missing-id tests**

Make the fake client configurable and add:

```python
def test_order_drill_missing_submit_id_recovers_by_client_id_and_cancels(self):
    client = _FakeClient()
    client.submit_response = {"status": "accepted"}
    with TemporaryDirectory() as tmp:
        summary = self._run(tmp, client, allow_order_drill=True)
    self.assertTrue(summary["ok"])
    self.assertEqual(client.cancel_calls, ["broker-drill-1"])
    self.assertEqual(summary["order_drill"]["final_status"], "canceled")

def test_order_drill_nonterminal_final_status_fails(self):
    client = _FakeClient()
    client.final_status = "new"
    with TemporaryDirectory() as tmp:
        summary = self._run(tmp, client, allow_order_drill=True)
    self.assertFalse(summary["ok"])
    self.assertIn("order_drill_failed", summary["failures"])

def test_order_drill_fill_is_never_ok(self):
    client = _FakeClient()
    client.final_status = "filled"
    with TemporaryDirectory() as tmp:
        summary = self._run(tmp, client, allow_order_drill=True)
    self.assertFalse(summary["ok"])
```

- [ ] **Step 2: Run the targeted tests and verify RED**

```bash
python3 -m unittest tests.agent.test_verify_alpaca_paper.TestVerifier
```

Expected: accepted-without-id or `new`/`filled` can incorrectly produce `ok`.

- [ ] **Step 3: Implement submit lookup, finally-cancel, and terminal verdict**

Define the safe terminal set and make final status part of the summary verdict:

```python
_SAFE_DRILL_TERMINAL = frozenset({"canceled", "rejected", "expired"})

drill_ok = (
    drill["error"] is None
    and drill["submitted"]
    and drill["final_status"] in _SAFE_DRILL_TERMINAL
)
if not drill_ok:
    failures.append("order_drill_failed")
```

If submit omits `id`, query by `client_order_id` before cancel. Put best-effort
cancel in `finally`, then perform the bounded final query. Never interpret a
cancel request alone as final cancellation.

- [ ] **Step 4: Run targeted tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_verify_alpaca_paper
```

Expected: all tests pass; `new`, unresolved, and `filled` are failures.

- [ ] **Step 5: Commit the isolated fix**

```bash
git add scripts/agent/verify_alpaca_paper.py tests/agent/test_verify_alpaca_paper.py
git commit -m "fix: require terminal paper drill cancellation"
```

### Task 6: Preserve Incremental Journal Replay Equivalence

**Files:**
- Modify: `scripts/agent/journal.py:80-128`
- Test: `tests/agent/test_journal_replay.py:204-269`

- [ ] **Step 1: Write failing nested-mutation and same-size replacement tests**

```python
def test_returned_nested_rows_cannot_mutate_reader_cache(self):
    writer = JournalWriter(
        self.path, run_id="run-1",
        clock=lambda: "2026-07-06T00:00:00Z")
    writer.append("evt", {"nested": {"value": 1}})
    reader = IncrementalJournalReader(self.path)
    rows = reader.read()
    rows[0]["nested"]["value"] = 999

    self.assertEqual(reader.read(), replay(self.path))

def test_same_size_replacement_forces_integrity_recheck(self):
    writer = JournalWriter(
        self.path, run_id="run-1",
        clock=lambda: "2026-07-06T00:00:00Z")
    writer.append("evt", {"value": "AAAA"})
    reader = IncrementalJournalReader(self.path)
    reader.read()
    original = self.path.read_bytes()
    self.path.write_bytes(original.replace(b"AAAA", b"BBBB"))

    with self.assertRaises(JournalCorruption):
        reader.read()
```

- [ ] **Step 2: Run the targeted tests and verify RED**

```bash
python3 -m unittest tests.agent.test_journal_replay.TestIncrementalJournalReader
```

Expected: nested mutation poisons the cache and same-size replacement is not
re-read.

- [ ] **Step 3: Track file identity/change metadata and deep-copy returns**

Store `(st_dev, st_ino, st_mtime_ns, st_ctime_ns)` after a successful read. If
the size is unchanged but identity/change metadata differs, reset and full-read.
Return `copy.deepcopy(self._rows)` from every path that exposes cached rows:

```python
import copy

def _file_version(stat_result):
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )

def _snapshot(self):
    return copy.deepcopy(self._rows)
```

Preserve the incomplete-tail rule and never commit the offset past corruption.

- [ ] **Step 4: Run journal and dependent tests and verify GREEN**

```bash
python3 -m unittest tests.agent.test_journal_replay tests.agent.test_orchestrator
```

Expected: all tests pass and incremental reads remain replay-equivalent.

- [ ] **Step 5: Commit the isolated fix**

```bash
git add scripts/agent/journal.py tests/agent/test_journal_replay.py
git commit -m "fix: preserve incremental journal integrity"
```

### Task 7: Integrate, Review, And Reconcile Status Documents

**Files:**
- Modify only if facts changed: `CLAUDE.md`, `PLAN.md`
- Verify: all files changed since the design commit

- [ ] **Step 1: Run the complete offline verification suite**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .
git diff --check
python3 -m compileall -q scripts tests
```

Expected: zero failures, zero diff-check output, compile exit `0`.

- [ ] **Step 2: Verify the committed safety canaries explicitly**

```bash
python3 -m unittest tests.agent.test_config_canary
jq -e '.enabled == false and .paper_trading.enabled == false' config/agent_rules.json
jq -e '.live_trading.enabled == false and ([.caps[]] | all(. == 0))' config/risk_rules.json
find artifacts/backtests -maxdepth 1 -type f -print | sort
```

Expected: canaries pass; all gates remain false; all caps remain zero; production
artifact listing remains only `.gitkeep`.

- [ ] **Step 3: Perform spec-compliance and code-quality reviews**

Review each Task 1-6 commit against this plan, then review the aggregate diff
from `a52f9b8` to `HEAD`. Resolve every Critical or Important finding with a new
red-green cycle before proceeding.

- [ ] **Step 4: Reconcile volatile status facts only**

Update `CLAUDE.md` and `PLAN.md` only where this wave changes current readiness,
test count, or remaining blockers. Do not claim autonomous paper readiness; Track
B-D remain open. Do not rewrite historical milestone evidence.

- [ ] **Step 5: Re-run the full suite after documentation changes**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .
git diff --check
git status --short --branch
```

Expected: zero test failures and only intentional branch changes.

- [ ] **Step 6: Commit the wave closeout**

```bash
git add CLAUDE.md PLAN.md
git commit -m "docs: record paper correctness hardening"
```

Skip this commit if neither document requires a factual change.
