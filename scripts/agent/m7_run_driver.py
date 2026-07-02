"""M7d packet §10 — the committed pull→pin→build→validate→stage orchestration driver.

The clean-rs-v1 sequencing lived only as a gitignored script; this module makes the
credentialed cross-sectional run reproducible AT THE ORCHESTRATION LEVEL:

    pin sessions (calendar provider, half-day/DST-correct)
      → pull normalized bbo-1m quotes (credentialed, or an injected offline source)
      → filter rows to the pinned per-date session windows (fail-closed drops)
      → write per-symbol quote JSONL + the hash-bound manifest under a STAGED dir
      → re-read everything FROM DISK and drive the real validate→write→run pipeline
      → write summary.json with the packet's per-gate table + failure diagnostics.

Staged-only by construction: the staging root may never resolve inside the
production ``artifacts/backtests`` directory, an existing run dir is refused
(no silent overwrite of a staged run), and a passing artifact lands under the
STAGED dir — the production write remains a separate, explicitly-gated
invocation of the artifact CLI. ``rules_hash`` is always DERIVED from the agent
rules config the harness runs (never operator-supplied).

Offline tests drive the pull through an injected ``quote_event_source``; the
credentialed path builds the lazy Databento source inside
``historical_backfill.pull_normalized_window`` (never imported offline).
"""
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional, Sequence

from agent.backtest_builder import PRODUCTION_ARTIFACTS_DIR
from agent.backtest_historical import (
    _realism_gap_bps,
    write_m7_historical_cross_sectional_artifact,
)
from agent.config import load as load_config
from agent.historical_backfill import (
    build_cross_sectional_input_manifest,
    cross_sectional_data_pin,
    filter_rows_to_pinned_sessions,
    pin_sessions_from_provider,
    pull_normalized_window,
    write_quote_rows_jsonl,
)
from agent.serializer import dumps
from agent.signal_config import SignalConfig
from agent.strategies.relative_strength import STRATEGY_ID as RS_STRATEGY_ID

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_AGENT_RULES = _REPO_ROOT / "config" / "agent_rules.json"

# The 12 pinned gates (M7d packet "Metrics And Acceptance Gates" 1-11 + the
# writer-gated equal-weight leg of 10) + the five zero-quality-breach counts of
# gate 12. Names match evaluate_paper_phase_criteria / the writer's failure keys
# exactly, so `passed` is derived from the authoritative failures tuple — never
# re-evaluated here.
_GATE_VALUE_PATHS = {
    "min_sessions": ("sample", "session_count"),
    "min_trades": ("sample", "trade_count"),
    "min_traded_sessions": ("sample", "traded_session_count"),
    "profit_factor_min": ("pnl", "profit_factor"),
    "max_drawdown_pct_allocated": ("risk", "max_drawdown_pct_allocated"),
    "worst_day_pct_allocated": ("risk", "worst_day_pct_allocated"),
    "p95_realism_gap_bps_max": ("risk", "p95_realism_gap_bps"),
    "max_single_fill_divergence_bps": ("risk", "max_single_fill_divergence_bps"),
    "positive_net_pnl": ("pnl", "net_execution_realistic_pnl_usd"),
    "positive_active_pnl": ("benchmark", "active_pnl_usd"),
    "avg_trade_bps_positive": ("pnl", "avg_trade_bps"),
}
_GATE_THRESHOLD_KEYS = {
    "min_sessions": "min_sessions",
    "min_trades": "min_trades",
    "min_traded_sessions": "min_traded_sessions",
    "profit_factor_min": "profit_factor_min",
    "max_drawdown_pct_allocated": "max_drawdown_pct_allocated",
    "worst_day_pct_allocated": "worst_day_pct_allocated",
    "p95_realism_gap_bps_max": "p95_realism_gap_bps_max",
    "max_single_fill_divergence_bps": "max_single_fill_divergence_bps",
}
_QUALITY_ZERO_KEYS = (
    "unresolved_reconcile_drift_count",
    "s1_canary_breach_count",
    "live_broker_submit_count",
    "artifact_mismatch_count",
    "unhandled_exception_count",
)


@dataclass(frozen=True)
class StagedRunResult:
    staging_dir: Path
    manifest_path: Path
    summary_path: Path
    manifest_hash: str
    data_pin: str
    rules_hash: str
    criteria_passed: bool
    production_artifact_written: bool   # ALWAYS False — staging never writes production
    summary: dict


def _derived_rules_hash(agent_rules_path) -> str:
    path = Path(agent_rules_path) if agent_rules_path is not None \
        else _DEFAULT_AGENT_RULES
    return SignalConfig.from_config(load_config(path)).rules_hash


def _percentile(sorted_values, pct: int):
    """The _gap_summary index convention (ceil(n*pct/100)-1) shared for p95/p99."""
    if not sorted_values:
        return Decimal("0.000000")
    index = max(0, ((len(sorted_values) * pct + 99) // 100) - 1)
    return sorted_values[index]


def _distribution(values) -> dict:
    present = sorted(v for v in values if v is not None)
    return {
        "count": len(present),
        "p50": str(_percentile(present, 50)),
        "p95": str(_percentile(present, 95)),
        "p99": str(_percentile(present, 99)),
        "max": str(present[-1]) if present else "0.000000",
    }


def _gate_table(metrics: Mapping, failures: Sequence[str],
                equal_weight_active_pnl_usd: Decimal) -> dict:
    failed = set(failures)
    table = {}
    thresholds = metrics.get("thresholds", {})
    for gate, (section, key) in _GATE_VALUE_PATHS.items():
        threshold_key = _GATE_THRESHOLD_KEYS.get(gate)
        table[gate] = {
            "value": metrics.get(section, {}).get(key),
            "threshold": (thresholds.get(threshold_key)
                          if threshold_key is not None else "> 0"),
            "passed": gate not in failed,
        }
    table["positive_equal_weight_long_active_pnl"] = {
        "value": str(equal_weight_active_pnl_usd),
        "threshold": "> 0",
        "passed": "positive_equal_weight_long_active_pnl" not in failed,
    }
    for key in _QUALITY_ZERO_KEYS:
        table[key] = {
            "value": metrics.get("quality", {}).get(key),
            "threshold": "== 0",
            "passed": key not in failed,
        }
    return table


def _entry_hour_histogram(trades) -> dict:
    histogram: dict = {}
    for trade in trades:
        hour = trade.entry_bar_end_utc[11:13]
        histogram[hour] = histogram.get(hour, 0) + 1
    return {hour: histogram[hour] for hour in sorted(histogram)}


def _per_symbol_gap_p95(trades) -> dict:
    by_symbol: dict = {}
    for trade in trades:
        by_symbol.setdefault(trade.symbol, []).append(_realism_gap_bps(trade))
    return {symbol: str(_percentile(sorted(gaps), 95))
            for symbol, gaps in sorted(by_symbol.items())}


def _per_symbol_net(trades) -> dict:
    by_symbol: dict = {}
    for trade in trades:
        by_symbol[trade.symbol] = (by_symbol.get(trade.symbol, Decimal("0"))
                                   + trade.net_execution_realistic_pnl_usd)
    return {symbol: str(total) for symbol, total in sorted(by_symbol.items())}


def _guard_staging_dir(staging_root, run_id: str) -> Path:
    # run_id is operator input (--run-id): it must be a single path component,
    # never a traversal that relocates the staging dir (e.g. into production).
    if (not isinstance(run_id, str) or not run_id or run_id in (".", "..")
            or "/" in run_id or "\\" in run_id or ".." in run_id
            or Path(run_id).is_absolute()):
        raise ValueError(
            f"run_id must be a single plain path component, got {run_id!r}")
    staging_root = Path(staging_root)
    resolved_root = staging_root.resolve()
    production = PRODUCTION_ARTIFACTS_DIR.resolve()
    if (resolved_root == production
            or production in resolved_root.parents
            or resolved_root in production.parents
            or resolved_root == production.parent):
        raise ValueError(
            "staging root must never resolve inside (or above) the production "
            f"artifacts directory {production}; got {resolved_root}")
    staging_dir = staging_root / run_id
    # belt-and-braces: the JOINED dir must still resolve inside the root
    # (symlinked run dirs or future run_id relaxations cannot escape).
    resolved_dir = staging_dir.resolve()
    if resolved_root not in resolved_dir.parents:
        raise ValueError(
            f"staged run dir {resolved_dir} escapes the staging root "
            f"{resolved_root} (fail-closed)")
    if staging_dir.exists():
        raise ValueError(
            f"staged run dir already exists: {staging_dir} — refusing to "
            "overwrite a prior staged run (pick a new run_id)")
    return staging_dir


def stage_cross_sectional_run(
        *, run_id: str,
        universe: Sequence[str],
        dataset: str,
        schema: str,
        session_dates: Sequence[str],
        schedule_provider,
        hypothesis_id: str,
        selection_rule: str,
        horizon: str,
        staging_root,
        created_utc: str,
        builder_git_commit: str,
        agent_rules_path=None,
        strategy_id: str = RS_STRATEGY_ID,
        quote_event_source=None,
        credentials_path=None,
        blackout_session_dates_et: Sequence[str] = (),
        window_rationale: str = "") -> StagedRunResult:
    """Run the full staged sequencing for ONE predeclared window/horizon config."""
    staging_dir = _guard_staging_dir(staging_root, run_id)

    # 1. pin the predeclared sessions from the calendar (fail-closed on any
    #    non-trading/unknown date — a mis-pinned window must die HERE, before
    #    any credentialed pull spends money).
    sessions = pin_sessions_from_provider(session_dates, schedule_provider)
    calendar_pin = schedule_provider.calendar_pin()
    start_utc = min(w["rth_open_utc"] for w in sessions.values())
    end_utc = max(w["rth_close_utc"] for w in sessions.values())

    # 2. pull + normalize (credentialed unless a source is injected). The naive
    #    fixed time-of-day filter is DISABLED; step 3 filters against the pinned
    #    per-date windows instead (half-day/DST-correct).
    pulled = pull_normalized_window(
        dataset=dataset, schema=schema, universe=universe,
        start_utc=start_utc, end_utc=end_utc,
        quote_event_source=quote_event_source,
        credentials_path=credentials_path, rth_window=None)

    # 3. session-pinned row filter (fail-closed drops, counted per symbol).
    kept, dropped = filter_rows_to_pinned_sessions(pulled, sessions)

    # 4. rules_hash is DERIVED from the config the harness runs.
    rules_hash = _derived_rules_hash(agent_rules_path)

    # 5. build the hash-bound manifest over the KEPT rows.
    manifest = build_cross_sectional_input_manifest(
        symbol_rows=kept, universe=universe, dataset=dataset, schema=schema,
        hypothesis_id=hypothesis_id, selection_rule=selection_rule,
        calendar_pin=calendar_pin, sessions=sessions,
        blackout_session_dates_et=blackout_session_dates_et,
        horizon=horizon)
    data_pin = cross_sectional_data_pin(manifest)
    manifest_hash = manifest["manifest_hash"]

    # 6. stage rows + manifest, then RE-READ FROM DISK so the exact staged bytes
    #    are what validate and run (file-level reproducibility).
    staging_dir.mkdir(parents=True)
    quotes_dir = staging_dir / "quotes"
    quotes_dir.mkdir()
    for symbol in universe:
        write_quote_rows_jsonl(quotes_dir / f"{symbol}.jsonl", kept[symbol])
    manifest_path = staging_dir / "manifest.json"
    manifest_path.write_text(dumps(manifest) + "\n", encoding="utf-8")

    manifest_from_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows_from_disk = {}
    for symbol in universe:
        lines = (quotes_dir / f"{symbol}.jsonl").read_text(encoding="utf-8")
        rows_from_disk[symbol] = tuple(
            json.loads(line) for line in lines.splitlines() if line)

    # 7. the real validate→run→(staged-)write pipeline. The artifact json is
    #    written only on a full criteria pass, and only under the STAGED dir.
    result = write_m7_historical_cross_sectional_artifact(
        artifacts_dir=staging_dir / "artifact",
        symbol_quote_rows=rows_from_disk,
        rules_hash=rules_hash,
        data_pin=data_pin,
        dataset=dataset,
        schema=schema,
        created_utc=created_utc,
        input_manifest=manifest_from_disk,
        builder_git_commit=builder_git_commit,
        allow_reviewed_artifact=True,
        strategy_id=strategy_id,
        agent_rules_path=agent_rules_path,
    )
    backtest = result.backtest
    metrics = result.payload["metrics"]

    # 8. summary.json — the packet's per-gate table + NULL diagnostics.
    trades = backtest.trades
    gaps = sorted(_realism_gap_bps(trade) for trade in trades)
    summary = {
        "run_id": run_id,
        "kind": "m7_cross_sectional_staged_run",
        "strategy_id": strategy_id,
        "dataset": dataset,
        "schema": schema,
        "horizon": horizon,
        "window": {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "sessions": len(sessions),
            "session_dates": sorted(sessions),
            "rationale": window_rationale,
        },
        "rules_hash": rules_hash,
        "manifest_hash": manifest_hash,
        "data_pin": data_pin,
        "calendar_pin": calendar_pin,
        "mode": "staged_only",
        "production_artifact_written": False,
        "criteria_passed": result.criteria.passed,
        "criteria_failures": list(result.criteria.failures),
        "gate_table": _gate_table(
            metrics, result.criteria.failures,
            backtest.equal_weight_long_active_pnl_usd),
        "metrics": metrics,
        "realism_decomposition": {
            "gap_bps": {
                "count": len(gaps),
                "p50": str(_percentile(gaps, 50)),
                "p95": str(_percentile(gaps, 95)),
                "p99": str(_percentile(gaps, 99)),
                "max": str(gaps[-1]) if gaps else "0.000000",
            },
            "entry_spread_bps": _distribution(
                trade.entry_spread_bps for trade in trades),
            "exit_spread_bps": _distribution(
                trade.exit_spread_bps for trade in trades),
            "per_symbol_gap_p95": _per_symbol_gap_p95(trades),
        },
        "sample_audit": {
            "trade_count": len(trades),
            "exclusion_reason_counts": dict(backtest.exclusion_reason_counts),
            "skip_reason_counts": _skip_counts(backtest.skips),
            "entry_hour_histogram_utc": _entry_hour_histogram(trades),
            "dropped_rows_by_symbol": dict(dropped),
            "kept_rows_by_symbol": {sym: len(kept[sym]) for sym in sorted(kept)},
            "overlap_suppressed_leg_count":
                backtest.overlap_suppressed_leg_count,
            "insufficient_valid_decision_count":
                backtest.insufficient_valid_decision_count,
        },
        "breadth": {
            "per_symbol_leg_counts": dict(backtest.per_symbol_leg_counts),
            "per_symbol_net_execution_realistic_pnl_usd": _per_symbol_net(trades),
        },
        "benchmarks": {
            "active_vs_exposure_matched_midbar_v1_usd":
                metrics.get("benchmark", {}).get("active_pnl_usd"),
            "universe_equal_weight_long_active_pnl_usd":
                str(backtest.equal_weight_long_active_pnl_usd),
        },
        "provenance": {
            "hypothesis_id": hypothesis_id,
            "selection_rule": selection_rule,
            "universe": list(universe),
            "created_utc": created_utc,
            "builder_git_commit": builder_git_commit,
            "agent_rules_path": str(agent_rules_path) if agent_rules_path
                                else str(_DEFAULT_AGENT_RULES),
        },
    }
    summary_path = staging_dir / "summary.json"
    summary_path.write_text(dumps(summary) + "\n", encoding="utf-8")

    return StagedRunResult(
        staging_dir=staging_dir,
        manifest_path=manifest_path,
        summary_path=summary_path,
        manifest_hash=manifest_hash,
        data_pin=data_pin,
        rules_hash=rules_hash,
        criteria_passed=result.criteria.passed,
        production_artifact_written=False,
        summary=summary,
    )


def _skip_counts(skips) -> dict:
    counts: dict = {}
    for skip in skips:
        counts[skip.reason] = counts.get(skip.reason, 0) + 1
    return {reason: counts[reason] for reason in sorted(counts)}


def _trading_dates_in_range(provider, start_date_et: str,
                            end_date_et: str) -> list:
    from agent.calendar_fixture import _date_range
    from agent.market_calendar import _is_weekend

    dates = []
    for session_date in _date_range(start_date_et, end_date_et):
        if _is_weekend(session_date):
            continue
        if provider.is_trading_day(session_date):
            dates.append(session_date)
    return dates


def _main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Stage a credentialed M7 cross-sectional run: pin sessions, "
                    "pull bbo-1m quotes, build+validate the hash-bound manifest, "
                    "run the harness, write summary.json (staged-only; never "
                    "writes production artifacts).")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--staging-root",
                        default=str(_REPO_ROOT / "reports" / "m7_historical_runs"))
    parser.add_argument("--universe", required=True,
                        help="comma-separated ordered symbols (predeclared)")
    parser.add_argument("--dataset", default="EQUS.MINI")
    parser.add_argument("--schema", default="bbo-1m")
    parser.add_argument("--session-dates",
                        help="comma-separated predeclared ET session dates")
    parser.add_argument("--start", help="derive session dates from calendar: start")
    parser.add_argument("--end", help="derive session dates from calendar: end")
    parser.add_argument("--calendar-fixture",
                        help="FixtureScheduleProvider fixture path (committed, "
                             "cross-checked)")
    parser.add_argument("--use-exchange-calendars", action="store_true",
                        help="use the pinned live provider instead of a fixture "
                             "(requires .venv)")
    parser.add_argument("--horizon", required=True,
                        help="explicit holding horizon, e.g. 120m (C2) / 60m (C1)")
    parser.add_argument("--hypothesis-id", required=True)
    parser.add_argument("--selection-rule", required=True)
    parser.add_argument("--agent-rules", default=None,
                        help="agent rules config the harness runs (rules_hash is "
                             "DERIVED from it)")
    parser.add_argument("--created-utc", required=True)
    parser.add_argument("--builder-git-commit", required=True)
    parser.add_argument("--credentials", default=None,
                        help="Databento credentials path (default "
                             ".secrets/databento.json)")
    parser.add_argument("--blackout-dates", default="",
                        help="comma-separated CA-blackout ET session dates")
    parser.add_argument("--window-rationale", default="")
    args = parser.parse_args(argv)

    if args.use_exchange_calendars:
        from agent.market_calendar import ExchangeCalendarsScheduleProvider
        provider = ExchangeCalendarsScheduleProvider()
    else:
        if not args.calendar_fixture:
            parser.error("--calendar-fixture is required unless "
                         "--use-exchange-calendars is set")
        from agent.market_calendar import FixtureScheduleProvider
        fixture = json.loads(Path(args.calendar_fixture).read_text(
            encoding="utf-8"))
        provider = FixtureScheduleProvider(fixture, pin=fixture["pin"])

    if args.session_dates:
        session_dates = [d for d in args.session_dates.split(",") if d]
    elif args.start and args.end:
        session_dates = _trading_dates_in_range(provider, args.start, args.end)
        print(f"derived {len(session_dates)} trading sessions "
              f"{session_dates[0]} -> {session_dates[-1]} (review before go)")
    else:
        parser.error("pass --session-dates or both --start and --end")

    result = stage_cross_sectional_run(
        run_id=args.run_id,
        universe=[s for s in args.universe.split(",") if s],
        dataset=args.dataset,
        schema=args.schema,
        session_dates=session_dates,
        schedule_provider=provider,
        hypothesis_id=args.hypothesis_id,
        selection_rule=args.selection_rule,
        horizon=args.horizon,
        staging_root=args.staging_root,
        created_utc=args.created_utc,
        builder_git_commit=args.builder_git_commit,
        agent_rules_path=args.agent_rules,
        credentials_path=args.credentials,
        blackout_session_dates_et=[d for d in args.blackout_dates.split(",")
                                   if d],
        window_rationale=args.window_rationale,
    )
    print(f"staged: {result.staging_dir}")
    print(f"manifest_hash={result.manifest_hash}")
    print(f"data_pin={result.data_pin}")
    print(f"rules_hash={result.rules_hash}")
    print(f"criteria_passed={result.criteria_passed} "
          f"(production_artifact_written={result.production_artifact_written})")
    return 0 if result.criteria_passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
