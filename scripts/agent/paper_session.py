"""Paper session day-runner — SOD reconcile → feed-driven decision loop → EOD → report.

The one missing operational wrapper the M5-M7 spine never had: ``agent paper``
only did startup + one SOD pass, and the tick loop was reachable only through
the observe/synthetic replay commands. ``run_paper_session`` composes what
already exists:

    SOD broker reconcile (paper mode)                — M6, broker = truth
      → ``orchestrator.run_with_feed(feed)``          — the frozen 10-step tick
        loop; the session edge inside it already cancels open orders, runs the
        margin close-of-day, and fires the in-loop EOD reconcile when the
        instant leaves RTH
      → ``ensure_eod_reconcile``                      — idempotent fallback for
        a feed that died before the session edge could fire
      → daily paper report                            — journal roll-up
        (``agent.paper_report``), written under the report dir.

Safety posture unchanged: the runner flips NOTHING. Committed config ⇒ the
orchestrator degrades to observe (no broker) and zero orders can be submitted
(S1); paper mode requires the operator-armed ``.secrets/run_gates.json`` +
``.secrets/alpaca_paper.json``; opens additionally require a reviewed passing
artifact (S9) and a paper-eligible strategy candidate. The runner never touches
config, gates, or secrets — it only composes and runs.

The strategy registry maps the per-symbol ``scan(ctx)`` strategies only
(momentum v1/v2). ``relative_strength.long_only_proxy_v1`` is CROSS-SECTIONAL
(``decide(snapshots, ...)``) and needs a predeclared scan-adapter before it can
drive the live loop — refusing it here is honest fail-closed, not an oversight
(the M7d GO path budgets this as provisioning lead time).

CLI (standalone, like calendar_fixture / m7_run_driver):
    python3 -m agent.paper_session --journal-dir journal --replay events.jsonl
    python3 -m agent.paper_session --journal-dir journal --live   # tier-2b gated
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from agent import config as agent_config
from agent.market_calendar import (
    FixtureScheduleProvider,
    MarketCalendar,
    UnknownSessionDate,
)
from agent.paper_report import build_daily_report, render_text, write_report

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRETS = _REPO_ROOT / ".secrets"
_DEFAULT_CALENDAR_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "calendar"
                             / "xnys_sessions_2026H2_v1.json")

# Per-symbol scan(ctx) strategies only — see module docstring.
STRATEGY_REGISTRY = {
    "directional.momentum_v1":
        "agent.strategies.directional_momentum:MomentumV1Strategy",
    "directional.momentum_v2":
        "agent.strategies.directional_momentum:MomentumV2Strategy",
}
_CROSS_SECTIONAL_IDS = frozenset({"relative_strength.long_only_proxy_v1"})


def build_strategy(strategy_id: Optional[str]):
    """Resolve a strategy id to an instance; fail closed on unknown/unadapted ids."""
    if strategy_id is None:
        return None
    if strategy_id in _CROSS_SECTIONAL_IDS:
        raise ValueError(
            f"{strategy_id!r} is a CROSS-SECTIONAL strategy (decide(snapshots,...)) "
            "and has no per-symbol scan adapter yet — it cannot drive the live "
            "session loop until that adapter is predeclared, built and reviewed")
    target = STRATEGY_REGISTRY.get(strategy_id)
    if target is None:
        raise ValueError(
            f"unknown strategy id {strategy_id!r}; known: "
            f"{sorted(STRATEGY_REGISTRY)}")
    module_name, _, class_name = target.partition(":")
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@dataclass(frozen=True)
class SessionResult:
    mode: str
    session_date_et: str
    trading_day: bool
    ticks_run: int
    kill_state: str
    sod_clean: Optional[bool]
    eod_clean: Optional[bool]
    drift_latched: bool
    feed_truncated: bool
    report_path: Optional[Path]
    report: Optional[dict]
    exit_code: int


def run_paper_session(*, orchestrator, feed, journal_dir,
                      session_date_et: str,
                      report_dir=None,
                      utc_now_iso_fn=None) -> SessionResult:
    """Run one full session over an ALREADY-COMPOSED orchestrator + feed.

    The caller owns composition (and therefore testability); the CLI below
    builds the production composition. The runner never constructs brokers,
    never reads secrets, never flips gates.
    """
    now_iso = utc_now_iso_fn or _utc_now_iso
    orch = orchestrator
    sod = None
    if orch.mode == "paper":
        sod = orch.run_reconcile(phase="sod", ts_utc=now_iso(),
                                 now_ms=feed.clock().now_ms())

    orch.run_with_feed(feed)

    eod = orch.ensure_eod_reconcile(session_date_et, ts_utc=now_iso(),
                                    now_ms=feed.clock().now_ms())

    data_quality = (feed.data_quality_counts()
                    if hasattr(feed, "data_quality_counts") else {})
    report = build_daily_report(
        journal_dir, session_date_et=session_date_et,
        run_id=orch.run_id, mode=orch.mode,
        kill_state=orch.risk_kill.state,
        data_quality_counts=data_quality)
    report_path = None
    if report_dir is not None:
        report_path = write_report(
            report, Path(report_dir) / f"{session_date_et}.json")

    kill_state = orch.risk_kill.state
    drift = bool(getattr(orch, "drift_latched", False))
    sod_clean = None if sod is None else bool(sod.clean)
    eod_clean = None if eod is None else bool(eod.clean)
    # A live source that died before stop_at truncated the session: the day is
    # NOT clean evidence (the paper phase needs FULL RTH sessions) — exit 1 so
    # unattended automation investigates instead of counting it.
    feed_truncated = bool(data_quality.get("source_exhausted_early"))
    if kill_state == "halted":
        exit_code = 4
    elif (drift or sod_clean is False or eod_clean is False
          or feed_truncated):
        exit_code = 1
    else:
        exit_code = 0
    return SessionResult(
        mode=orch.mode,
        session_date_et=session_date_et,
        trading_day=True,
        ticks_run=orch.ticks_run,
        kill_state=kill_state,
        sod_clean=sod_clean,
        eod_clean=eod_clean,
        drift_latched=drift,
        feed_truncated=feed_truncated,
        report_path=report_path,
        report=report,
        exit_code=exit_code,
    )


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI. Exit codes (the daily-automation contract, mirrored in the paper
    runbook): 0 clean (incl. non-trading day) · 1 unclean (reconcile drift /
    truncated feed) · 2 lock held or usage · 3 journal corruption · 4
    kill-switch HALTED · 5 calendar coverage expired (regenerate the fixture)."""
    import argparse
    import sys

    from agent import orchestrator as orch_mod

    parser = argparse.ArgumentParser(
        description="Run ONE full paper/observe session: SOD reconcile -> "
                    "feed-driven loop until after the RTH close -> EOD "
                    "reconcile -> daily report. Flips nothing; committed "
                    "config degrades to observe and submits zero orders.")
    parser.add_argument("--journal-dir", required=True)
    parser.add_argument("--report-dir", default=str(_REPO_ROOT / "reports"
                                                    / "paper_sessions"))
    parser.add_argument("--calendar-fixture",
                        default=str(_DEFAULT_CALENDAR_FIXTURE))
    parser.add_argument("--session-date",
                        help="ET session date (default: today per calendar)")
    feed_group = parser.add_mutually_exclusive_group(required=True)
    feed_group.add_argument("--replay", help="recorded events.jsonl (rehearsal)")
    feed_group.add_argument("--live", action="store_true",
                            help="live Databento feed (tier-2b: requires the "
                                 "paid subscription + verification)")
    parser.add_argument("--symbols", default="",
                        help="comma-separated universe override")
    parser.add_argument("--strategy-id", default=None,
                        help=f"one of {sorted(STRATEGY_REGISTRY)} (S9 still "
                             "gates opens on the reviewed artifact)")
    parser.add_argument("--dataset", default="EQUS.MINI")
    parser.add_argument("--schema", default="bbo-1s")
    parser.add_argument("--stop-buffer-minutes", type=int, default=15)
    parser.add_argument("--allow-unverified-live", action="store_true",
                        help="tier-2b verification session ONLY")
    parser.add_argument("--record-events", default=None,
                        help="record the live stream to this events.jsonl")
    args = parser.parse_args(argv)

    fixture = json.loads(Path(args.calendar_fixture).read_text(
        encoding="utf-8"))
    provider = FixtureScheduleProvider(fixture, pin=fixture["pin"])
    calendar = MarketCalendar(provider)
    session_date = args.session_date or calendar.session_date_for(
        _utc_now_iso())
    try:
        schedule = provider.schedule_for(session_date)
    except UnknownSessionDate:
        # Distinct exit code: for a cron/launchd operator this is the fixture
        # EXPIRING (a silent 0 here would end the paper program unnoticed).
        print(f"session date {session_date} outside calendar coverage — "
              "fail-closed, not trading; regenerate the session fixture "
              "(agent.calendar_fixture) and review it", file=sys.stderr)
        return 5
    if not schedule.is_trading_day:
        print(f"{session_date} is not a trading day; nothing to run")
        return 0

    config = {
        "agent_rules": agent_config.load(
            _REPO_ROOT / "config" / "agent_rules.json"),
        "risk_rules": agent_config.load(
            _REPO_ROOT / "config" / "risk_rules.json"),
    }
    symbols = [s for s in args.symbols.split(",") if s]
    if not symbols:
        symbols = list(config["agent_rules"].get("universe", {})
                       .get("symbols", []))
    if not symbols:
        print("no symbols: pass --symbols or set agent_rules.universe.symbols")
        return 2

    if args.replay:
        from agent.marketdata.replay_feed import ReplayQuoteFeed

        feed = ReplayQuoteFeed(args.replay, symbols=symbols)
    else:
        from datetime import timedelta

        from agent.marketdata.live_feed import (
            LiveQuoteFeed,
            databento_live_source,
        )
        from agent.bar_series import _parse_utc
        from recorder.persistence import EventWriter

        stop_at = (_parse_utc(schedule.rth_close_utc)
                   + timedelta(minutes=args.stop_buffer_minutes))
        post_close = _parse_utc(schedule.post_close_utc)
        if stop_at > post_close:
            stop_at = post_close
        writer = None
        if args.record_events:
            writer = EventWriter(args.record_events,
                                 f"live-{session_date}")
        feed = LiveQuoteFeed(
            record_source=databento_live_source(
                dataset=args.dataset, schema=args.schema, symbols=symbols,
                allow_unverified_live=args.allow_unverified_live),
            symbols=symbols, dataset=args.dataset, schema=args.schema,
            stop_at_utc=stop_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            event_writer=writer,
            max_runtime_ms=14 * 3600 * 1000)

    import os
    import platform

    strategy = build_strategy(args.strategy_id)
    try:
        orch = orch_mod.Orchestrator(
            journal_dir=args.journal_dir,
            run_id=orch_mod.mint_run_id(
                host=platform.node() or "unknown", pid=os.getpid(),
                now_utc=datetime.now(timezone.utc)),
            clock=feed.clock(),
            quote_view=feed.quote_view(),
            bar_reader=feed.bar_reader(),
            calendar_provider=provider,
            config=config,
            strategy=strategy,
            credentials_path=_SECRETS / "alpaca_paper.json",
            run_gates_path=_SECRETS / "run_gates.json",
        )
    except orch_mod.RunLockHeld as exc:
        print(f"run lock held: {exc}", file=sys.stderr)
        return 2
    except orch_mod.JournalCorruption as exc:
        print(f"journal corruption: {exc}", file=sys.stderr)
        return 3
    try:
        result = run_paper_session(
            orchestrator=orch, feed=feed, journal_dir=args.journal_dir,
            session_date_et=session_date, report_dir=args.report_dir)
    finally:
        orch.close()
    if result.report is not None:
        print(render_text(result.report), end="")
    print(f"mode={result.mode} ticks={result.ticks_run} "
          f"kill={result.kill_state} exit={result.exit_code}")
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
