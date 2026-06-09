"""M3 §G — calibration report: funnel + scoring aggregates over replayed journal
rows ONLY (calibration is never interpreted without the funnel, design §5).

Frozen identities (rev2 BUILD-F9/SAFETY-F8; §M.8 asserts both):
- ticks := count of distinct (symbol, instrument_id, event_start_bar_key) over
  decision rows; ticks_reaching_horizon := ticks with NO pre-horizon do_nothing row.
- Identity 1: do_nothing_identity + do_nothing_features + do_nothing_quote +
  do_nothing_market_state + ticks_reaching_horizon == ticks.
- Identity 2: do_nothing_horizon + forecasts == ticks_reaching_horizon × len(horizons).

Deterministic ordering (rev2 SAFETY-F13): per_cell sorted by (symbol, horizon);
bins by bin index; scored rows deduped by forecast_id, first by stream seq.
Rendering is stdlib-only (JSON + Markdown table — no plotting dependency).
"""
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

from agent.calibration import (
    BRIER_QUANTUM,
    REPORT_QUANTUM,
    brier,
    brier_skill_score,
    murphy_decomposition,
    reliability_bins,
)
from agent.serializer import dumps
from decimal import Context, ROUND_HALF_EVEN, localcontext

_PRE_HORIZON_STAGES = ("identity", "features", "quote", "market_state")

# Pinned context for persisted-value arithmetic (harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _q(value: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CTX):
        return value.quantize(REPORT_QUANTUM, ROUND_HALF_EVEN)


def _ref_brier(samples_with_ref) -> Decimal:
    """Mean reference brier: per-sample (p_ref - o)^2 exact at BRIER_QUANTUM,
    mean quantized REPORT_QUANTUM (rev2 MATH-Q5)."""
    with localcontext(_DECIMAL_CTX):
        total = sum(((p_ref - outcome) ** 2).quantize(BRIER_QUANTUM, ROUND_HALF_EVEN)
                    for _, outcome, p_ref in samples_with_ref)
        return _q(total / Decimal(len(samples_with_ref)))


def build_report(*, decision_rows, scored_rows, run_id: str, rules_hash: str,
                 generated_ts_utc: str, bins: int = 10) -> dict:
    decision_rows = list(decision_rows)
    scored_rows = list(scored_rows)

    # --- funnel over decision rows ---
    tick_keys = set()
    pre_horizon_fail_keys = set()
    stage_counts = {stage: 0 for stage in _PRE_HORIZON_STAGES}
    do_nothing_horizon = 0
    forecasts = 0
    for row in decision_rows:
        key = (row["symbol"], row["instrument_id"], row["event_start_bar_key"])
        tick_keys.add(key)
        if row["action"] == "forecast_only":
            forecasts += 1
        elif row["action"] == "do_nothing":
            stage = row.get("gate_stage")
            if stage in _PRE_HORIZON_STAGES:
                stage_counts[stage] += 1
                pre_horizon_fail_keys.add(key)
            elif stage == "horizon":
                do_nothing_horizon += 1
    ticks = len(tick_keys)
    ticks_reaching_horizon = ticks - len(pre_horizon_fail_keys)

    # --- dedupe scored rows by forecast_id, first by stream seq ---
    deduped: Dict[str, dict] = {}
    duplicate_scored_dropped = 0
    for row in sorted(scored_rows, key=lambda r: r["seq"]):
        forecast_id = row["forecast_id"]
        if forecast_id in deduped:
            duplicate_scored_dropped += 1
            continue
        deduped[forecast_id] = row

    decisions_by_forecast_id = {}
    for row in decision_rows:
        forecast_id = row.get("forecast_id")
        if forecast_id is not None and forecast_id not in decisions_by_forecast_id:
            decisions_by_forecast_id[forecast_id] = row

    model_version = None
    for row in decision_rows:
        if row["action"] == "forecast_only" and row.get("signal_provenance"):
            model_version = row["signal_provenance"]["model_version"]
            break

    # --- samples: scored rows joined to their decision rows ---
    samples: List[Tuple[Decimal, int]] = []
    ref_samples: List[Tuple[Decimal, int, Decimal]] = []
    per_cell_members: Dict[Tuple[str, str], List[Tuple[Decimal, int]]] = {}
    unresolved_by_reason: Dict[str, int] = {}
    unresolved_count = 0
    for forecast_id, row in deduped.items():
        if row["event_type"] == "forecast_unresolved":
            unresolved_count += 1
            reason = row["reason"]
            unresolved_by_reason[reason] = unresolved_by_reason.get(reason, 0) + 1
            continue
        decision = decisions_by_forecast_id.get(forecast_id)
        if decision is None:
            raise ValueError(
                f"scored row has no matching decision row (forecast_id={forecast_id!r})")
        p = Decimal(str(decision["forecast"]["p"]))
        outcome = row["outcome"]
        p_ref = Decimal(str(decision["reference_base_rate_asof_t0"]))
        samples.append((p, outcome))
        ref_samples.append((p, outcome, p_ref))
        cell = (decision["symbol"], decision["horizon"])
        per_cell_members.setdefault(cell, []).append((p, outcome))

    aggregate = {
        "n": len(samples),
        "brier": None, "reliability": None, "resolution": None,
        "uncertainty": None, "base_rate": None,
        "bss_vs_climatology_asof": None, "bss_vs_constant_half": None,
        "brier_ref_climatology_asof": None, "brier_ref_constant_half": None,
        "full_run_base_rate": None,
    }
    # FD-11: ALWAYS the full ten-bin array — a zero-sample report renders ten
    # empty bins, never [] (harden round 1, M3-EDGE-4).
    bins_out = reliability_bins(samples, bins=bins)
    if samples:
        murphy = murphy_decomposition(samples, bins=bins)
        bs_model = murphy["brier"]
        constant_half = [(p, o, Decimal("0.500000")) for p, o, _ in ref_samples]
        bs_ref_clim = _ref_brier(ref_samples)
        bs_ref_half = _ref_brier(constant_half)
        with localcontext(_DECIMAL_CTX):
            full_run_base_rate = _q(
                Decimal(sum(o for _, o in samples)) / Decimal(len(samples)))
        aggregate.update({
            "brier": bs_model,
            "reliability": murphy["reliability"],
            "resolution": murphy["resolution"],
            "uncertainty": murphy["uncertainty"],
            "base_rate": murphy["base_rate"],
            "bss_vs_climatology_asof": brier_skill_score(bs_model, bs_ref_clim),
            "bss_vs_constant_half": brier_skill_score(bs_model, bs_ref_half),
            "brier_ref_climatology_asof": bs_ref_clim,
            "brier_ref_constant_half": bs_ref_half,
            "full_run_base_rate": full_run_base_rate,
        })

    per_cell = []
    for (symbol, horizon) in sorted(per_cell_members):
        members = per_cell_members[(symbol, horizon)]
        cell_brier = brier(members)
        with localcontext(_DECIMAL_CTX):
            cell_base = _q(Decimal(sum(o for _, o in members)) / Decimal(len(members)))
        cell_ref_half = _ref_brier([(p, o, Decimal("0.500000")) for p, o in members])
        per_cell.append({
            "symbol": symbol, "horizon": horizon, "n": len(members),
            "brier": cell_brier, "base_rate": cell_base,
            "bss_vs_constant_half": brier_skill_score(cell_brier, cell_ref_half),
        })

    return {
        "run_id": run_id,
        "rules_hash": rules_hash,
        "model_version": model_version,
        "generated_ts_utc": generated_ts_utc,
        "funnel": {
            "ticks": ticks,
            "ticks_reaching_horizon": ticks_reaching_horizon,
            "do_nothing_identity": stage_counts["identity"],
            "do_nothing_features": stage_counts["features"],
            "do_nothing_quote": stage_counts["quote"],
            "do_nothing_market_state": stage_counts["market_state"],
            "do_nothing_horizon": do_nothing_horizon,
            "forecasts": forecasts,
            "unresolved": unresolved_count,
            "scored": len(samples),
        },
        "dedupe": {
            "decision_rows": len(decision_rows),
            "unique_forecast_ids": len({
                row.get("forecast_id") for row in decision_rows
                if row.get("forecast_id") is not None}),
            "duplicate_scored_dropped": duplicate_scored_dropped,
        },
        "aggregate": aggregate,
        "bins": bins_out,
        "per_cell": per_cell,
        "unresolved": {
            "count": unresolved_count,
            "by_reason": dict(sorted(unresolved_by_reason.items())),
        },
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# Calibration report — `{report['run_id']}`",
        "",
        f"- generated: `{report['generated_ts_utc']}`",
        f"- rules_hash: `{report['rules_hash']}`",
        f"- model: `{report['model_version']}`",
        "",
        "## Funnel",
        "",
        "| stage | count |",
        "|---|---|",
    ]
    for key, value in report["funnel"].items():
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Aggregate", "", "| metric | value |", "|---|---|"]
    for key, value in report["aggregate"].items():
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Reliability bins", "",
              "| bin | count | mean_forecast_p | observed_freq | thin |", "|---|---|---|---|---|"]
    for i, bin_row in enumerate(report["bins"]):
        lines.append(
            f"| [{bin_row['lo']}, {bin_row['hi']}) | {bin_row['count']} | "
            f"{bin_row['mean_forecast_p']} | {bin_row['observed_freq']} | {bin_row['thin']} |")
    lines += ["", "## Per cell", "",
              "| symbol | horizon | n | brier | base_rate | bss_vs_constant_half |",
              "|---|---|---|---|---|---|"]
    for cell in report["per_cell"]:
        lines.append(
            f"| {cell['symbol']} | {cell['horizon']} | {cell['n']} | {cell['brier']} | "
            f"{cell['base_rate']} | {cell['bss_vs_constant_half']} |")
    unresolved = report["unresolved"]
    lines += ["", f"## Unresolved: {unresolved['count']}", ""]
    for reason, count in unresolved["by_reason"].items():
        lines.append(f"- `{reason}`: {count}")
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict, *, out_dir) -> Path:
    """reports/calibration/<run_id>.json (+ .md); canonical dumps, one write per file."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{report['run_id']}.json"
    json_path.write_text(dumps(report) + "\n", encoding="utf-8")
    (out / f"{report['run_id']}.md").write_text(render_markdown(report), encoding="utf-8")
    return json_path
