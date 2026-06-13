# M7b Strategy Hardening Contract

- **Date:** 2026-06-13
- **Status:** Active hardening loop after M7 offline closeout.
- **Parent:** `docs/superpowers/specs/2026-06-13-M7-backtest-gate-contract.md`

## Purpose

M7 proved the anti-lookahead backtest gate and shipped `directional.momentum_v1`, but the reviewed historical
artifact attempts failed the pinned criteria. M7b is the next bounded loop: create a new strategy version with a
predeclared hypothesis and run it through the same historical reviewed-artifact path. This contract does not relax
M7 paper/M8 criteria and does not authorize paper or live trading.

## Hard Constraints

- `directional.momentum_v1` remains compatible. Existing artifacts and tests keep their v1 strategy id.
- No threshold tuning in `paper_phase_criteria.py` or artifact payload thresholds.
- No production write to `artifacts/backtests/` unless the reviewed historical artifact criteria pass and the
  existing explicit reviewed-artifact flag is present.
- `config/agent_rules.json` and `config/risk_rules.json` committed run/live gates stay off.
- `artifacts/backtests/` remains `.gitkeep` only if the reviewed v2 replay fails.
- Historical evidence must be bound to a predeclared manifest/universe; no post-run symbol cherry-picking.

## Strategy Hypothesis

`directional.momentum_v2` tests whether v1's failure was primarily overtrading weak momentum and paying too much
spread/latency cost. It is still long-only and still pure, but emits candidates only when all of these are true:

- M3 feature snapshot is available.
- `momentum_9`, `momentum_21`, `ema_gap_9_21`, and `sma_gap_21_50` are all positive.
- `z_ret_21` is positive enough to confirm momentum but not extreme enough to chase an overextended bar.
- `realized_vol_21` is positive.
- Quote verdict has a finite positive mid and spread no wider than the v2 cap.
- Edge score is based on the weakest aligned trend feature after a fixed buffer, and must remain positive.

The output remains at most one whole-share BUY candidate per scan, with `paper_eligible=True`; S9 artifact
verification remains the source of truth before any broker submit.

## Historical Builder Contract

The historical artifact builder accepts an explicit `strategy_id`, defaulting to `directional.momentum_v1` for
compatibility. Supported ids are registered in `agent.strategies.directional_momentum.strategy_for_id()`. Unknown
ids fail before backtest execution. Artifact payloads and filenames use the selected strategy id.

## Stop Rule

Run v2 against the existing local broader and holdout historical manifests. If no symbol verifies `ok`, write a
failure review and leave production artifacts unchanged. Paper edge-validation and M8 remain blocked.
