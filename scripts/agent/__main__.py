"""M5 §O.1 / M6 §E / M7 — the CLI:
``observe | synthetic | paper | reconcile | m7-backtest |
m7-historical-artifact | m7-historical-diagnostics |
m7-historical-diagnostics-index``.

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
  live quote feed exists (M1-2b) this performs the startup sequence, then the
  M6 SOD reconcile pass in paper mode, and exits with reconcile's 0/1/3 code.
- ``reconcile`` — committed config + optional tighten-only ``--overlay`` +
  `.secrets/` paper credentials. Runs exactly one M6 CLI reconcile pass,
  supports the operator-only ``--rebaseline-cash`` flag, and exits 0 clean,
  1 drift/latch, 2 usage/lock, or 3 could-not-reconcile.
- ``m7-backtest`` — local deterministic fixture artifact builder for M7. Writes
  only after paper-phase criteria pass and always refuses committed
  artifacts/backtests; the historical reviewed-artifact flow is separate.
- ``m7-historical-artifact`` — reviewed historical artifact builder over
  normalized quote JSONL plus a hash-bound input manifest. Production writes
  require ``--allow-reviewed-artifact``.
- ``m7-historical-diagnostics`` — bounded, non-artifact trade/skip diagnostic
  export over the same historical input path. It never writes raw quote rows.
- ``m7-historical-diagnostics-index`` — bounded, non-artifact cross-symbol
  diagnostic index over per-symbol diagnostic exports.
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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_feed(events_path, symbols, cadence_ms):
    return orch_mod.build_replay_feed(events_path, symbols=symbols,
                                      refresh_cadence_ms=cadence_ms)


def _cadence_ms(config: dict) -> int:
    raw = config["agent_rules"].get("signal", {}).get("refresh_cadence_ms", "1000")
    return int(raw)


def _load_overlay(path):
    return agent_config.load(path) if path else None


def _paper_orchestrator(args) -> Orchestrator:
    return Orchestrator(
        journal_dir=args.journal_dir,
        run_id=_new_run_id(),
        clock=orch_mod._ZeroClock(),
        quote_view=orch_mod._LatestQuoteView(),
        bar_reader=None,
        calendar_provider=orch_mod.schedule_provider_from_fixture(
            args.calendar_fixture),
        config=_committed_config(),
        overlay=_load_overlay(args.overlay),
        credentials_path=_SECRETS / "alpaca_paper.json",
        run_gates_path=_SECRETS / "run_gates.json",
    )


def _bool_token(value: bool) -> str:
    return "true" if value else "false"


def _print_reconcile_summary(result, orch) -> None:
    print(
        " ".join((
            f"reconcile_id={result.reconcile_id}",
            f"phase={result.phase}",
            f"session_date_et={result.session_date_et}",
            f"completed={_bool_token(result.completed)}",
            f"clean={_bool_token(result.clean)}",
            f"drift_count={len(result.findings)}",
            f"adjusted_count={len(result.adjustments)}",
            f"note_count={len(result.notes)}",
            f"latched={_bool_token(orch.drift_latched)}",
        ))
    )


def _reconcile_exit_code(result, orch) -> int:
    if not result.completed:
        return 3
    if result.findings or orch.drift_latched:
        return 1
    return 0


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
    try:
        orch = _paper_orchestrator(args)
    except orch_mod.RunLockHeld as exc:
        print(f"run lock held: {exc}", file=sys.stderr)
        return 2
    except orch_mod.JournalCorruption as exc:
        print(f"journal corruption: {exc}", file=sys.stderr)
        return 3
    try:
        if orch.mode not in ("paper", "observe"):
            print(f"mode mismatch: derived {orch.mode!r}, expected 'paper' "
                  "(or its documented credentials-missing degrade)",
                  file=sys.stderr)
            return 2
        if orch.mode == "observe":
            # Credentials-missing degrade preserves the M5 startup-only return.
            return 0
        result = orch.run_reconcile(
            phase="sod", ts_utc=_now_utc_iso(), now_ms=0)
        _print_reconcile_summary(result, orch)
        return _reconcile_exit_code(result, orch)
    finally:
        orch.close()


def _cmd_reconcile(args) -> int:
    try:
        orch = _paper_orchestrator(args)
    except orch_mod.RunLockHeld as exc:
        print(f"run lock held: {exc}", file=sys.stderr)
        return 2
    except orch_mod.JournalCorruption as exc:
        print(f"journal corruption: {exc}", file=sys.stderr)
        return 3
    try:
        if orch.mode not in ("paper", "observe"):
            print(f"mode mismatch: derived {orch.mode!r}, expected 'paper' "
                  "(or its documented credentials-missing degrade)",
                  file=sys.stderr)
            return 2
        result = orch.run_reconcile(
            phase="cli", ts_utc=_now_utc_iso(), now_ms=0,
            rebaseline_cash=args.rebaseline_cash)
        _print_reconcile_summary(result, orch)
        return _reconcile_exit_code(result, orch)
    finally:
        orch.close()


def _cmd_m7_backtest(args) -> int:
    from agent.backtest_builder import ArtifactWriteRefused
    from agent.backtest_builder import write_m7_fixture_artifact

    try:
        result = write_m7_fixture_artifact(
            artifacts_dir=args.artifacts_dir,
            rules_hash=args.rules_hash,
            data_pin=args.data_pin,
            created_utc=args.created_utc,
            input_manifest_hash=args.input_manifest_hash,
            builder_git_commit=args.builder_git_commit,
            tier=args.tier,
            fixture_net_pnl_usd=args.fixture_net_pnl_usd,
            allow_reviewed_artifact=args.write_reviewed_artifact,
        )
    except ArtifactWriteRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"m7-backtest usage error: {exc}", file=sys.stderr)
        return 2
    if not result.criteria.passed:
        print("criteria_failed=" + ",".join(result.criteria.failures),
              file=sys.stderr)
        return 1
    print(
        f"artifact_path={result.artifact_path} "
        f"artifact_hash={result.payload['artifact_hash']}"
    )
    return 0


def _cmd_m7_historical_artifact(args) -> int:
    from agent.backtest_historical import HistoricalArtifactWriteRefused
    from agent.backtest_historical import load_input_manifest_json
    from agent.backtest_historical import load_quote_rows_jsonl
    from agent.backtest_historical import write_m7_historical_artifact

    try:
        result = write_m7_historical_artifact(
            artifacts_dir=args.artifacts_dir,
            quote_rows=load_quote_rows_jsonl(args.quotes_jsonl),
            symbol=args.symbol,
            instrument_id=args.instrument_id,
            rules_hash=args.rules_hash,
            data_pin=args.data_pin,
            dataset=args.dataset,
            schema=args.schema,
            created_utc=args.created_utc,
            input_manifest=load_input_manifest_json(args.input_manifest_json),
            builder_git_commit=args.builder_git_commit,
            allow_reviewed_artifact=args.allow_reviewed_artifact,
            strategy_id=args.strategy_id,
        )
    except HistoricalArtifactWriteRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"m7-historical-artifact usage error: {exc}", file=sys.stderr)
        return 2
    if not result.criteria.passed:
        print("criteria_failed=" + ",".join(result.criteria.failures),
              file=sys.stderr)
        print(
            " ".join((
                f"bar_count={result.backtest.bar_count}",
                f"candidate_count={result.backtest.candidate_count}",
                f"trade_count={len(result.backtest.trades)}",
                f"skip_count={len(result.backtest.skips)}",
            )),
            file=sys.stderr,
        )
        return 1
    print(
        f"artifact_path={result.artifact_path} "
        f"artifact_hash={result.payload['artifact_hash']} "
        f"trade_count={len(result.backtest.trades)}"
    )
    return 0


def _cmd_m7_historical_diagnostics(args) -> int:
    from agent.backtest_historical import HistoricalArtifactWriteRefused
    from agent.backtest_historical import load_input_manifest_json
    from agent.backtest_historical import load_quote_rows_jsonl
    from agent.backtest_historical import write_m7_historical_diagnostic_export

    try:
        result = write_m7_historical_diagnostic_export(
            output_path=args.output_path,
            quote_rows=load_quote_rows_jsonl(args.quotes_jsonl),
            symbol=args.symbol,
            instrument_id=args.instrument_id,
            rules_hash=args.rules_hash,
            data_pin=args.data_pin,
            dataset=args.dataset,
            schema=args.schema,
            created_utc=args.created_utc,
            input_manifest=load_input_manifest_json(args.input_manifest_json),
            builder_git_commit=args.builder_git_commit,
            strategy_id=args.strategy_id,
            max_trade_rows=args.max_trade_rows,
            max_skip_rows=args.max_skip_rows,
        )
    except HistoricalArtifactWriteRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"m7-historical-diagnostics usage error: {exc}", file=sys.stderr)
        return 2
    print(
        f"diagnostics_path={result.output_path} "
        f"trade_count={len(result.backtest.trades)} "
        f"skip_count={len(result.backtest.skips)}"
    )
    return 0


def _cmd_m7_historical_diagnostics_index(args) -> int:
    from agent.backtest_historical import HistoricalArtifactWriteRefused
    from agent.backtest_historical import write_m7_historical_diagnostic_index

    try:
        result = write_m7_historical_diagnostic_index(
            output_path=args.output_path,
            diagnostic_paths=args.diagnostics_path,
            created_utc=args.created_utc,
            source_run_id=args.source_run_id,
            source_summary_path=args.source_summary_path,
            sample_window_id=args.sample_window_id,
            holdout_window_id=args.holdout_window_id,
        )
    except HistoricalArtifactWriteRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"m7-historical-diagnostics-index usage error: {exc}",
              file=sys.stderr)
        return 2
    print(
        f"diagnostics_index_path={result.output_path} "
        f"symbol_count={len(result.payload['symbols'])} "
        f"trade_count={result.payload['aggregate']['trade_count']}"
    )
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

    reconcile = sub.add_parser("reconcile",
                               help="paper broker reconcile pass (M6)")
    reconcile.add_argument("--overlay", default=None)
    reconcile.add_argument("--journal-dir", dest="journal_dir",
                           default="journal")
    reconcile.add_argument("--calendar-fixture", dest="calendar_fixture",
                           default=str(_DEFAULT_CALENDAR))
    reconcile.add_argument("--rebaseline-cash", dest="rebaseline_cash",
                           action="store_true")
    reconcile.set_defaults(func=_cmd_reconcile)

    m7 = sub.add_parser("m7-backtest",
                        help="build deterministic M7 fixture artifact")
    m7.add_argument("--artifacts-dir", dest="artifacts_dir", required=True)
    m7.add_argument("--rules-hash", dest="rules_hash", required=True)
    m7.add_argument("--data-pin", dest="data_pin", required=True)
    m7.add_argument("--created-utc", dest="created_utc", required=True)
    m7.add_argument("--input-manifest-hash", dest="input_manifest_hash",
                    required=True)
    m7.add_argument("--builder-git-commit", dest="builder_git_commit",
                    default="unknown")
    m7.add_argument("--tier", default="fixture")
    m7.add_argument("--fixture-net-pnl-usd", dest="fixture_net_pnl_usd",
                    default="1.900000")
    m7.add_argument("--write-reviewed-artifact",
                    dest="write_reviewed_artifact", action="store_true",
                    help="reserved; fixture builder still refuses production")
    m7.set_defaults(func=_cmd_m7_backtest)

    hist = sub.add_parser(
        "m7-historical-artifact",
        help="build reviewed M7 historical artifact from normalized quote JSONL")
    hist.add_argument("--quotes-jsonl", dest="quotes_jsonl", required=True)
    hist.add_argument("--input-manifest-json", dest="input_manifest_json",
                      required=True)
    hist.add_argument("--artifacts-dir", dest="artifacts_dir", required=True)
    hist.add_argument("--symbol", required=True)
    hist.add_argument("--instrument-id", dest="instrument_id", type=int,
                      required=True)
    hist.add_argument("--dataset", default="EQUS.MINI")
    hist.add_argument("--schema", default="tbbo")
    hist.add_argument("--rules-hash", dest="rules_hash", required=True)
    hist.add_argument("--data-pin", dest="data_pin", required=True)
    hist.add_argument("--created-utc", dest="created_utc", required=True)
    hist.add_argument("--builder-git-commit", dest="builder_git_commit",
                      default="unknown")
    hist.add_argument("--strategy-id", dest="strategy_id",
                      default="directional.momentum_v1")
    hist.add_argument("--allow-reviewed-artifact",
                      dest="allow_reviewed_artifact", action="store_true")
    hist.set_defaults(func=_cmd_m7_historical_artifact)

    diag = sub.add_parser(
        "m7-historical-diagnostics",
        help="write bounded M7 historical trade/skip diagnostics")
    diag.add_argument("--quotes-jsonl", dest="quotes_jsonl", required=True)
    diag.add_argument("--input-manifest-json", dest="input_manifest_json",
                      required=True)
    diag.add_argument("--output-path", dest="output_path", required=True)
    diag.add_argument("--symbol", required=True)
    diag.add_argument("--instrument-id", dest="instrument_id", type=int,
                      required=True)
    diag.add_argument("--dataset", default="EQUS.MINI")
    diag.add_argument("--schema", default="tbbo")
    diag.add_argument("--rules-hash", dest="rules_hash", required=True)
    diag.add_argument("--data-pin", dest="data_pin", required=True)
    diag.add_argument("--created-utc", dest="created_utc", required=True)
    diag.add_argument("--builder-git-commit", dest="builder_git_commit",
                      default="unknown")
    diag.add_argument("--strategy-id", dest="strategy_id",
                      default="directional.momentum_v1")
    diag.add_argument("--max-trade-rows", dest="max_trade_rows", type=int,
                      default=2000)
    diag.add_argument("--max-skip-rows", dest="max_skip_rows", type=int,
                      default=2000)
    diag.set_defaults(func=_cmd_m7_historical_diagnostics)

    diag_index = sub.add_parser(
        "m7-historical-diagnostics-index",
        help="write a bounded cross-symbol M7 diagnostic index")
    diag_index.add_argument("--output-path", dest="output_path", required=True)
    diag_index.add_argument("--diagnostics-path", dest="diagnostics_path",
                            action="append", required=True)
    diag_index.add_argument("--created-utc", dest="created_utc", required=True)
    diag_index.add_argument("--source-run-id", dest="source_run_id")
    diag_index.add_argument("--source-summary-path", dest="source_summary_path")
    diag_index.add_argument("--sample-window-id", dest="sample_window_id")
    diag_index.add_argument("--holdout-window-id", dest="holdout_window_id")
    diag_index.set_defaults(func=_cmd_m7_historical_diagnostics_index)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
