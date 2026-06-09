# M1 — Data Tier Implementation Plan

- **Date:** 2026-06-08
- **Status:** Draft plan for review (reconciled with adversarial multi-lens review 2026-06-08)
- **Parent spec:** `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`
- **Milestone goal:** prove the Databento transport/recorder/replay stack with **dataset-scoped, entitlement-verified** schema behavior before any strategy uses live data. Data quality is the load-bearing risk (spec §14).

## Scope

Data-plane only:

- `MarketDataTransport` implementation for Databento behind the injectable transport seam (same name/contract as M0).
- Recorder event parser per Databento schema (top-of-book, trades, bars, depth, definitions; status only where entitled).
- Equity `book_hash` (rewritten for L2 MBP-10 depth ladders — the derived-state hash `replay.py` depends on).
- Bar cache + **deterministic ET-session-boundary resampler** (spec §11 timezone policy).
- Dataset/schema/entitlement verification tool — **offline-testable** + a credentialed live mode.
- Always-on reconnect with backoff cap + `data_quality_alert` on prolonged disconnect (not silent termination).
- Sequence/heartbeat/gap handling keyed to each dataset's actual sequencing semantics.
- Replay/reconcile harness against recorded fixtures + (credentialed) Databento historical pulls.

**Not in M1 (moved/owned elsewhere):** market calendar + session/halt state machine → **M2**; full `execution_preflight`/S4 → **M5** (M1 only produces the freshness/epoch/gap *inputs*).

## Dataset / schema matrix contract (dataset-scoped)

M1 starts from a `planned_matrix` (placeholder dataset codes, written offline) and produces a `verified_matrix` with **exact dataset codes after per-`(dataset, schema)` entitlement verification**. EQUS.MINI is an L1 top-of-book composite (verified 2026-06-08): it has **no `mbp-10`** and **no `status`** schema. Each schema is bound to a specific dataset — no single mixed query.

**VERIFIED against the live Historical API 2026-06-09** (artifact `reports/databento_entitlements/verified_matrix.json`, gitignored/reproducible; `access=historical`, `live_subscription=pending`):

| Use | Dataset (verified code) | Required schemas | Status |
|-----|--------------------------|------------------|--------------|
| L1 signals / bars / NBBO | `EQUS.MINI` | `tbbo` (primary NBBO), `bbo-1s`, `bbo-1m`, `trades`, `ohlcv-1s`, `ohlcv-1m`, **`definition`** (singular, NOT `definitions`) | ✓ all entitled (historical) |
| L2 depth-aware modeled fills | **`XNAS.ITCH`** (Nasdaq TotalView) | `mbp-10` | ✓ entitled; **REPLACE-per-record** (each record = full post-event top-10 book; `UNDEF_PRICE` = empty level — confirmed empirically + Databento docs). `DBEQ.BASIC` **rejected**: its consolidated `mbp-10` carried only 1 populated level on ~598/604 records. **Scope note:** XNAS.ITCH is single-venue (Nasdaq-listed names) → depth-aware universe downgrade. |
| L3 queue-position (optional upgrade) | `XNAS.ITCH` (`mbo`, entitled back to 2018) | `mbo` | deferred (optional) |
| halt/LULD/SSR status | **broker (Alpaca) + `exchange_calendars`** (EQUS.MINI has no `status`) | n/a (status feed) | downgrade recorded; primary halt source is broker/calendar for M1 |

Live-transport note (for the credentialed/live DBN decoder): raw DBN prices are **int 1e-9 fixed-point** (e.g. `315030000000` = `315.03`) → convert to **Decimal exactly, never float** (do NOT use `to_df()` which yields floats); level fields are `bid_px_00..09`/`ask_px_00..09`/`*_sz_*`/`*_ct_*`; empty levels are `UNDEF_PRICE`; carry `action`/`side`/`depth`/`sequence`/`flags`.

Rule: if a candidate dataset lacks a required schema, M1 must **either** pin another dataset **or** downgrade the feature in writing (e.g. status → broker/calendar). **No silent fallback.**

## Files to create

- `scripts/agent/marketdata/databento.py`
- `scripts/recorder/recorder.py`, `scripts/recorder/event.py`, `scripts/recorder/book_state.py`
- `scripts/recorder/book_hash.py` (L2 depth-ladder rewrite; the hash `replay.py` imports)
- `scripts/recorder/persistence.py`, `scripts/recorder/replay.py`, `scripts/recorder/reconcile.py`, `scripts/recorder/status.py`
- `scripts/recorder/verify_databento_entitlements.py`
- `tests/recorder/test_databento_event_parser.py`
- `tests/recorder/test_sequence_gap_detection.py`
- `tests/recorder/test_replay_hashes.py` (binds explicitly to `book_hash`)
- `tests/recorder/test_bar_cache.py` (incl. ET/DST boundary + empty-window VWAP NaN/Inf rejection)
- `tests/recorder/test_transport_fakes.py`
- `tests/recorder/test_entitlement_verifier.py` (offline; faked list-schemas response)
- `tests/recorder/test_reconnect_alert.py` (sustained disconnect → `data_quality_alert`, not silent exit)

## Fixtures

- `tests/fixtures/databento/equs_mini_tbbo_sample.jsonl`, `tests/fixtures/databento/equs_mini_sequence_zero_sample.jsonl`
- `tests/fixtures/databento/mbp10_depth_sample.jsonl`
- `tests/fixtures/databento/flaky_transport_gap.jsonl`
- `tests/fixtures/databento/list_schemas_response.json` (drives the offline entitlement-verifier test)
- `tests/fixtures/databento/replay_expected_hashes.json`
- `tests/fixtures/bars/dst_boundary_events.jsonl` (straddles an ET session close + DST transition)

## Safety invariants covered

| Invariant | Test target |
|-----------|-------------|
| S3 replay hashes (+ `book_hash` dependency) | `tests/recorder/test_replay_hashes.py` |
| S4 **inputs only** (freshness/epoch/gap for preflight; full S4 in M5) | `tests/recorder/test_sequence_gap_detection.py` (+ M5 preflight tests) |
| S6 **contributor** (event correlation metadata; S6 owned by M0 journal tests) | `tests/recorder/test_databento_event_parser.py` |
| S2 re-verify (empty-window VWAP NaN/Inf) | `tests/recorder/test_bar_cache.py::test_empty_window_vwap_rejects_naninf` |

## Verification commands

Offline (no credentials):

```bash
python3 -m unittest \
  tests.recorder.test_databento_event_parser \
  tests.recorder.test_sequence_gap_detection \
  tests.recorder.test_replay_hashes \
  tests.recorder.test_bar_cache \
  tests.recorder.test_transport_fakes \
  tests.recorder.test_entitlement_verifier \
  tests.recorder.test_reconnect_alert
```

Entitlement check (only when credentials are intentionally available in `.secrets/`), **dataset-scoped**:

```bash
python3 scripts/recorder/verify_databento_entitlements.py \
  --dataset EQUS.MINI --schemas tbbo,bbo-1s,bbo-1m,trades,ohlcv-1s,ohlcv-1m,definitions \
  --symbols AAPL,MSFT --write-artifact reports/databento_entitlements/equs_mini.json
python3 scripts/recorder/verify_databento_entitlements.py \
  --dataset <DEPTH_DATASET> --schemas mbp-10 \
  --symbols AAPL,MSFT --write-artifact reports/databento_entitlements/depth.json
```

## Stop condition

M1 has **two acceptance tiers**:

1. **Offline-complete:** the offline command above exits 0; replay re-derives expected hashes from fixtures; the `planned_matrix` + any downgrade notes (e.g. status → broker/calendar) are written.
2. **Entitlement-verified.** Split by access (the provisioned key is historical-only; live realtime is an unprovisioned paid subscription):
   - **(2a) Historical-verified — DONE 2026-06-09.** Ran the credentialed verifier against the live Historical API; the `verified_matrix` (`reports/databento_entitlements/verified_matrix.json`) records exact dataset IDs, per-`(dataset,schema)` availability, ranges, and sample costs. Resolved: L1 = `EQUS.MINI` (`tbbo`/`bbo`/`trades`/`ohlcv`/`definition`), L2 depth = `XNAS.ITCH` `mbp-10` (REPLACE-per-record confirmed; `DBEQ.BASIC` rejected), status → broker/calendar downgrade, `live_subscription=pending`. No silent fallback.
   - **(2b) Live-verified — DEFERRED.** Real live-gateway reconnect/heartbeat/snapshot behavior; blocked on the paid live subscription (not provisioned). Reconnect/gap logic is tested against fixtures in M1 tier-1; real-gateway validation lands when the subscription exists.

Tier 2a is complete; tier 2b is an explicit, written deferral (not a silent footnote).
