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

M1 replaces placeholders with **exact dataset codes after per-`(dataset, schema)` entitlement verification**. EQUS.MINI is an L1 top-of-book composite (verified 2026-06-08): it has **no `mbp-10`** and **no `status`** schema. Each schema is bound to a specific dataset — no single mixed query.

| Use | Dataset (pin exact code) | Required schemas | Verification |
|-----|--------------------------|------------------|--------------|
| L1 signals / bars / NBBO | `EQUS.MINI` (or equivalent L1 composite) | `tbbo` (primary NBBO), `bbo-1s`, `bbo-1m`, `trades`, `ohlcv-1s`, `ohlcv-1m`, `definitions` | `--dataset EQUS.MINI` list-schemas + one-symbol sample |
| L2 depth-aware modeled fills | depth-capable US equities dataset (pin exact) | `mbp-10` | separate `--dataset <depth>` list-schemas + one-symbol sample |
| L3 queue-position (optional upgrade) | venue-native ITCH-class (if entitled) | `mbo` | `--dataset <venue>` + queue-position fixture |
| halt/LULD/SSR status | **broker (Alpaca) + `exchange_calendars`** (EQUS.MINI has no `status`) | n/a (status feed) | record the downgrade explicitly; primary halt source is broker/calendar for M1 |

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

1. **Offline-complete:** the offline command above exits 0; replay re-derives expected hashes from fixtures; matrix placeholders + any downgrade notes (e.g. status → broker/calendar) are written.
2. **Entitlement-verified (requires credentials):** the dataset-scoped artifacts record exact dataset IDs, schemas, **per-`(dataset,schema)` availability**, sequence-number behavior, and timestamps; any unsupported schema is explicitly downgraded or moved to a later milestone in writing.

M1 is not fully done until tier 2 runs — Databento account provisioning is an explicit blocker, not an optional footnote.
