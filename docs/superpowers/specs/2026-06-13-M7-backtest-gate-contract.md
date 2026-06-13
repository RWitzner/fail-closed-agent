# M7 (Backtest gate + first paper-eligible directional strategy) - OFFLINE-COMPLETE CONTRACT

> **Status:** Review-hardened offline-complete contract, 2026-06-13. Historical reviewed artifact is deferred.
> **Parent:** `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.
> **Build base:** `m6-reconcile` closeout (`d83f392`, 1639-test M6 closeout). M7 is stacked after M6.
>
> M7 closes the offline gate/engine/strategy path while keeping production `artifacts/backtests/` empty. A
> reviewed historical artifact is a separate credentialed tier before the paper edge-validation phase can open.

## 0. Scope and hard boundaries

M7 is the first milestone that may make a **real, non-synthetic strategy** eligible for the paper open pipeline.
It does that by building the historical anti-lookahead artifact gate. In the offline-complete closeout, no
production strategy artifact is committed; the default artifact directory remains fail-closed.

**In scope**

- `scripts/agent/backtest_gate.py`: extend the M5 artifact verifier/shape without weakening fail-closed behavior.
- New pure backtest modules under `scripts/agent/`:
  - `backtest_engine.py` for event-time simulation over M3 `MidBar`/`SignalSnapshot` seams.
  - `backtest_metrics.py` for `execution_realistic_pnl` metrics and benchmark attribution.
  - `backtest_builder.py` for deterministic temp-dir fixture artifacts only.
  - `paper_phase_criteria.py` for the post-M7 paper/M8 evidence gate.
- New real strategy module under `scripts/agent/strategies/`:
  - `directional_momentum.py` with `strategy_id = "directional.momentum_v1"`.
- CLI/runbook surface for producing and reviewing an artifact:
  - `python3 -m agent m7-backtest ...` for fixture/temp artifacts only.
  - `docs/runbooks/m7-paper-edge-validation.md`
- Tests and fixtures for anti-lookahead, artifact verification, strategy behavior, and orchestrator/preflight
  integration.
- A separate historical reviewed-artifact tier. It is deferred unless credentials/data are available and a reviewed
  v2 artifact is intentionally committed.

**Out of scope**

- Any real-money behavior, live arming, or live broker enablement.
- Flipping committed run gates. `config/agent_rules.json` keeps `enabled=false` and `paper_trading.enabled=false`.
- Raising committed risk caps. `config/risk_rules.json` caps stay `0`; committed config remains S1 fail-closed.
- Short selling. The first real strategy is long-only.
- Online learning or retraining.
- Opening a real strategy without a verified artifact in `artifacts/backtests/`.
- Using adjusted OHLCV closes as a trading label or fill source.

## 1. Repo facts M7 builds on

| Fact | Source |
|---|---|
| M5 already ships `verify_artifact(strategy_id, rules_hash, data_pin)` and `ArtifactCheck{ok,missing,key_mismatch,hash_invalid}`. The current top-level artifact key set is exactly `v,strategy_id,rules_hash,data_pin,metrics,created_utc,artifact_hash`. | `scripts/agent/backtest_gate.py` |
| M5 preflight maps non-synthetic artifact status to `backtest_artifact_missing`, `artifact_key_mismatch`, or `artifact_hash_invalid`. Synthetics do not consult the artifact. | `scripts/agent/execution_preflight.py` |
| The orchestrator currently caches artifact checks by `strategy_id` only, even though the M5 contract says once per `(strategy_id, rules_hash, data_pin)`. M7 must fix this before artifacts can be trusted across changing data pins. | `scripts/agent/orchestrator.py::_artifact_check` |
| Real strategy scans run before run-gate refusal. The committed config still stops at `can_open` rung 1 (`run_gates_off`) before preflight/mint. | `scripts/agent/orchestrator.py`, `scripts/agent/risk/can_open.py` |
| M3 pins the anti-lookahead predicate: a mid bar is eligible at `as_of` iff `bucket_end_utc <= as_of` and `watermark_utc <= as_of`, with parsed UTC instants, never lexicographic timestamp compare. | `scripts/agent/bar_series.py`, M3 contract |
| `FeatureSnapshot` carries `feature_cutoff_bar_end_utc`, `watermark_utc`, `data_pin`, `rules_hash`, and `feature_snapshot_id`. | `scripts/agent/feature_engine.py` |
| `SignalSnapshot` is the shared scan input and already enforces feature, quote, market-state, and horizon gates. | `scripts/agent/signal_snapshot.py` |
| `Candidate.paper_eligible is True` is identity-checked by both risk and preflight. | `scripts/agent/candidate.py`, `scripts/agent/risk/can_open.py`, `scripts/agent/execution_preflight.py` |
| `execution_realistic_pnl` is the strategy-evaluation basis. Broker ledger PnL remains separate. | design spec S5/S9, `scripts/agent/paper_book.py` |

## 2. Frozen decisions

| # | Decision |
|---|---|
| FD-M7-1 | **Backtest data source:** M7 uses recorded point-in-time quote rows and M3 `MidBar` reconstruction. The runner may consume committed fixtures offline and credentialed historical pulls when explicitly provided, but all scoring uses the same normalized row shape. |
| FD-M7-2 | **Raw-vs-adjusted policy:** trading labels, entries, exits, and benchmark marks use raw point-in-time quotes/mid bars. Corporate-action windows are excluded unless M2 CA provenance is confirmed. Adjusted daily closes are not a label/fill fallback in M7. |
| FD-M7-3 | **Anti-lookahead predicate:** every feature, signal, label, entry, exit, and benchmark read passes an explicit `as_of_utc`; the reader rejects future receipt (`watermark_utc > as_of_utc`) and future bucket end (`bucket_end_utc > as_of_utc`). No M7 production path calls a reader with `as_of_utc=None`. |
| FD-M7-4 | **Simulation clock:** the backtest advances only on completed event-start bars. A decision at bar `t0` can use only feature/quote/market-state facts eligible at `decision_ts_utc`; fills use the next eligible quote after the configured latency budget, mirroring M5 quote B semantics. |
| FD-M7-5 | **Cost basis:** artifact metrics use `execution_realistic_pnl`, including modeled spread/slippage/fees. Raw signal returns and optimistic fills may be reported under diagnostics but cannot satisfy S9. |
| FD-M7-6 | **Benchmark attribution:** the pass/fail metric includes an exposure-matched benchmark: same symbol, same side, same entry/exit windows, equal notional, raw mid-bar marks, same blackout calendar, no cost advantage. Strategy pass requires positive active PnL versus this benchmark. |
| FD-M7-7 | **First strategy:** `directional.momentum_v1` is long-only and emits at most one BUY candidate per symbol per completed bar. It never emits sells-to-open, shorts, multi-leg candidates, or reduce instructions. Existing reduce/close handling remains M5-owned. |
| FD-M7-8 | **Paper eligibility source of truth:** `directional.momentum_v1` remains pure and may emit `Candidate(..., paper_eligible=True)` after local feature/edge predicates pass. That flag is necessary but not sufficient to open. The orchestrator/preflight S9 gate is the only artifact source of truth before broker submit: it verifies `(strategy_id, rules_hash, data_pin)` and rejects missing/mismatched/invalid artifacts before minting an open token or writing a submit attempt. |
| FD-M7-9 | **Artifact check cache key:** orchestrator artifact checks are cached by the full triple `(strategy_id, rules_hash, data_pin)`, never by strategy alone. A data-pin change in one run must re-check the artifact and can re-close the gate. |
| FD-M7-10 | **Artifact top-level compatibility:** M7 keeps the M5 top-level key set exactly unchanged: `v,strategy_id,rules_hash,data_pin,metrics,created_utc,artifact_hash`. New M7 data lives under `metrics` so old malformed/extra top-level payloads still fail closed. |
| FD-M7-11 | **Artifact version:** M7 artifact payloads use `v = 2`. `verify_artifact` accepts v1 only for existing M5 tests and v2 for production M7 artifacts. Unknown versions are `hash_invalid`. |
| FD-M7-12 | **Artifact hash:** `artifact_hash = row_hash(payload_without_artifact_hash)`. Hash mismatch, float/non-serializable metric value, missing metric, wrong basis, or wrong version returns `hash_invalid`, never raises. |
| FD-M7-13 | **Artifact path:** production verifier still defaults to `artifacts/backtests/<strategy_id>.json`; fixture builders/tests must take a mandatory temp `artifacts_dir` and must never write the committed production directory. Production artifacts require a separate historical reviewed-artifact flow. |
| FD-M7-14 | **No secret signing in M7:** "signed" means committed, reviewed, and hash-bound by `artifact_hash` plus git history. HMAC or external attestation is deferred unless explicitly approved. |
| FD-M7-15 | **Artifact pass criteria are data with pinned floors:** pass/fail thresholds live inside the v2 artifact metrics under `thresholds`, but `verify_artifact` rejects thresholds looser than the M7-pinned paper/M8 criteria. It verifies required fields and that `metrics.pass is True`; it does not recompute the backtest. Recompute is owned by the builder tests and runbook. |
| FD-M7-16 | **Backtest runner determinism:** identical inputs, rules hash, data pin, and strategy id produce byte-identical artifact body and report rows, except for `created_utc` when not pinned. Golden tests pin `created_utc`. |
| FD-M7-17 | **Rules hash discipline:** artifact key uses the same assembled-config `rules_hash` used by orchestrator/preflight. A changed committed config re-closes S9 until a new artifact is produced. |
| FD-M7-18 | **Data pin discipline:** `data_pin` remains the M3 format `dataset:schema:interval:source_id`. The `source_id` for M7 must include a manifest hash of the historical/fixture input set, not a mutable directory name. |
| FD-M7-19 | **No paper-run shortcut:** a green historical artifact only allows paper eligibility. It does not satisfy the post-M7 paper edge-validation phase and does not move M8. |
| FD-M7-20 | **Committed config canary remains strongest:** with committed config, even a valid M7 artifact and real paper-eligible strategy must submit zero orders because run gates/caps are off. |
| FD-M7-21 | **No network in offline tests:** credentialed historical pulls are optional tier-2 verification and must be behind explicit CLI flags. The normal suite remains stdlib-only and no-network/no-creds. |
| FD-M7-22 | **Close path unchanged:** M7 gates opens only. It does not add anti-lookahead checks to reduce-only/flatten paths; those remain risk-reducing broker-ledger operations from M5/M6. |

## 3. V2 artifact shape

Top-level shape remains exact:

```json
{
  "v": 2,
  "strategy_id": "directional.momentum_v1",
  "rules_hash": "<assembled config hash>",
  "data_pin": "EQUS.MINI:tbbo:1m:<manifest-hash-source-id>",
  "metrics": {},
  "created_utc": "2026-06-13T00:00:00.000000Z",
  "artifact_hash": "<row_hash of payload without artifact_hash>"
}
```

Required `metrics` keys for v2:

| Key | Type | Semantics |
|---|---|---|
| `basis` | string | Must be `"execution_realistic_pnl"`. |
| `pass` | bool | Must be identity `true` for `verify_artifact(...).status == "ok"`. |
| `runner_version` | string | Frozen M7 runner id, initially `"m7-backtest-v1"`. |
| `strategy_version` | string | Must match `"directional.momentum_v1"`. |
| `sample` | object | `{start_utc,end_utc,session_count,decision_count,trade_count,traded_session_count,symbols[]}`. |
| `pnl` | object | Decimal-strings for `gross_modeled_usd`, `fees_usd`, `net_execution_realistic_pnl_usd`, `avg_trade_bps`, `profit_factor`. |
| `benchmark` | object | `{method:"exposure_matched_midbar_v1", benchmark_pnl_usd, active_pnl_usd}`. |
| `risk` | object | Decimal-strings for `max_drawdown_usd`, `max_drawdown_pct_allocated`, `worst_day_usd`, `worst_day_pct_allocated`, `p95_realism_gap_bps`, `max_single_fill_divergence_bps`. |
| `quality` | object | `future_receipt_count`, `missing_bar_count`, `ca_blackout_skips`, `data_quality_skip_count`, plus zero-count paper safety fields for unresolved reconcile drift, S1 canary breaches, live broker submits, artifact mismatches, and unhandled loop exceptions. |
| `thresholds` | object | The exact pass thresholds used, as strings/ints/bools. |
| `provenance` | object | input manifest hash, artifact builder git commit if known, and fixture/historical tier. |

`verify_artifact` returns:

- `missing`: no file or invalid strategy id/path.
- `hash_invalid`: malformed JSON, extra/missing top-level keys, bad hash, wrong v2 required fields, wrong basis,
  `metrics.pass is not True`, non-string Decimal fields, thresholds looser than the pinned criteria, or float
  anywhere in payload.
- `key_mismatch`: hash-valid artifact whose `(strategy_id, rules_hash, data_pin)` does not match the current run.
- `ok`: hash-valid, key-matching, v2 metric-valid artifact.

## 4. Backtest semantics

The pure runner simulates only the opening decision and the strategy-owned planned exit used for scoring. It is
not a broker ledger and cannot write `journal/positions.jsonl`.

Per bar:

1. Build/update M3 `FeatureView` from eligible mid bars only.
2. Assemble `SignalSnapshot` with the same gate order as live/paper.
3. Call `directional.momentum_v1.scan(ctx)`.
4. For a candidate:
   - reject if not single-leg BUY, whole-share qty, positive limit, and `paper_eligible=True`;
   - apply the M5 pricing model over quote A and the selected quote-B bar after latency;
   - reject a delayed receipt of the decision bucket; quote B must be a later selected bar after the latency instant;
   - mark a simulated open only if quote B is eligible and marketable after costs.
5. Close by deterministic horizon policy from the strategy config/code constants:
   - default: close at the configured signal horizon's eligible mid bar;
   - if horizon crosses RTH close or data becomes ineligible, record a reject/skip, never fill from future data.
6. Score broker-independent modeled economics as `execution_realistic_pnl`.

All timestamp comparisons parse UTC instants. Raw strings are never compared lexicographically.

## 5. `directional.momentum_v1`

Initial behavior:

- Reads only `ScanContext.snapshot` and pure Decimal values.
- Uses M3 features already present in `FeatureSnapshot`; it does not reach into bar readers or journals.
- Long-only BUY candidate when:
  - snapshot passed M3 gates;
  - `momentum_9 > 0`;
  - `momentum_21 > 0`;
  - `z_ret_21 >= 0`;
  - `realized_vol_21 > 0`;
  - quote mid exists;
  - expected edge after the strategy's fixed bps buffer is positive.
- Qty is deterministic whole shares from a fixed paper notional ceiling, capped before risk by candidate sizing.
- `limit_price` is on the M5 price grid and no worse than the strategy's edge budget.
- `score` is a Decimal-string-compatible edge score in bps.

The strategy has no broker imports, no preflight imports, no dynamic imports, no file I/O, and no clocks.

## 6. Paper-phase success criteria pinned by M7

The post-M7 paper edge-validation phase can start only after M7 is built and reviewed. M8 remains blocked unless
the paper phase meets all criteria below.

Minimum sample:

- At least 20 full RTH sessions.
- At least 30 opened-and-closed paper trades.
- At least 5 traded sessions with one or more closed trades.
- No unresolved broker reconciliation drift at SOD/EOD.

Return and benchmark:

- `net_execution_realistic_pnl_usd > 0`.
- `active_pnl_usd > 0` versus `exposure_matched_midbar_v1`.
- Profit factor `>= 1.10` on execution-realistic closed trades.
- Average trade after fees `> 0 bps`.

Risk and realism:

- Maximum drawdown `<= 1.50%` of allocated paper notional.
- Worst single-session loss `<= 0.75%` of allocated paper notional.
- P95 broker-vs-modeled realism gap `<= 15 bps`.
- No single fill divergence worse than `50 bps` unless the trade is excluded by documented data-quality rules.

Safety and operations:

- Zero S1 canary breaches.
- Zero live-broker submissions.
- Zero artifact hash/key mismatches during the paper phase.
- Zero unhandled exceptions in the orchestrator loop.
- Every data-quality exclusion is journaled with a machine-readable reason.

Failure of any criterion blocks M8. Passing all criteria is necessary but not sufficient for live: M8 still needs
the separate two-key arming, broker dry-run, kill-switch drill, caps, and runbook approval.

## 7. Test map

| Test area | Required coverage |
|---|---|
| `tests/agent/test_backtest_gate_m7.py` | v2 artifact ok/missing/key mismatch/hash invalid; wrong basis; `pass:false`; float metric; unknown version; path traversal; top-level extra key; Decimal-as-string validation. |
| `tests/agent/test_orchestrator_m7_artifact_cache.py` | artifact cache keyed by `(strategy_id,rules_hash,data_pin)`; data-pin change re-checks and can reject; committed config still submits zero orders. |
| `tests/agent/test_backtest_engine_m7.py` | future receipt rejected; watermark equality accepted; lexicographic timestamp trap; horizon crossing close skipped; quote B latency semantics; deterministic artifact body. |
| `tests/agent/test_directional_momentum_m7.py` | long-only candidate, no candidate on weak/negative features, no side effects/imports, price grid, whole-share qty, edge score. |
| `tests/agent/test_m7_paper_phase_criteria.py` | criteria evaluator blocks each failed threshold and passes only the full matrix. |
| `tests/agent/test_m7_backtest_cli.py` | fixture artifact builder writes only on passing criteria, always refuses the production dir, and pins the paper-phase runbook. |
| `tests/agent/test_m7_s9_integration.py` | missing/mismatched/valid artifact S9 paths plus committed-config zero-submit with a valid artifact. |
| Existing canaries | S1 committed config, no network/no creds, synthetic isolation, M5 preflight reasons, full suite. |

## 8. Acceptance tiers

M7 has two acceptance tiers:

1. **Offline complete:** all M7 unit/integration tests pass; artifact verifier/runner are deterministic; temp-dir
   fixture artifacts prove anti-lookahead behavior and artifact verification without credentials; committed config
   canary remains zero-submit and production `artifacts/backtests/` contains only `.gitkeep`.
2. **Historical artifact complete:** an explicitly run historical backtest over the pinned data manifest produces
   the reviewed `directional.momentum_v1` artifact. If credentials or sufficient historical data are unavailable,
   tier 2 is a written deferral and no paper-eligible real strategy artifact is committed.

M7 is closed only when the artifact state is explicit: either a reviewed v2 artifact exists and passes, or the
artifact is deferred and the strategy remains unable to open.

## 9. Verification commands

Targeted offline block:

```bash
python3 -m unittest \
  tests.agent.test_backtest_gate_m7 \
  tests.agent.test_orchestrator_m7_artifact_cache \
  tests.agent.test_backtest_engine_m7 \
  tests.agent.test_directional_momentum_m7 \
  tests.agent.test_m7_paper_phase_criteria \
  tests.agent.test_m7_backtest_cli \
  tests.agent.test_m7_s9_integration
```

Affected regression block:

```bash
python3 -m unittest \
  tests.agent.test_execution_preflight_m5 \
  tests.agent.test_synthetic_isolation \
  tests.agent.test_synthetic_e2e \
  tests.agent.test_config_canary \
  tests.agent.test_orchestrator
```

Full suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .
```

Static checks:

```bash
git diff --check
python3 -m compileall scripts tests
```

## 10. Revision log

- rev 1, 2026-06-13: review hardening. Contract now matches the offline-complete/artifact-deferred closeout,
  pins threshold floors in the verifier/evaluator, quarantines fixture artifacts to temp dirs, and assigns S9
  artifact authority to orchestrator/preflight.
- rev 0, 2026-06-13: initial draft contract from current repo seams after M6 closeout.
