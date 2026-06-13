"""M5 §Q — execution-tier fixture builders + the orchestrator test harness.

Pure and deterministic: no wall clock, no randomness beyond signal_fixtures'
seeded LCG; every builder takes explicit paths (R11: the suite never touches
`.secrets/` — run-gates/credential files are written to injected tmp paths).

Contents (the §Q row, verbatim obligations):

- ``permissive_paper_fixture_config()`` — IN-MEMORY gates-True + nonzero caps +
  one-symbol universe with sector/beta (FD-M5-3); a FULL agent_rules shape
  (committed signal block verbatim + latency_budget_ms + the §B execution block
  + universe) so SignalConfig / RiskConfig / ExecutionConfig ALL parse green
  (M5C-B7). NEVER written under ``config/``.
- ``RealStrategyStub`` (M5C-T3) — a non-``"synthetic."`` strategy emitting
  ``paper_eligible=True`` buy Candidates from a scripted list. Lives HERE (in
  tests/lib, never under ``scripts/agent/strategies/``) so wall 3 is untouched.
- ``ReEmittingExitProvider`` — the RC-1 driver: re-emits the same
  ExitInstruction on every ``exits()`` call from a given call ordinal.
- ``status_script(...)`` (M5C-T4) — per-symbol session windows ⇒
  ``StatusFlags(halt=HaltState.NONE, luld=LuldState.NORMAL,
  ssr=SsrState.INACTIVE)`` (REAL enum members) inside the window, None (⇒
  UNKNOWN fail-closed) outside. Thin wrapper over the production seam
  ``agent.orchestrator.status_provider_from_windows``.
- quote A/B pair builders (clean / B-not-later / identical-provenance re-serve
  / epoch flip / adverse move / the EX-1 boundary pair ``ask_A=0.7999 →
  ask_B=0.8019`` / sub-$1 / crossed-locked-stale B).
- run-gates-file builders (valid / malformed / hostile-extra-keys /
  non-identity-True) — each writes to an EXPLICIT directory.
- artifact builders (valid triple / tampered hash / mismatched key) — each
  takes a MANDATORY ``artifacts_dir`` argument, no default (M5C-S12).
- recorder-events builders satisfying the §Q open-driving density rule (EX-12):
  ≥ 1 quote event in recorded ``(t0 + latency_budget, t0 + RISK_VERDICT_TTL_MS]``
  after each scripted decision bar.
- ``GOLDEN_RUN_ID`` + ``run_synthetic_golden(journal_dir)`` /
  ``run_observe_golden(journal_dir)`` (M5C-T5) — thin deterministic runners
  over the REAL orchestrator with a pinned row clock (the
  ``tests/lib/signal_pipeline.py`` mechanism; the byte-golden fixtures
  themselves land in a later wave).
- ``ExecPipeline`` — the orchestrator composition harness test_orchestrator.py
  drives tick-by-tick (the SignalPipeline shape).
"""
import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from agent import config as agent_config
from agent.bar_series import MidBarSeriesReader, resample_midbars
from agent.candidate import Candidate, Leg
from agent.execution_config import ExecutionConfig
from agent.market_calendar import FixtureScheduleProvider
from agent.orchestrator import (
    Orchestrator,
    status_provider_from_windows,
)
from agent.quote_quality import QuoteSnapshot
from agent.serializer import row_hash
from agent.signal_config import SignalConfig
from agent.strategies.synthetic import ExitInstruction, ScriptedSyntheticStrategy
from recorder.persistence import EventWriter

from tests.lib.fakes import FakeClock
from tests.lib.risk_fixtures import (
    margin_calendar_fixture,
    permissive_fixture_config,
)
from tests.lib.signal_fixtures import DATASET, SCHEMA, quotes_session

REPO_ROOT = Path(__file__).resolve().parents[2]

GOLDEN_RUN_ID = "run-m5-golden-v1"
FIXED_WRITER_TS = "2026-06-15T21:00:00+00:00"   # pinned row clock (M3 precedent)
GOLDEN_GENERATED_TS = "2026-06-10T00:00:00.000000Z"  # pinned report ts (M3 mechanism)
DATA_PIN_EXEC_V1 = "EQUS.MINI:tbbo:1m:fixture:exec-aapl-v1"

_EVENTS_RUN_ID = "run-recorder-fixture-v1"

# §Q committed-fixture pins (M5C-T5: regeneration = run the builder, copy bytes)
OBSERVE_FIXTURE_PATH = (REPO_ROOT / "tests" / "fixtures" / "execution"
                        / "observe_session_tbbo.jsonl")
OBSERVE_FIXTURE_RUN_ID = "run-observe-fixture-v1"
OBSERVE_FIXTURE_SYMBOL = "AAPL"
OBSERVE_FIXTURE_SESSION_DATE = "2026-06-15"
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "execution" / "golden"
CALENDAR_FIXTURE_PATH = (REPO_ROOT / "tests" / "fixtures" / "calendar"
                         / "nyse_margin_window_v1.json")


# --- config builders -----------------------------------------------------------------


def committed_assembled_config() -> dict:
    """The REAL committed pair {agent_rules, risk_rules} (read-only load)."""
    return {
        "agent_rules": agent_config.load(REPO_ROOT / "config" / "agent_rules.json"),
        "risk_rules": agent_config.load(REPO_ROOT / "config" / "risk_rules.json"),
    }


def permissive_paper_fixture_config(*, symbols=("AAPL",)) -> dict:
    """FD-M5-3 / M5C-B7: the FULL in-memory permissive assembled config.

    agent_rules = the COMMITTED file (signal block verbatim + latency_budget_ms
    + the §B execution block ride along) with gates identity-True and a small
    universe; risk_rules = the M4 permissive fixture (nonzero integer caps +
    universe WITH sector/beta). All three parsers parse green.
    """
    assembled = committed_assembled_config()
    agent_rules = copy.deepcopy(assembled["agent_rules"])
    agent_rules["enabled"] = True
    agent_rules["paper_trading"] = {"enabled": True}
    agent_rules["universe"]["symbols"] = list(symbols)
    config = {
        "agent_rules": agent_rules,
        "risk_rules": permissive_fixture_config()["risk_rules"],
    }
    # M5C-B7 self-check: every parser must accept this shape (fail-loud here,
    # not in the middle of an orchestrator test).
    SignalConfig.from_config(config["agent_rules"])
    ExecutionConfig.from_config(config)
    return config


# --- strategy doubles (M5C-T3) --------------------------------------------------------


class RealStrategyStub:
    """A REAL (non-synthetic) strategy double: ``strategy_id`` does NOT start
    with ``"synthetic."``; emits ``paper_eligible=True`` BUY Candidates from a
    scripted list. Matching mirrors ScriptedSyntheticStrategy (M5C-B2): an int
    ``on_bar`` matches the 1-based ordinal of scan() invocations; rows are NOT
    consumed (a due row re-emits on every matching call)."""

    strategy_id = "stub.real_v1"

    def __init__(self, script):
        self._script = [dict(row) for row in script]
        self.scan_calls = 0

    def scan(self, ctx):
        self.scan_calls += 1
        out = []
        for row in self._script:
            if row.get("symbol", ctx.snapshot.symbol) != ctx.snapshot.symbol:
                continue
            if row["on_bar"] != self.scan_calls:
                continue
            out.append(Candidate(
                strategy_id=self.strategy_id,
                legs=(Leg(
                    symbol=ctx.snapshot.symbol,
                    instrument_id=ctx.snapshot.instrument_id,
                    side="buy",
                    qty=Decimal(str(row.get("qty", "10"))),
                    # default an explicit on-grid limit: M4 leg_cap_notional
                    # prices opening legs ONLY from limit_price (FD-M4-16).
                    limit_price=Decimal(str(row.get("limit") or "210.00")),
                ),),
                paper_eligible=True,
                score=None,
            ))
        return tuple(out)


class ReEmittingExitProvider:
    """RC-1 driver: from ``exits()`` call ordinal ``start_call`` onward, emits
    the SAME ExitInstruction on EVERY call (a re-emitted exit while a close is
    in flight must be dropped unjournaled — §M.3 step 8)."""

    def __init__(self, *, symbol, qty, reason="strategy_exit", start_call=1):
        self._symbol = symbol
        self._qty = Decimal(str(qty))
        self._reason = reason
        self._start_call = int(start_call)
        self.calls = 0

    def exits(self, ctx):
        self.calls += 1
        if ctx.snapshot.symbol != self._symbol or self.calls < self._start_call:
            return ()
        return (ExitInstruction(
            symbol=self._symbol,
            instrument_id=ctx.snapshot.instrument_id,
            qty=self._qty,
            reason=self._reason,
        ),)


# --- status injection (M5C-T4) ---------------------------------------------------------


def status_script(windows):
    """``windows`` = {symbol: [(start_utc_iso, end_utc_iso), ...]} ⇒ a
    ``status_provider(symbol, ts_utc)`` callable yielding
    ``StatusFlags(halt=HaltState.NONE, luld=LuldState.NORMAL,
    ssr=SsrState.INACTIVE)`` (with a wide LULD band so RTH step 5b passes)
    inside a window, and ``None`` (⇒ UNKNOWN fail-closed defaults) outside."""
    return status_provider_from_windows(windows)


def full_day_windows(symbol: str, session_date_et: str) -> dict:
    """A whole-trading-day status window for one ET session date (UTC bounds
    wide enough for any DST offset)."""
    return {symbol: [(f"{session_date_et}T08:00:00.000000Z",
                      f"{session_date_et}T23:59:59.000000Z")]}


# --- quote builders --------------------------------------------------------------------


def quote(*, symbol="AAPL", instrument_id=1001, bid="99.99", ask="100.01",
          bid_sz="300", ask_sz="200", seen_at_ms=0, reconnect_epoch=0,
          vendor_seq=1, ts_event_utc="2026-06-15T13:31:00.100000Z",
          ts_recv_utc=None, dataset=DATASET, schema=SCHEMA) -> QuoteSnapshot:
    def _dec(value):
        return None if value is None else Decimal(str(value))
    return QuoteSnapshot(
        symbol=symbol, instrument_id=instrument_id,
        bid=_dec(bid), ask=_dec(ask), bid_sz=_dec(bid_sz), ask_sz=_dec(ask_sz),
        ts_event_utc=ts_event_utc,
        ts_recv_utc=ts_recv_utc if ts_recv_utc is not None else ts_event_utc,
        seen_at_ms=seen_at_ms, reconnect_epoch=reconnect_epoch,
        vendor_seq=vendor_seq, dataset=dataset, schema=schema)


def quote_pair(kind: str, *, t0: int = 0, budget_ms: int = 250):
    """The §Q A/B pairs. A is stamped at ``t0``; B (where meaningful) lands at
    ``t0 + budget_ms + 50`` with strictly-later provenance."""
    t1 = t0 + budget_ms + 50
    a = quote(seen_at_ms=t0, vendor_seq=10,
              ts_event_utc="2026-06-15T13:31:00.100000Z")
    later = dict(seen_at_ms=t1, vendor_seq=11,
                 ts_event_utc="2026-06-15T13:31:00.400000Z")
    if kind == "clean":
        return a, quote(**later)
    if kind == "b_not_later":
        return a, quote(seen_at_ms=t0, vendor_seq=11,
                        ts_event_utc="2026-06-15T13:31:00.400000Z")
    if kind == "identical_provenance":
        return a, quote(seen_at_ms=t1, vendor_seq=10,
                        ts_event_utc="2026-06-15T13:31:00.100000Z")
    if kind == "epoch_flip":
        return a, quote(reconnect_epoch=1, **later)
    if kind == "adverse_move":   # +100 bps on the ask (≫ committed 25 bps cap)
        return a, quote(ask="101.02", **later)
    if kind == "ex1_boundary":   # EX-1: raw 25.003…bps fires, quantized 25.00 does NOT
        a = quote(bid="0.7990", ask="0.7999", seen_at_ms=t0, vendor_seq=10,
                  ts_event_utc="2026-06-15T13:31:00.100000Z")
        return a, quote(bid="0.8010", ask="0.8019", **later)
    if kind == "sub_dollar":
        a = quote(bid="0.4990", ask="0.5000", seen_at_ms=t0, vendor_seq=10,
                  ts_event_utc="2026-06-15T13:31:00.100000Z")
        return a, quote(bid="0.4992", ask="0.5002", **later)
    if kind == "crossed_b":
        return a, quote(bid="100.05", ask="100.01", **later)
    if kind == "locked_b":
        return a, quote(bid="100.01", ask="100.01", **later)
    if kind == "stale_b":
        # B exists but is OLD relative to the preflight's now_ms (caller passes
        # now_ms ≥ B.seen_at_ms + staleness budget + 1).
        return a, quote(seen_at_ms=t0 - 5000, vendor_seq=11,
                        ts_event_utc="2026-06-15T13:30:55.100000Z")
    raise ValueError(f"unknown quote_pair kind: {kind!r}")


# --- run-gates-file builders (FD-M5-2) --------------------------------------------------


def write_run_gates(dir_path, kind: str = "valid") -> Path:
    """Write a run-gates file of the given kind into ``dir_path`` and return
    its path. Kinds: valid / malformed / hostile (extra loosening keys, which
    must be IGNORED) / non_identity (truthy non-True values)."""
    path = Path(dir_path) / "run_gates.json"
    if kind == "valid":
        path.write_text(json.dumps(
            {"enabled": True, "paper_trading": {"enabled": True}}),
            encoding="utf-8")
    elif kind == "malformed":
        path.write_text("{not json", encoding="utf-8")
    elif kind == "hostile":
        path.write_text(json.dumps({
            "enabled": True,
            "paper_trading": {"enabled": True},
            "caps": {"max_position_usd": 10_000_000},
            "universe": {"symbols": ["GME"]},
            "latency_budget_ms": 1,
        }), encoding="utf-8")
    elif kind == "non_identity":
        path.write_text(json.dumps(
            {"enabled": 1, "paper_trading": {"enabled": "true"}}),
            encoding="utf-8")
    else:
        raise ValueError(f"unknown run-gates kind: {kind!r}")
    return path


# --- backtest-artifact builders (FD-M5-27; MANDATORY artifacts_dir — M5C-S12) ------------


def _artifact_payload(strategy_id: str, rules_hash_value: str, data_pin: str) -> dict:
    body = {
        "v": 1,
        "strategy_id": strategy_id,
        "rules_hash": rules_hash_value,
        "data_pin": data_pin,
        "metrics": {"basis": "execution_realistic_pnl",
                    "sharpe": "0.5", "n_trades": "100"},
        "created_utc": "2026-06-10T00:00:00.000000Z",
    }
    body["artifact_hash"] = row_hash(body)
    return body


def write_valid_artifact(*, artifacts_dir, strategy_id, rules_hash_value,
                         data_pin) -> Path:
    directory = Path(artifacts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{strategy_id}.json"
    path.write_text(json.dumps(
        _artifact_payload(strategy_id, rules_hash_value, data_pin)),
        encoding="utf-8")
    return path


def write_tampered_artifact(*, artifacts_dir, strategy_id, rules_hash_value,
                            data_pin) -> Path:
    payload = _artifact_payload(strategy_id, rules_hash_value, data_pin)
    payload["artifact_hash"] = "0" * 64       # broken binding ⇒ hash_invalid
    directory = Path(artifacts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{strategy_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_mismatched_artifact(*, artifacts_dir, strategy_id, rules_hash_value,
                              data_pin) -> Path:
    """Internally consistent artifact bound to a DIFFERENT key triple ⇒
    key_mismatch when verified against (rules_hash_value, data_pin)."""
    payload = _artifact_payload(strategy_id, "f" * 64, data_pin + ":other")
    directory = Path(artifacts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{strategy_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- recorder-events builders (§Q density rule EX-12) ------------------------------------


def dense_session_rows(*, symbol="AAPL", instrument_id=1001,
                       session_date="2026-06-15", minutes=75,
                       dense_from_minute=50) -> list:
    """quotes_session rows + ONE extra quote 1.2 s after each main quote from
    ``dense_from_minute`` on — the EX-12 density rule: each decision tick t0
    gets a quote-B candidate in recorded ``(t0+250, t0+2000]`` ms."""
    rows = quotes_session(symbol=symbol, instrument_id=instrument_id,
                          session_date=session_date, minutes=minutes,
                          include_special_rows=False)
    out = []
    seq = 50_000
    for row in rows:
        out.append(row)
        minute_str = row["ts_event_utc"][14:16]
        del minute_str
        # minute index from the row order: quotes_session emits exactly one row
        # per minute when include_special_rows=False.
        index = len([r for r in out if r["vendor_seq"] < 50_000]) - 1
        if index >= dense_from_minute:
            extra = dict(row)
            base_event = row["ts_event_utc"]
            extra["ts_event_utc"] = _shift_iso(base_event, 1200)
            extra["ts_recv_utc"] = _shift_iso(row["ts_recv_utc"], 1200)
            extra["vendor_seq"] = seq
            seq += 1
            out.append(extra)
    return out


def _shift_iso(ts: str, ms: int) -> str:
    from agent.bar_series import _parse_utc
    shifted = _parse_utc(ts) + timedelta(milliseconds=ms)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def write_events_jsonl(path, rows, *, run_id=_EVENTS_RUN_ID) -> Path:
    """Persist recorder-shaped flat rows WITH the journal envelope
    (event_type/run_id/seq/hash) so ``replay_stream`` verifies them — the
    M5C-5 recorder-shaped form ``ReplayQuoteFeed`` requires."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    writer = EventWriter(path, run_id, clock=lambda: FIXED_WRITER_TS)
    for row in rows:
        writer.record("market_event", dict(row))
    return path


def observe_session_rows(*, symbol=OBSERVE_FIXTURE_SYMBOL, instrument_id=1001,
                         session_date=OBSERVE_FIXTURE_SESSION_DATE, minutes=75,
                         epoch_flip_minute=55) -> list:
    """The §Q observe-fixture rows (fully deterministic field values): one
    valid tbbo row per minute (75 ≥ 60 one-minute buckets, so the 51-bar
    feature gate opens with resolver room), one symbol; minutes 2 and 3 ride
    the recorder's whole-second ``ts_recv_utc`` form (≥ 2 rows — the M3 §K
    mixed-ISO precedent, EX-5); every row from ``epoch_flip_minute`` on
    carries ``reconnect_epoch = 1`` (the §Q mid-session epoch-flip variant)."""
    rows = quotes_session(symbol=symbol, instrument_id=instrument_id,
                          session_date=session_date, minutes=minutes,
                          include_special_rows=False)
    for index, row in enumerate(rows):
        if index >= epoch_flip_minute:
            row["reconnect_epoch"] = 1
    return rows


def write_observe_session_fixture(path=None) -> Path:
    """Write the COMMITTED §Q fixture ``observe_session_tbbo.jsonl``: recorder
    rows WITH the journal envelope (``replay_stream``-verified hashes), pinned
    ``run_id`` + pinned EventWriter row clock — byte-deterministic.
    Regeneration = call this and copy bytes (M5C-T5)."""
    target = Path(path) if path is not None else OBSERVE_FIXTURE_PATH
    return write_events_jsonl(target, observe_session_rows(),
                              run_id=OBSERVE_FIXTURE_RUN_ID)


# --- the orchestrator harness ------------------------------------------------------------


class HeldQuoteView:
    """Trivial QuoteView: the harness sets the latest snapshot explicitly."""

    def __init__(self):
        self._latest = {}

    def put(self, snapshot: QuoteSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot

    def latest(self, symbol, instrument_id):
        return self._latest.get((symbol, instrument_id))


def margin_schedule_provider() -> FixtureScheduleProvider:
    fixture = margin_calendar_fixture()
    return FixtureScheduleProvider(fixture, pin=fixture["pin"])


class ExecPipeline:
    """One orchestrator composition over one journal dir + one injected clock
    (the SignalPipeline shape, §Q/M5C-T5). The caller MUST close() it
    (unbinds the preflight runtime + releases the run lock)."""

    def __init__(self, *, journal_dir, run_id="run-m5-exec-test",
                 broker=None, strategy=None, exit_provider=None,
                 account_provider=None,
                 symbol="AAPL", instrument_id=1001,
                 config=None, run_gates=None, credentials_path=None,
                 artifacts=None, artifacts_dir=None,
                 session_date="2026-06-15", start_et="09:30", minutes=75,
                 status_windows=None, fill_policy="immediate_full",
                 row_clock=None, durable_ids=None):
        self.symbol = symbol
        self.instrument_id = instrument_id
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock(start_ms=1_000_000)
        self.quote_view = HeldQuoteView()
        self._vendor_seq = 100_000

        self.config = config if config is not None else \
            permissive_paper_fixture_config(symbols=(symbol,))
        self.exec_config = ExecutionConfig.from_config(self.config)

        quote_rows = quotes_session(
            symbol=symbol, instrument_id=instrument_id,
            session_date=session_date, start_et=start_et, minutes=minutes,
            include_special_rows=False)
        self.bars, self.missing = resample_midbars(
            quote_rows, symbol=symbol, instrument_id=instrument_id,
            interval="1m", dataset=DATASET, schema=SCHEMA,
            data_pin=DATA_PIN_EXEC_V1)
        self.bar_reader = MidBarSeriesReader(self.bars, self.missing)

        run_gates_path = None
        if run_gates is not None:
            run_gates_path = write_run_gates(self.journal_dir, run_gates)
        elif credentials_path is None and broker is not None:
            # paper composition with an ABSENT run-gates file: point at a path
            # that does not exist (the loader reads all-False).
            run_gates_path = self.journal_dir / "run_gates_absent.json"

        self.artifacts_dir = (Path(artifacts_dir) if artifacts_dir is not None
                              else self.journal_dir / "artifacts")
        if artifacts == "valid" and strategy is not None:
            write_valid_artifact(
                artifacts_dir=self.artifacts_dir,
                strategy_id=strategy.strategy_id,
                rules_hash_value=self.exec_config.rules_hash,
                data_pin=DATA_PIN_EXEC_V1)

        if status_windows is None:
            status_windows = full_day_windows(symbol, session_date)
        status_provider = status_script(status_windows)

        self.orch = Orchestrator(
            journal_dir=self.journal_dir,
            run_id=run_id,
            clock=self.clock,
            quote_view=self.quote_view,
            bar_reader=self.bar_reader,
            calendar_provider=margin_schedule_provider(),
            config=self.config,
            broker=broker,
            strategy=strategy,
            status_provider=status_provider,
            exit_provider=exit_provider,
            account_provider=account_provider,
            credentials_path=credentials_path,
            run_gates_path=run_gates_path,
            instrument_ids={symbol: instrument_id},
            durable_ids=durable_ids,
            artifacts_dir=str(self.artifacts_dir),
            fill_policy=fill_policy,
            row_clock=row_clock or (lambda: FIXED_WRITER_TS),
        )
        self.run_id = run_id

    # -- drive helpers --

    def close(self):
        self.orch.close()

    def _bar_quote(self, bar, *, shift_ms=250, **overrides):
        ts = _shift_iso(bar.bucket_end_utc, shift_ms)
        self._vendor_seq += 1
        fields = dict(
            symbol=self.symbol, instrument_id=self.instrument_id,
            bid=bar.mid - Decimal("0.0100"), ask=bar.mid + Decimal("0.0100"),
            bid_sz="300", ask_sz="200",
            seen_at_ms=self.clock.now_ms(), vendor_seq=self._vendor_seq,
            ts_event_utc=ts, ts_recv_utc=ts, reconnect_epoch=0)
        fields.update(overrides)
        return quote(**fields)

    def tick_on_bar(self, index, *, advance_ms=1000, quote_overrides=None):
        """Deliver the bar-completing quote + the completed bar, then tick."""
        bar = self.bars[index]
        self.clock.advance(advance_ms)
        snapshot = self._bar_quote(bar, seen_at_ms=self.clock.now_ms(),
                                   **(quote_overrides or {}))
        self.quote_view.put(snapshot)
        self.orch.on_bar_complete(bar)
        self.orch.on_tick(now_ms=self.clock.now_ms())
        return snapshot

    def tick_quote_only(self, bar_index, *, advance_ms=300, shift_ms=500,
                        quote_overrides=None):
        """Advance recorded time, deliver a FRESH quote (no new bar), tick —
        the EX-12 quote-B delivery that makes an AWAIT_LATENCY task due."""
        bar = self.bars[bar_index]
        self.clock.advance(advance_ms)
        snapshot = self._bar_quote(bar, shift_ms=shift_ms,
                                   seen_at_ms=self.clock.now_ms(),
                                   **(quote_overrides or {}))
        self.quote_view.put(snapshot)
        self.orch.on_tick(now_ms=self.clock.now_ms())
        return snapshot

    # -- journal accessors --

    def rows(self, stream: str) -> list:
        from agent.journal import replay
        return replay(self.journal_dir / f"{stream}.jsonl")

    def rows_of(self, stream: str, event_type: str) -> list:
        return [row for row in self.rows(stream)
                if row.get("event_type") == event_type]


# --- golden helpers (M5C-T5; thin runners — the byte goldens land in a later wave) --------


def synthetic_golden_script() -> list:
    """Pinned script: open 10 sh on scan ordinal 2, close on ordinal 4.

    The open row carries an EXPLICIT on-grid strategy limit: the M4
    `leg_cap_notional` rule prices an opening leg ONLY from its limit_price
    (marks may tighten but never substitute — FD-M4-16), so a limitless
    candidate is `unpriceable_candidate` at `can_open` (report-noted)."""
    return [
        {"on_bar": 2, "action": "open", "symbol": "AAPL", "qty": "10",
         "limit": "210.00"},
        {"on_bar": 4, "action": "close", "symbol": "AAPL", "qty": "10",
         "limit": None},
    ]


def run_synthetic_golden(journal_dir):
    """Deterministic synthetic open→mark→close run over the REAL orchestrator
    (FakeBroker ``partial_then_full`` — the §R 14 lifecycle, exercising
    broker_order_update + multi-slice fills; ScriptedSyntheticStrategy +
    permissive fixture config + the §Q status_script TRADABLE injection via
    ExecPipeline's full-day default windows; every open-driving script row
    carries an explicit on-grid limit — FD-M4-16) with the GOLDEN_RUN_ID and
    the pinned row clock. The tick cadence honors the EX-12 density rule: each
    decision bar is followed by an in-window quote B. Returns the ExecPipeline
    (closed) for inspection; regeneration = run + copy bytes (M5C-T5):
    the committed byte goldens live under ``tests/fixtures/execution/golden/``
    (orders.jsonl / fills.jsonl / positions.jsonl)."""
    strategy = ScriptedSyntheticStrategy(synthetic_golden_script())
    pipeline = ExecPipeline(
        journal_dir=journal_dir, run_id=GOLDEN_RUN_ID,
        strategy=strategy, exit_provider=strategy,
        fill_policy="partial_then_full")
    try:
        # bars 50..58: ordinal 1 fires at the first feature-complete bar.
        for index in range(50, 59):
            pipeline.tick_on_bar(index)
            # deliver the EX-12 in-window quote B so a pending open submits.
            pipeline.tick_quote_only(index)
    finally:
        pipeline.close()
    return pipeline


def run_observe_golden(journal_dir, *, events_path=None):
    """Deterministic observe-mode run for the §R 16 E2E: the REAL orchestrator
    driven by a ``ReplayQuoteFeed`` over the COMMITTED §Q fixture
    ``observe_session_tbbo.jsonl`` + the REAL committed config (gates OFF; no
    broker, no strategy — observe constructs no Broker-Protocol instance),
    GOLDEN_RUN_ID + the pinned row clock + the pinned report timestamp.

    Status windows ride the pinned M5C-T4 injection seam (EQUS.MINI has no
    status schema — §N honesty note: without injection every probe tick
    gate-fails ``market_state_not_tradable`` and the funnel never reaches a
    forecast), so the golden exercises the FULL probe → resolver → report
    funnel: decision rows, scored rows, calibration report.

    Returns ``{"orchestrator": <closed Orchestrator>, "report": dict,
    "report_path": Path}``; regeneration = run this helper and copy bytes
    (M5C-T5): the committed goldens are ``golden/observe_decisions.jsonl``,
    ``golden/observe_scored.jsonl`` and ``golden/observe_report.json``."""
    from agent.orchestrator import (
        build_replay_feed,
        schedule_provider_from_fixture,
    )

    events = Path(events_path) if events_path is not None \
        else OBSERVE_FIXTURE_PATH
    journal_dir = Path(journal_dir)
    feed = build_replay_feed(str(events))
    orch = Orchestrator(
        journal_dir=journal_dir, run_id=GOLDEN_RUN_ID,
        clock=feed.clock(), quote_view=feed.quote_view(),
        bar_reader=feed.bar_reader(),
        calendar_provider=schedule_provider_from_fixture(CALENDAR_FIXTURE_PATH),
        config=committed_assembled_config(),
        status_provider=status_script(full_day_windows(
            OBSERVE_FIXTURE_SYMBOL, OBSERVE_FIXTURE_SESSION_DATE)),
        row_clock=lambda: FIXED_WRITER_TS)
    report_path = journal_dir / "observe_report.json"
    try:
        if orch.mode != "observe":
            raise AssertionError(
                f"run_observe_golden derived mode {orch.mode!r}, expected "
                "'observe' (no broker / strategy / credentials were injected)")
        orch.run_with_feed(feed)
        report = orch.write_report(report_path,
                                   generated_ts_utc=GOLDEN_GENERATED_TS)
    finally:
        orch.close()
    return {"orchestrator": orch, "report": report,
            "report_path": report_path}
