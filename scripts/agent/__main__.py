"""M5 §O.1 — the CLI: ``observe | synthetic | paper`` (FD-M5-4).

Import discipline (§3): this module imports ``agent.orchestrator``,
``agent.config`` and ``agent.secrets_runtime`` ONLY (plus stdlib). Everything
heavier — feed construction, calendar provider, synthetic strategy — is reached
THROUGH ``agent.orchestrator``'s exported seams.

Mode is DERIVED, never a flag: each subcommand asserts its derived mode and
exits non-zero on a mismatch; no flag can flip a gate (§M.2 step 9).

- ``observe`` — committed config; constructs NO broker object at all
  (FD-M5-4); symbol set = events-file symbols ∩ ``--symbols`` ∩
  ``agent_rules.universe.symbols`` (when non-empty; the committed ``[]`` is
  file-driven). Runs ingest → bars → features → market-state → M3 probe →
  resolver; ``--report-out`` writes the calibration report.
- ``synthetic`` — builds the IN-MEMORY permissive fixture config (FD-M5-3 —
  gates identity-True, nonzero caps, one-symbol universe WITH sector/beta;
  never written under ``config/``), a FakeBroker (constructed by the
  orchestrator's step-9 mode select) and a ``ScriptedSyntheticStrategy``;
  journals to an isolated dir; REFUSES (``ExecError``) if the gates path would
  consult the committed config or a non-fake broker was constructed.
- ``paper`` — committed config + optional tighten-only ``--overlay`` + the
  §O.2 run-gates view + ``.secrets/alpaca_paper.json``. Credentials are the
  ONLY broker-construction key (M5C-S3); gates govern OPENS only. Until the
  live quote feed exists (M1-2b) this performs the startup sequence (lock,
  config, gates provenance row, risk/exec rehydrate, broker-touching order
  recovery) and exits cleanly — there is no live tick source to drive yet
  (documented resolution; the §M.3 loop is feed-driven).
"""
import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent import config as agent_config
from agent import orchestrator as orch_mod
from agent.orchestrator import Orchestrator, mint_run_id

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CALENDAR = (_REPO_ROOT / "tests" / "fixtures" / "calendar"
                     / "nyse_margin_window_v1.json")
_SECRETS = _REPO_ROOT / ".secrets"

_DEFAULT_SYNTHETIC_SCRIPT = [
    {"on_bar": 2, "action": "open", "symbol": "", "qty": "10", "limit": None},
    {"on_bar": 4, "action": "close", "symbol": "", "qty": "10", "limit": None},
]


def _committed_config() -> dict:
    return {
        "agent_rules": agent_config.load(_REPO_ROOT / "config" / "agent_rules.json"),
        "risk_rules": agent_config.load(_REPO_ROOT / "config" / "risk_rules.json"),
    }


def _synthetic_config(symbols) -> dict:
    """FD-M5-3: the in-memory permissive fixture config — gates identity-True,
    NONZERO caps, the feed's symbols as the universe WITH sector/beta. The
    committed files contribute ONLY the (gate-irrelevant) signal/execution
    shape; the GATES path reads exclusively from this in-memory dict."""
    committed = _committed_config()
    agent_rules = json.loads(json.dumps(committed["agent_rules"]))
    agent_rules["enabled"] = True
    agent_rules["paper_trading"] = {"enabled": True}
    agent_rules["universe"]["symbols"] = list(symbols)
    return {
        "agent_rules": agent_rules,
        "risk_rules": {
            "live_trading": {"enabled": False, "max_live_position_usd": 0},
            "caps": {
                "max_position_usd": 10000,
                "max_gross_exposure_usd": 50000,
                "max_net_exposure_usd": 50000,
                "max_daily_loss_usd": 1000,
                "max_drawdown_usd": 2000,
                "max_sector_exposure_usd": 20000,
                "max_abs_beta_notional_usd": 30000,
            },
            "risk": {
                "short_selling": {"enabled": False},
                "universe": {symbol: {"sector": "fixture", "beta": "1.0"}
                             for symbol in symbols},
            },
        },
    }


def _new_run_id() -> str:
    return mint_run_id(host=platform.node() or "unknown", pid=os.getpid(),
                       now_utc=datetime.now(timezone.utc))


def _build_feed(events_path, symbols, cadence_ms):
    return orch_mod.build_replay_feed(events_path, symbols=symbols,
                                      refresh_cadence_ms=cadence_ms)


def _cadence_ms(config: dict) -> int:
    raw = config["agent_rules"].get("signal", {}).get("refresh_cadence_ms", "1000")
    return int(raw)


def _cmd_observe(args) -> int:
    config = _committed_config()
    symbols = None
    if args.symbols:
        symbols = [token for token in args.symbols.split(",") if token]
    universe = config["agent_rules"].get("universe", {}).get("symbols", [])
    if universe:
        symbols = list(set(universe) & set(symbols)) if symbols else list(universe)
    feed = _build_feed(args.events, symbols, _cadence_ms(config))
    orch = Orchestrator(
        journal_dir=args.journal_dir,
        run_id=_new_run_id(),
        clock=feed.clock(),
        quote_view=feed.quote_view(),
        bar_reader=feed.bar_reader(),
        calendar_provider=orch_mod.schedule_provider_from_fixture(
            args.calendar_fixture),
        config=config,
    )
    try:
        if orch.mode != "observe":
            print(f"mode mismatch: derived {orch.mode!r}, expected 'observe'",
                  file=sys.stderr)
            return 2
        if orch.broker is not None:
            print("observe constructed a broker (FD-M5-4 violation)",
                  file=sys.stderr)
            return 2
        orch.run_with_feed(feed, max_ticks=args.ticks)
        if args.report_out:
            orch.write_report(
                args.report_out,
                generated_ts_utc=datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f") + "Z")
    finally:
        orch.close()
    return 0


def _cmd_synthetic(args) -> int:
    ExecError = orch_mod.ExecError

    instrument_ids = orch_mod.instrument_map_from_events(args.events)
    symbols = sorted(instrument_ids)
    if not symbols:
        print("no symbols in events file", file=sys.stderr)
        return 2
    config = _synthetic_config(symbols)
    if args.script:
        script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    else:
        script = [dict(row, symbol=symbols[0])
                  for row in _DEFAULT_SYNTHETIC_SCRIPT]
    status_provider = None
    if args.status_script:
        windows = json.loads(Path(args.status_script).read_text(encoding="utf-8"))
        status_provider = orch_mod.status_provider_from_windows(windows)
    feed = _build_feed(args.events, symbols, _cadence_ms(config))
    strategy = orch_mod.ScriptedSyntheticStrategy(script)
    orch = Orchestrator(
        journal_dir=args.journal_dir,
        run_id=_new_run_id(),
        clock=feed.clock(),
        quote_view=feed.quote_view(),
        bar_reader=feed.bar_reader(),
        calendar_provider=orch_mod.schedule_provider_from_fixture(
            args.calendar_fixture),
        config=config,
        strategy=strategy,
        status_provider=status_provider,
        instrument_ids=instrument_ids,
        fill_policy="partial_then_full",
    )
    try:
        if orch.mode != "synthetic":
            raise ExecError(f"mode mismatch: derived {orch.mode!r}, "
                            "expected 'synthetic'")
        if orch.config_source != "in_memory":
            raise ExecError("synthetic gates path consulted the committed "
                            "config (FD-M5-3 violation)")
        if getattr(orch.broker, "kind", None) != "fake":
            raise ExecError("synthetic constructed a non-fake broker "
                            "(FD-M5-8 violation)")
        orch.run_with_feed(feed)
    finally:
        orch.close()
    return 0


def _cmd_paper(args) -> int:
    config = _committed_config()
    overlay = None
    if args.overlay:
        overlay = agent_config.load(args.overlay)
    orch = Orchestrator(
        journal_dir=args.journal_dir,
        run_id=_new_run_id(),
        clock=orch_mod._ZeroClock(),
        quote_view=orch_mod._LatestQuoteView(),
        bar_reader=None,
        calendar_provider=orch_mod.schedule_provider_from_fixture(
            args.calendar_fixture),
        config=config,
        overlay=overlay,
        credentials_path=_SECRETS / "alpaca_paper.json",
        run_gates_path=_SECRETS / "run_gates.json",
    )
    try:
        if orch.mode not in ("paper", "observe"):
            print(f"mode mismatch: derived {orch.mode!r}, expected 'paper' "
                  "(or its documented credentials-missing degrade)",
                  file=sys.stderr)
            return 2
        # No live tick source exists until M1-2b (module docstring): startup,
        # gates provenance row and order recovery have run; exit cleanly.
    finally:
        orch.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser("observe", help="file-driven observe run (FD-M5-4)")
    observe.add_argument("--events", required=True)
    observe.add_argument("--symbols", default=None)
    observe.add_argument("--journal-dir", dest="journal_dir", default="journal")
    observe.add_argument("--calendar-fixture", dest="calendar_fixture",
                         default=str(_DEFAULT_CALENDAR))
    observe.add_argument("--report-out", dest="report_out", default=None)
    observe.add_argument("--ticks", type=int, default=None)
    observe.set_defaults(func=_cmd_observe)

    synthetic = sub.add_parser("synthetic",
                               help="offline synthetic E2E (FakeBroker)")
    synthetic.add_argument("--events", required=True)
    synthetic.add_argument("--journal-dir", dest="journal_dir",
                           default="journal/synthetic")
    synthetic.add_argument("--script", default=None)
    synthetic.add_argument("--calendar-fixture", dest="calendar_fixture",
                           default=str(_DEFAULT_CALENDAR))
    synthetic.add_argument("--status-script", dest="status_script", default=None)
    synthetic.set_defaults(func=_cmd_synthetic)

    paper = sub.add_parser("paper", help="paper startup/recovery (gates §O.2)")
    paper.add_argument("--overlay", default=None)
    paper.add_argument("--journal-dir", dest="journal_dir", default="journal")
    paper.add_argument("--calendar-fixture", dest="calendar_fixture",
                       default=str(_DEFAULT_CALENDAR))
    paper.set_defaults(func=_cmd_paper)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
