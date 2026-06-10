"""M4 §E — IntradayMarginModel (Rule 4210(d)(2), Notice 26-10 — §0.2 V1-V10).

COMPUTES deficit detection, the outstanding ledger, business-day windows, freeze
state, minor classification. RECONCILES (never derives) equity / maintenance_margin /
buying_power (FD-M4-2): the only arithmetic on broker numbers is subtraction / max /
comparison. NO margin percentage, NO day-trade count exists in this module
(source-scan test). Stateful per account; rehydratable from journal/risk.jsonl
(pure fold, §K).

Frozen intra-close order (RM-9/LD-R6): (1) store IML_eod(E) + satisfaction scan
(first qualifying E wins; an unbaselined record — iml_eod_d None — is UNSATISFIABLE
and skipped, with ONE margin_deficit_unbaselined marker row); (2) bd15 expiry;
(3) the freeze trigger as a CATCH-UP predicate over every record with
`satisfaction_deadline_et <= E` still unsatisfied — a missed bd5 close can never
skip the 90-day freeze, and `effective_from_et` stays anchored to the DEADLINE date.
"""
from dataclasses import dataclass
from datetime import date as _date, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Optional, Tuple

from agent.market_calendar import ScheduleProvider, UnknownSessionDate
from agent.risk.account_state import BrokerAccountRead
from agent.risk.reasons import RiskError
from agent.risk.risk_ledger import rehydrate_risk_state
from agent.serializer import BrokerUSD, row_hash

# regulatory CODE CONSTANTS (FD-M4-6/14; never config):
SATISFACTION_DEADLINE_BD = 5          # V8
OUTSTANDING_WINDOW_BD = 15            # V7
FREEZE_CALENDAR_DAYS = 90             # V8
MINOR_DEFICIT_USD = Decimal("1000")   # V9
MINOR_DEFICIT_EQUITY_PCT = Decimal("0.05")  # V9
_BUSINESS_DAY_SCAN_LIMIT = 4          # max calendar days scanned per business day requested

_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


class ScanLimitExceeded(RiskError):
    """add_business_days exceeded its calendar-date scan bound (fail-loud)."""


def add_business_days(start_date_et: str, n: int, *, calendar: ScheduleProvider) -> str:
    """The (n)th XNYS trading session strictly after start_date_et (FD-M4-8): forward
    calendar-date scan consulting calendar.is_trading_day per date. Scan bound =
    n * _BUSINESS_DAY_SCAN_LIMIT + 10 days; exceeding it raises ScanLimitExceeded and
    UnknownSessionDate (fixture coverage exhausted) propagates — windows are computed
    EAGERLY at detection so coverage gaps surface immediately (safety-F11)."""
    if n < 1:
        raise RiskError(f"add_business_days requires n >= 1, got {n}")
    bound = n * _BUSINESS_DAY_SCAN_LIMIT + 10
    current = _date.fromisoformat(start_date_et)
    count = 0
    scanned = 0
    while count < n:
        current += timedelta(days=1)
        scanned += 1
        if scanned > bound:
            raise ScanLimitExceeded(
                f"scanned {scanned} calendar days from {start_date_et} without "
                f"finding {n} trading sessions (bound {bound})")
        if calendar.is_trading_day(current.isoformat()):
            count += 1
    return current.isoformat()


def classify_iml_reducing(side: str, qty: Decimal, held_qty: Decimal) -> bool:
    """V2, validated against the HELD position (never a self-asserted flag — the
    mint_reduce_only_token discipline). Total function:
      buy,  held >= 0                  -> True   (purchase, not covering)
      buy,  held < 0, qty <= |held|    -> False  (cover)
      buy,  held < 0, qty >  |held|    -> True   (flip remainder increases exposure)
      sell, held > 0, qty <= held      -> False  (close)
      sell, otherwise                  -> True   (short establish/increase)
    Out-of-vocab side -> RiskError."""
    if side == "buy":
        if held_qty >= 0:
            return True
        return qty > abs(held_qty)
    if side == "sell":
        if held_qty > 0 and qty <= held_qty:
            return False
        return True
    raise RiskError(f"out-of-vocabulary side: {side!r}")


def minor_deficit(amount: Decimal, equity) -> bool:
    """FD-M4-14: minor iff amount <= min(equity × 0.05, 1000) — boundary-equal IS
    minor ("do not exceed", V9). Equity unknown/invalid -> NOT minor (fail-closed:
    minor is the permissive direction)."""
    if (equity is None or isinstance(equity, bool) or isinstance(equity, float)
            or not isinstance(equity, Decimal) or not equity.is_finite()):
        return False
    with localcontext(_DECIMAL_CTX):
        threshold = min(equity * MINOR_DEFICIT_EQUITY_PCT, MINOR_DEFICIT_USD)
        return amount <= threshold


@dataclass(frozen=True)
class MarginObservation:
    """One reconciled point-in-time margin read, built from a BrokerAccountRead via
    observation_from_read(read, *, session_date_et, after_iml_reducing, eod=False)."""

    session_date_et: str
    ts_read_utc: str
    equity: BrokerUSD
    maintenance_margin: BrokerUSD
    after_iml_reducing: bool      # True iff this read follows an IML-reducing transaction
    eod: bool                     # True for the close_of_day observation
    account_snapshot_id: str

    @property
    def iml(self) -> Decimal:
        """equity - maintenance_margin (exact, V2)."""
        with localcontext(_DECIMAL_CTX):
            return Decimal(self.equity) - Decimal(self.maintenance_margin)

    @property
    def deficiency(self) -> Decimal:
        """max(0, maintenance_margin - equity) (exact, V3)."""
        with localcontext(_DECIMAL_CTX):
            return max(Decimal("0"),
                       Decimal(self.maintenance_margin) - Decimal(self.equity))


def observation_from_read(read: BrokerAccountRead, *, session_date_et: str,
                          after_iml_reducing: bool, eod: bool = False
                          ) -> MarginObservation:
    return MarginObservation(
        session_date_et=session_date_et,
        ts_read_utc=read.ts_read_utc,
        equity=read.equity,
        maintenance_margin=read.maintenance_margin,
        after_iml_reducing=bool(after_iml_reducing),
        eod=bool(eod),
        account_snapshot_id=read.account_snapshot_id)


@dataclass(frozen=True)
class DeficitRecord:
    deficit_id: str               # "imd-" + row_hash({"session_date_et": D}) — FD-M4-13
    session_date_et: str          # deficit date D (ONE record per date)
    amount: Decimal               # > 0; max-merged monotonically over the day (V3/V5)
    minor: bool                   # FD-M4-14; LATCHED one-way (safety-F3)
    equity_at_detection: BrokerUSD
    iml_eod_d: Optional[Decimal]  # IML at EOD of D (satisfaction baseline, V6/FD-M4-12);
                                  #   None => UNSATISFIABLE, skipped by the scan (M4C-2)
    satisfaction_deadline_et: Optional[str]  # add_business_days(D, 5); None ONLY while
    expires_after_et: Optional[str]          #   margin_window_unresolved (safety-F11)
    satisfied_on_et: Optional[str]


@dataclass(frozen=True)
class FreezeState:
    active: bool
    trigger_deficit_id: Optional[str]
    effective_from_et: Optional[str]  # first business day after the triggering bd5 close
    expires_on_et: Optional[str]      # effective_from + 90 calendar days (EXCLUSIVE end)

    def active_on(self, session_date_et: str) -> bool:
        """FD-M4-11: effective_from_et <= session_date_et < expires_on_et (ISO-date
        string compare is safe)."""
        if not self.active or self.effective_from_et is None or self.expires_on_et is None:
            return False
        return self.effective_from_et <= session_date_et < self.expires_on_et


@dataclass(frozen=True)
class MarginRead:
    """The injected margin input to can_open (LD5). Snapshot of model state."""

    outstanding_nonminor: Tuple[DeficitRecord, ...]
    freeze: FreezeState
    asof_session_date_et: str


def deficit_id_for(session_date_et: str) -> str:
    """FD-M4-13: run-independent identity — deficits are account-level facts."""
    return "imd-" + row_hash({"session_date_et": session_date_et})


class IntradayMarginModel:
    def __init__(self, *, calendar: ScheduleProvider, ledger=None) -> None:
        self._calendar = calendar
        self._ledger = ledger
        self._records: dict = {}    # session_date_et -> mutable record dict
        self._iml_eod: dict = {}    # session_date_et -> Decimal IML at that close
        self._freeze: Optional[dict] = None

    # --- observation / detection -------------------------------------------------

    def observe(self, obs: MarginObservation) -> Optional[DeficitRecord]:
        if not isinstance(obs, MarginObservation):
            raise RiskError(f"observe takes a MarginObservation, got {type(obs).__name__}")
        if obs.eod:
            raise RiskError("eod observations route through close_of_day (wiring bug)")
        self._journal_observation(obs)
        if not obs.after_iml_reducing:
            return None  # deficiency is evaluated ONLY following an IML-reducing txn (V3)
        if obs.deficiency <= 0:
            return None
        if obs.session_date_et in self._iml_eod:
            raise RiskError(
                f"deficit detected after its own EOD for {obs.session_date_et} "
                "(impossible by construction)")
        return self._detect(obs)

    def _journal_observation(self, obs: MarginObservation) -> None:
        if self._ledger is not None:
            self._ledger.record_iml_observation(
                session_date_et=obs.session_date_et,
                ts_market_utc=obs.ts_read_utc,
                equity=obs.equity,
                maintenance_margin=obs.maintenance_margin,
                iml=obs.iml,
                deficiency=obs.deficiency,
                after_iml_reducing=obs.after_iml_reducing,
                eod=obs.eod,
                account_snapshot_id=obs.account_snapshot_id)

    def _detect(self, obs: MarginObservation) -> DeficitRecord:
        deficiency = obs.deficiency
        session_date = obs.session_date_et
        rec = self._records.get(session_date)
        if rec is None:
            rec = {
                "deficit_id": deficit_id_for(session_date),
                "session_date_et": session_date,
                "amount": deficiency,
                "minor": minor_deficit(deficiency, obs.equity),
                "equity_at_detection": obs.equity,
                "iml_eod_d": None,
                "satisfaction_deadline_et": None,
                "expires_after_et": None,
                "satisfied_on_et": None,
                "unbaselined_noted": False,
                "window_unresolved_noted": False,
                "expired_noted": False,
                "freeze_triggered": False,
                "deadline_missed": False,
            }
            self._records[session_date] = rec
            cause = "opened"
        elif deficiency > rec["amount"]:
            rec["amount"] = deficiency
            # safety-F3: minor is LATCHED one-way — once non-minor, never back.
            rec["minor"] = rec["minor"] and minor_deficit(deficiency, obs.equity)
            rec["equity_at_detection"] = obs.equity
            cause = "increased"
        else:
            # no increase -> no max-merge event (V5) — but a null-window record
            # SELF-HEALS on every observation of its date (harden round 1, M4-R1:
            # without this, windows resolved only on a strictly LARGER deficiency
            # and the bd5 freeze could be skipped forever).
            self._heal_windows(rec)
            return self._materialize(rec)

        # bd5/bd15 windows, computed eagerly (safety-F11: a failure must still leave
        # the detection row journaled, then a margin_window_unresolved marker, then
        # the exception propagates fail-loud; the record stays outstanding-NON-MINOR
        # with null windows until a later successful recomputation resolves them).
        # The two windows resolve INDEPENDENTLY (harden round 1, M4-R1): a resolvable
        # bd5 deadline must never be discarded because bd15 runs past calendar
        # coverage — the freeze trigger needs only the deadline.
        window_error: Optional[Exception] = None
        if rec["satisfaction_deadline_et"] is None or rec["expires_after_et"] is None:
            deadline, expires, window_error = self._compute_windows(session_date)
            if rec["satisfaction_deadline_et"] is None and deadline is not None:
                rec["satisfaction_deadline_et"] = deadline
            if rec["expires_after_et"] is None and expires is not None:
                rec["expires_after_et"] = expires

        if self._ledger is not None:
            self._ledger.record_margin_deficit_detected(
                deficit_id=rec["deficit_id"],
                cause=cause,
                session_date_et=session_date,
                amount=rec["amount"],
                minor=rec["minor"],
                equity_at_detection=rec["equity_at_detection"],
                satisfaction_deadline_et=rec["satisfaction_deadline_et"],
                expires_after_et=rec["expires_after_et"])

        if window_error is not None:
            if not rec["window_unresolved_noted"]:
                rec["window_unresolved_noted"] = True
                if self._ledger is not None:
                    self._ledger.record_margin_window_unresolved(
                        deficit_id=rec["deficit_id"],
                        session_date_et=session_date,
                        error=("scan_limit_exceeded"
                               if isinstance(window_error, ScanLimitExceeded)
                               else "unknown_session_date"))
            raise window_error
        return self._materialize(rec)

    def _compute_windows(self, session_date: str):
        """(deadline|None, expires|None, first_error|None) — each window in its OWN
        try so a bd15 coverage failure never discards a resolvable bd5 deadline
        (harden round 1, M4-R1)."""
        deadline = expires = None
        first_error: Optional[Exception] = None
        try:
            deadline = add_business_days(session_date, SATISFACTION_DEADLINE_BD,
                                         calendar=self._calendar)
        except (UnknownSessionDate, ScanLimitExceeded) as exc:
            first_error = exc
        try:
            expires = add_business_days(session_date, OUTSTANDING_WINDOW_BD,
                                        calendar=self._calendar)
        except (UnknownSessionDate, ScanLimitExceeded) as exc:
            if first_error is None:
                first_error = exc
        return deadline, expires, first_error

    def _heal_windows(self, rec: dict) -> bool:
        """Re-attempt window resolution for a null-window record (harden round 1,
        M4-R1). Any NEWLY-resolved window is set AND a `window_resolved`
        detected-row re-emission is journaled so the §K.4 field-merge (and hence
        rehydrate) picks it up. Failure is silent — the record stays
        outstanding-NON-MINOR fail-closed (the marker was journaled once at
        detection); the next observation/close retries."""
        if (rec["satisfaction_deadline_et"] is not None
                and rec["expires_after_et"] is not None):
            return True
        deadline, expires, _error = self._compute_windows(rec["session_date_et"])
        resolved_something = False
        if rec["satisfaction_deadline_et"] is None and deadline is not None:
            rec["satisfaction_deadline_et"] = deadline
            resolved_something = True
        if rec["expires_after_et"] is None and expires is not None:
            rec["expires_after_et"] = expires
            resolved_something = True
        if resolved_something and self._ledger is not None:
            self._ledger.record_margin_deficit_detected(
                deficit_id=rec["deficit_id"],
                cause="window_resolved",
                session_date_et=rec["session_date_et"],
                amount=rec["amount"],
                minor=rec["minor"],
                equity_at_detection=rec["equity_at_detection"],
                satisfaction_deadline_et=rec["satisfaction_deadline_et"],
                expires_after_et=rec["expires_after_et"])
        return (rec["satisfaction_deadline_et"] is not None
                and rec["expires_after_et"] is not None)

    # --- close of day ----------------------------------------------------------

    def close_of_day(self, session_date_et: str, eod: MarginObservation) -> None:
        if not isinstance(eod, MarginObservation) or eod.eod is not True:
            raise RiskError("close_of_day requires an eod=True MarginObservation")
        if eod.session_date_et != session_date_et:
            raise RiskError(
                f"close_of_day date mismatch: {session_date_et!r} vs observation "
                f"{eod.session_date_et!r}")
        if session_date_et in self._iml_eod:
            raise RiskError(f"close_of_day already ran for {session_date_et}")
        self._journal_observation(eod)
        if eod.after_iml_reducing and eod.deficiency > 0:
            self._detect(eod)   # EOD single-calculation mode (V4); never lowers a max

        close_date = session_date_et
        iml_e = eod.iml
        self._iml_eod[close_date] = iml_e
        own = self._records.get(close_date)
        if own is not None:
            own["iml_eod_d"] = iml_e   # the satisfaction baseline for day D (V6)

        # (0) window self-heal (harden round 1, M4-R1): every unsatisfied record
        # with null windows retries resolution at every close, so the bd15 expiry
        # and the step-3 catch-up trigger can fire from the resolved deadlines.
        for day in sorted(self._records):
            rec = self._records[day]
            if rec["satisfied_on_et"] is None:
                self._heal_windows(rec)

        # (1) satisfaction scan — strictly-subsequent days only ("end of such day to
        # the end of a SUBSEQUENT day"); first qualifying E wins.
        for day in sorted(self._records):
            rec = self._records[day]
            if day >= close_date or rec["satisfied_on_et"] is not None:
                continue
            expires = rec["expires_after_et"]
            if expires is not None and close_date > expires:
                continue  # past bd15 — no longer outstanding
            if rec["iml_eod_d"] is None:
                # M4C-2/RM-6: UNSATISFIABLE — skipped (DATA, never a raise), marker once.
                if not rec["unbaselined_noted"]:
                    rec["unbaselined_noted"] = True
                    if self._ledger is not None:
                        self._ledger.record_margin_deficit_unbaselined(
                            deficit_id=rec["deficit_id"],
                            session_date_et=day,
                            noted_at_close_et=close_date)
                continue
            with localcontext(_DECIMAL_CTX):
                delta = iml_e - rec["iml_eod_d"]
            if delta >= rec["amount"]:
                rec["satisfied_on_et"] = close_date
                if self._ledger is not None:
                    self._ledger.record_margin_deficit_satisfied(
                        deficit_id=rec["deficit_id"],
                        session_date_et=day,
                        satisfied_on_et=close_date,
                        iml_eod_d=rec["iml_eod_d"],
                        iml_eod_e=iml_e)

        # (2) bd15 expiry ("immediately after the close" of expires_after_et).
        for day in sorted(self._records):
            rec = self._records[day]
            if (rec["satisfied_on_et"] is None and not rec["expired_noted"]
                    and rec["expires_after_et"] is not None
                    and close_date >= rec["expires_after_et"]):
                rec["expired_noted"] = True
                if self._ledger is not None:
                    self._ledger.record_margin_deficit_expired(
                        deficit_id=rec["deficit_id"],
                        session_date_et=day,
                        expires_after_et=rec["expires_after_et"])

        # (3) freeze trigger — CATCH-UP predicate (LD-R6/safety-F2): EVERY record,
        # minor included (LD-R2), with deadline <= E still unsatisfied.
        for day in sorted(self._records):
            rec = self._records[day]
            if rec["satisfied_on_et"] is not None or rec["freeze_triggered"]:
                continue
            deadline = rec["satisfaction_deadline_et"]
            if deadline is None or deadline > close_date:
                continue
            # COMPUTE-THEN-LATCH (harden round 1, M4-EDGE-1): the window arithmetic
            # is fallible; latching the flags before it succeeds would permanently
            # skip the freeze via the `freeze_triggered` guard above. On a raise the
            # flags stay unlatched and the next close retries (fail-loud).
            effective_from = add_business_days(deadline, 1, calendar=self._calendar)
            expires_on = (_date.fromisoformat(effective_from)
                          + timedelta(days=FREEZE_CALENDAR_DAYS)).isoformat()
            # MERGE-WHEN-IN-FORCE (harden round 1, M4-R1-F1): any trigger landing
            # while the existing freeze has not yet expired relative to E max-merges
            # (tighten-only; active_on can never flip True->False inside the window).
            # Only a freeze already expired at E is replaced by a fresh one.
            if self._freeze is None or close_date >= self._freeze["expires_on_et"]:
                # fresh freeze (none yet, or the previous one expired before E)
                self._freeze = {"trigger_deficit_id": rec["deficit_id"],
                                "effective_from_et": effective_from,
                                "expires_on_et": expires_on,
                                "end_emitted": False}
                if self._ledger is not None:
                    self._ledger.record_margin_freeze_start(
                        trigger_deficit_id=rec["deficit_id"],
                        effective_from_et=effective_from,
                        expires_on_et=expires_on)
            else:
                # F13 max-merge: ORIGINAL trigger_deficit_id/effective_from_et KEPT;
                # only expires_on_et extends; a SECOND start row carries the merged end.
                merged_end = max(self._freeze["expires_on_et"], expires_on)
                self._freeze["expires_on_et"] = merged_end
                if self._ledger is not None:
                    self._ledger.record_margin_freeze_start(
                        trigger_deficit_id=self._freeze["trigger_deficit_id"],
                        effective_from_et=self._freeze["effective_from_et"],
                        expires_on_et=merged_end)
            rec["freeze_triggered"] = True
            rec["deadline_missed"] = True

        # margin_freeze_end: observability only (active_on stays the authority, F13).
        if (self._freeze is not None and not self._freeze["end_emitted"]
                and close_date >= self._freeze["expires_on_et"]):
            self._freeze["end_emitted"] = True
            if self._ledger is not None:
                self._ledger.record_margin_freeze_end(
                    trigger_deficit_id=self._freeze["trigger_deficit_id"],
                    expires_on_et=self._freeze["expires_on_et"])

    # --- reads -----------------------------------------------------------------

    def _materialize(self, rec: dict) -> DeficitRecord:
        return DeficitRecord(
            deficit_id=rec["deficit_id"],
            session_date_et=rec["session_date_et"],
            amount=rec["amount"],
            minor=rec["minor"],
            equity_at_detection=rec["equity_at_detection"],
            iml_eod_d=rec["iml_eod_d"],
            satisfaction_deadline_et=rec["satisfaction_deadline_et"],
            expires_after_et=rec["expires_after_et"],
            satisfied_on_et=rec["satisfied_on_et"])

    def _is_outstanding(self, rec: dict, asof_date_et: str) -> bool:
        if rec["satisfied_on_et"] is not None:
            return False
        expires = rec["expires_after_et"]
        # null windows (margin_window_unresolved) => outstanding, fail-closed
        return expires is None or asof_date_et <= expires

    def outstanding(self, asof_date_et: str) -> Tuple[DeficitRecord, ...]:
        return tuple(self._materialize(self._records[day])
                     for day in sorted(self._records)
                     if self._is_outstanding(self._records[day], asof_date_et))

    def read(self, asof_session_date_et: str) -> MarginRead:
        nonminor = tuple(
            self._materialize(self._records[day])
            for day in sorted(self._records)
            if self._is_outstanding(self._records[day], asof_session_date_et)
            and (not self._records[day]["minor"]
                 # safety-F11: window-unresolved records are treated outstanding-
                 # NON-MINOR regardless of their computed minor flag
                 or self._records[day]["satisfaction_deadline_et"] is None
                 or self._records[day]["expires_after_et"] is None))
        return MarginRead(outstanding_nonminor=nonminor,
                          freeze=self.freeze_state(),
                          asof_session_date_et=asof_session_date_et)

    def freeze_state(self) -> FreezeState:
        if self._freeze is None:
            return FreezeState(active=False, trigger_deficit_id=None,
                               effective_from_et=None, expires_on_et=None)
        return FreezeState(active=True,
                           trigger_deficit_id=self._freeze["trigger_deficit_id"],
                           effective_from_et=self._freeze["effective_from_et"],
                           expires_on_et=self._freeze["expires_on_et"])

    def practice_count(self) -> int:
        """Observability ONLY (FD-M4-10); gates nothing. RM-10: count of NON-MINOR
        deficits that reached their bd5 close unsatisfied — derived ON DEMAND from
        the records + the close history (harden round 1, M4-R2: a transient flag
        would reset on rehydrate; this derivation is rehydrate-free because both
        inputs are rebuilt by the §K.4 fold). A record satisfied AT the earliest
        close >= its deadline was satisfied by step 1 BEFORE the step-3 trigger
        scan, so it never reached a close unsatisfied."""
        count = 0
        for rec in self._records.values():
            if rec["minor"]:
                continue
            deadline = rec["satisfaction_deadline_et"]
            if deadline is None:
                continue
            closes_at_or_after = [d for d in self._iml_eod if d >= deadline]
            if not closes_at_or_after:
                continue  # the bd5 close has not happened yet
            earliest = min(closes_at_or_after)
            satisfied_on = rec["satisfied_on_et"]
            if satisfied_on is None or satisfied_on > earliest:
                count += 1
        return count

    # --- rehydrate ---------------------------------------------------------------

    def rehydrate(self, rows) -> None:
        """Re-seed from rehydrate_risk_state(rows)["margin"] (§K.4): per-deficit_id
        FIELD-merged records; the iml_eod join (a deficit date with no eod row =>
        iml_eod_d None); the latest freeze. `freeze_triggered`/`deadline_missed`
        flags are deliberately rehydrate-free (RM-10) — a post-rehydrate catch-up
        re-trigger is an idempotent max-merge (state-identical)."""
        margin = rehydrate_risk_state(rows)["margin"]
        self._iml_eod = {day: Decimal(value)
                         for day, value in margin["iml_eod"].items()}
        self._records = {}
        for deficit_id, fields in margin["deficits"].items():
            day = fields["session_date_et"]
            iml_eod_d = self._iml_eod.get(day)
            self._records[day] = {
                "deficit_id": deficit_id,
                "session_date_et": day,
                "amount": Decimal(fields["amount"]),
                "minor": fields["minor"],
                "equity_at_detection": BrokerUSD(fields["equity_at_detection"]),
                "iml_eod_d": iml_eod_d,
                "satisfaction_deadline_et": fields.get("satisfaction_deadline_et"),
                "expires_after_et": fields.get("expires_after_et"),
                "satisfied_on_et": fields.get("satisfied_on_et"),
                "unbaselined_noted": fields.get("unbaselined_noted", False),
                "window_unresolved_noted": "window_unresolved" in fields,
                "expired_noted": fields.get("expired_noted", False),
                "freeze_triggered": False,
                "deadline_missed": False,
            }
        freeze = margin["freeze"]
        self._freeze = dict(freeze) if freeze is not None else None
