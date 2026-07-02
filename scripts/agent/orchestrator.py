"""M5 §M — the IMPURE composer: startup/rehydrate, tick loop, OrderTask FSM,
post-submit watcher, kill wiring, strategy close path.

Frozen behavior is the M5 contract (rev 2): §M.1 process discipline, §M.2
startup steps 1-10 in FROZEN order, §M.3 tick loop steps 1-10 (incl. the rev-2
step-7 snapshot-assembly paragraph and the step-8 GLOBAL in-flight close guard
RC-1), §M.4 OrderTask FSM, §M.5 watcher (incl. ``late_fill_post_kill``), §M.6
kill sequence (rev 2), §M.7 close path, §P.3 id derivations, FD-M5-9/10/13/17/
21/22/23/24/25/26.

Import discipline (§3): module scope may import ``agent.broker.base`` (light
dataclasses) and ``agent.broker.order_state`` ONLY from the broker package;
``agent.broker.alpaca`` / ``agent.broker.fake`` / ``agent.broker.flatten_proxy``
are imported LAZILY inside the §M.2 step-9 mode-select branch / §M.6 kill
wiring — an observe run never has ``agent.broker.alpaca`` in ``sys.modules``
(M5C-T10). NO wall clock and NO sleep anywhere in this module (FD-M5-10): time
enters ONLY via the injected ms clock and the recorded-event instants;
``mint_run_id`` is pure given its inputs (the CLI supplies the wall values).

Documented build resolutions (faithful to the contract; report-listed):

1.  **Mode is DERIVED from structural ctor facts, never a flag (§M.2 step 9):**
    ``synthetic`` iff the injected strategy is a ``SyntheticStrategy``;
    else ``paper`` iff a broker was injected OR a ``credentials_path`` was
    given; else ``observe``. The CLI subcommands assert the derived mode.
2.  **The tick instant** (calendar phase, probe ``decision_ts_utc``,
    ``ts_read_utc`` provenance, resolver ``now_utc``) is the LATEST recorded
    ``ts_recv_utc`` observed across delivered quotes/bars — never a wall read.
3.  **§M.7 close-path rows:** the contract pins no ``paper_eligible`` value for
    ``would_close`` rows; journaled ``True`` (closes are actionable by
    construction — they ride the reduce path). A close tick with NO quote at
    all journals the ``reject{stage:"reduce_pricing"}`` row only (the
    ``strategy_decision`` row shape REQUIRES a ``quote_a`` provenance dict,
    which does not exist without a quote); retried next tick as a NEW decision.
4.  **Close-path terminal rows:** the shared WATCH terminal handling emits
    ``order_terminal`` for closes too, and a ``fill_divergence`` row with
    ``flag="unassessed"`` (the §M.7 chain journals no modeled fill, so the
    modeled side is null — FD-M5-20's "flag ALWAYS" honored without inventing
    a modeled basis).
5.  **§M.6 kill-flatten booking ids:** ``exec_ledger`` requires
    ``"o-"``-prefixed ledger ids, but the M0 kill actuator (unedited, FD-M4-4)
    builds ``flatten-<symbol>`` intent ids. The booking path therefore mints a
    ledger-side ``decision_id``/``order_id`` (§P.3 close-path event_basis with
    the kill tick's ms stamp, ``strategy_id="kill_flatten"``); the broker-side
    client_order_id remains ``flatten-<symbol>`` (M6's reconcile treats the
    kill actuator as the documented exception to FD-M5-7).
6.  **PaperBook seeding:** ``PaperBook.rehydrate`` returns the folded
    positions but the class exposes no public seeding API — the orchestrator
    seeds the private maps (``_positions``/``_fees_assessed``/
    ``_divergence_flags``) directly (reported as a wave-4 API gap).
7.  **Account/positions provider:** when none is injected, the broker's
    ``account()``/``positions()`` raw payloads feed the M4 parse chokepoints;
    ``source`` maps from ``broker.kind`` (``alpaca_paper``→``alpaca_paper``,
    ``fake``→``fixture``, anything else→``spy``). A non-mapping payload (e.g.
    a BrokerHttpError returned as data) parses to ``AccountInvalid`` /
    portfolio ``None`` (fail-closed); the F4 snapshot row is journaled either
    way.
8.  **Universe:** scan universe = ``agent_rules.universe.symbols``; the
    market-state refresh set additionally unions every symbol with a known
    quote (observe mode has an empty committed universe but the probe still
    needs fresh verdicts).
9.  **modeled-fill fee block:** ``fees_assumed`` on the buy-side
    ``modeled_execution_fill`` row is the §J zero buy assumption; the notional
    operand uses the modeled cost when present else ``capped_limit×qty``
    (``fees_for`` validates positivity; the buy side returns the zero block
    regardless).
10. **ExitInstruction for a symbol with no open book position is DROPPED
    silently** (there is no position_id to key the §P.3 close decision on; the
    broker is the position-of-record and M6 reconcile owns drift).
"""
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from agent import config as agent_config
from agent.backtest_gate import ARTIFACTS_DIR, verify_artifact
from agent.bar_series import MidBarSeriesReader, _parse_utc
from agent.broker_reconcile import (
    NOT_FOUND,
    PROBE_FAILED,
    DriftFinding,
    ReconcileError,
    ReconcilePassResult,
    canonical_decimal_str,
    diff_cash,
    diff_positions,
    identity_note,
    make_finding,
    require_phase,
    resolve_order_probe,
)
from agent.broker.base import OrderIntent
from agent.broker.order_state import (
    BrokerOrder,
    FillDelta,
    OrderInvalid,
    classify_rejection,
    fill_delta,
    parse_order_payload,
)
from agent.calibration import AsOfClimatology, ForecastResolver, ScoredLedger
from agent.candidate import Candidate
from agent.corporate_actions import BrokerAdjustDetector
from agent.exec_ledger import ExecLedger, rehydrate_exec_state, replay_orders
from agent.exec_reasons import ExecError, TERMINAL_STATES
from agent.execution_config import (
    ExecutionConfig,
    FLATTEN_CAP_BPS,
    SUBMIT_RECOVERY_ATTEMPTS,
)
from agent.execution_preflight import (
    DecisionStamp,
    PreflightInputs,
    PreflightRejected,
    PreflightStale,
    bind_runtime,
    mint_open_token,
    mint_reduce_only_token,
    unbind_runtime,
    void_token,
)
from agent.execution_realism import bind_modeled_fill_id, assess_divergence, model_fill
from agent.feature_engine import FeatureEngine, FeatureView
from agent.fees import fees_for
from agent.gates import opening_allowed
from agent.journal import replay as journal_replay
from agent.market_calendar import (
    FixtureScheduleProvider,
    MarketCalendar,
    SessionPhase,
    UnknownSessionDate,
)
from agent.market_state import (
    HaltReason,
    HaltState,
    LuldBand,
    LuldState,
    LuldTier,
    Nbbo,
    SsrState,
    StatusFlags,
    Tradability,
    TradabilityDecider,
    TradabilityInputs,
)
from agent.market_state_cache import MarketStateCache
from agent.order_pricing import reduce_cap
from agent.paper_book import PaperBook
from agent.quote_quality import QuoteSnapshot, evaluate as evaluate_quote
from agent.reconcile_ledger import (
    JournalCorruption,
    ReconcileLedger,
    rehydrate_reconcile_state,
    replay_reconcile,
)
from agent.risk.account_state import (
    AccountInvalid,
    AccountStore,
    Mark,
    parse_account_payload,
    parse_positions_payload,
    portfolio_is_stale,
)
from agent.risk.can_open import RiskEngine
from agent.risk.intraday_margin import (
    IntradayMarginModel,
    classify_iml_reducing,
    observation_from_read,
)
from agent.risk.loss_limits import LossLimitsMonitor
from agent.risk.pdt_compat import BrokerRejectionObservation, LegacyPdtCompatMode
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_kill import MAX_ACCOUNT_BLIND_MS, RiskKillSwitch
from agent.risk.risk_ledger import RiskLedger, replay_risk, rehydrate_risk_state
from agent.run_lock import RunLock, RunLockHeld
from agent.secrets_runtime import (
    assemble_gates_view,
    load_alpaca_paper_credentials,
    load_run_gates,
)
from agent.serializer import BrokerUSD, ModeledUSD, as_broker_usd, row_hash
from agent.signal_config import SignalConfig
from agent.signal_snapshot import GateFail, assemble
from agent.status_ledger import (
    EVT_BROKER_ADJUST_FREEZE,
    StatusLedger,
    rehydrate_state,
    replay_status,
)
from agent.strategies.calibration_probe import CalibrationProbe, DecisionLedger
from agent.strategies.synthetic import ExitProvider, ScriptedSyntheticStrategy, SyntheticStrategy
from agent.strategy import ScanContext
from recorder.persistence import EventWriter, replay_stream

__all__ = [
    "ExecError",
    "Orchestrator",
    "ScriptedSyntheticStrategy",
    "build_replay_feed",
    "instrument_map_from_events",
    "mint_run_id",
    "schedule_provider_from_fixture",
    "status_provider_from_windows",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# status-stream event types (§M.1 / §O.2 — the EventWriter convention)
STATUS_EVT_RUN_GATES_FILE = "run_gates_file"
STATUS_EVT_MODE_DEGRADED = "mode_degraded"
STATUS_EVT_LOCK_RECLAIMED = "run_lock_reclaimed"
STATUS_EVT_HALTED_LATCH = "halted_latch"

_BAR_INTERVAL = "1m"
_ZERO = Decimal("0")

# broker.kind -> M4 ACCOUNT_SOURCES / §2.4 FILL_SOURCES members (resolution 7)
_ACCOUNT_SOURCE_BY_KIND = {"alpaca_paper": "alpaca_paper", "fake": "fixture",
                           "spy": "spy"}
_FILL_SOURCE_BY_KIND = {"alpaca_paper": "alpaca_paper", "fake": "fake",
                        "spy": "fake"}

# A LULD band wide enough that any test/fixture NBBO stays strictly inside —
# the M2 step-5b band-presence rule needs SOME band for LULD NORMAL in RTH.
_WIDE_BAND = LuldBand(reference_px=Decimal("100"), lower_px=Decimal("0.0001"),
                      upper_px=Decimal("1000000"), tier=LuldTier.TIER1,
                      doubled=False)


def _canonical_utc_str(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _bar_key(symbol: str, bucket_end_utc: str) -> str:
    """The M3 frozen bar-key format over the canonical surface form."""
    return f"{symbol}|{_BAR_INTERVAL}|{_canonical_utc_str(_parse_utc(bucket_end_utc))}"


def _mint_reconcile_id(run_id: str, phase: str, session_date_et: str,
                       occurrence: int) -> str:
    return "rc-" + row_hash({
        "run_id": run_id,
        "phase": phase,
        "session_date_et": session_date_et,
        "occurrence": occurrence,
    })


def _mint_drift_id(reconcile_id: str, finding: DriftFinding) -> str:
    return "rd-" + row_hash({
        "reconcile_id": reconcile_id,
        "kind": finding.kind,
        "symbol": finding.symbol,
        "field": finding.field,
        "position_id": finding.position_id,
        "local_order_id": finding.local_order_id,
    })


def _mint_adjust_id(reconcile_id: str, position_id: str) -> str:
    return "adj-" + row_hash({
        "reconcile_id": reconcile_id,
        "position_id": position_id,
    })


def _finding_key(finding: DriftFinding):
    return (finding.kind, finding.symbol or "", finding.position_id or "",
            finding.local_order_id or "")


def _note_key(note):
    return (note[0], note[1] or "", note[2])


def _residue_from_row(row: Optional[dict]) -> Optional[DriftFinding]:
    if row is None:
        return None
    return DriftFinding(
        kind=row["kind"],
        symbol=row.get("symbol"),
        field=row["field"],
        local=row.get("local"),
        broker=row.get("broker"),
        diff=row.get("diff"),
        action=row["action"],
        position_id=row.get("position_id"),
        local_order_id=row.get("local_order_id"),
        broker_order_id=row.get("broker_order_id"),
    )


def _carried_residue_finding(residue: DriftFinding) -> DriftFinding:
    return DriftFinding(
        kind=residue.kind,
        symbol=residue.symbol,
        field=residue.field,
        local=residue.local,
        broker=residue.broker,
        diff=residue.diff,
        action=residue.action,
        position_id=residue.position_id,
        local_order_id=residue.local_order_id,
        broker_order_id=residue.broker_order_id,
    )


def mint_run_id(*, host: str, pid: int, now_utc) -> str:
    """§M.2 step 3 — PURE given its inputs (the CLI supplies wall values;
    tests inject run_id directly and never call this)."""
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    ts_utc = _canonical_utc_str(now_utc)
    return "run-" + stamp + "-" + row_hash(
        {"host": host, "pid": pid, "ts_utc": ts_utc})[:12]


def schedule_provider_from_fixture(path) -> FixtureScheduleProvider:
    fixture = agent_config.load(path)
    return FixtureScheduleProvider(fixture, pin=fixture["pin"])


def build_replay_feed(path, *, symbols=None, refresh_cadence_ms=1000):
    """CLI seam (§3: __main__ imports orchestrator/config/secrets_runtime
    only, so the feed is reached THROUGH this module)."""
    from agent.marketdata.replay_feed import ReplayQuoteFeed
    return ReplayQuoteFeed(path, symbols=symbols,
                           refresh_cadence_ms=refresh_cadence_ms)


def instrument_map_from_events(path) -> Dict[str, int]:
    """symbol -> instrument_id from a recorder events.jsonl (first seen)."""
    mapping: Dict[str, int] = {}
    for row in replay_stream(path):
        symbol = row.get("symbol")
        instrument_id = row.get("instrument_id")
        if isinstance(symbol, str) and isinstance(instrument_id, int) \
                and symbol not in mapping:
            mapping[symbol] = instrument_id
    return mapping


def status_provider_from_windows(windows: Mapping):
    """M5C-T4 — the pinned status-injection seam: ``windows`` = {symbol:
    [(start_utc_iso, end_utc_iso), ...]}. Inside a window the provider yields
    healthy flags (halt NONE / luld NORMAL with a wide band / ssr INACTIVE);
    outside (or for an unknown symbol) it yields None ⇒ the §M.3 step-2
    builder falls back to the UNKNOWN fail-closed defaults."""
    parsed = {}
    for symbol, spans in dict(windows).items():
        parsed[symbol] = [(_parse_utc(start), _parse_utc(end))
                          for start, end in spans]

    def provider(symbol: str, ts_utc: str) -> Optional[StatusFlags]:
        spans = parsed.get(symbol)
        if not spans:
            return None
        instant = _parse_utc(ts_utc)
        for start, end in spans:
            if start <= instant <= end:
                return StatusFlags(
                    symbol=symbol, halt=HaltState.NONE,
                    halt_reason=HaltReason.NONE, luld=LuldState.NORMAL,
                    luld_band=_WIDE_BAND, ssr=SsrState.INACTIVE,
                    prior_close=None, source="calendar")
        return None

    return provider


class _LatestQuoteView:
    """Minimal QuoteView for feed-less (paper startup-only) compositions."""

    def __init__(self):
        self._latest: Dict[Tuple[str, int], QuoteSnapshot] = {}

    def put(self, snapshot: QuoteSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot

    def latest(self, symbol, instrument_id):
        return self._latest.get((symbol, instrument_id))


class _ZeroClock:
    """Feed-less paper startup clock (no wall reads; M5 paper has no live feed)."""

    def now_ms(self) -> int:
        return 0


class _TickLimit(Exception):
    """Internal: stop a feed-driven run after --ticks N (observe CLI)."""


@dataclass
class _OrderTask:
    """One in-flight order (FD-M5-21: at most ONE globally, open OR close)."""

    kind: str                       # "open" | "close"
    state: str                      # "await_latency" | "watch"
    decision_id: str
    symbol: str
    instrument_id: int
    strategy_id: str
    side: str
    qty: Decimal
    stamp: DecisionStamp
    session_date_et: str
    candidate: Optional[Candidate] = None
    risk_verdict: Optional[object] = None
    risk_verdict_row: Optional[dict] = None
    risk_now_ms: Optional[int] = None
    order_id: Optional[str] = None
    preflight: Optional[object] = None
    capped_limit: Optional[Decimal] = None
    modeled: Optional[object] = None
    quote_b: Optional[QuoteSnapshot] = None
    bound_epoch: Optional[int] = None
    broker_order_id: Optional[str] = None
    last_state: Optional[str] = None
    prev_emitted: Optional[BrokerOrder] = None
    deltas: List[FillDelta] = field(default_factory=list)
    last_poll_ms: Optional[int] = None
    cancel_causes: set = field(default_factory=set)
    position_id: Optional[str] = None
    close_reason: Optional[str] = None
    adopted_from_restart: bool = False


class Orchestrator:
    """§M — composes the seven tiers around the M4 risk core. Construction IS
    the frozen §M.2 startup sequence; ``on_bar_complete``/``on_tick`` ARE the
    §M.3 tick loop (driven by a ReplayQuoteFeed via ``run_with_feed`` or
    directly by tests)."""

    def __init__(self, *, journal_dir, run_id: str, clock, quote_view,
                 bar_reader: Optional[MidBarSeriesReader] = None,
                 calendar_provider=None,
                 config: Optional[dict] = None, overlay: Optional[dict] = None,
                 broker=None, strategy=None, status_provider=None,
                 exit_provider=None, account_provider=None,
                 credentials_path=None, run_gates_path=None,
                 instrument_ids: Optional[Mapping[str, int]] = None,
                 durable_ids: Optional[Mapping[str, object]] = None,
                 artifacts_dir: Optional[str] = None,
                 fill_policy: str = "immediate_full",
                 row_clock: Optional[Callable[[], str]] = None) -> None:
        self._journal_dir = Path(journal_dir)
        self._closed = False
        self._runtime_bound = False

        # ---- step 1: lock --------------------------------------------------
        self._lock = RunLock(self._journal_dir)
        lock_reclaimed = self._lock.acquire()
        try:
            self._startup(
                run_id=run_id, clock=clock, quote_view=quote_view,
                bar_reader=bar_reader, calendar_provider=calendar_provider,
                config=config, overlay=overlay, broker=broker,
                strategy=strategy, status_provider=status_provider,
                exit_provider=exit_provider, account_provider=account_provider,
                credentials_path=credentials_path,
                run_gates_path=run_gates_path, instrument_ids=instrument_ids,
                durable_ids=durable_ids,
                artifacts_dir=artifacts_dir, fill_policy=fill_policy,
                row_clock=row_clock, lock_reclaimed=lock_reclaimed)
        except BaseException:
            # §M.2: any step failing => fail-loud exit, nothing submitted —
            # and nothing left bound/locked behind us.
            if self._runtime_bound:
                unbind_runtime()
            self._lock.release()
            raise

    # ------------------------------------------------------------------ startup

    def _startup(self, *, run_id, clock, quote_view, bar_reader,
                 calendar_provider, config, overlay, broker, strategy,
                 status_provider, exit_provider, account_provider,
                 credentials_path, run_gates_path, instrument_ids,
                 durable_ids, artifacts_dir, fill_policy, row_clock,
                 lock_reclaimed):
        # ---- step 2: config (parse fail-loud; gates view paper-only;
        #              rules_hash PRE-substitution — M5C-B7/M5C-S4) ----------
        if config is None:
            assembled = {
                "agent_rules": agent_config.load(
                    _REPO_ROOT / "config" / "agent_rules.json"),
                "risk_rules": agent_config.load(
                    _REPO_ROOT / "config" / "risk_rules.json"),
            }
            self.config_source = "committed"
        else:
            assembled = config
            self.config_source = "in_memory"
        if overlay is not None:
            assembled = agent_config.tighten_only_merge(assembled, overlay)
        self._assembled = assembled
        self._signal_config = SignalConfig.from_config(assembled["agent_rules"])
        self._risk_config = RiskConfig.from_config(assembled)
        self._exec_config = ExecutionConfig.from_config(assembled)
        self.rules_hash = self._exec_config.rules_hash

        # Mode is DERIVED (resolution 1); broker construction stays step 9.
        if isinstance(strategy, SyntheticStrategy):
            mode = "synthetic"
        elif broker is not None or credentials_path is not None:
            mode = "paper"
        else:
            mode = "observe"
        self.mode = mode

        gates_reading = None
        if mode == "paper":
            gates_reading = load_run_gates(
                run_gates_path if run_gates_path is not None
                else self._journal_dir / "run_gates_never_present.json")
            self._gates_view = assemble_gates_view(assembled, gates_reading)
        else:
            # observe: committed gates (False); synthetic: the in-memory
            # fixture config IS the gates view (FD-M5-2 §O.2.1).
            self._gates_view = assembled

        # ---- step 3: run_id (tests inject; the CLI mints via mint_run_id) --
        if not isinstance(run_id, str) or not run_id:
            raise ExecError("run_id must be a non-empty string (tests inject; "
                            "the CLI mints via mint_run_id)")
        self.run_id = run_id
        self._clock = clock
        self._quote_view = quote_view
        self._status_provider = status_provider
        self._strategy = strategy
        self._exit_provider = exit_provider if exit_provider is not None else (
            strategy if isinstance(strategy, ExitProvider) else None)
        self._fill_policy = fill_policy
        self._artifacts_dir = (artifacts_dir if artifacts_dir is not None
                               else str(_REPO_ROOT / ARTIFACTS_DIR))
        self._instrument_ids: Dict[str, int] = dict(instrument_ids or {})
        if calendar_provider is None:
            raise ExecError("calendar_provider is required (§M.2 step 5: the "
                            "IntradayMarginModel needs a ScheduleProvider)")
        self._schedule_provider = calendar_provider
        self._calendar = MarketCalendar(calendar_provider)
        self._decider = TradabilityDecider()

        universe = assembled["agent_rules"].get("universe", {}).get("symbols", [])
        self._universe: Tuple[str, ...] = tuple(universe)

        # ---- step 4: ledgers ------------------------------------------------
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        writer_kwargs = {} if row_clock is None else {"clock": row_clock}
        self._decisions_path = self._journal_dir / "decisions.jsonl"
        self._scored_path = self._journal_dir / "forecast_scored.jsonl"
        self._risk_path = self._journal_dir / "risk.jsonl"
        self._reconcile_path = self._journal_dir / "reconcile_alerts.jsonl"
        self._status_path = self._journal_dir / "status.jsonl"
        self._decision_ledger = DecisionLedger(
            EventWriter(self._decisions_path, run_id, **writer_kwargs),
            rules_hash=self._signal_config.rules_hash)
        self._scored_ledger = ScoredLedger(
            EventWriter(self._scored_path, run_id, **writer_kwargs),
            rules_hash=self._signal_config.rules_hash)
        self._risk_ledger = RiskLedger(
            EventWriter(self._risk_path, run_id, **writer_kwargs),
            rules_hash=self.rules_hash)
        self._exec_ledger = ExecLedger(
            orders=EventWriter(self._journal_dir / "orders.jsonl", run_id,
                               **writer_kwargs),
            fills=EventWriter(self._journal_dir / "fills.jsonl", run_id,
                              **writer_kwargs),
            positions=EventWriter(self._journal_dir / "positions.jsonl",
                                  run_id, **writer_kwargs),
            rules_hash=self.rules_hash)
        self._reconcile_ledger = ReconcileLedger(
            EventWriter(self._reconcile_path, run_id, **writer_kwargs),
            rules_hash=self.rules_hash)
        self._status_writer = EventWriter(self._status_path, run_id,
                                          **writer_kwargs)
        self._status_ledger = StatusLedger(
            self._status_writer, rules_hash=self.rules_hash)
        if lock_reclaimed:
            self._status_row(STATUS_EVT_LOCK_RECLAIMED,
                             {"note": "stale dead-pid lock reclaimed"})
        if gates_reading is not None:
            self._status_row(STATUS_EVT_RUN_GATES_FILE, dict(gates_reading))

        # ---- step 5: risk rehydrate (the M4 §O obligations, VERBATIM order:
        #              fold risk.jsonl + seed ALL FOUR components BEFORE the
        #              first AccountStore.put / can_open) ---------------------
        risk_rows = replay_risk(self._risk_path)
        rehydrated = rehydrate_risk_state(risk_rows)
        self._margin = IntradayMarginModel(calendar=self._schedule_provider,
                                           ledger=self._risk_ledger)
        self._margin.rehydrate(risk_rows)
        self._risk_kill = RiskKillSwitch(cfg=self._risk_config,
                                         ledger=self._risk_ledger)
        self._risk_kill.rehydrate(risk_rows)
        self._pdt = LegacyPdtCompatMode(ledger=self._risk_ledger,
                                        rehydrated_state=rehydrated["pdt"])
        self._loss = LossLimitsMonitor(cfg=self._risk_config,
                                       ledger=self._risk_ledger)
        self._loss.rehydrate(risk_rows)
        self._account_store = AccountStore(clock=clock)
        self._engine = RiskEngine(cfg=self._risk_config,
                                  gates_config=self._gates_view,
                                  run_id=run_id)

        # ---- step 6: exec rehydrate (PURE — NO broker call; M5C-S2) ---------
        order_rows = replay_orders(self._journal_dir / "orders.jsonl")
        fill_rows = journal_replay(self._journal_dir / "fills.jsonl")
        position_rows = journal_replay(self._journal_dir / "positions.jsonl")
        exec_state = rehydrate_exec_state(
            order_rows, fill_rows, position_rows, run_id=run_id,
            book_rehydrate=PaperBook.rehydrate)
        self._book = PaperBook(
            ledger=self._exec_ledger, run_id=run_id,
            quote_staleness_ms_max=self._exec_config.quote_staleness_ms_max,
            spread_bps_max=self._exec_config.spread_bps_max)
        for position_id, position in exec_state["positions"].items():
            # resolution 6: PaperBook exposes no public seed API (wave-4 gap).
            self._book._positions[position_id] = position
            self._book._fees_assessed.setdefault(position_id, Decimal("0.00"))
            self._book._divergence_flags.setdefault(position_id, "unassessed")
            if position.symbol not in self._instrument_ids:
                self._instrument_ids[position.symbol] = position.instrument_id
        self._open_deny: set = set(exec_state["open_deny"])
        self._recovery_queue: List[dict] = []
        dangling = exec_state["open_orders"]
        attempt_rows = {row.get("order_id"): row for row in order_rows
                        if row.get("event_type") == "order_submit_attempt"}
        for order_id in sorted(dangling):
            attempt = attempt_rows.get(order_id)
            if attempt is None:
                continue  # no write-ahead row => the order never existed
            self._recovery_queue.append(attempt)
        if mode != "paper" and self._recovery_queue:
            # observe/offline orphans resolve NOW (step 6, M5C-S2).
            self._resolve_offline_orphans()

        # ---- step 6.5: reconcile rehydrate (PURE — NO broker call) ----------
        reconcile_rows = replay_reconcile(self._reconcile_path)
        reconcile_state = rehydrate_reconcile_state(reconcile_rows)
        self._latest_baseline = reconcile_state["latest_baseline"]
        self._pass_count = reconcile_state["pass_count"]
        self._reconcile_drift_in_window = reconcile_state["drift_in_window"]
        self._outstanding_cash_residue = _residue_from_row(
            reconcile_state["outstanding_cash_residue"])
        self._reconciled_eod_sessions: set = set()
        self._durable_ids = dict(durable_ids or {})
        self._observe_suspended: set = set()
        self._noted_durables: set = set()
        self._detector = BrokerAdjustDetector()
        self._durable_seed_qty: Dict[str, Decimal] = {}
        status_state = rehydrate_state(replay_status(self._status_path))
        frozen_rows = [
            row for row in status_state["corporate_actions"].values()
            if row.get("event_type") == EVT_BROKER_ADJUST_FREEZE
        ]
        self._frozen_durables: set = {
            row["durable_key"] for row in frozen_rows}
        self._frozen_durable_symbols: set = {
            row["symbol"] for row in frozen_rows if row.get("symbol")}
        self._rehydrate_detector_baselines()
        self._drift_latch = bool(reconcile_state["latched"]
                                 or self._frozen_durables)

        # ---- step 7: HALTED latch -------------------------------------------
        if self._risk_kill.state == "halted":
            self._status_row(STATUS_EVT_HALTED_LATCH, {
                "kill_state": self._risk_kill.state,
                "kill_generation": self._risk_kill.generation,
                "residual": sorted(self._risk_kill.residual_symbols()),
            })

        # ---- step 8: bind runtime (FD-M5-13) ---------------------------------
        bind_runtime(clock=clock,
                     kill_generation_source=lambda: self._risk_kill.generation)
        self._runtime_bound = True

        # ---- step 9: mode select (credentials construct the broker — gates
        #              govern OPENS only, M5C-S3; degrade-to-observe loud) -----
        self.broker = broker
        if self.broker is None:
            if mode == "synthetic":
                from agent.broker.fake import FakeBroker  # lazy (§3, M5C-T10)
                self.broker = FakeBroker(
                    quote_view=self._quote_view, clock=clock,
                    instrument_ids=self._instrument_ids,
                    fill_policy=fill_policy)
            elif mode == "paper":
                creds = Path(credentials_path) if credentials_path else None
                if creds is not None and creds.exists():
                    from agent.broker.alpaca import AlpacaPaperBroker  # lazy
                    self.broker = AlpacaPaperBroker(
                        credentials_loader=lambda: load_alpaca_paper_credentials(creds))
                else:
                    self.mode = mode = "observe"  # degrade — loud status row
                    self._status_row(STATUS_EVT_MODE_DEGRADED, {
                        "requested_mode": "paper",
                        "effective_mode": "observe",
                        "reason": "credentials_missing",
                    })
        # Wall 1 (FD-M5-8) runs on injected pairs too — type identity.
        if isinstance(self._strategy, SyntheticStrategy):
            from agent.broker.fake import FakeBroker  # lazy
            if type(self.broker) is not FakeBroker:
                from agent.broker.alpaca import SyntheticConfinementError
                raise SyntheticConfinementError(
                    "synthetic strategy requires a FakeBroker (wall 1, "
                    "FD-M5-8 type-identity)")
        self._account_provider = account_provider
        self._account_source = "fixture"
        self._fill_source = "fake"
        if self.broker is not None:
            kind = getattr(self.broker, "kind", "spy")
            self._account_source = _ACCOUNT_SOURCE_BY_KIND.get(kind, "spy")
            self._fill_source = _FILL_SOURCE_BY_KIND.get(kind, "fake")
        if account_provider is not None:
            self._account_source = "fixture"

        # Signal tier collaborators (probe ALWAYS runs — the observe core).
        reader = bar_reader if bar_reader is not None \
            else MidBarSeriesReader([], [])
        self._bar_reader = reader
        self._feature_engine = FeatureEngine(reader=reader,
                                             config=self._signal_config,
                                             clock=clock)
        self._feature_view = FeatureView(engine=self._feature_engine,
                                         clock=clock)
        self._cache = MarketStateCache(clock=clock)
        self._climatology = AsOfClimatology(
            min_samples=self._signal_config.min_reference_samples)
        self._probe = CalibrationProbe(
            config=self._signal_config, calendar=self._calendar,
            market_state_cache=self._cache, feature_view=self._feature_view,
            quote_view=self._quote_view, ledger=self._decision_ledger,
            climatology=self._climatology, run_id=run_id, clock=clock)
        self._resolver = ForecastResolver(
            reader=reader, ledger=self._scored_ledger,
            scored_stream_path=self._scored_path,
            climatology=self._climatology)

        # mutable tick state
        self._task: Optional[_OrderTask] = None
        self._pending_bars: List[object] = []
        self._last_scanned_bar: Dict[str, str] = {}
        self._instant_utc: Optional[str] = None
        self._portfolio = None
        self._last_account_ms: Optional[int] = None
        self._last_phase: Optional[SessionPhase] = None
        self._closed_sessions: set = set()
        self._kill_skipped_latch = False
        self._kill_blind_since_ms: Optional[int] = None
        self._artifact_checks: Dict[Tuple[str, str, str], object] = {}
        self._outstanding_open_tokens: List[Tuple[object, _OrderTask]] = []
        self._position_marked: set = set()
        self._tick_budget: Optional[int] = None
        self._ticks_run = 0

        # ---- step 10: broker-touching order recovery (after mode select;
        #               runs even when the gates view is False — M5C-S3) ------
        if self.mode == "paper" and self.broker is not None:
            self._recover_dangling_orders()
        elif self._recovery_queue:
            # RC-3: degrade-to-observe resolves queued candidates NOW.
            self._resolve_offline_orphans()

    # ---------------------------------------------------------------- helpers

    def _status_row(self, event_type: str, fields: dict) -> None:
        body = {"v": 1}
        body.update(fields)
        body["rules_hash"] = self.rules_hash
        self._status_writer.record(event_type, body)

    @property
    def open_deny(self) -> Tuple[str, ...]:
        return tuple(sorted(self._open_deny))

    @property
    def book(self) -> PaperBook:
        return self._book

    @property
    def risk_kill(self) -> RiskKillSwitch:
        return self._risk_kill

    @property
    def in_flight(self) -> bool:
        return self._task is not None

    @property
    def drift_latched(self) -> bool:
        return self._drift_latch

    @property
    def ticks_run(self) -> int:
        return self._ticks_run

    def ensure_eod_reconcile(self, session_date_et: str, *, ts_utc: str,
                             now_ms: int):
        """Idempotent EOD reconcile for the session runner: a no-op when the
        in-loop session edge already ran it (or outside paper mode). Shares the
        session-edge guard set, so EOD runs exactly once per session date."""
        if self.mode != "paper":
            return None
        if session_date_et in self._reconciled_eod_sessions:
            return None
        self._reconciled_eod_sessions.add(session_date_et)
        return self.run_reconcile(phase="eod", ts_utc=ts_utc, now_ms=now_ms)

    # ----------------------------------------------------------- M6 reconcile

    def _reconcile_ts(self, ts_utc: Optional[str]) -> str:
        return (ts_utc or self._instant_utc
                or "1970-01-01T00:00:00.000000Z")

    def _open_positions_by_symbol(self) -> Dict[str, Tuple]:
        open_seq = self._position_open_seq_by_id()
        grouped: Dict[str, List[object]] = {}
        for position in self._book._positions.values():
            if getattr(position, "status", None) != "open":
                continue
            grouped.setdefault(position.symbol, []).append(position)
        return {symbol: tuple(sorted(positions,
                                     key=lambda p: (
                                         open_seq.get(p.position_id, -1),
                                         p.position_id),
                                     reverse=True))
                for symbol, positions in grouped.items()}

    def _position_open_seq_by_id(self) -> Dict[str, int]:
        seq_by_id: Dict[str, int] = {}
        for row in journal_replay(self._journal_dir / "positions.jsonl"):
            if row.get("event_type") != "position_open":
                continue
            position_id = row.get("position_id")
            seq = row.get("seq")
            if isinstance(position_id, str) and isinstance(seq, int):
                seq_by_id[position_id] = seq
        return seq_by_id

    def _fill_rows(self) -> List[dict]:
        return journal_replay(self._journal_dir / "fills.jsonl")

    def _fills_seq_watermark(self, fill_rows: List[dict]) -> int:
        seqs = [row["seq"] for row in fill_rows
                if row.get("event_type") == "broker_fill"]
        return max(seqs) if seqs else 0

    def _fills_cum_watermarks(self, fill_rows: List[dict]) -> Dict[str, Decimal]:
        marks: Dict[str, Decimal] = {}
        for row in fill_rows:
            if row.get("event_type") != "broker_fill":
                continue
            order_id = row.get("order_id")
            if not isinstance(order_id, str):
                continue
            qty = Decimal(str(row.get("cum_filled_qty", "0")))
            marks[order_id] = max(marks.get(order_id, _ZERO), qty)
        return marks

    def _fill_rows_since_baseline(self, fill_rows: List[dict]) -> List[dict]:
        baseline = self._latest_baseline
        if baseline is None:
            return []
        watermark = int(baseline["fills_seq_watermark"])
        seen_before = {
            row.get("fill_id") for row in fill_rows
            if row.get("event_type") == "broker_fill" and row["seq"] <= watermark
        }
        return [row for row in fill_rows
                if row.get("event_type") == "broker_fill"
                and row["seq"] > watermark
                and row.get("fill_id") not in seen_before]

    def _checked_symbols(self, local_positions: Mapping,
                         portfolio) -> List[str]:
        broker_symbols = {position.symbol for position in portfolio.positions}
        return sorted(set(local_positions) | broker_symbols)

    def _baseline_positions(self, portfolio) -> List[dict]:
        return [{"symbol": position.symbol,
                 "qty": canonical_decimal_str(position.qty)}
                for position in portfolio.positions]

    def _wrapped_probe(self, order_id: str):
        try:
            result = self.broker.order_status(order_id)
        except KeyError:
            return NOT_FOUND
        except Exception as err:  # noqa: BLE001 — probe failures are DATA.
            if getattr(err, "status_code", None) == 404:
                return NOT_FOUND
            return PROBE_FAILED
        if not isinstance(result, Mapping):
            if getattr(result, "status_code", None) == 404:
                return NOT_FOUND
            return PROBE_FAILED
        parsed = parse_order_payload(result, source=self._fill_source)
        if isinstance(parsed, OrderInvalid):
            return PROBE_FAILED
        return parsed

    def _current_open_orders(self) -> Dict[str, dict]:
        order_rows = replay_orders(self._journal_dir / "orders.jsonl")
        fill_rows = self._fill_rows()
        position_rows = journal_replay(self._journal_dir / "positions.jsonl")
        state = rehydrate_exec_state(
            order_rows, fill_rows, position_rows, run_id=self.run_id,
            book_rehydrate=PaperBook.rehydrate)
        attempts = {
            row["order_id"]: row for row in order_rows
            if row.get("event_type") == "order_submit_attempt"
        }
        enriched = {}
        for order_id, row in state["open_orders"].items():
            attempt = attempts.get(order_id)
            if attempt is None:
                enriched[order_id] = row
                continue
            merged = dict(attempt)
            merged.update(row)
            for key in ("symbol", "instrument_id", "strategy_id",
                        "decision_id", "quote_b"):
                if key not in merged or merged[key] is None:
                    merged[key] = attempt.get(key)
            enriched[order_id] = merged
        return enriched

    def _session_over(self, order_row: Mapping, ts_utc: str) -> bool:
        quote = order_row.get("quote_b")
        order_ts = quote.get("ts_recv_utc") if isinstance(quote, Mapping) else None
        if not isinstance(order_ts, str):
            order_ts = order_row.get("ts_utc")
        if not isinstance(order_ts, str):
            return False
        try:
            order_session = self._calendar.session_date_for(order_ts)
            current_session = self._calendar.session_date_for(ts_utc)
            if order_session < current_session:
                return True
            if order_session != current_session:
                return False
            schedule = self._schedule_provider.schedule_for(order_session)
        except UnknownSessionDate:
            return False
        close_utc = schedule.rth_close_utc
        return bool(close_utc and _parse_utc(ts_utc) >= _parse_utc(close_utc))

    def _frozen_symbols(self) -> set:
        frozen = set(self._frozen_durable_symbols)
        for symbol, durable in self._durable_ids.items():
            if durable.key() in self._frozen_durables:
                frozen.add(symbol)
            elif self._detector.is_frozen(durable):
                frozen.add(symbol)
        return frozen

    def _local_folded_qty(self, symbol: str) -> Decimal:
        total = Decimal("0")
        for position in self._book._positions.values():
            if position.symbol == symbol and position.status == "open":
                total += position.qty
        return total

    def _rehydrate_detector_baselines(self) -> None:
        baseline = self._latest_baseline
        if baseline is None:
            return
        by_symbol = {
            row["symbol"]: Decimal(row["qty"])
            for row in baseline.get("positions", [])
        }
        seeded = set(baseline.get("durable_seeded", []))
        for symbol, durable in self._durable_ids.items():
            key = durable.key()
            if key not in seeded or symbol not in by_symbol:
                continue
            qty = by_symbol[symbol]
            self._detector.seed_baseline(durable, qty)
            self._durable_seed_qty[key] = qty

    def _durable_seed_candidates(self, portfolio, defer, phase: str) -> List[str]:
        if phase == "immediate":
            return []
        frozen = self._frozen_symbols()
        seeded = []
        for symbol, durable in sorted(self._durable_ids.items()):
            if symbol in defer or symbol in frozen:
                continue
            broker_qty = portfolio.qty_for(symbol)
            if broker_qty == 0:
                continue
            if self._local_folded_qty(symbol) != broker_qty:
                continue
            seeded.append(durable.key())
        return seeded

    def _commit_durable_seeds(self, portfolio, durable_keys) -> None:
        wanted = set(durable_keys)
        for symbol, durable in self._durable_ids.items():
            key = durable.key()
            if key not in wanted:
                continue
            qty = portfolio.qty_for(symbol)
            self._detector.seed_baseline(durable, qty)
            self._durable_seed_qty[key] = qty
            self._observe_suspended.discard(symbol)

    def _note_missing_durable_ids(self, checked_symbols, portfolio,
                                  local_positions, notes) -> None:
        for symbol in checked_symbols:
            if symbol in self._durable_ids or symbol in self._noted_durables:
                continue
            local_qty = sum((position.qty for position in
                             local_positions.get(symbol, ())), Decimal("0"))
            if local_qty == 0 and portfolio.qty_for(symbol) == 0:
                continue
            notes.append(("durable_id_missing", symbol, ""))
            self._noted_durables.add(symbol)

    def _durable_observation_blocked(self, symbol: str) -> bool:
        if symbol in self._observe_suspended:
            return True
        if self._task is not None and self._task.symbol == symbol:
            return True
        return symbol in self._open_deny

    def _observe_durable_positions(self, portfolio, now_ms: int) -> None:
        if not self._durable_ids:
            return
        frozen = self._frozen_symbols()
        for symbol, durable in sorted(self._durable_ids.items()):
            key = durable.key()
            if key not in self._durable_seed_qty:
                continue
            if symbol in frozen or self._durable_observation_blocked(symbol):
                continue
            if self._local_folded_qty(symbol) != self._durable_seed_qty[key]:
                self._observe_suspended.add(symbol)
                continue
            broker_qty = portfolio.qty_for(symbol)
            signal = self._detector.observe_broker_qty(
                durable, broker_qty, blacked_out=False)
            if signal is None:
                continue
            instrument_id = self._instrument_ids.get(symbol)
            if instrument_id is None:
                for position in portfolio.positions:
                    if position.symbol == symbol:
                        instrument_id = position.instrument_id
                        break
            if instrument_id is None:
                self._observe_suspended.add(symbol)
                continue
            self._frozen_durables.add(key)
            self._frozen_durable_symbols.add(symbol)
            self._status_ledger.record_broker_adjust_freeze(
                freeze_signal=signal,
                instrument_id=instrument_id,
                ts_market_utc=self._reconcile_ts(self._instant_utc))
            self.run_reconcile(
                phase="immediate",
                ts_utc=self._reconcile_ts(self._instant_utc),
                now_ms=now_ms,
                trigger=signal)

    def _finish_reconcile_pass(self, *, reconcile_id: str, phase: str,
                               session_date_et: str, trigger_durable_key,
                               broker_source: str, checked_symbols,
                               findings, adjustments, resolutions, notes,
                               completed: bool, wrote_baseline=None,
                               durable_seed_context=None
                               ) -> ReconcilePassResult:
        findings = tuple(sorted(findings, key=_finding_key))
        adjustments = tuple(adjustments)
        notes = tuple(sorted(set(notes), key=_note_key))
        window_had_drift = bool(self._reconcile_drift_in_window or findings)

        for finding in findings:
            self._reconcile_ledger.record_reconcile(
                reconcile_id=reconcile_id,
                drift_id=_mint_drift_id(reconcile_id, finding),
                kind=finding.kind, symbol=finding.symbol, field=finding.field,
                local=finding.local, broker=finding.broker, diff=finding.diff,
                action=finding.action, position_id=finding.position_id,
                local_order_id=finding.local_order_id,
                broker_order_id=finding.broker_order_id)

        applied = []
        if phase != "immediate":
            for plan in adjustments:
                self._book.apply_position_adjust(
                    position_id=plan.position_id,
                    adjusted_qty=plan.adjusted_qty,
                    adjusted_broker_cost_usd=plan.adjusted_broker_cost_usd,
                    adjust_id=_mint_adjust_id(reconcile_id, plan.position_id),
                    reconcile_id=reconcile_id)
                applied.append(plan)

        for resolution in resolutions:
            self._exec_ledger.record_order_terminal(
                terminal_state=resolution.terminal_state,
                filled_qty=resolution.filled_qty,
                cum_notional_usd=resolution.cum_notional_usd,
                ts_broker_utc=resolution.ts_broker_utc,
                decision_id=resolution.decision_id,
                order_id=resolution.order_id)

        for note, symbol, detail in notes:
            self._reconcile_ledger.record_reconcile_note(
                reconcile_id=reconcile_id, note=note, symbol=symbol,
                detail=detail)

        durable_portfolio = None
        durable_seeded = []
        if wrote_baseline is not None:
            durable_seeded = list(wrote_baseline["durable_seeded"])
            if durable_seed_context is not None:
                durable_portfolio, durable_defer = durable_seed_context
                durable_seeded = self._durable_seed_candidates(
                    durable_portfolio, durable_defer, phase)

        if wrote_baseline is not None:
            self._latest_baseline = self._reconcile_ledger.record_reconcile_baseline(
                reconcile_id=reconcile_id,
                session_date_et=session_date_et,
                cash_usd=wrote_baseline["cash_usd"],
                equity_usd=wrote_baseline["equity_usd"],
                buying_power_usd=wrote_baseline["buying_power_usd"],
                fills_seq_watermark=wrote_baseline["fills_seq_watermark"],
                positions=wrote_baseline["positions"],
                durable_seeded=durable_seeded)

        clean = bool(completed and not findings)
        self._reconcile_ledger.record_reconcile_run(
            reconcile_id=reconcile_id, phase=phase,
            session_date_et=session_date_et,
            trigger_durable_key=trigger_durable_key,
            broker_source=broker_source,
            checked_symbols=checked_symbols,
            drift_count=len(findings), adjusted_count=len(applied),
            note_count=len(notes), completed=completed, clean=clean)
        self._pass_count += 1

        if findings:
            self._drift_latch = True
        elif (completed and phase != "immediate" and not window_had_drift
              and not self._frozen_symbols()):
            self._drift_latch = False
        self._reconcile_drift_in_window = 0

        if (completed and wrote_baseline is not None
                and durable_portfolio is not None):
            self._commit_durable_seeds(durable_portfolio, durable_seeded)

        return ReconcilePassResult(
            reconcile_id=reconcile_id, phase=phase,
            session_date_et=session_date_et, completed=completed,
            findings=findings, adjustments=tuple(applied), notes=notes,
            clean=clean)

    def run_reconcile(self, *, phase: str, ts_utc: Optional[str] = None,
                      now_ms: Optional[int] = None,
                      trigger=None,
                      rebaseline_cash: bool = False) -> ReconcilePassResult:
        """M6 SOD/EOD/CLI/immediate broker reconciliation.

        Observation + journal only: this method never submits, cancels, or mints
        preflight tokens. Position changes are explicit `position_adjust` rows
        applied through PaperBook after the reconcile drift rows are written.
        """
        phase = require_phase(phase)
        if rebaseline_cash and phase != "cli":
            raise ReconcileError("rebaseline_cash is legal only for cli phase")
        ts = self._reconcile_ts(ts_utc)
        now = self._clock.now_ms() if now_ms is None else int(now_ms)
        session_date_et = self._calendar.session_date_for(ts)
        reconcile_id = _mint_reconcile_id(
            self.run_id, phase, session_date_et, self._pass_count)
        trigger_durable_key = None
        notes: List[tuple] = []
        findings: List[DriftFinding] = []
        plans: List[object] = []
        completed = True

        if phase == "immediate":
            if trigger is None:
                raise ExecError("immediate reconcile requires a FreezeSignal")
            trigger_durable_key = trigger.durable_id.key()
            findings.append(make_finding(
                kind="ca_silent_adjust", symbol=trigger.symbol,
                field="qty", local=trigger.prev_qty, broker=trigger.curr_qty,
                action="frozen_immediate"))
            self._drift_latch = True

        if self.broker is None and self._account_provider is None:
            notes.append(("reconcile_skipped_no_broker", None, self.mode))
            return self._finish_reconcile_pass(
                reconcile_id=reconcile_id, phase=phase,
                session_date_et=session_date_et,
                trigger_durable_key=trigger_durable_key,
                broker_source=self._account_source, checked_symbols=[],
                findings=findings, adjustments=plans, resolutions=[],
                notes=notes, completed=False)

        account_payload, positions_payload = self._provider_payloads()
        if (not isinstance(account_payload, Mapping)
                or not isinstance(positions_payload, list)):
            notes.append(("broker_read_failed", None, "payload shape"))
            return self._finish_reconcile_pass(
                reconcile_id=reconcile_id, phase=phase,
                session_date_et=session_date_et,
                trigger_durable_key=trigger_durable_key,
                broker_source=self._account_source, checked_symbols=[],
                findings=findings, adjustments=plans, resolutions=[],
                notes=notes, completed=False)

        parsed_account = parse_account_payload(
            account_payload, source=self._account_source,
            seen_at_ms=now, ts_read_utc=ts)
        if isinstance(parsed_account, AccountInvalid):
            notes.append(("broker_read_failed", None, parsed_account.reason))
            completed = False
            portfolio = None
        else:
            try:
                portfolio = parse_positions_payload(
                    positions_payload, source=self._account_source,
                    seen_at_ms=now, stale=False)
                self._portfolio = portfolio
            except ValueError as exc:
                notes.append(("broker_read_failed", None, str(exc)))
                completed = False
                portfolio = None

        if not completed:
            return self._finish_reconcile_pass(
                reconcile_id=reconcile_id, phase=phase,
                session_date_et=session_date_et,
                trigger_durable_key=trigger_durable_key,
                broker_source=self._account_source, checked_symbols=[],
                findings=findings, adjustments=plans, resolutions=[],
                notes=notes, completed=False)

        fill_rows = self._fill_rows()
        open_orders = self._current_open_orders()
        watermarks = self._fills_cum_watermarks(fill_rows)
        defer = set()
        resolutions = []

        for order_id, row in sorted(open_orders.items()):
            probe = self._wrapped_probe(order_id)
            if probe is PROBE_FAILED:
                notes.append(("order_probe_failed", row.get("symbol"),
                              order_id))
                completed = False
                defer.add(row.get("symbol"))
                continue
            verdict = resolve_order_probe(
                row, probe, cum_filled_watermark=watermarks.get(order_id, _ZERO),
                session_over=self._session_over(row, ts), flatten_symbol=None,
                reconcile_id=reconcile_id)
            findings.extend(verdict.findings)
            notes.extend(verdict.notes)
            resolutions.extend(verdict.terminal_resolutions)
            defer.update(verdict.defer_symbols)

        for symbol in sorted(self._risk_kill.residual_symbols()):
            probe_id = f"flatten-{symbol}"
            probe = self._wrapped_probe(probe_id)
            if probe is PROBE_FAILED:
                notes.append(("order_probe_failed", symbol, probe_id))
                completed = False
                defer.add(symbol)
                continue
            verdict = resolve_order_probe(
                None, probe,
                cum_filled_watermark=watermarks.get(probe_id, _ZERO),
                session_over=False, flatten_symbol=symbol,
                reconcile_id=reconcile_id)
            findings.extend(verdict.findings)
            notes.extend(verdict.notes)
            resolutions.extend(verdict.terminal_resolutions)
            defer.update(verdict.defer_symbols)

        resolved_ids = {resolution.order_id for resolution in resolutions}
        for order_id, row in open_orders.items():
            if order_id not in resolved_ids:
                symbol = row.get("symbol")
                if isinstance(symbol, str) and symbol:
                    defer.add(symbol)
        if self._task is not None:
            defer.add(self._task.symbol)
        defer.update(self._open_deny)

        local_positions = self._open_positions_by_symbol()
        checked_symbols = self._checked_symbols(local_positions, portfolio)
        self._note_missing_durable_ids(
            checked_symbols, portfolio, local_positions, notes)
        pos_findings, pos_plans, pos_notes = diff_positions(
            local_positions, portfolio,
            frozen_symbols=frozenset(self._frozen_symbols()),
            inflight_symbols=frozenset(s for s in defer if isinstance(s, str)),
            adjusts_allowed=(phase != "immediate"),
            reconcile_id=reconcile_id)
        findings.extend(pos_findings)
        plans.extend(pos_plans)
        notes.extend(pos_notes)

        cash_clean = False
        if self._latest_baseline is None:
            if defer:
                notes.append(("cash_skipped_inflight", None, ""))
            else:
                notes.append(("baseline_seeded", None, ""))
                cash_clean = True
        elif defer:
            notes.append(("cash_skipped_inflight", None, ""))
            if self._outstanding_cash_residue is not None:
                findings.append(
                    _carried_residue_finding(self._outstanding_cash_residue))
        else:
            baseline_cash = BrokerUSD(Decimal(self._latest_baseline["cash_usd"]))
            cash_finding = diff_cash(
                baseline_cash=baseline_cash,
                fill_rows_since_watermark=self._fill_rows_since_baseline(fill_rows),
                broker_cash=parsed_account.cash, reconcile_id=reconcile_id)
            if cash_finding is None:
                cash_clean = True
                self._outstanding_cash_residue = None
            elif rebaseline_cash:
                cash_finding = replace(cash_finding, action="rebaselined")
                findings.append(cash_finding)
                self._outstanding_cash_residue = None
                cash_clean = True
            else:
                findings.append(cash_finding)
                self._outstanding_cash_residue = cash_finding

        id_note = identity_note(
            equity=parsed_account.equity, cash=parsed_account.cash,
            market_values=[position.market_value
                           for position in portfolio.positions])
        if id_note is not None:
            notes.append(id_note)
        if findings:
            self._drift_latch = True

        wrote_baseline = None
        if completed and (self._latest_baseline is not None or not defer):
            if self._latest_baseline is None or cash_clean:
                cash_usd = parsed_account.cash
                watermark = self._fills_seq_watermark(fill_rows)
            else:
                cash_usd = BrokerUSD(Decimal(self._latest_baseline["cash_usd"]))
                watermark = int(self._latest_baseline["fills_seq_watermark"])
            wrote_baseline = {
                "cash_usd": as_broker_usd(cash_usd),
                "equity_usd": parsed_account.equity,
                "buying_power_usd": parsed_account.buying_power,
                "fills_seq_watermark": watermark,
                "positions": self._baseline_positions(portfolio),
                "durable_seeded": [],
            }

        result = self._finish_reconcile_pass(
            reconcile_id=reconcile_id, phase=phase,
            session_date_et=session_date_et,
            trigger_durable_key=trigger_durable_key,
            broker_source=self._account_source, checked_symbols=checked_symbols,
            findings=findings, adjustments=plans, resolutions=resolutions,
            notes=notes,
            completed=completed, wrote_baseline=wrote_baseline,
            durable_seed_context=(portfolio, defer))
        return result

    def write_report(self, path, *, generated_ts_utc: str, bins: int = 10) -> dict:
        """Observe-mode calibration report over this run's journal (§O.1
        --report-out; FD-M5-4 'probe → resolver → calibration report')."""
        from agent.calibration_report import build_report
        import json as _json
        report = build_report(
            decision_rows=journal_replay(self._decisions_path),
            scored_rows=journal_replay(self._scored_path),
            run_id=self.run_id, rules_hash=self._signal_config.rules_hash,
            generated_ts_utc=generated_ts_utc, bins=bins)
        Path(path).write_text(
            _json.dumps(report, sort_keys=True, indent=2, default=str),
            encoding="utf-8")
        return report

    def close(self) -> None:
        """Release the run lock + unbind the preflight runtime. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._runtime_bound:
            unbind_runtime()
            self._runtime_bound = False
        self._lock.release()

    # ------------------------------------------------- step-6/10 order recovery

    def _resolve_offline_orphans(self) -> None:
        for attempt in self._recovery_queue:
            order_id = attempt["order_id"]
            self._exec_ledger.record_order_submit_unconfirmed(
                client_order_id=order_id, error="offline_orphan",
                attempts=0, resolution="offline_orphan", order_id=order_id)
            self._open_deny.add(attempt["symbol"])
        self._recovery_queue = []

    def _recover_dangling_orders(self) -> None:
        """FD-M5-24: re-query by client_order_id, ADOPT, ONE best-effort cancel
        (restart_unknown_state), resume polling; not found x3 => unconfirmed +
        open-deny."""
        for attempt in self._recovery_queue:
            order_id = attempt["order_id"]
            parsed = None
            attempts = 0
            for _ in range(SUBMIT_RECOVERY_ATTEMPTS):
                attempts += 1
                try:
                    result = self.broker.order_status(order_id)
                except Exception:  # noqa: BLE001 — keep trying (FD-M5-24)
                    continue
                if isinstance(result, Mapping):
                    candidate = parse_order_payload(result,
                                                    source=self._fill_source)
                    if isinstance(candidate, BrokerOrder):
                        parsed = candidate
                        break
            if parsed is None:
                self._exec_ledger.record_order_submit_unconfirmed(
                    client_order_id=order_id, error="not_found",
                    attempts=attempts, resolution="not_found",
                    order_id=order_id)
                self._open_deny.add(attempt["symbol"])
                continue
            task = self._adopt_task(attempt, parsed)
            self._task = task
            self._attempt_cancel(task, "restart_unknown_state")
            if parsed.state in TERMINAL_STATES:
                self._terminal(task, parsed)

    def _adopt_task(self, attempt: dict, parsed: BrokerOrder) -> _OrderTask:
        symbol = attempt["symbol"]
        instrument_id = attempt["instrument_id"]
        kind = "open" if attempt.get("token_kind") == "open" else "close"
        position_id = None
        if kind == "open":
            position_id_candidate = "pos-" + row_hash(
                {"symbol": symbol, "opening_order_id": attempt["order_id"]})
            try:
                self._book.position(position_id_candidate)
                position_id = position_id_candidate
            except ExecError:
                position_id = None
        else:
            for pid, pos in self._book._positions.items():
                if pos.symbol == symbol and pos.status == "open":
                    position_id = pid
                    break
        stamp = DecisionStamp(
            decision_id=attempt.get("decision_id", "d-restart"),
            decision_ts_utc="1970-01-01T00:00:00.000000Z",
            decision_seen_at_ms=0, quote_a=None)
        task = _OrderTask(
            kind=kind, state="watch",
            decision_id=attempt.get("decision_id", "d-restart"),
            symbol=symbol, instrument_id=instrument_id,
            strategy_id=attempt["strategy_id"], side=attempt["side"],
            qty=Decimal(str(attempt["qty"])), stamp=stamp,
            session_date_et="1970-01-01",
            order_id=attempt["order_id"],
            broker_order_id=parsed.broker_order_id,
            last_state=parsed.state, last_poll_ms=self._clock.now_ms(),
            position_id=position_id, close_reason="operator",
            adopted_from_restart=True)
        # FD-M5-18 across restart: prev = the last EMITTED journaled fill.
        last_fill = None
        for row in journal_replay(self._journal_dir / "fills.jsonl"):
            if (row.get("event_type") == "broker_fill"
                    and row.get("order_id") == task.order_id):
                last_fill = row
        if last_fill is not None:
            task.prev_emitted = BrokerOrder(
                broker_order_id=parsed.broker_order_id,
                client_order_id=task.order_id, symbol=symbol,
                side=task.side, state="accepted", raw_status="restart",
                qty=task.qty,
                filled_qty=Decimal(str(last_fill["cum_filled_qty"])),
                filled_avg_price=Decimal(str(last_fill["filled_avg_price_after"])),
                limit_price=None, ts_broker_utc=None,
                source=self._fill_source)
        return task

    # ----------------------------------------------------------------- tick loop

    def on_bar_complete(self, bar) -> None:
        """Feed callback: queue a completed 1m bar for §M.3 step 4."""
        self._pending_bars.append(bar)
        if bar.symbol not in self._instrument_ids:
            self._instrument_ids[bar.symbol] = bar.instrument_id
        self._observe_instant(bar.watermark_utc)

    def on_tick(self, *, now_ms: int) -> None:
        """One §M.3 tick (frozen step order)."""
        self._ticks_run += 1
        # step 1 — ingest: the feed delivered events upstream; bookkeeping.
        self._ingest_bookkeeping()
        # step 2 — market state for universe ∪ held ∪ in-flight symbols.
        self._refresh_market_state(now_ms)
        # step 3 — account refresh (paper/synthetic compositions).
        self._refresh_account(now_ms)
        # step 4 — bars -> features -> M3 probe (ALWAYS) -> resolver.
        bars = self._process_bars(now_ms)
        # step 5 — kill evaluation.
        self._evaluate_kill(now_ms)
        # step 6 — advance in-flight OrderTask + watcher.
        self._advance_task(now_ms)
        # step 7 — snapshot assembly + scan (NO gates pre-check — M5C-S9).
        ctxs = self._assemble_ctxs(bars, now_ms)
        self._scan(ctxs, now_ms)
        # step 8 — exits (RC-1: GLOBAL in-flight guard).
        self._exits(ctxs, now_ms)
        # step 9 — marks per held position per completed bar.
        self._marks(bars, now_ms)
        # step 10 — session edge.
        self._session_edge(now_ms)
        if self._tick_budget is not None and self._ticks_run >= self._tick_budget:
            raise _TickLimit()

    def run_with_feed(self, feed, *, max_ticks: Optional[int] = None) -> None:
        """Drive the tick loop from a ReplayQuoteFeed (FD-M5-4)."""
        self._tick_budget = max_ticks
        try:
            feed.run(on_tick=lambda *, now_ms: self.on_tick(now_ms=now_ms),
                     on_bar_complete=self.on_bar_complete)
        except _TickLimit:
            pass
        finally:
            self._tick_budget = None

    # -- step 1/2 -------------------------------------------------------------

    def _observe_instant(self, ts_utc: Optional[str]) -> None:
        if not isinstance(ts_utc, str) or not ts_utc:
            return
        if self._instant_utc is None or \
                _parse_utc(ts_utc) > _parse_utc(self._instant_utc):
            self._instant_utc = _canonical_utc_str(_parse_utc(ts_utc))

    def _ingest_bookkeeping(self) -> None:
        for symbol in self._known_symbols():
            instrument_id = self._instrument_ids.get(symbol)
            if instrument_id is None:
                continue
            snapshot = self._quote_view.latest(symbol, instrument_id)
            if snapshot is not None:
                self._observe_instant(snapshot.ts_recv_utc)

    def _known_symbols(self) -> Tuple[str, ...]:
        held = tuple(pos.symbol for pos in self._book._positions.values()
                     if pos.status == "open")
        in_flight = (self._task.symbol,) if self._task is not None else ()
        return tuple(sorted(set(self._universe) | set(held) | set(in_flight)
                            | set(self._instrument_ids)))

    def _refresh_market_state(self, now_ms: int) -> None:
        for symbol in self._known_symbols():
            instrument_id = self._instrument_ids.get(symbol)
            if instrument_id is None:
                continue
            snapshot = self._quote_view.latest(symbol, instrument_id)
            if snapshot is None:
                continue  # no instant -> cache miss -> safe default (fail-closed)
            instant = snapshot.ts_recv_utc
            session_date_et = self._calendar.session_date_for(instant)
            try:
                phase = self._calendar.phase_at(instant)
            except UnknownSessionDate:
                phase = SessionPhase.UNKNOWN
            flags = None
            if self._status_provider is not None:
                flags = self._status_provider(symbol, instant)
            if flags is None:
                flags = StatusFlags(symbol=symbol)  # UNKNOWN fail-closed (§N)
            nbbo = Nbbo(symbol=symbol, best_bid=snapshot.bid,
                        best_ask=snapshot.ask, bid_sz=snapshot.bid_sz,
                        ask_sz=snapshot.ask_sz, ts_utc=instant)
            verdict = self._decider.decide(TradabilityInputs(
                symbol=symbol, instrument_id=instrument_id, ts_utc=instant,
                session_date_et=session_date_et, session_phase=phase,
                status=flags, nbbo=nbbo, ca_blackout=False, frozen=False))
            self._cache.put(verdict, now_ms=now_ms)

    # -- step 3 -----------------------------------------------------------------

    def _provider_payloads(self):
        if self._account_provider is not None:
            return (self._account_provider.account_payload(),
                    self._account_provider.positions_payload())
        if self.broker is None:
            return None, None
        return self.broker.account(), self.broker.positions()

    def _refresh_account(self, now_ms: int) -> None:
        if self.mode == "observe" and self._account_provider is None:
            return
        interval = self._exec_config.account_refresh_interval_ms
        if (self._last_account_ms is not None
                and now_ms - self._last_account_ms < interval):
            return
        account_payload, positions_payload = self._provider_payloads()
        if account_payload is None and positions_payload is None:
            return
        self._last_account_ms = now_ms
        ts_read = self._instant_utc or "1970-01-01T00:00:00.000000Z"
        if isinstance(account_payload, Mapping):
            parsed = parse_account_payload(
                account_payload, source=self._account_source,
                seen_at_ms=now_ms, ts_read_utc=ts_read)
        else:
            parsed = AccountInvalid(reason="provider_error",
                                    source=self._account_source,
                                    seen_at_ms=now_ms)
        self._account_store.put(parsed)
        self._risk_ledger.record_account_snapshot(result=parsed)  # F4
        account = self._account_store.get(now_ms=now_ms)
        if account.status == "fresh":
            self._pdt.observe_account(account.read)
            if self._instant_utc is not None:
                self._loss.observe(
                    account,
                    session_date_et=self._calendar.session_date_for(
                        self._instant_utc))
        try:
            rows = positions_payload if positions_payload is not None else []
            self._portfolio = parse_positions_payload(
                rows, source=self._account_source, seen_at_ms=now_ms,
                stale=False)
            self._observe_durable_positions(self._portfolio, now_ms)
        except ValueError:
            self._portfolio = None  # caller maps to portfolio_missing

    def _portfolio_read(self, now_ms: int):
        if self._portfolio is None:
            return None
        return replace(self._portfolio,
                       stale=portfolio_is_stale(self._portfolio.seen_at_ms,
                                                now_ms),
                       unreconciled_drift=self._drift_latch)

    # -- step 4 -----------------------------------------------------------------

    def _process_bars(self, now_ms: int) -> List[object]:
        bars, self._pending_bars = self._pending_bars, []
        for bar in bars:
            self._feature_view.refresh(symbol=bar.symbol,
                                       instrument_id=bar.instrument_id,
                                       as_of_utc=bar.bucket_end_utc)
            decision_ts = self._instant_utc or bar.watermark_utc
            if _parse_utc(decision_ts) < _parse_utc(bar.bucket_end_utc):
                decision_ts = bar.bucket_end_utc
            self._probe.on_bar_complete(
                symbol=bar.symbol, instrument_id=bar.instrument_id,
                event_start_bar_end_utc=bar.bucket_end_utc,
                decision_ts_utc=_canonical_utc_str(_parse_utc(decision_ts)))
        if bars and self._instant_utc is not None:
            self._resolver.resolve_due(journal_replay(self._decisions_path),
                                       now_utc=self._instant_utc)
        return bars

    # -- step 5 -----------------------------------------------------------------

    def _evaluate_kill(self, now_ms: int) -> None:
        if self._risk_kill.state != "monitoring":
            return
        account = self._account_store.get(now_ms=now_ms)
        if account.status == "fresh":
            self._kill_blind_since_ms = None
            self._kill_skipped_latch = False
            evaluation = self._risk_kill.evaluate(account, self._loss.read())
            if evaluation.cause is not None:
                self._kill_sequence(evaluation.cause, evaluation, now_ms)
            return
        # Non-fresh (missing/stale/invalid/skew): the numeric caps stay skipped
        # (FD-M4-3 — never trip on unverified numbers) but the blindness itself
        # is BOUNDED (2026-07-02 review): past MAX_ACCOUNT_BLIND_MS of
        # continuous non-fresh reads with an open position held, flatten-then-
        # halt under `account_blind_cap` — a cause that consumes no account
        # numbers. `missing` keeps its pre-existing no-skip-row semantics (an
        # observe composition reads `missing` every tick and must stay
        # row-silent) but counts toward the blind clock; the flat-book guard
        # keeps broker-less compositions from ever tripping.
        if self._kill_blind_since_ms is None:
            self._kill_blind_since_ms = now_ms
        if account.status != "missing" and not self._kill_skipped_latch:
            self._kill_skipped_latch = True
            self._risk_ledger.record_kill_eval_skipped(
                account_status=account.status,
                generation=self._risk_kill.generation)
        if (now_ms - self._kill_blind_since_ms > MAX_ACCOUNT_BLIND_MS
                and self._has_open_positions()):
            self._kill_sequence("account_blind_cap", None, now_ms)

    def _has_open_positions(self) -> bool:
        """Local book-of-record view (rehydrate-backed) — usable while the
        broker account read is degraded."""
        return any(pos.status == "open"
                   for pos in self._book._positions.values())

    def trigger_kill(self, cause: str = "drill") -> None:
        """Operator/drill entry into the §M.6 sequence."""
        self._kill_sequence(cause, None, self._clock.now_ms())

    def retry_residual(self):
        """Operator-attended residual retry (HALTED only; M4 §I)."""
        now_ms = self._clock.now_ms()
        return self._risk_kill.retry_residual(
            self._flatten_broker(), self._flatten_portfolio(now_ms))

    def _flatten_broker(self):
        from agent.broker.flatten_proxy import PriceCappedFlattenBroker  # lazy
        instrument_ids = dict(self._instrument_ids)  # universe ∪ held (M5C-S8)
        return PriceCappedFlattenBroker(
            inner=self.broker, quote_view=self._quote_view,
            instrument_ids=instrument_ids, cap_bps=FLATTEN_CAP_BPS,
            void_token=void_token)   # SF-2: void a stranded reduce auth on raise

    def _flatten_portfolio(self, now_ms: int):
        portfolio = self._portfolio_read(now_ms)
        if portfolio is not None:
            return portfolio
        rows = [{"symbol": pos.symbol, "qty": str(pos.qty),
                 "market_value": str(pos.broker_cost_usd),
                 "instrument_id": pos.instrument_id}
                for pos in self._book._positions.values()
                if pos.status == "open"]
        return parse_positions_payload(rows, source=self._account_source,
                                       seen_at_ms=now_ms, stale=False)

    def _kill_sequence(self, cause: str, evaluation, now_ms: int) -> None:
        """§M.6 (FD-M5-25, rev 2): cancel-opens -> void tokens -> trigger with
        the price-capped flatten proxy -> book the resulting closes."""
        # (1) best-effort cancel EVERY open order (cause=kill_trip) so nothing
        # can still change exposure mid-flatten.
        task = self._task
        if task is not None and task.state == "watch":
            self._attempt_cancel(task, "kill_trip")
        # (1b) LC-2: a non-terminal strategy CLOSE (reduce sell) in flight is a
        # SELL on the same position the kill flatten is about to flatten. We
        # cancelled it above (a single flatten of record — never two concurrent
        # broker sells on one position; FD-M5-26/§M.6); now RETIRE the superseded
        # close task so step 6 never terminal-polls it into
        # `PaperBook.close_position` on the position the kill flatten just closed
        # (which would raise "position ... is closed (terminal)" and crash the
        # loop). The position is flattened by the kill (book-of-record close);
        # any late fill on the now-cancelled close is the M6 reconcile's job
        # (the broker is position-of-record). An OPEN task is kept: it is a BUY
        # (never a double-sell) and must survive for the M5C-S11
        # `late_fill_post_kill` tripwire.
        if task is not None and task.kind == "close":
            self._task = None
        # (2) void any minted-unconsumed open token (hygiene).
        for token, token_task in list(self._outstanding_open_tokens):
            void_token(token, "kill_generation_changed")
            self._exec_ledger.record_reject(
                symbol=token_task.symbol,
                instrument_id=token_task.instrument_id,
                strategy_id=token_task.strategy_id, stage="consume",
                reasons=("kill_generation_changed",), stages_skipped=(),
                detail={"risk_reasons": None, "quote_reasons": None,
                        "broker_code": None, "broker_message": None},
                preflight_id=(token_task.preflight.preflight_id
                              if token_task.preflight is not None else None),
                capped_limit=token_task.capped_limit, token_kind="open",
                kill_state=self._risk_kill.state,
                kill_generation=self._risk_kill.generation,
                quote_b=self._provenance(token_task.quote_b),
                decision_id=token_task.decision_id,
                order_id=token_task.order_id)
        self._outstanding_open_tokens = []
        # (3) trigger with the FD-M5-1 proxy; account via AccountStore.get
        # (M5C-B5: no wrap() helper — the read carries its real freshness).
        portfolio = self._flatten_portfolio(now_ms)
        tradability = {}
        for pos in portfolio.positions:
            instrument_id = self._instrument_ids.get(pos.symbol)
            if instrument_id is None:
                continue
            session_date = (self._calendar.session_date_for(self._instant_utc)
                            if self._instant_utc is not None else "1970-01-01")
            tradability[pos.symbol] = self._cache.get(
                pos.symbol, instrument_id, session_date, now_ms=now_ms)
        report = self._risk_kill.trigger(
            cause, self._flatten_broker(), portfolio, evaluation=evaluation,
            account=self._account_store.get(now_ms=self._clock.now_ms()),
            tradability=tradability)
        # (4) book the flatten closes (reason="kill_flatten"); generation bump
        # already invalidates every outstanding open token at consume.
        self._book_flatten_closes(report, now_ms)

    def _book_flatten_closes(self, report, now_ms: int) -> None:
        for symbol in report.flattened:
            position = None
            for pos in self._book._positions.values():
                if pos.symbol == symbol and pos.status == "open":
                    position = pos
                    break
            if position is None:
                continue
            payload = None
            try:
                result = self.broker.order_status(f"flatten-{symbol}")
                if isinstance(result, Mapping):
                    payload = result
            except Exception:  # noqa: BLE001 — booking is best-effort (M6 reconciles)
                payload = None
            if payload is None:
                continue
            parsed = parse_order_payload(payload, source=self._fill_source)
            if not isinstance(parsed, BrokerOrder):
                continue
            delta = fill_delta(None, parsed)
            if not isinstance(delta, FillDelta):
                continue
            decision_id = "d-" + row_hash({
                "run_id": self.run_id, "strategy_id": "kill_flatten",
                "symbol": symbol, "instrument_id": position.instrument_id,
                "event_basis": f"exit:{position.position_id}:{now_ms}"})
            order_id = self._order_id_prefix() + row_hash({
                "run_id": self.run_id, "decision_id": decision_id,
                "preflight_id": None})
            self._exec_ledger.record_broker_fill(
                delta=delta, cur=parsed, position_id=position.position_id,
                liquidity_flag=None, venue=None, decision_id=decision_id,
                order_id=order_id)
            self._book.close_position(
                position_id=position.position_id, order_id=order_id,
                fills=[delta], modeled=None, reason="kill_flatten",
                decision_id=decision_id)

    # -- step 6: the OrderTask FSM (§M.4) ------------------------------------------

    def _advance_task(self, now_ms: int) -> None:
        task = self._task
        if task is None:
            return
        if task.state == "await_latency":
            due_at = (task.stamp.decision_seen_at_ms
                      + self._exec_config.effective_latency_budget_ms)
            if self._clock.now_ms() >= due_at:  # FD-M5-10 scheduler item
                self._requote_and_submit(task)
        elif task.state == "watch":
            interval = self._exec_config.order_poll_interval_ms
            if (task.last_poll_ms is None
                    or now_ms - task.last_poll_ms >= interval):
                self._poll(task, now_ms)

    @staticmethod
    def _provenance(snapshot: Optional[QuoteSnapshot]) -> Optional[dict]:
        if snapshot is None:
            return None
        return {
            "dataset": snapshot.dataset, "schema": snapshot.schema,
            "ts_event_utc": snapshot.ts_event_utc,
            "ts_recv_utc": snapshot.ts_recv_utc,
            "seen_at_ms": snapshot.seen_at_ms,
            "reconnect_epoch": snapshot.reconnect_epoch,
            "vendor_seq": snapshot.vendor_seq,
        }

    def _order_id_prefix(self) -> str:
        return ("synthetic-o-"
                if isinstance(self._strategy, SyntheticStrategy) else "o-")

    def _artifact_check(self, strategy_id: str, data_pin: str):
        key = (strategy_id, self.rules_hash, data_pin)
        if key not in self._artifact_checks:
            self._artifact_checks[key] = verify_artifact(
                strategy_id, rules_hash=self.rules_hash, data_pin=data_pin,
                artifacts_dir=self._artifacts_dir)
        return self._artifact_checks[key]

    def _record_preflight_reject(self, task: _OrderTask, reject,
                                 quote_b: Optional[QuoteSnapshot]) -> None:
        detail = dict(reject.detail)
        self._exec_ledger.record_reject(
            symbol=task.symbol, instrument_id=task.instrument_id,
            strategy_id=task.strategy_id, stage=reject.gate_stage,
            reasons=tuple(reject.reasons),
            stages_skipped=tuple(reject.stages_skipped),
            detail={"risk_reasons": detail.get("risk_reasons"),
                    "quote_reasons": detail.get("quote_reasons"),
                    "broker_code": None, "broker_message": None},
            preflight_id=reject.preflight_id,
            capped_limit=reject.capped_limit, token_kind="open",
            kill_state=self._risk_kill.state,
            kill_generation=self._risk_kill.generation,
            quote_b=self._provenance(quote_b),
            decision_id=task.decision_id, order_id=None)

    def _requote_and_submit(self, task: _OrderTask) -> None:
        """REQUOTE -> PREFLIGHT -> write-ahead -> SUBMIT (§M.4)."""
        t1 = self._clock.now_ms()
        quote_b = self._quote_view.latest(task.symbol, task.instrument_id)
        quote_b_verdict = None
        if quote_b is not None:
            quote_b_verdict = evaluate_quote(
                quote_b, now_ms=t1,
                spread_bps_max=self._exec_config.spread_bps_max,
                staleness_ms_max=self._exec_config.quote_staleness_ms_max)
        verdict = self._cache.get(task.symbol, task.instrument_id,
                                  task.session_date_et, now_ms=t1)
        data_pin = ""
        feature = self._feature_view.latest(task.symbol, task.instrument_id)
        if feature is not None:
            data_pin = feature.data_pin
        artifact_check = self._artifact_check(task.strategy_id, data_pin)
        inputs = PreflightInputs(
            run_id=self.run_id, stamp=task.stamp, candidate=task.candidate,
            strategy_id=task.strategy_id,
            strategy_is_synthetic=isinstance(self._strategy, SyntheticStrategy),
            quote_b=quote_b, quote_b_verdict=quote_b_verdict,
            feed_epoch_now=(quote_b.reconnect_epoch if quote_b is not None
                            else 0),
            market_state=verdict, risk_verdict=task.risk_verdict,
            risk_verdict_row=task.risk_verdict_row,
            risk_verdict_now_ms=task.risk_now_ms,
            kill_state=self._risk_kill.state,
            kill_generation=self._risk_kill.generation,
            open_orders_in_flight=0, artifact_check=artifact_check,
            broker_kind=getattr(self.broker, "kind", "spy"),
            gates_config=self._gates_view, exec_config=self._exec_config,
            now_ms=t1)
        try:
            token, pf = mint_open_token(inputs)
        except PreflightRejected as err:
            self._record_preflight_reject(task, err.reject, quote_b)
            self._task = None
            return

        order_id = self._order_id_prefix() + row_hash({
            "run_id": self.run_id, "decision_id": task.decision_id,
            "preflight_id": pf.preflight_id})
        task.order_id = order_id
        task.preflight = pf
        task.capped_limit = pf.capped_limit
        task.quote_b = quote_b
        task.bound_epoch = quote_b.reconnect_epoch

        # WRITE-AHEAD (FD-M5-17): journaled BEFORE the network call.
        self._exec_ledger.record_order_submit_attempt(
            client_order_id=order_id, preflight_id=pf.preflight_id,
            risk_verdict_id=pf.risk_verdict_id, strategy_id=task.strategy_id,
            symbol=task.symbol, instrument_id=task.instrument_id,
            side=task.side, qty=task.qty,
            order_intent={"order_type": "marketable_limit", "tif": "day",
                          "limit_price": pf.capped_limit},
            token_kind="open", kill_generation=self._risk_kill.generation,
            quote_b=self._provenance(quote_b), decision_id=task.decision_id,
            order_id=order_id)

        intent = OrderIntent(
            symbol=task.symbol, side="buy", qty=task.qty,
            order_type="marketable_limit", tif="day",
            limit_price=pf.capped_limit, is_reducing=False,
            intent_id=order_id)
        self._outstanding_open_tokens.append((token, task))
        try:
            result = self.broker.submit_order(intent, token)
        except PreflightStale as stale:
            self._outstanding_open_tokens = [
                pair for pair in self._outstanding_open_tokens
                if pair[0] is not token]
            self._exec_ledger.record_reject(
                symbol=task.symbol, instrument_id=task.instrument_id,
                strategy_id=task.strategy_id, stage="consume",
                reasons=(stale.reason,), stages_skipped=(),
                detail={"risk_reasons": None, "quote_reasons": None,
                        "broker_code": None, "broker_message": None},
                preflight_id=pf.preflight_id, capped_limit=pf.capped_limit,
                token_kind="open", kill_state=self._risk_kill.state,
                kill_generation=self._risk_kill.generation,
                quote_b=self._provenance(quote_b),
                decision_id=task.decision_id, order_id=order_id)
            self._task = None
            return
        except Exception as err:  # noqa: BLE001 — timeout/ambiguous (FD-M5-17)
            self._outstanding_open_tokens = [
                pair for pair in self._outstanding_open_tokens
                if pair[0] is not token]
            self._submit_recovery(task, err)
            return
        self._outstanding_open_tokens = [
            pair for pair in self._outstanding_open_tokens
            if pair[0] is not token]
        self._after_submit(task, result, quote_b, quote_b_verdict, t1)

    def _after_submit(self, task: _OrderTask, result, quote_b,
                      quote_b_verdict, t1: int) -> None:
        if not isinstance(result, Mapping):
            # BrokerHttpError returned as DATA (duck-typed: §3 forbids the
            # module-scope alpaca import).
            rejection = classify_rejection(
                http_status=getattr(result, "status_code", None),
                code=getattr(result, "code", None),
                message=getattr(result, "message", str(result)))
            self._broker_reject(task, broker_order_id=None,
                                http_status=rejection.http_status,
                                broker_code=rejection.code,
                                message=rejection.message,
                                pdt=rejection.pdt_marker_matched)
            return
        parsed = parse_order_payload(result, source=self._fill_source)
        if isinstance(parsed, OrderInvalid):
            # An unparseable ack is AMBIGUOUS — never blind-resubmit.
            self._submit_recovery(task, ExecError(
                f"invalid submit ack: {parsed.reason}"))
            return
        if parsed.state == "rejected":
            rejection = classify_rejection(http_status=None, code=None,
                                           message=parsed.raw_status)
            self._broker_reject(task, broker_order_id=parsed.broker_order_id,
                                http_status=None, broker_code=None,
                                message=parsed.raw_status,
                                pdt=rejection.pdt_marker_matched)
            return
        self._exec_ledger.record_order_submitted(
            client_order_id=task.order_id,
            broker_order_id=parsed.broker_order_id, state=parsed.state,
            raw_status=parsed.raw_status, ts_broker_utc=parsed.ts_broker_utc,
            source=self._fill_source, decision_id=task.decision_id,
            order_id=task.order_id)
        task.broker_order_id = parsed.broker_order_id
        task.last_state = parsed.state
        task.state = "watch"
        task.last_poll_ms = t1
        if task.kind == "open":
            modeled = model_fill(side="buy", qty=task.qty,
                                 capped_limit=task.capped_limit,
                                 quote_b=quote_b,
                                 quote_b_verdict=quote_b_verdict,
                                 depth=None, now_ms=t1)
            modeled = bind_modeled_fill_id(modeled, order_id=task.order_id)
            task.modeled = modeled
            notional = (modeled.modeled_cost_usd
                        if modeled.modeled_cost_usd is not None
                        else task.capped_limit * task.qty)
            fees = fees_for(side="buy", qty=task.qty,
                            notional=Decimal(notional))
            self._exec_ledger.record_modeled_execution_fill(
                modeled_fill_id=modeled.modeled_fill_id, model=modeled.model,
                realism_class=modeled.realism_class,
                requested_qty=modeled.requested_qty,
                modeled_fillable_qty=modeled.modeled_fillable_qty,
                modeled_vwap=modeled.modeled_vwap,
                worst_price=modeled.worst_price,
                touch_price=modeled.touch_price,
                levels_consumed=modeled.levels_consumed,
                slippage_vs_mid_bps=modeled.slippage_vs_mid_bps,
                modeled_cost_usd=modeled.modeled_cost_usd,
                fees_assumed=fees, quote=dict(modeled.quote),
                reasons=list(modeled.reasons), decision_id=task.decision_id,
                order_id=task.order_id)
        # The ack IS the first observation: process its fills + terminal state.
        self._observe_order(task, parsed, t1, journal_update=False)

    def _broker_reject(self, task: _OrderTask, *, broker_order_id,
                       http_status, broker_code, message, pdt: bool) -> None:
        self._exec_ledger.record_broker_reject(
            broker_order_id=broker_order_id, http_status=http_status,
            broker_code=broker_code, message=message, pdt_marker_matched=pdt,
            decision_id=task.decision_id, order_id=task.order_id)
        # FD-M4-15: forward the rejection to the pdt latch.
        self._pdt.observe_broker_rejection(BrokerRejectionObservation(
            code=broker_code, message=message,
            ts_utc=self._instant_utc or "1970-01-01T00:00:00.000000Z"))
        self._task = None

    def _submit_recovery(self, task: _OrderTask, error: BaseException) -> None:
        """FD-M5-17: NEVER blind-resubmit; query by client_order_id up to
        SUBMIT_RECOVERY_ATTEMPTS, found => adopt, else unconfirmed + open-deny."""
        attempts = 0
        parsed = None
        for _ in range(SUBMIT_RECOVERY_ATTEMPTS):
            attempts += 1
            try:
                result = self.broker.order_status(task.order_id)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(result, Mapping):
                candidate = parse_order_payload(result,
                                                source=self._fill_source)
                if isinstance(candidate, BrokerOrder):
                    parsed = candidate
                    break
        if parsed is None:
            error_text = str(error) or type(error).__name__
            self._exec_ledger.record_order_submit_unconfirmed(
                client_order_id=task.order_id, error=error_text,
                attempts=attempts, resolution="not_found",
                order_id=task.order_id)
            self._open_deny.add(task.symbol)
            self._task = None
            return
        # ADOPT the broker's answer.
        self._exec_ledger.record_order_submitted(
            client_order_id=task.order_id,
            broker_order_id=parsed.broker_order_id, state=parsed.state,
            raw_status=parsed.raw_status, ts_broker_utc=parsed.ts_broker_utc,
            source=self._fill_source, decision_id=task.decision_id,
            order_id=task.order_id)
        task.broker_order_id = parsed.broker_order_id
        task.last_state = parsed.state
        task.state = "watch"
        task.last_poll_ms = self._clock.now_ms()
        self._observe_order(task, parsed, self._clock.now_ms(),
                            journal_update=False)

    # -- WATCH ---------------------------------------------------------------------

    def _poll(self, task: _OrderTask, now_ms: int) -> None:
        task.last_poll_ms = now_ms
        try:
            result = self.broker.order_status(task.order_id)
        except Exception as err:  # noqa: BLE001 — keep polling, alert
            self._exec_ledger.record_order_state_alert(
                broker_order_id=task.broker_order_id,
                raw_status=type(err).__name__, note="poll_error",
                order_id=task.order_id)
            return
        if not isinstance(result, Mapping):
            self._exec_ledger.record_order_state_alert(
                broker_order_id=task.broker_order_id,
                raw_status=type(result).__name__, note="poll_error",
                order_id=task.order_id)
            return
        parsed = parse_order_payload(result, source=self._fill_source)
        if isinstance(parsed, OrderInvalid):
            self._exec_ledger.record_order_state_alert(
                broker_order_id=task.broker_order_id,
                raw_status=str(result.get("status")), note=parsed.reason,
                order_id=task.order_id)
            return
        self._observe_order(task, parsed, now_ms, journal_update=True)

    def _observe_order(self, task: _OrderTask, parsed: BrokerOrder,
                       now_ms: int, *, journal_update: bool) -> None:
        if journal_update and parsed.state != task.last_state:
            self._exec_ledger.record_broker_order_update(
                broker_order_id=parsed.broker_order_id,
                from_state=task.last_state, to_state=parsed.state,
                raw_status=parsed.raw_status, filled_qty=parsed.filled_qty,
                filled_avg_price=parsed.filled_avg_price,
                ts_broker_utc=parsed.ts_broker_utc, order_id=task.order_id)
        task.last_state = parsed.state
        if task.broker_order_id is None:
            task.broker_order_id = parsed.broker_order_id

        delta = fill_delta(task.prev_emitted, parsed)
        if isinstance(delta, OrderInvalid):
            self._exec_ledger.record_order_state_alert(
                broker_order_id=parsed.broker_order_id,
                raw_status=parsed.raw_status, note=delta.reason,
                order_id=task.order_id)
        elif isinstance(delta, FillDelta):
            self._handle_fill(task, delta, parsed)

        # §M.5 watcher guards run every poll.
        if parsed.state == "unknown":
            self._exec_ledger.record_order_state_alert(
                broker_order_id=parsed.broker_order_id,
                raw_status=parsed.raw_status, note="unknown_status",
                order_id=task.order_id)
            self._attempt_cancel(task, "unexpected_status")
        if parsed.state not in TERMINAL_STATES:
            self._watcher_triggers(task, now_ms)
        if parsed.state in TERMINAL_STATES:
            self._terminal(task, parsed)

    def _watcher_triggers(self, task: _OrderTask, now_ms: int) -> None:
        latest = self._quote_view.latest(task.symbol, task.instrument_id)
        if (latest is not None and task.bound_epoch is not None
                and latest.reconnect_epoch != task.bound_epoch):
            self._attempt_cancel(task, "epoch_changed")
        verdict = self._cache.get(task.symbol, task.instrument_id,
                                  task.session_date_et, now_ms=now_ms)
        if "cache_stale_safe_default" in verdict.reasons:
            self._attempt_cancel(task, "market_state_stale_default")
        if verdict.tradability == Tradability.NOT_TRADABLE:
            self._attempt_cancel(task, "market_state_not_tradable")
        if (verdict.halt != HaltState.NONE
                or verdict.luld in (LuldState.LIMIT, LuldState.PAUSED,
                                    LuldState.UNKNOWN)):
            self._attempt_cancel(task, "halt_luld_auction")

    def _attempt_cancel(self, task: _OrderTask, cause: str) -> None:
        """One best-effort cancel per cause per order (FD-M5-23). NO token, NO
        mint — cancel is risk-reducing-or-neutral and never gated."""
        if cause in task.cancel_causes or task.order_id is None:
            return
        task.cancel_causes.add(cause)
        outcome = "cancel_submitted"
        try:
            result = self.broker.cancel_order(task.order_id)
        except Exception:  # noqa: BLE001
            outcome = "error"
            result = None
        if result is not None and not isinstance(result, Mapping):
            outcome = ("cancel_rejected"
                       if getattr(result, "status_code", None) is not None
                       else "error")
        elif (task.last_state in TERMINAL_STATES):
            outcome = "already_terminal"
        self._exec_ledger.record_post_submit_cancel_attempt(
            broker_order_id=task.broker_order_id, cause=cause,
            outcome=outcome, broker_state_at_attempt=task.last_state,
            order_id=task.order_id)

    def _handle_fill(self, task: _OrderTask, delta: FillDelta,
                     parsed: BrokerOrder) -> None:
        if task.kind == "open" and task.position_id is None:
            task.position_id = "pos-" + row_hash(
                {"symbol": task.symbol, "opening_order_id": task.order_id})
        self._exec_ledger.record_broker_fill(
            delta=delta, cur=parsed, position_id=task.position_id,
            liquidity_flag=None, venue=None, decision_id=task.decision_id,
            order_id=task.order_id)
        if parsed.side == "buy":
            # M4 §O / RM-8 at fill granularity: one MarginObservation per buy
            # fill (long-only => every opening buy fill is IML-reducing).
            read = self._account_store.latest_unsafe()
            if read is not None:
                held = _ZERO
                try:
                    pos = self._book.position(task.position_id)
                    held = pos.qty
                except ExecError:
                    held = _ZERO
                self._margin.observe(observation_from_read(
                    read, session_date_et=task.session_date_et,
                    after_iml_reducing=classify_iml_reducing(
                        parsed.side, delta.delta_qty, held),
                    eod=False))
        if task.kind == "open":
            booked = task.position_id in self._book._positions
            if not booked:
                opened_ts = (parsed.ts_broker_utc or self._instant_utc
                             or task.stamp.decision_ts_utc)
                self._book.open_position(
                    decision_id=task.decision_id, order_id=task.order_id,
                    symbol=task.symbol, instrument_id=task.instrument_id,
                    strategy_id=task.strategy_id, fills=[delta],
                    modeled=task.modeled, opened_ts_utc=opened_ts)
            else:
                self._book.apply_fill(task.position_id, delta)
        task.deltas.append(delta)
        task.prev_emitted = parsed

    def _terminal(self, task: _OrderTask, parsed: BrokerOrder) -> None:
        filled = parsed.filled_qty
        total_cost = _ZERO
        for delta in task.deltas:
            total_cost += delta.delta_cost_usd
        consistent = True
        if filled > 0:
            final = filled * parsed.filled_avg_price \
                if parsed.filled_avg_price is not None else None
            if final is None or total_cost != final:
                consistent = False
                self._exec_ledger.record_order_state_alert(
                    broker_order_id=parsed.broker_order_id,
                    raw_status=parsed.raw_status,
                    note="terminal_consistency_mismatch",
                    order_id=task.order_id)
            cum_notional = BrokerUSD(final if final is not None else total_cost)
        else:
            cum_notional = BrokerUSD(_ZERO)
        self._exec_ledger.record_order_terminal(
            terminal_state=parsed.state, filled_qty=filled,
            cum_notional_usd=cum_notional, ts_broker_utc=parsed.ts_broker_utc,
            decision_id=task.decision_id, order_id=task.order_id)
        # §M.5 post-kill late-fill tripwire (M5C-S11).
        if filled > 0 and self._risk_kill.state != "monitoring":
            self._exec_ledger.record_order_state_alert(
                broker_order_id=parsed.broker_order_id,
                raw_status=parsed.raw_status, note="late_fill_post_kill",
                order_id=task.order_id)
        if filled > 0 and consistent:
            self._fill_divergence(task, parsed, total_cost, filled)
            if task.kind == "close" and task.position_id is not None:
                if self._position_open(task.position_id):
                    self._book.close_position(
                        position_id=task.position_id, order_id=task.order_id,
                        fills=list(task.deltas), modeled=task.modeled,
                        reason=task.close_reason or "strategy_exit",
                        decision_id=task.decision_id)
                else:
                    # LC-2 (defense-in-depth): the target position was already
                    # closed (e.g. a kill flatten superseded this close, or M6
                    # reconciled it). Closing again would raise "position ... is
                    # closed (terminal)" and crash on_tick — log a benign
                    # reconcile note and skip (the broker stays position-of-
                    # record; M6 folds any residual fill).
                    self._exec_ledger.record_order_state_alert(
                        broker_order_id=parsed.broker_order_id,
                        raw_status=parsed.raw_status,
                        note="close_superseded_position_closed",
                        order_id=task.order_id)
        self._task = None

    def _position_open(self, position_id: str) -> bool:
        position = self._book._positions.get(position_id)
        return position is not None and position.status == "open"

    def _fill_divergence(self, task: _OrderTask, parsed: BrokerOrder,
                         total_cost: Decimal, filled: Decimal) -> None:
        # EX-3: side-aware divergence whenever a modeled basis exists — for the
        # OPEN buy AND (EC-1) the strategy CLOSE sell. `broker_cost_usd` is the
        # broker fill notional on EITHER side (sale PROCEEDS on a sell — the sign
        # asymmetry is handled inside assess_divergence). modeled None ⇒
        # unassessed (a kill-flatten close, which carries no modeled fill).
        if task.modeled is not None:
            result = assess_divergence(
                side=parsed.side, broker_cost_usd=BrokerUSD(total_cost),
                filled_qty=filled, modeled=task.modeled)
            modeled_over_filled = None
            if result.divergence_usd is not None:
                modeled_over_filled = ModeledUSD(
                    Decimal(total_cost) - result.divergence_usd)
            self._exec_ledger.record_fill_divergence(
                side=parsed.side, broker_cost_usd=BrokerUSD(total_cost),
                modeled_cost_usd=modeled_over_filled,
                divergence_usd=result.divergence_usd,
                divergence_bps=result.divergence_bps, flag=result.flag,
                order_id=task.order_id)
            if result.alert:
                self._exec_ledger.record_divergence_alert(
                    divergence_usd=result.divergence_usd,
                    divergence_bps=result.divergence_bps,
                    order_id=task.order_id)
            if task.position_id is not None:
                self._book.set_divergence_flag(task.position_id, result.flag)
        else:
            # no modeled basis (kill-flatten close) -> unassessed.
            self._exec_ledger.record_fill_divergence(
                side=parsed.side, broker_cost_usd=BrokerUSD(total_cost),
                modeled_cost_usd=None, divergence_usd=None,
                divergence_bps=None, flag="unassessed",
                order_id=task.order_id)

    # -- step 7: scan (M5C-S9 — NO gates pre-check) -----------------------------------

    def _assemble_ctxs(self, bars, now_ms: int) -> Dict[str, ScanContext]:
        latest_bar: Dict[str, object] = {}
        for bar in bars:
            if self._universe and bar.symbol not in self._universe:
                continue
            latest_bar[bar.symbol] = bar
        ctxs: Dict[str, ScanContext] = {}
        for symbol in sorted(latest_bar):
            bar = latest_bar[symbol]
            instrument_id = bar.instrument_id
            decision_ts = self._instant_utc or bar.watermark_utc
            session_date_et = self._calendar.session_date_for(decision_ts)
            snapshot = assemble(
                symbol=symbol, instrument_id=instrument_id,
                decision_ts_utc=_canonical_utc_str(_parse_utc(decision_ts)),
                decision_seen_at_ms=now_ms,
                event_start_bar_end_utc=bar.bucket_end_utc,
                feature=self._feature_view.latest(symbol, instrument_id),
                quote=self._quote_view.latest(symbol, instrument_id),
                market_state=self._cache.get(symbol, instrument_id,
                                             session_date_et, now_ms=now_ms),
                calendar_pin=self._calendar.calendar_pin(),
                config=self._signal_config, now_ms=now_ms)
            if isinstance(snapshot, GateFail):
                continue  # M5C-B2: no scan call, nothing journaled
            ctxs[symbol] = ScanContext(snapshot=snapshot,
                                       rules_hash=self._signal_config.rules_hash,
                                       now_ms=now_ms)
        return ctxs

    def _scan(self, ctxs: Dict[str, ScanContext], now_ms: int) -> None:
        if self._strategy is None or self.broker is None:
            return
        if getattr(self.broker, "kind", "spy") == "spy":
            return  # scans need a non-spy broker (§M.3 step 7)
        for symbol in sorted(ctxs):
            if self._task is not None:
                return  # FD-M5-21: one in flight
            if symbol in self._open_deny:
                continue
            ctx = ctxs[symbol]
            bar_end = ctx.snapshot.event_start_bar_end_utc
            if self._last_scanned_bar.get(symbol) == bar_end:
                continue  # one scan per completed bar per symbol
            self._last_scanned_bar[symbol] = bar_end
            candidates = self._strategy.scan(ctx)
            if not candidates:
                continue  # an empty scan journals nothing (FD-M5-9)
            self._start_open(ctx, candidates[0], now_ms)

    def _start_open(self, ctx: ScanContext, candidate: Candidate,
                    now_ms: int) -> None:
        snapshot = ctx.snapshot
        leg = candidate.legs[0]
        event_basis = _bar_key(leg.symbol, snapshot.event_start_bar_end_utc)
        decision_id = "d-" + row_hash({
            "run_id": self.run_id, "strategy_id": candidate.strategy_id,
            "symbol": leg.symbol, "instrument_id": leg.instrument_id,
            "event_basis": event_basis})
        stamp = DecisionStamp(
            decision_id=decision_id,
            decision_ts_utc=snapshot.decision_ts_utc,
            decision_seen_at_ms=now_ms, quote_a=snapshot.quote)
        self._exec_ledger.record_strategy_decision(
            symbol=leg.symbol, instrument_id=leg.instrument_id,
            strategy_id=candidate.strategy_id,
            strategy_kind=("synthetic"
                           if isinstance(self._strategy, SyntheticStrategy)
                           else "real"),
            action="would_open", side=leg.side, qty=leg.qty,
            strategy_limit=leg.limit_price, score=candidate.score,
            paper_eligible=candidate.paper_eligible, position_id=None,
            event_basis=event_basis, decision_ts_utc=snapshot.decision_ts_utc,
            decision_seen_at_ms=now_ms,
            quote_a=self._provenance(snapshot.quote),
            decision_id=decision_id)

        marks = {}
        if snapshot.quote_verdict.mid is not None:
            marks[leg.symbol] = Mark(
                symbol=leg.symbol, instrument_id=leg.instrument_id,
                mid=snapshot.quote_verdict.mid,
                seen_at_ms=snapshot.quote.seen_at_ms, source="quote_mid")
        verdict = self._engine.can_open(
            candidate, self._portfolio_read(now_ms),
            self._account_store.get(now_ms=now_ms),
            market_state={leg.symbol: snapshot.market_state}, marks=marks,
            kill_state=self._risk_kill.state,
            kill_generation=self._risk_kill.generation,
            margin_read=self._margin.read(snapshot.session_date_et),
            pdt_read=self._pdt.read(), loss_read=self._loss.read(),
            now_ms=now_ms, decision_id=decision_id)
        row = self._risk_ledger.record_risk_verdict(verdict,
                                                    decision_id=decision_id)
        if verdict.allowed is not True:
            return  # STOP — the risk row IS the refusal record (no preflight)
        self._task = _OrderTask(
            kind="open", state="await_latency", decision_id=decision_id,
            symbol=leg.symbol, instrument_id=leg.instrument_id,
            strategy_id=candidate.strategy_id, side=leg.side, qty=leg.qty,
            stamp=stamp, session_date_et=snapshot.session_date_et,
            candidate=candidate, risk_verdict=verdict, risk_verdict_row=row,
            risk_now_ms=now_ms)

    # -- step 8: exits (§M.7; RC-1) ------------------------------------------------------

    def _exits(self, ctxs: Dict[str, ScanContext], now_ms: int) -> None:
        if self._exit_provider is None or self.broker is None:
            return
        for symbol in sorted(ctxs):
            ctx = ctxs[symbol]
            instructions = self._exit_provider.exits(ctx)
            for instruction in instructions:
                if self._task is not None:
                    # RC-1: a re-emitted exit while ANY order is in flight is
                    # DROPPED, unjournaled (FD-M5-21 is GLOBAL).
                    continue
                self._start_close(instruction, ctx, now_ms)

    def _start_close(self, instruction, ctx: ScanContext, now_ms: int) -> None:
        """§M.7 — the strategy close path (reduce; no open preflight, no risk
        verdict, no run-gate consultation)."""
        position = None
        for pos in self._book._positions.values():
            if pos.symbol == instruction.symbol and pos.status == "open":
                position = pos
                break
        if position is None:
            return  # resolution 10: nothing to close
        strategy_id = (self._strategy.strategy_id
                       if self._strategy is not None else "exit_provider")
        event_basis = f"exit:{position.position_id}:{now_ms}"  # §P.3 rev 2
        decision_id = "d-" + row_hash({
            "run_id": self.run_id, "strategy_id": strategy_id,
            "symbol": instruction.symbol,
            "instrument_id": instruction.instrument_id,
            "event_basis": event_basis})
        quote_b = self._quote_view.latest(instruction.symbol,
                                          instruction.instrument_id)
        if quote_b is not None:
            self._exec_ledger.record_strategy_decision(
                symbol=instruction.symbol,
                instrument_id=instruction.instrument_id,
                strategy_id=strategy_id,
                strategy_kind=("synthetic"
                               if isinstance(self._strategy, SyntheticStrategy)
                               else "real"),
                action="would_close", side="sell", qty=instruction.qty,
                strategy_limit=None, score=None, paper_eligible=True,
                position_id=position.position_id, event_basis=event_basis,
                decision_ts_utc=ctx.snapshot.decision_ts_utc,
                decision_seen_at_ms=now_ms,
                quote_a=self._provenance(quote_b), decision_id=decision_id)
        cap = None
        if quote_b is not None:
            cap = reduce_cap(side="sell", quote=quote_b,
                             cap_bps=self._exec_config.slippage_cap_bps)
        if cap is None:
            self._exec_ledger.record_reject(
                symbol=instruction.symbol,
                instrument_id=instruction.instrument_id,
                strategy_id=strategy_id, stage="reduce_pricing",
                reasons=("no_price_for_cap",), stages_skipped=(),
                detail={"risk_reasons": None, "quote_reasons": None,
                        "broker_code": None, "broker_message": None},
                preflight_id=None, capped_limit=None,
                token_kind="reduce_only", kill_state=self._risk_kill.state,
                kill_generation=self._risk_kill.generation,
                quote_b=self._provenance(quote_b), decision_id=decision_id,
                order_id=None)
            return  # retry next tick as a NEW decision (M5C-S5)
        order_id = self._order_id_prefix() + row_hash({
            "run_id": self.run_id, "decision_id": decision_id,
            "preflight_id": None})
        intent = OrderIntent(
            symbol=instruction.symbol, side="sell", qty=instruction.qty,
            order_type="marketable_limit", tif="day", limit_price=cap,
            is_reducing=True, intent_id=order_id)
        # LC-1/SF-1: an over-qty (or otherwise position-invalid) ExitInstruction
        # makes the reduce-only mint raise PreflightRejected ("may flatten, never
        # flip"). The reduce path NEVER gains a gate (FD-M4-3), but a bad
        # instruction must not crash the loop: refuse it with a diagnostic
        # order_state_alert and return. self._task is still None here (set after
        # the mint), so the tick loop survives and the next tick re-scans.
        # An order_state_alert carries a free-form `note` (§P.2) — the
        # contract-faithful surface for this anomaly: the reduce-side reject
        # vocabulary is {no_price_for_cap, broker_rejected} (§2.1/§M.7), and
        # neither fits a qty>held refusal, so we do NOT fabricate a reject reason.
        try:
            token = mint_reduce_only_token(position, intent)
        except PreflightRejected:
            self._exec_ledger.record_order_state_alert(
                broker_order_id=None, raw_status="reduce_only_mint_rejected",
                note="reduce_qty_exceeds_held", order_id=order_id)
            return
        self._exec_ledger.record_order_submit_attempt(
            client_order_id=order_id, preflight_id=None, risk_verdict_id=None,
            strategy_id=strategy_id, symbol=instruction.symbol,
            instrument_id=instruction.instrument_id, side="sell",
            qty=instruction.qty,
            order_intent={"order_type": "marketable_limit", "tif": "day",
                          "limit_price": cap},
            token_kind="reduce_only",
            kill_generation=self._risk_kill.generation,
            quote_b=self._provenance(quote_b), decision_id=decision_id,
            order_id=order_id)
        # EC-1 (§J/§K/EX-9, EX-3): compute a SELL-side ModeledFill from the SAME
        # quote_b (and cap) the close priced reduce_cap on — never re-shopped
        # after broker fills (no hindsight; §I). Journal its modeled_execution_
        # fill row, carry it on the close task, and pass it to close_position at
        # terminal so realized_modeled_pnl reflects the EX-9 sell fee (notional =
        # the MODELED exit proceeds) and the close fill_divergence gets a real
        # side-aware flag (EX-3) instead of the dead hardcoded "unassessed".
        modeled = self._close_modeled_fill(quote_b, cap, instruction.qty,
                                           order_id, decision_id, now_ms)
        stamp = DecisionStamp(decision_id=decision_id,
                              decision_ts_utc=ctx.snapshot.decision_ts_utc,
                              decision_seen_at_ms=now_ms, quote_a=quote_b)
        task = _OrderTask(
            kind="close", state="watch", decision_id=decision_id,
            symbol=instruction.symbol,
            instrument_id=instruction.instrument_id,
            strategy_id=strategy_id, side="sell", qty=instruction.qty,
            stamp=stamp, session_date_et=ctx.snapshot.session_date_et,
            order_id=order_id, position_id=position.position_id,
            close_reason=instruction.reason, capped_limit=cap, modeled=modeled,
            quote_b=quote_b, bound_epoch=quote_b.reconnect_epoch,
            last_poll_ms=now_ms)
        self._task = task
        try:
            result = self.broker.submit_order(intent, token)
        except Exception as err:  # noqa: BLE001 — ambiguous (FD-M5-17)
            self._submit_recovery(task, err)
            return
        self._after_submit(task, result, quote_b, None, now_ms)

    def _close_modeled_fill(self, quote_b, cap, qty, order_id, decision_id,
                            now_ms: int):
        """EC-1: the SELL-side ModeledFill for a strategy close, computed from
        quote_b (the quote the close priced reduce_cap on) and the close cap —
        symmetric with the open-side block in `_after_submit`. Journals the
        `modeled_execution_fill` row and returns the bound ModeledFill (carried
        on the close task and passed to close_position at terminal)."""
        quote_b_verdict = evaluate_quote(
            quote_b, now_ms=now_ms,
            spread_bps_max=self._exec_config.spread_bps_max,
            staleness_ms_max=self._exec_config.quote_staleness_ms_max)
        modeled = model_fill(side="sell", qty=qty, capped_limit=cap,
                             quote_b=quote_b, quote_b_verdict=quote_b_verdict,
                             depth=None, now_ms=now_ms)
        modeled = bind_modeled_fill_id(modeled, order_id=order_id)
        notional = (modeled.modeled_cost_usd
                    if modeled.modeled_cost_usd is not None else cap * qty)
        fees = fees_for(side="sell", qty=qty, notional=Decimal(notional))
        self._exec_ledger.record_modeled_execution_fill(
            modeled_fill_id=modeled.modeled_fill_id, model=modeled.model,
            realism_class=modeled.realism_class,
            requested_qty=modeled.requested_qty,
            modeled_fillable_qty=modeled.modeled_fillable_qty,
            modeled_vwap=modeled.modeled_vwap, worst_price=modeled.worst_price,
            touch_price=modeled.touch_price,
            levels_consumed=modeled.levels_consumed,
            slippage_vs_mid_bps=modeled.slippage_vs_mid_bps,
            modeled_cost_usd=modeled.modeled_cost_usd, fees_assumed=fees,
            quote=dict(modeled.quote), reasons=list(modeled.reasons),
            decision_id=decision_id, order_id=order_id)
        return modeled

    # -- step 9: marks --------------------------------------------------------------------

    def _marks(self, bars, now_ms: int) -> None:
        if not bars:
            return
        bar_by_symbol = {bar.symbol: bar for bar in bars}
        for position_id in sorted(self._book._positions):
            position = self._book._positions[position_id]
            if position.status != "open":
                continue
            bar = bar_by_symbol.get(position.symbol)
            if bar is None:
                continue
            quote_snapshot = self._quote_view.latest(position.symbol,
                                                     position.instrument_id)
            if quote_snapshot is None:
                continue
            bar_key = _bar_key(position.symbol, bar.bucket_end_utc)
            row = self._book.mark(position_id, quote_snapshot, now_ms=now_ms,
                                  bar_key=bar_key)
            if row is not None:
                self._position_marked.add(position_id)
            if position_id in self._position_marked:
                self._book.pnl_snapshot(position_id, bar_key=bar_key)

    # -- step 10: session edge --------------------------------------------------------------

    def _session_edge(self, now_ms: int) -> None:
        if self._instant_utc is None:
            return
        try:
            phase = self._calendar.phase_at(self._instant_utc)
        except UnknownSessionDate:
            phase = SessionPhase.UNKNOWN
        previous = self._last_phase
        self._last_phase = phase
        if previous != SessionPhase.RTH or phase == SessionPhase.RTH:
            return
        # leaving RTH: best-effort cancel of open orders + close_of_day.
        task = self._task
        if task is not None and task.state == "watch":
            self._attempt_cancel(task, "session_end")
        session_date_et = self._calendar.session_date_for(self._instant_utc)
        already_closed = session_date_et in self._closed_sessions
        read = self._account_store.latest_unsafe()
        if not already_closed and read is not None:
            self._closed_sessions.add(session_date_et)
            self._margin.close_of_day(session_date_et, observation_from_read(
                read, session_date_et=session_date_et,
                after_iml_reducing=False, eod=True))
        if (self.mode == "paper"
                and session_date_et not in self._reconciled_eod_sessions):
            self._reconciled_eod_sessions.add(session_date_et)
            self.run_reconcile(
                phase="eod", ts_utc=self._instant_utc, now_ms=now_ms)
