"""M3 §G — resolve, score, reference (contract rev2/rev3).

The S3 wall for LABELS and the CLIMATOLOGY lives in `ForecastResolver.resolve_due`:
label bars are read AS-OF resolve time (`as_of_utc=now_utc`); a `future_receipt`
bar DEFERS the forecast (no row, retried later) so a future-received label can
never leak into a score or into the as-of reference (rev2 SAFETY-F1). Unresolved is
TERMINAL per forecast_id (FD-8): the resolver replays the scored stream ONCE per
instance (the stream is single-writer — S6; ids this resolver writes are tracked
in-memory, so a per-bar-batch caller does not re-read the whole file each call)
and skips seen ids — rerun appends nothing, including from a FRESH resolver,
which re-replays the stream on its first call.

Scoring math is frozen (rev2 MATH-Q1/Q4/Q5): every formula in this module is pinned
in the contract; the 3-term Murphy identity BS = REL - RES + UNC is exact ONLY when
every occupied bin's forecasts are constant (the within-bin variance/covariance
residual is otherwise O(1e-3)) — the fixture is constrained accordingly.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Dict, Iterable, List, Optional, Tuple, Union

from agent.bar_series import MISSING_BAR_REASONS, MidBar, MidBarSeriesReader, MissingBar
from agent.journal import replay
from recorder.persistence import EventWriter

PROB_QUANTUM = Decimal("0.000001")
BRIER_QUANTUM = Decimal("0.000000000001")
REPORT_QUANTUM = Decimal("0.00000001")   # rev2 BUILD-F15: all means/divisions

UNRESOLVED_REASONS = frozenset({          # rev2 BUILD-F12: closed, ledger-validated
    "no_mid_bar_t0", "no_mid_bar_resolve", "label_source_mismatch",
})

# Persisted-value arithmetic runs under THIS pinned context, never the ambient
# (thread-global, caller-mutable) one — a caller-shrunk precision would otherwise
# change persisted bytes or make 8/12-dp quantize raise (harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _quantize_checked(value: Decimal, quantum: Decimal, *, field: str) -> Decimal:
    """Round-trip-enforced quantize for PROVABLY-EXACT quanta (MID/BRIER — §L):
    a value that does not round-trip is a logic error, fail-loud (status_ledger
    precedent; harden round 1, M3-R1-003)."""
    q = value.quantize(quantum, ROUND_HALF_EVEN)
    if q != value:
        raise ValueError(
            f"{field}: {value} does not round-trip through {quantum} (precision loss)")
    return q

# Sample shapes crossing the calibration<->report boundary (rev2 BUILD-F15):
#   Sample    = (p: Decimal, outcome: int)
#   RefSample = (p: Decimal, outcome: int, p_ref: Decimal)


class AsOfClimatology:
    """D5/FD-6. Ingest order IS resolution order; rate() sees only previously-
    ingested outcomes. Below min_samples it degrades to constant 0.5.

    Ingestion is IDEMPOTENT per forecast_id (harden round 1, M3-01 BLOCKER): the
    resolver re-seeds from the replayed scored stream on its FIRST resolve_due
    call AND ingests live after each new score — without id-level dedupe HERE, a
    fresh resolver instance sharing this climatology would double-count outcomes
    it already ingested live, corrupting the persisted FD-6 reference fields.
    A repeated forecast_id is a silent no-op.
    """

    def __init__(self, *, min_samples: int) -> None:
        self._min_samples = int(min_samples)
        self._cells: Dict[Tuple[str, str], List[int]] = {}
        self._seen_forecast_ids = set()

    def ingest_resolved(self, *, symbol: str, horizon: str, outcome: int,
                        forecast_id: str) -> None:
        if outcome not in (0, 1):
            raise ValueError(f"outcome must be 0 or 1, got {outcome!r}")
        if not forecast_id:
            raise ValueError("forecast_id is required for idempotent ingestion")
        if forecast_id in self._seen_forecast_ids:
            return  # already counted — idempotent by construction
        self._seen_forecast_ids.add(forecast_id)
        self._cells.setdefault((symbol, horizon), []).append(outcome)

    def rate(self, *, symbol: str, horizon: str) -> Tuple[Decimal, str, int]:
        """(p_ref quantized PROB_QUANTUM — quantize-only, the division is generically
        off-grid (rev2 MATH-Q6) — forecaster_id, n)."""
        outcomes = self._cells.get((symbol, horizon), [])
        n = len(outcomes)
        if n < self._min_samples:
            return (Decimal("0.500000"), "constant_0.5", n)
        with localcontext(_DECIMAL_CTX):
            p_ref = (Decimal(sum(outcomes)) / Decimal(n)).quantize(
                PROB_QUANTUM, ROUND_HALF_EVEN)
        return (p_ref, "climatology_asof_v1", n)


class ScoredLedger:
    """Validating writer for journal/forecast_scored.jsonl (StatusLedger pattern).

    Raises on: reason not in UNRESOLVED_REASONS; outcome not in (0, 1);
    missing_bar_reason outside the MissingBar vocabulary when non-None; AND
    missing_bar_reason == "future_receipt" — a deferral must NEVER persist
    (structural SAFETY-F1 enforcement, rev3 minor-4).
    """

    EVT_SCORED = "forecast_scored"
    EVT_UNRESOLVED = "forecast_unresolved"

    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None:
        self._writer = writer
        self._rules_hash = rules_hash

    def record_scored(self, *, decision_id, forecast_id, outcome: int, brier_i: Decimal,
                      mid_t0: Decimal, mid_th: Decimal, event_start_bar_key,
                      resolve_bar_key, resolved_as_of_utc: str,
                      label_provenance: dict, data_pin: str) -> dict:
        if outcome not in (0, 1) or isinstance(outcome, bool):
            raise ValueError(f"outcome must be 0 or 1, got {outcome!r}")
        fields = {
            "forecast_id": forecast_id,
            "outcome": outcome,
            "brier_i": brier_i,
            "mid_t0": mid_t0,
            "mid_th": mid_th,
            "event_start_bar_key": event_start_bar_key,
            "resolve_bar_key": resolve_bar_key,
            "resolved_as_of_utc": resolved_as_of_utc,
            "label_provenance": label_provenance,
            "data_pin": data_pin,
            "rules_hash": self._rules_hash,
        }
        return self._writer.record(self.EVT_SCORED, fields, decision_id=decision_id)

    def record_unresolved(self, *, decision_id, forecast_id, reason: str,
                          missing_bar_reason: Optional[str],
                          event_start_bar_key, resolve_bar_key,
                          resolved_as_of_utc: str, data_pin: str) -> dict:
        if reason not in UNRESOLVED_REASONS:
            raise ValueError(f"out-of-vocabulary unresolved reason: {reason!r}")
        if missing_bar_reason is not None:
            if missing_bar_reason not in MISSING_BAR_REASONS:
                raise ValueError(
                    f"out-of-vocabulary missing_bar_reason: {missing_bar_reason!r}")
            if missing_bar_reason == "future_receipt":
                raise ValueError(
                    "future_receipt is a DEFERRAL and must never persist as "
                    "unresolved (SAFETY-F1)")
        fields = {
            "forecast_id": forecast_id,
            "reason": reason,
            "missing_bar_reason": missing_bar_reason,
            "event_start_bar_key": event_start_bar_key,
            "resolve_bar_key": resolve_bar_key,
            "resolved_as_of_utc": resolved_as_of_utc,
            "data_pin": data_pin,
            "rules_hash": self._rules_hash,
        }
        return self._writer.record(self.EVT_UNRESOLVED, fields, decision_id=decision_id)


@dataclass(frozen=True)
class ResolveStats:
    """rev2 BUILD-F7 — frozen shape; §M.7 asserts these."""

    considered: int                  # decision rows seen with action=="forecast_only"
    due: int                         # resolve_bar_end_utc <= now_utc (parsed compare)
    scored: int
    unresolved: int                  # terminal unresolved rows written this call
    skipped_already_resolved: int    # forecast_id already in the scored stream (FD-8)
    deferred_not_eligible: int       # SAFETY-F1: future_receipt/not-yet-eligible -> retry later


def _split_bar_key(bar_key: str) -> Tuple[str, str, str]:
    """The frozen format `symbol|interval|bucket_end_utc` (rev3 minor-2)."""
    parts = bar_key.split("|")
    if len(parts) != 3:
        raise ValueError(f"malformed bar key: {bar_key!r}")
    return parts[0], parts[1], parts[2]


def _label_leg(bar: MidBar) -> dict:
    prov = bar.quote_provenance
    return {
        "watermark_utc": bar.watermark_utc,
        "ts_event_utc": prov.get("ts_event_utc"),
        "ts_recv_utc": prov.get("ts_recv_utc"),
        "reconnect_epoch": prov.get("reconnect_epoch"),
        "vendor_seq": prov.get("vendor_seq"),
    }


class ForecastResolver:
    def __init__(self, *, reader: MidBarSeriesReader, ledger: ScoredLedger,
                 scored_stream_path, climatology: Optional[AsOfClimatology] = None) -> None:
        self._reader = reader
        self._ledger = ledger
        self._scored_stream_path = scored_stream_path
        self._climatology = climatology
        # FD-8 seen ids: replayed ONCE per instance (single-writer stream), then
        # maintained incrementally from this resolver's own writes — resolve_due
        # runs per bar batch and a full re-read + re-hash per call is O(day²).
        self._seen: Optional[set] = None

    def resolve_due(self, decision_rows: Iterable[dict], *, now_utc: str) -> ResolveStats:
        from agent.bar_series import _parse_utc  # shared parse-then-compare chokepoint

        decision_rows = list(decision_rows)
        by_forecast_id: Dict[str, dict] = {}
        for row in decision_rows:
            forecast_id = row.get("forecast_id")
            if forecast_id is not None and forecast_id not in by_forecast_id:
                by_forecast_id[forecast_id] = row

        # FD-8: replay -> seen ids (both event types), in stream seq order —
        # once per instance; later calls trust the in-memory set plus our own
        # appended ids (no other writer exists on this stream — S6).
        if self._seen is None:
            replayed = replay(self._scored_stream_path)
            self._seen = set()
            for row in sorted(replayed, key=lambda r: r["seq"]):
                self._seen.add(row["forecast_id"])
                # rev2 SAFETY-F14 + rev3 minor-3: seed the climatology from replayed
                # scored rows (resolution order == seq order) via the forecast_id join.
                if self._climatology is not None and row["event_type"] == ScoredLedger.EVT_SCORED:
                    decision = by_forecast_id.get(row["forecast_id"])
                    if decision is None:
                        raise ValueError(
                            "scored stream row has no matching decision row "
                            f"(forecast_id={row['forecast_id']!r}) — corrupt stream pair")
                    self._climatology.ingest_resolved(
                        symbol=decision["symbol"], horizon=decision["horizon"],
                        outcome=row["outcome"], forecast_id=row["forecast_id"])
        seen = self._seen

        now = _parse_utc(now_utc)
        considered = due = scored = unresolved = skipped = deferred = 0
        resolved_this_call: set = set()

        for row in decision_rows:
            if row.get("action") != "forecast_only":
                continue
            considered += 1
            forecast_id = row["forecast_id"]
            if forecast_id in seen or forecast_id in resolved_this_call:
                skipped += 1
                continue
            _, _, resolve_end = _split_bar_key(row["resolve_bar_key"])
            if _parse_utc(resolve_end) > now:
                continue  # not due yet
            due += 1

            symbol = row["symbol"]
            instrument_id = row["instrument_id"]
            _, _, t0_end = _split_bar_key(row["event_start_bar_key"])

            t0 = self._reader.get(symbol, instrument_id, t0_end, as_of_utc=now_utc)
            th = self._reader.get(symbol, instrument_id, resolve_end, as_of_utc=now_utc)

            # SAFETY-F1: a future-received label defers — NO row, retried later.
            if (isinstance(t0, MissingBar) and t0.reason == "future_receipt") or (
                    isinstance(th, MissingBar) and th.reason == "future_receipt"):
                deferred += 1
                continue

            decision_id = row.get("decision_id")
            if isinstance(t0, MissingBar) or isinstance(th, MissingBar):
                if isinstance(t0, MissingBar):
                    reason, missing_reason = "no_mid_bar_t0", t0.reason
                else:
                    reason, missing_reason = "no_mid_bar_resolve", th.reason
                self._ledger.record_unresolved(
                    decision_id=decision_id, forecast_id=forecast_id, reason=reason,
                    missing_bar_reason=missing_reason,
                    event_start_bar_key=row["event_start_bar_key"],
                    resolve_bar_key=row["resolve_bar_key"],
                    resolved_as_of_utc=now_utc, data_pin=row["data_pin"])
                unresolved += 1
                resolved_this_call.add(forecast_id)
                continue

            if (t0.source_dataset, t0.source_schema, t0.data_pin) != (
                    th.source_dataset, th.source_schema, th.data_pin):
                self._ledger.record_unresolved(
                    decision_id=decision_id, forecast_id=forecast_id,
                    reason="label_source_mismatch", missing_bar_reason=None,
                    event_start_bar_key=row["event_start_bar_key"],
                    resolve_bar_key=row["resolve_bar_key"],
                    resolved_as_of_utc=now_utc, data_pin=row["data_pin"])
                unresolved += 1
                resolved_this_call.add(forecast_id)
                continue

            forecast = row["forecast"]
            k = Decimal(str(forecast["k"]))
            p = Decimal(str(forecast["p"]))
            with localcontext(_DECIMAL_CTX):
                outcome = 1 if th.mid >= t0.mid * (Decimal("1") + k) else 0
                # (p−o)² of 6dp p is EXACTLY 12dp — round-trip enforced (§L).
                brier_i = _quantize_checked((p - outcome) ** 2, BRIER_QUANTUM,
                                            field="brier_i")
            label_provenance = {
                "t0": _label_leg(t0),
                "th": _label_leg(th),
                "source_dataset": t0.source_dataset,
                "source_schema": t0.source_schema,
            }
            self._ledger.record_scored(
                decision_id=decision_id, forecast_id=forecast_id, outcome=outcome,
                brier_i=brier_i, mid_t0=t0.mid, mid_th=th.mid,
                event_start_bar_key=row["event_start_bar_key"],
                resolve_bar_key=row["resolve_bar_key"],
                resolved_as_of_utc=now_utc,
                label_provenance=label_provenance, data_pin=row["data_pin"])
            scored += 1
            resolved_this_call.add(forecast_id)
            if self._climatology is not None:
                self._climatology.ingest_resolved(
                    symbol=symbol, horizon=row["horizon"], outcome=outcome,
                    forecast_id=forecast_id)

        self._seen.update(resolved_this_call)
        return ResolveStats(
            considered=considered, due=due, scored=scored, unresolved=unresolved,
            skipped_already_resolved=skipped, deferred_not_eligible=deferred)


def _bin_index(p: Decimal, bins: int) -> int:
    """FD-11: min(int(p*bins), bins-1) computed in Decimal."""
    return min(int(p * bins), bins - 1)


def brier(samples) -> Decimal:
    """Mean of exact per-sample (p-o)^2 (each at BRIER_QUANTUM), quantized REPORT_QUANTUM."""
    items = list(samples)
    if not items:
        raise ValueError("brier of zero samples is undefined")
    with localcontext(_DECIMAL_CTX):
        total = sum(_quantize_checked((p - outcome) ** 2, BRIER_QUANTUM, field="brier_i")
                    for p, outcome in items)
        return (total / Decimal(len(items))).quantize(REPORT_QUANTUM, ROUND_HALF_EVEN)


def murphy_decomposition(samples, *, bins: int = 10) -> dict:
    """Frozen keys {brier, reliability, resolution, uncertainty, base_rate, n}
    (rev3 minor-6). REL/RES/UNC computed in exact Decimal from UNQUANTIZED bin
    means; each OUTPUT quantized REPORT_QUANTUM. The 3-term identity
    BS = REL - RES + UNC holds EXACTLY only when every occupied bin's forecasts are
    constant; for heterogeneous bins the within-bin variance/covariance residual is
    O(1e-3) — do NOT assert the identity on arbitrary samples (rev2 MATH-Q1).
    """
    items = list(samples)
    n = len(items)
    if n == 0:
        raise ValueError("murphy_decomposition of zero samples is undefined")
    by_bin: Dict[int, List[Tuple[Decimal, int]]] = {}
    for p, outcome in items:
        by_bin.setdefault(_bin_index(p, bins), []).append((p, outcome))

    with localcontext(_DECIMAL_CTX):
        n_dec = Decimal(n)
        o_bar = Decimal(sum(outcome for _, outcome in items)) / n_dec
        rel = Decimal("0")
        res = Decimal("0")
        for members in by_bin.values():
            n_k = Decimal(len(members))
            p_bar_k = sum(p for p, _ in members) / n_k
            o_bar_k = Decimal(sum(outcome for _, outcome in members)) / n_k
            rel += n_k * (p_bar_k - o_bar_k) ** 2
            res += n_k * (o_bar_k - o_bar) ** 2
        unc = o_bar * (Decimal("1") - o_bar)

        def _q(value: Decimal) -> Decimal:
            return value.quantize(REPORT_QUANTUM, ROUND_HALF_EVEN)

        return {
            "brier": brier(items),
            "reliability": _q(rel / n_dec),
            "resolution": _q(res / n_dec),
            "uncertainty": _q(unc),
            "base_rate": _q(o_bar),
            "n": n,
        }


def reliability_bins(samples, *, bins: int = 10) -> list:
    """FD-11 bins: left-closed/right-open, last bin closed. Empty bin ->
    count=0, mean_forecast_p=None, observed_freq=None; 0 < count < 30 -> thin."""
    items = list(samples)
    by_bin: Dict[int, List[Tuple[Decimal, int]]] = {}
    for p, outcome in items:
        by_bin.setdefault(_bin_index(p, bins), []).append((p, outcome))

    out = []
    with localcontext(_DECIMAL_CTX):
        step = Decimal("1") / Decimal(bins)
    for i in range(bins):
        members = by_bin.get(i, [])
        count = len(members)
        if count:
            with localcontext(_DECIMAL_CTX):
                mean_p = (sum(p for p, _ in members) / Decimal(count)).quantize(
                    REPORT_QUANTUM, ROUND_HALF_EVEN)
                observed = (Decimal(sum(o for _, o in members)) / Decimal(count)).quantize(
                    REPORT_QUANTUM, ROUND_HALF_EVEN)
        else:
            mean_p = None
            observed = None
        out.append({
            "lo": (step * i).quantize(REPORT_QUANTUM, ROUND_HALF_EVEN),
            "hi": (step * (i + 1)).quantize(REPORT_QUANTUM, ROUND_HALF_EVEN),
            "count": count,
            "mean_forecast_p": mean_p,
            "observed_freq": observed,
            "thin": 0 < count < 30,
        })
    return out


def brier_skill_score(bs_model: Decimal, bs_ref: Decimal) -> Union[Decimal, str]:
    """BSS = 1 - BS_model/BS_ref (Decimal division, REPORT_QUANTUM). Returns the
    string "unavailable:zero_reference_brier" when BS_ref == 0 — never NaN/Inf."""
    if bs_ref == 0:
        return "unavailable:zero_reference_brier"
    with localcontext(_DECIMAL_CTX):
        return (Decimal("1") - bs_model / bs_ref).quantize(REPORT_QUANTUM, ROUND_HALF_EVEN)
