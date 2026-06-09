"""M3 §H — observe-only calibration probe (design §2 data flow). Emits decision
rows ONLY.

S1: this module never imports broker/execution_preflight/kill_switch/arming and
never constructs Candidate/OrderIntent; `paper_eligible` is hard-pinned False at the
ledger (a True value RAISES). The probe holds NO rolling numeric state (that lives
in FeatureEngine); its only state is injected collaborators + run_id. It performs no
I/O beyond the ledger.

Tick semantics (contract §H rev2/rev3): gates 1-4 stop the tick with ONE do_nothing
row; the per-horizon gate never suppresses sibling horizons; `calendar_unknown` is
attributed per-horizon when the schedule fetch raised UnknownSessionDate.
"""
from dataclasses import dataclass
from typing import Optional, Protocol, Tuple, runtime_checkable

from agent.bar_series import _canonical_utc, _parse_utc
from agent.calibration import AsOfClimatology
from agent.feature_engine import FeatureSnapshot, FeatureView
from agent.forecast import Forecast, ForecastEvent, predict
from agent.market_calendar import MarketCalendar, UnknownSessionDate
from agent.market_state import Verdict
from agent.market_state_cache import MarketStateCache
from agent.quote_quality import QuoteSnapshot
from agent.serializer import row_hash
from agent.signal_config import SignalConfig
from agent.signal_snapshot import GATE_STAGES, GateFail, REASONS, SignalSnapshot, assemble, horizon_gate
from recorder.persistence import EventWriter

STRATEGY_ID = "calibration_probe_v1"
ACTIONS = frozenset({"do_nothing", "forecast_only"})     # would_open FORBIDDEN until M5/M7

# Frozen decision-row field set (contract §I; decision_id rides the journal kwarg).
_ROW_FIELDS = frozenset({
    "symbol", "instrument_id", "strategy", "action", "gate_stage", "reasons",
    "horizon", "forecast_id", "forecast", "reference_base_rate_asof_t0",
    "reference_forecaster_id", "reference_n", "edge_label", "signal_provenance",
    "quote_provenance", "market_state_provenance", "event_start_bar_key",
    "resolve_bar_key", "decision_ts_utc", "decision_seen_at_ms", "data_pin",
    "rules_hash", "paper_eligible",
})


class DecisionLedger:
    """Validating writer for journal/decisions.jsonl (StatusLedger pattern).

    Raises on: action not in ACTIONS, paper_eligible not (identity) False, any
    reason outside the frozen vocabulary, gate_stage outside GATE_STAGES, a field
    set differing from the frozen §I set (reserved-key collisions are impossible by
    construction — decision_id rides the journal kwarg).
    """

    EVT_DECISION = "decision"

    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None:
        self._writer = writer
        self._rules_hash = rules_hash

    def record_decision(self, *, decision_id: str, fields: dict) -> dict:
        if set(fields) != _ROW_FIELDS:
            missing = _ROW_FIELDS - set(fields)
            extra = set(fields) - _ROW_FIELDS
            raise ValueError(
                f"decision row field set mismatch: missing={sorted(missing)}, "
                f"extra={sorted(extra)}")
        if fields["action"] not in ACTIONS:
            raise ValueError(f"action {fields['action']!r} not in {sorted(ACTIONS)}")
        if fields["paper_eligible"] is not False:
            raise ValueError("paper_eligible must be identity-False on every M3 row (S1)")
        for reason in fields["reasons"]:
            if reason not in REASONS:
                raise ValueError(f"out-of-vocabulary reason: {reason!r}")
        stage = fields["gate_stage"]
        if stage is not None and stage not in GATE_STAGES:
            raise ValueError(f"out-of-vocabulary gate_stage: {stage!r}")
        if fields["rules_hash"] != self._rules_hash:
            raise ValueError("row rules_hash differs from the ledger's rules_hash")
        return self._writer.record(self.EVT_DECISION, fields, decision_id=decision_id)


@dataclass(frozen=True)
class ForecastDecision:
    action: str                       # ∈ ACTIONS
    symbol: str
    instrument_id: int
    horizon: Optional[str]            # None on pre-horizon do_nothing (FD-9)
    reasons: Tuple[str, ...]
    forecast: Optional[Forecast]
    decision_id: str
    forecast_id: Optional[str]
    row: dict                         # the journaled row (as returned by the ledger)


@runtime_checkable
class QuoteView(Protocol):
    def latest(self, symbol: str, instrument_id: int) -> Optional[QuoteSnapshot]: ...


def _bar_key(symbol: str, interval: str, bucket_end_utc: str) -> str:
    return f"{symbol}|{interval}|{bucket_end_utc}"


class CalibrationProbe:
    def __init__(self, *, config: SignalConfig, calendar: MarketCalendar,
                 market_state_cache: MarketStateCache, feature_view: FeatureView,
                 quote_view: QuoteView, ledger: DecisionLedger,
                 climatology: AsOfClimatology, run_id: str, clock) -> None:
        self._config = config
        self._calendar = calendar
        self._market_state_cache = market_state_cache
        self._feature_view = feature_view
        self._quote_view = quote_view
        self._ledger = ledger
        self._climatology = climatology
        self._run_id = run_id
        self._clock = clock

    # --- deterministic ids (contract §I) ---

    def _decision_id(self, *, symbol, instrument_id, event_start_bar_key,
                     horizon: Optional[str]) -> str:
        return "d-" + row_hash({
            "run_id": self._run_id, "symbol": symbol, "instrument_id": instrument_id,
            "strategy": STRATEGY_ID, "event_start_bar_key": event_start_bar_key,
            "horizon": horizon,
        })

    def _forecast_id(self, *, decision_id, symbol, instrument_id, data_pin,
                     feature_snapshot_id, event_start_bar_key, resolve_bar_key,
                     horizon, threshold_k, p, reference_forecaster_id) -> str:
        return "f-" + row_hash({
            "run_id": self._run_id, "decision_id": decision_id, "symbol": symbol,
            "instrument_id": instrument_id, "strategy": STRATEGY_ID,
            "rules_hash": self._config.rules_hash, "data_pin": data_pin,
            "model_version": self._config.model_version,
            "feature_snapshot_id": feature_snapshot_id,
            "event_start_bar_key": event_start_bar_key,
            "resolve_bar_key": resolve_bar_key,
            "h": horizon, "k": str(threshold_k), "p": str(p),
            "reference_forecaster_id": reference_forecaster_id,
        })

    # --- row assembly (frozen §I field set) ---

    def _market_state_provenance(self, verdict: Verdict, calendar_pin: str) -> dict:
        return {
            "tradability": verdict.tradability.value,
            "session_state": verdict.session_state.value,
            "reasons": list(verdict.reasons),
            "ca_blackout": verdict.ca_blackout,
            "stale_safe_default": "cache_stale_safe_default" in verdict.reasons,
            "calendar_pin": calendar_pin,
            "session_date_et": verdict.session_date_et,
        }

    def _signal_provenance(self, feature: Optional[FeatureSnapshot]) -> Optional[dict]:
        if feature is None:
            return None
        return {
            "feature_snapshot_id": feature.feature_snapshot_id,
            "feature_cutoff_bar_end_utc": feature.feature_cutoff_bar_end_utc,
            "feature_watermark_utc": feature.watermark_utc,
            "data_pin": feature.data_pin,
            "model_version": self._config.model_version,
            "model_artifact_hash": self._config.model_artifact_hash,
        }

    def _quote_provenance(self, quote: Optional[QuoteSnapshot]) -> Optional[dict]:
        if quote is None:
            return None
        return {
            "ts_event_utc": quote.ts_event_utc,
            "ts_recv_utc": quote.ts_recv_utc,
            "seen_at_ms": quote.seen_at_ms,
            "reconnect_epoch": quote.reconnect_epoch,
            "vendor_seq": quote.vendor_seq,
            "dataset": quote.dataset,
            "schema": quote.schema,
        }

    def _base_fields(self, *, symbol, instrument_id, event_start_bar_key,
                     decision_ts_utc, decision_seen_at_ms,
                     feature: Optional[FeatureSnapshot],
                     quote: Optional[QuoteSnapshot], verdict: Verdict,
                     calendar_pin: str) -> dict:
        return {
            "symbol": symbol,
            "instrument_id": instrument_id,
            "strategy": STRATEGY_ID,
            "action": "do_nothing",
            "gate_stage": None,
            "reasons": [],
            "horizon": None,
            "forecast_id": None,
            "forecast": None,
            "reference_base_rate_asof_t0": None,
            "reference_forecaster_id": None,
            "reference_n": None,
            "edge_label": None,
            "signal_provenance": self._signal_provenance(feature),
            "quote_provenance": self._quote_provenance(quote),
            "market_state_provenance": self._market_state_provenance(verdict, calendar_pin),
            "event_start_bar_key": event_start_bar_key,
            "resolve_bar_key": None,   # null on ALL do_nothing rows (rev3 minor-5)
            "decision_ts_utc": decision_ts_utc,
            "decision_seen_at_ms": decision_seen_at_ms,
            "data_pin": feature.data_pin if feature is not None else "",
            "rules_hash": self._config.rules_hash,
            "paper_eligible": False,
        }

    # --- the frozen tick algorithm (§H) ---

    def on_bar_complete(self, *, symbol: str, instrument_id: int,
                        event_start_bar_end_utc: str, decision_ts_utc: str
                        ) -> Tuple[ForecastDecision, ...]:
        # Re-mint the tick boundary in the ONE canonical surface form (§0/SAFETY-F2;
        # harden round 1, M3-02): a recorder-derived whole-second form would otherwise
        # journal a second bar-key string for the same instant — two id sets for one
        # logical bar, double-counted funnel ticks.
        event_start_bar_end_utc = _canonical_utc(_parse_utc(event_start_bar_end_utc))
        decision_seen_at_ms = self._clock.now_ms()
        # Pure date conversion — raises only on a malformed timestamp (programming
        # error, propagates; rev2 REPO-F1).
        session_date_et = self._calendar.session_date_for(decision_ts_utc)
        try:
            schedule = self._calendar.schedule_for(session_date_et)
        except UnknownSessionDate:
            schedule = None  # calendar_unknown attributed per-horizon at gate 5
        calendar_pin = self._calendar.calendar_pin()

        feature = self._feature_view.latest(symbol, instrument_id)
        quote = self._quote_view.latest(symbol, instrument_id)
        verdict = self._market_state_cache.get(
            symbol, instrument_id, session_date_et, now_ms=decision_seen_at_ms)

        event_start_bar_key = _bar_key(symbol, self._config.interval,
                                       event_start_bar_end_utc)

        result = assemble(
            symbol=symbol, instrument_id=instrument_id,
            decision_ts_utc=decision_ts_utc, decision_seen_at_ms=decision_seen_at_ms,
            event_start_bar_end_utc=event_start_bar_end_utc,
            feature=feature, quote=quote, market_state=verdict,
            calendar_pin=calendar_pin, config=self._config,
            now_ms=decision_seen_at_ms,
        )

        if isinstance(result, GateFail):
            # gates 1-4: exactly ONE do_nothing row (horizon=null), STOP (FD-9).
            decision_id = self._decision_id(
                symbol=symbol, instrument_id=instrument_id,
                event_start_bar_key=event_start_bar_key, horizon=None)
            fields = self._base_fields(
                symbol=symbol, instrument_id=instrument_id,
                event_start_bar_key=event_start_bar_key,
                decision_ts_utc=decision_ts_utc,
                decision_seen_at_ms=decision_seen_at_ms,
                feature=feature, quote=quote, verdict=verdict,
                calendar_pin=calendar_pin)
            fields["gate_stage"] = result.stage
            fields["reasons"] = list(result.reasons)
            row = self._ledger.record_decision(decision_id=decision_id, fields=fields)
            return (ForecastDecision(
                action="do_nothing", symbol=symbol, instrument_id=instrument_id,
                horizon=None, reasons=result.reasons, forecast=None,
                decision_id=decision_id, forecast_id=None, row=row),)

        snapshot: SignalSnapshot = result
        decisions = []
        for horizon in self._config.horizons:
            decision_id = self._decision_id(
                symbol=symbol, instrument_id=instrument_id,
                event_start_bar_key=event_start_bar_key, horizon=horizon)
            gate = horizon_gate(snapshot, horizon, schedule)
            if isinstance(gate, GateFail):
                # per-horizon do_nothing; sibling horizons UNAFFECTED (SAFETY-F4).
                fields = self._base_fields(
                    symbol=symbol, instrument_id=instrument_id,
                    event_start_bar_key=event_start_bar_key,
                    decision_ts_utc=decision_ts_utc,
                    decision_seen_at_ms=decision_seen_at_ms,
                    feature=snapshot.feature, quote=snapshot.quote,
                    verdict=snapshot.market_state, calendar_pin=calendar_pin)
                fields["gate_stage"] = gate.stage
                fields["reasons"] = list(gate.reasons)
                fields["horizon"] = horizon
                row = self._ledger.record_decision(decision_id=decision_id, fields=fields)
                decisions.append(ForecastDecision(
                    action="do_nothing", symbol=symbol, instrument_id=instrument_id,
                    horizon=horizon, reasons=gate.reasons, forecast=None,
                    decision_id=decision_id, forecast_id=None, row=row))
                continue

            resolve_bar_end_utc = gate
            resolve_bar_key = _bar_key(symbol, self._config.interval, resolve_bar_end_utc)
            p_ref, forecaster_id, reference_n = self._climatology.rate(
                symbol=symbol, horizon=horizon)
            event = ForecastEvent(
                horizon=horizon, threshold_k=snapshot.threshold_k,
                event_start_bar_end_utc=event_start_bar_end_utc,
                resolve_bar_end_utc=resolve_bar_end_utc)
            forecast = predict(
                snapshot.feature.features,
                coefficients=self._config.coefficients[horizon],
                model_version=self._config.model_version,
                model_artifact_hash=self._config.model_artifact_hash,
                event=event)
            forecast_id = self._forecast_id(
                decision_id=decision_id, symbol=symbol, instrument_id=instrument_id,
                data_pin=snapshot.feature.data_pin,
                feature_snapshot_id=snapshot.feature.feature_snapshot_id,
                event_start_bar_key=event_start_bar_key,
                resolve_bar_key=resolve_bar_key, horizon=horizon,
                threshold_k=snapshot.threshold_k, p=forecast.p,
                reference_forecaster_id=forecaster_id)
            # edge_label: exact Decimal difference of two PROB_QUANTUM operands
            # (exponent 1e-6 by construction; NO further quantization — MATH-Q9).
            edge_label = forecast.p - p_ref

            fields = self._base_fields(
                symbol=symbol, instrument_id=instrument_id,
                event_start_bar_key=event_start_bar_key,
                decision_ts_utc=decision_ts_utc,
                decision_seen_at_ms=decision_seen_at_ms,
                feature=snapshot.feature, quote=snapshot.quote,
                verdict=snapshot.market_state, calendar_pin=calendar_pin)
            fields["action"] = "forecast_only"
            fields["horizon"] = horizon
            fields["forecast_id"] = forecast_id
            fields["forecast"] = {
                "event_type": "up_move",            # M3 constant (rev2 BUILD-F18)
                "h": horizon,
                "k": snapshot.threshold_k,
                "p": forecast.p,
            }
            fields["reference_base_rate_asof_t0"] = p_ref
            fields["reference_forecaster_id"] = forecaster_id
            fields["reference_n"] = reference_n
            fields["edge_label"] = edge_label
            fields["resolve_bar_key"] = resolve_bar_key
            row = self._ledger.record_decision(decision_id=decision_id, fields=fields)
            decisions.append(ForecastDecision(
                action="forecast_only", symbol=symbol, instrument_id=instrument_id,
                horizon=horizon, reasons=(), forecast=forecast,
                decision_id=decision_id, forecast_id=forecast_id, row=row))

        return tuple(decisions)
