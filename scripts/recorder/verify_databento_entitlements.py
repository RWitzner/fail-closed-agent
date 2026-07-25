"""Databento entitlement verifier (contract §K, §P 2a).

Offline mode reads a FAKED ``list-schemas`` JSON (no network, no creds) and turns a
``planned_matrix`` into a ``verified_matrix``. No silent fallback: an unavailable
``(dataset, schema)`` WITHOUT a downgrade note is a hard failure
(``UnverifiableSchema``).

2026-06-09 access constraint: the provisioned key (``.secrets/databento.json``)
entitles HISTORICAL data only; live realtime is a separate
paid subscription, NOT provisioned. The ``verified_matrix`` carries a per-cell
``access`` field and a top-level ``live_subscription: "pending"``.

Credentialed (``--live``) mode is the M1 tier-2 HISTORICAL-verified deliverable
(§P 2a). Given a planned matrix it reproduces, against the REAL Databento Historical
API:
  * schema availability per (dataset, schema) — ``metadata.list_schemas``;
  * each dataset's coverage range — ``metadata.get_dataset_range``;
  * a tiny ``get_cost`` preview for the load-bearing cells — ``metadata.get_cost``;
  * entitlement-by-sample-pull — a tiny ``timeseries.get_range`` per AVAILABLE
    selected cell (§957-963): asserts >=1 record and a minimal decode sanity (each
    record's price is an int 1e-9 fixed-point convertible to Decimal with NO float;
    for ``mbp-10`` each record carries the full 10-level book = the XNAS.ITCH REPLACE
    structural property). Only a REDACTED SUMMARY is recorded (record_count + flags),
    never raw licensed data.
It assembles the ``verified_matrix`` with per-cell ``{available, access,
dataset_range, sample_cost_usd?, sample_record_count?, sample_decode_ok?,
sample_levels?}``.

SCOPE (honest claim): this tool reproduces schema/range/cost availability +
entitlement-by-sample-pull + the ``mbp-10`` 10-level/REPLACE *structural* check. A
FULL decode through the project's own parser/``book_state`` (byte-level book
reconstruction + hash) remains a TRACKED tier-2b / live item — it is NOT what this
tool asserts.

The ``databento`` SDK import is LAZY (inside the live path only) so
``tests/agent/test_no_network_no_creds.py`` stays green; the assembly + sample +
downgrade logic is testable OFFLINE via an injected (mocked) ``client``.
"""
import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# M4: this is a standalone CLI entrypoint. The test suite bootstraps `<repo>/scripts`
# onto sys.path (tests/__init__.py + conftest.py), but running this file directly
# (the documented invocation) has no such bootstrap, so `import agent...` fails with
# ModuleNotFoundError. Prepend `<repo>/scripts` here, BEFORE the agent import, so the
# documented command works standalone. `parents[1]` of scripts/recorder/<file>.py IS
# the `scripts/` dir (mirrors the ROOT/scripts convention; repo root is NOT added).
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from agent.serializer import dumps as _dumps  # noqa: E402 (after sys.path bootstrap)

# Path to the historical-only key (git-ignored). Read lazily inside the live path.
SECRETS_PATH = Path(__file__).resolve().parents[2] / ".secrets" / "databento.json"


@dataclass(frozen=True)
class PlannedCell:
    """MINOR 7: the verify() input element is a PlannedCell dataclass, not a bare tuple."""
    dataset: str   # placeholder code OK offline, e.g. "EQUS.MINI"
    schema: str    # e.g. "tbbo"
    use: str       # "L1_nbbo" | "L2_depth" | "status" | ...


@dataclass(frozen=True)
class VerifiedCell:
    dataset: str
    schema: str
    use: str
    available: bool                 # True iff schema present in this dataset's list-schemas
    access: str                     # "historical" | "live" | "both" (offline default "historical")
    downgrade: Optional[str]        # REQUIRED non-None when available is False, else UnverifiableSchema
    # Live-only enrichment (§P 2a). None offline / for unavailable cells.
    dataset_range: Optional[Dict[str, str]] = None   # {"start","end"} from get_dataset_range
    sample_cost_usd: Optional[str] = None            # Decimal-as-string get_cost preview (NEVER float)
    # H3 entitlement-by-sample-pull SUMMARY (REDACTED — counts/flags only, never raw
    # licensed data). None unless this cell was sampled via timeseries.get_range.
    sample_record_count: Optional[int] = None        # >=1 records returned by the tiny get_range pull
    sample_decode_ok: Optional[bool] = None          # each record's price is int 1e-9 fixed-point (no float)
    sample_levels: Optional[int] = None              # populated book levels (mbp-10 => 10; the REPLACE property)


@dataclass(frozen=True)
class VerifiedMatrix:
    cells: Tuple[VerifiedCell, ...]
    all_available: bool
    downgrades: Tuple[VerifiedCell, ...]
    live_subscription: str          # 2026-06-09: top-level "pending" offline (live not provisioned)


class UnverifiableSchema(RuntimeError):
    """An unavailable (dataset,schema) was left without an explicit downgrade note (no-silent-fallback)."""


def planned_matrix() -> Tuple[PlannedCell, ...]:
    """The pinned M1 placeholder matrix as PlannedCell instances.

    EQUS.MINI L1 schemas + a depth-dataset mbp-10 + status (downgraded).
    Hardcoded/offline. Depth dataset code stays the placeholder string
    ``'<DEPTH_DATASET>'`` (the real entitled code is a tier-2
    historical-verified deliverable, §P 2a).
    """
    return (
        PlannedCell("EQUS.MINI", "tbbo", "L1_nbbo"),
        PlannedCell("EQUS.MINI", "mbp-10", "L2_depth"),
        PlannedCell("EQUS.MINI", "status", "status"),
        PlannedCell("<DEPTH_DATASET>", "mbp-10", "L2_depth"),
    )


def list_schemas_offline(response_path) -> Dict[str, List[str]]:
    """Pure file read of the FAKED list-schemas fixture: {dataset: [schema, ...]}.
    No client, no creds.
    """
    path = Path(response_path)
    return json.loads(path.read_text(encoding="utf-8"))


def verify(
    planned: Iterable[PlannedCell],
    schemas_by_dataset: Dict[str, List[str]],
    downgrades: Optional[Dict[Tuple[str, str], str]] = None,
    access_by_cell: Optional[Dict[Tuple[str, str], str]] = None,
) -> VerifiedMatrix:
    """MINOR 7: ``planned`` is an iterable of PlannedCell.

    For each cell:
      available = cell.schema in schemas_by_dataset.get(cell.dataset, []).
      access    = access_by_cell.get((cell.dataset, cell.schema), 'historical').
      An unavailable cell with no registered downgrade -> UnverifiableSchema
      (no silent fallback).

    ``downgrades`` is keyed by (dataset, schema) tuples (aligned to
    PlannedCell-derived keys; MINOR 7).

    Asserts EQUS.MINI has NO 'mbp-10' and NO 'status' (the verified-2026-06-08 fact)
    so a regression that silently maps depth onto EQUS.MINI fails loudly. Sets
    live_subscription='pending' (live not provisioned).
    """
    if downgrades is None:
        downgrades = {}
    if access_by_cell is None:
        access_by_cell = {}

    # Verified fact: EQUS.MINI must NOT have mbp-10 or status.
    equs_schemas = set(schemas_by_dataset.get("EQUS.MINI", []))
    if "mbp-10" in equs_schemas:
        raise UnverifiableSchema(
            "EQUS.MINI unexpectedly has 'mbp-10' — regression against verified-2026-06-08 matrix"
        )
    if "status" in equs_schemas:
        raise UnverifiableSchema(
            "EQUS.MINI unexpectedly has 'status' — regression against verified-2026-06-08 matrix"
        )

    cells: List[VerifiedCell] = []
    for cell in planned:
        dataset_schemas = schemas_by_dataset.get(cell.dataset, [])
        available = cell.schema in dataset_schemas
        access = access_by_cell.get((cell.dataset, cell.schema), "historical")
        downgrade_note = downgrades.get((cell.dataset, cell.schema))

        if not available and downgrade_note is None:
            raise UnverifiableSchema(
                f"({cell.dataset!r}, {cell.schema!r}) is unavailable but has no downgrade note "
                "(no-silent-fallback; add a downgrade entry or remove from planned_matrix)"
            )

        cells.append(VerifiedCell(
            dataset=cell.dataset,
            schema=cell.schema,
            use=cell.use,
            available=available,
            access=access,
            downgrade=downgrade_note,
        ))

    cells_tuple = tuple(cells)
    all_available = all(c.available for c in cells_tuple)
    downgrades_tuple = tuple(c for c in cells_tuple if not c.available)

    return VerifiedMatrix(
        cells=cells_tuple,
        all_available=all_available,
        downgrades=downgrades_tuple,
        live_subscription="pending",
    )


def _cell_payload(c: VerifiedCell) -> Dict[str, Any]:
    """Canonical per-cell dict. Live enrichment fields are emitted only when present
    so the offline artifact stays byte-identical to the pre-tier-2 shape.
    """
    payload: Dict[str, Any] = {
        "dataset": c.dataset,
        "schema": c.schema,
        "use": c.use,
        "available": c.available,
        "access": c.access,
        "downgrade": c.downgrade,
    }
    if c.dataset_range is not None:
        payload["dataset_range"] = c.dataset_range
    if c.sample_cost_usd is not None:
        payload["sample_cost_usd"] = c.sample_cost_usd
    if c.sample_record_count is not None:
        payload["sample_record_count"] = c.sample_record_count
    if c.sample_decode_ok is not None:
        payload["sample_decode_ok"] = c.sample_decode_ok
    if c.sample_levels is not None:
        payload["sample_levels"] = c.sample_levels
    return payload


def write_artifact(verified: VerifiedMatrix, out_path) -> None:
    """Write the verified_matrix via agent.serializer.dumps (Decimal-safe, sorted, canonical).

    Includes the per-cell ``access`` field and top-level ``live_subscription``. Live cells
    additionally carry ``dataset_range`` and ``sample_cost_usd`` (Decimal-as-string).
    """
    path = Path(out_path)
    payload = {
        "live_subscription": verified.live_subscription,
        "all_available": verified.all_available,
        "cells": [_cell_payload(c) for c in verified.cells],
        "downgrades": [_cell_payload(c) for c in verified.downgrades],
    }
    path.write_text(_dumps(payload) + "\n", encoding="utf-8")


def credentialed_planned_matrix() -> Tuple[PlannedCell, ...]:
    """The REAL M1 tier-2 (2a) planned matrix with entitled dataset codes.

    L1 (NBBO/bars/signals) = EQUS.MINI; primary NBBO schema = ``tbbo`` (verified
    2026-06-08 against the live API). L2 depth = ``XNAS.ITCH`` (Nasdaq TotalView),
    whose ``mbp-10`` carries a FULL 10-level book per record (snapshot/replace-on-apply,
    1003/1003 records had 10 populated levels) — DBEQ.BASIC was REJECTED for depth
    (consolidated ``mbp-10`` carried only 1 populated level on ~598/604 records).
    ``status`` is NOT on EQUS.MINI; halt/LULD/SSR downgrades to broker + calendar.

    NOTE: the EQUS.MINI definition schema is singular ``definition`` (the offline
    FAKE fixture uses the plural ``definitions``; the live list_schemas confirms the
    singular).
    """
    return (
        PlannedCell("EQUS.MINI", "tbbo", "L1_nbbo"),
        PlannedCell("EQUS.MINI", "bbo-1s", "L1_nbbo_1s"),
        PlannedCell("EQUS.MINI", "bbo-1m", "L1_nbbo_1m"),
        PlannedCell("EQUS.MINI", "trades", "trades"),
        PlannedCell("EQUS.MINI", "ohlcv-1s", "bars_1s"),
        PlannedCell("EQUS.MINI", "ohlcv-1m", "bars_1m"),
        PlannedCell("EQUS.MINI", "definition", "definitions"),
        PlannedCell("EQUS.MINI", "mbp-10", "L2_depth"),
        PlannedCell("EQUS.MINI", "status", "status"),
        PlannedCell("XNAS.ITCH", "mbp-10", "L2_depth"),
    )


def credentialed_downgrades() -> Dict[Tuple[str, str], str]:
    """Downgrade notes for the credentialed matrix (no silent fallback).

    EQUS.MINI lacks depth and status; both must carry an explicit written downgrade.
    DBEQ.BASIC rejection rationale is recorded against the EQUS.MINI depth downgrade.
    """
    return {
        ("EQUS.MINI", "mbp-10"): (
            "depth -> XNAS.ITCH (Nasdaq TotalView); mbp-10 = FULL 10-level book per "
            "record (replace-on-apply). DBEQ.BASIC rejected: consolidated mbp-10 had "
            "only 1 populated level on ~598/604 records (REPLACE would corrupt the book). "
            "Scope note: XNAS.ITCH is single-venue (Nasdaq-listed names) -> depth-aware "
            "universe downgrade."
        ),
        ("EQUS.MINI", "status"): (
            "status -> broker (Alpaca) + exchange_calendars (M2); EQUS.MINI has no "
            "status schema (halt/LULD/SSR fail-closed via broker/calendar downgrade)."
        ),
    }


def _historical_client():
    """LAZY: import the ``databento`` SDK and build a Historical client from the
    git-ignored historical-only key. Called ONLY on the live path; no offline test
    reaches it, so ``databento`` never enters ``sys.modules`` offline.
    """
    import databento  # noqa: WPS433  (lazy by contract — keeps test_no_network_no_creds green)

    api_key = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))["api_key"]
    return databento.Historical(api_key)


# Number of book levels mbp-10 must carry per record (the XNAS.ITCH REPLACE property).
_MBP10_REQUIRED_LEVELS = 10
# databento_dbn fixed-point: prices are int scaled by 1e9 (1_000_000_000).
_DBN_UNDEF_PRICE = 9223372036854775807  # databento_dbn.UNDEF_PRICE sentinel


def _decode_sample_summary(records: List[Any], schema: str) -> Dict[str, Any]:
    """H3 decode sanity over a tiny get_range pull. Returns a REDACTED summary
    (counts + structural flags only — NEVER any raw licensed price/size).

    Each record's ``price`` must be an int 1e-9 fixed-point convertible to Decimal
    with NO float (bool is rejected — a bool is an int subclass). For ``mbp-10`` each
    record must carry the full 10-level book (the REPLACE property). Raises
    UnverifiableSchema on an empty pull or a failed decode/structure check.
    """
    if not records:
        raise UnverifiableSchema(
            f"sample pull for schema {schema!r} returned ZERO records "
            "(entitlement-by-sample-pull requires >=1)"
        )

    levels_seen: Optional[int] = None
    for rec in records:
        price = getattr(rec, "price", None)
        # bool is an int subclass; reject it explicitly. Reject any non-int (float).
        if isinstance(price, bool) or not isinstance(price, int):
            raise UnverifiableSchema(
                f"sample record for schema {schema!r} has a non-int price "
                f"(type {type(price).__name__}); expected int 1e-9 fixed-point, NO float"
            )
        # Convertible to Decimal as a fixed-point value (no float ever constructed).
        if price != _DBN_UNDEF_PRICE:
            _ = Decimal(price) / Decimal(1_000_000_000)

        if schema == "mbp-10":
            levels = getattr(rec, "levels", None) or []
            populated = sum(
                1 for lvl in levels
                if getattr(lvl, "bid_px", _DBN_UNDEF_PRICE) != _DBN_UNDEF_PRICE
                or getattr(lvl, "ask_px", _DBN_UNDEF_PRICE) != _DBN_UNDEF_PRICE
            )
            if populated < _MBP10_REQUIRED_LEVELS:
                raise UnverifiableSchema(
                    f"sample record for schema {schema!r} has only {populated} populated "
                    f"book level(s); the REPLACE property requires {_MBP10_REQUIRED_LEVELS} "
                    "(XNAS.ITCH full-depth check)"
                )
            levels_seen = _MBP10_REQUIRED_LEVELS

    summary: Dict[str, Any] = {
        "sample_record_count": len(records),
        "sample_decode_ok": True,
    }
    if levels_seen is not None:
        summary["sample_levels"] = levels_seen
    return summary


def verify_credentialed(
    planned: Iterable[PlannedCell],
    downgrades: Optional[Dict[Tuple[str, str], str]] = None,
    *,
    client: Any = None,
    cost_window: Optional[Tuple[str, str]] = None,
    cost_symbols: Optional[Tuple[str, ...]] = None,
    cost_for: Optional[Iterable[Tuple[str, str]]] = None,
    sample_window: Optional[Tuple[str, str]] = None,
    sample_symbols: Optional[Tuple[str, ...]] = None,
    sample_for: Optional[Iterable[Tuple[str, str]]] = None,
) -> VerifiedMatrix:
    """M1 tier-2 (2a) HISTORICAL-verified credentialed mode (§P 2a).

    Hits the REAL Databento Historical metadata API to confirm per-(dataset,schema)
    availability via ``client.metadata.list_schemas(dataset=...)``, records each
    dataset's ``get_dataset_range`` and a small ``get_cost`` preview for selected
    cells. The assembly + pollution-guard + downgrade logic is shared with the
    offline ``verify()`` (same no-silent-fallback contract); availability is then
    re-derived from the live ``list_schemas`` response.

    H3 (entitlement-by-sample-pull): when ``sample_window``/``sample_for`` are given,
    performs a tiny ``timeseries.get_range`` pull per selected AVAILABLE cell, asserts
    >=1 record, and records a REDACTED decode summary (record_count + decode_ok flag,
    plus the 10-level structural flag for mbp-10 = the REPLACE property) — NEVER the
    raw licensed data. A failed decode/structure check raises UnverifiableSchema.

    The ``databento`` SDK import is LAZY (only when ``client is None``) so offline
    tests can inject a MOCKED client and run with no network and no credential read.

    Args:
      planned: PlannedCell instances (credentialed_planned_matrix()).
      downgrades: (dataset,schema)->note (credentialed_downgrades()).
      client: injected Databento Historical-like client; built lazily if None.
      cost_window: (start, end) ISO strings for the get_cost preview.
      cost_symbols: tuple of symbols for the cost preview (e.g. ("AAPL","MSFT")).
      cost_for: which (dataset,schema) cells to price; defaults to none.
      sample_window: (start, end) ISO strings for the tiny get_range sample pull.
      sample_symbols: tuple of symbols for the sample pull (e.g. ("AAPL",)).
      sample_for: which (dataset,schema) cells to sample; defaults to none.

    Returns a VerifiedMatrix with every available cell access='historical',
    per-cell dataset_range, optional sample_cost_usd (Decimal-as-string), an optional
    REDACTED sample summary (sample_record_count/sample_decode_ok/sample_levels), and
    top-level live_subscription='pending' (live realtime is the deferred paid
    subscription, §P 2b).
    """
    if downgrades is None:
        downgrades = {}
    if client is None:
        client = _historical_client()

    planned = tuple(planned)
    metadata = client.metadata

    # One list_schemas + get_dataset_range call per distinct dataset.
    datasets = []
    for cell in planned:
        if cell.dataset not in datasets:
            datasets.append(cell.dataset)

    schemas_by_dataset: Dict[str, List[str]] = {}
    range_by_dataset: Dict[str, Dict[str, str]] = {}
    for ds in datasets:
        schemas_by_dataset[ds] = list(metadata.list_schemas(dataset=ds))
        rng = metadata.get_dataset_range(dataset=ds)
        # Keep only the canonical start/end strings (metadata-only; deterministic).
        range_by_dataset[ds] = {
            "start": str(rng["start"]),
            "end": str(rng["end"]),
        }

    # Reuse the shared assembly + §K pollution guard + no-silent-fallback contract.
    base = verify(planned, schemas_by_dataset, downgrades=downgrades)

    # Optional get_cost preview for selected available cells (Decimal-as-string).
    cost_targets = set(cost_for or ())
    cost_by_cell: Dict[Tuple[str, str], str] = {}
    if cost_targets and cost_window is not None and cost_symbols is not None:
        start, end = cost_window
        for ds, schema in cost_targets:
            cost_float = metadata.get_cost(
                dataset=ds,
                start=start,
                end=end,
                symbols=list(cost_symbols),
                schema=schema,
            )
            # NEVER store the float: round-trip through str -> Decimal -> str.
            cost_by_cell[(ds, schema)] = str(Decimal(str(cost_float)))

    # H3: optional entitlement-by-sample-pull for selected AVAILABLE cells. A tiny
    # timeseries.get_range pull -> decode sanity -> REDACTED summary (counts/flags only).
    available_cells = {(c.dataset, c.schema) for c in base.cells if c.available}
    sample_targets = set(sample_for or ())
    sample_by_cell: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if sample_targets and sample_window is not None and sample_symbols is not None:
        s_start, s_end = sample_window
        for ds, schema in sample_targets:
            if (ds, schema) not in available_cells:
                continue  # never sample an unavailable/downgraded cell
            store = client.timeseries.get_range(
                dataset=ds,
                start=s_start,
                end=s_end,
                symbols=list(sample_symbols),
                schema=schema,
            )
            records = list(store)
            sample_by_cell[(ds, schema)] = _decode_sample_summary(records, schema)

    # Enrich each cell with dataset_range (available cells) + sample_cost_usd + sample summary.
    enriched: List[VerifiedCell] = []
    for c in base.cells:
        dataset_range = range_by_dataset.get(c.dataset) if c.available else None
        sample_cost = cost_by_cell.get((c.dataset, c.schema))
        sample_summary = sample_by_cell.get((c.dataset, c.schema), {})
        enriched.append(VerifiedCell(
            dataset=c.dataset,
            schema=c.schema,
            use=c.use,
            available=c.available,
            access=c.access,
            downgrade=c.downgrade,
            dataset_range=dataset_range,
            sample_cost_usd=sample_cost,
            sample_record_count=sample_summary.get("sample_record_count"),
            sample_decode_ok=sample_summary.get("sample_decode_ok"),
            sample_levels=sample_summary.get("sample_levels"),
        ))

    enriched_tuple = tuple(enriched)
    return VerifiedMatrix(
        cells=enriched_tuple,
        all_available=base.all_available,
        downgrades=tuple(c for c in enriched_tuple if not c.available),
        live_subscription="pending",
    )


# get_cost preview defaults (§P 2a): tiny recent window, total cost must stay < ~$0.05.
_DEFAULT_COST_WINDOW = ("2026-06-08T15:00:00", "2026-06-08T15:00:02")
_DEFAULT_COST_SYMBOLS = ("AAPL", "MSFT")
# Price the two load-bearing cells: the primary NBBO source + the depth source.
_DEFAULT_COST_FOR = (("EQUS.MINI", "tbbo"), ("XNAS.ITCH", "mbp-10"))

# H3 sample-pull defaults (§957-963): a tiny ~1-2s window on a liquid name; the same
# two load-bearing cells (NBBO source + depth source). Total cost must stay < ~$0.05.
_DEFAULT_SAMPLE_WINDOW = ("2026-06-08T15:00:00", "2026-06-08T15:00:02")
_DEFAULT_SAMPLE_SYMBOLS = ("AAPL",)
_DEFAULT_SAMPLE_FOR = (("EQUS.MINI", "tbbo"), ("XNAS.ITCH", "mbp-10"))


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (argparse, fixed arg array, never shell=True).

    --offline --list-schemas <fixture> -> verified_matrix to stdout / --write-artifact.
    --live -> credentialed HISTORICAL verification against the real Databento API
    (reads .secrets/databento.json). Exit 0 iff every planned cell is available OR
    carries an explicit downgrade note; non-zero otherwise.
    """
    parser = argparse.ArgumentParser(description="Databento entitlement verifier")
    parser.add_argument("--offline", action="store_true",
                        help="Run in offline mode using a faked list-schemas JSON fixture")
    parser.add_argument("--live", action="store_true",
                        help="Run credentialed HISTORICAL verification (real API; reads .secrets/)")
    parser.add_argument("--list-schemas", metavar="PATH",
                        help="Path to the faked list-schemas JSON (required with --offline)")
    parser.add_argument("--write-artifact", metavar="OUT_PATH",
                        help="Write the verified_matrix artifact to this path")
    parser.add_argument("--no-cost", action="store_true",
                        help="Skip the get_cost preview in --live mode")
    parser.add_argument("--no-sample", action="store_true",
                        help="Skip the timeseries.get_range entitlement-by-sample-pull in --live mode")
    args = parser.parse_args(argv)

    if args.offline and args.live:
        parser.error("--offline and --live are mutually exclusive")

    if args.live:
        try:
            verified = verify_credentialed(
                credentialed_planned_matrix(),
                downgrades=credentialed_downgrades(),
                cost_window=None if args.no_cost else _DEFAULT_COST_WINDOW,
                cost_symbols=None if args.no_cost else _DEFAULT_COST_SYMBOLS,
                cost_for=None if args.no_cost else _DEFAULT_COST_FOR,
                sample_window=None if args.no_sample else _DEFAULT_SAMPLE_WINDOW,
                sample_symbols=None if args.no_sample else _DEFAULT_SAMPLE_SYMBOLS,
                sample_for=None if args.no_sample else _DEFAULT_SAMPLE_FOR,
            )
        except UnverifiableSchema as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if args.write_artifact:
            write_artifact(verified, args.write_artifact)
        else:
            print(_dumps({
                "live_subscription": verified.live_subscription,
                "all_available": verified.all_available,
                "cell_count": len(verified.cells),
                "downgrade_count": len(verified.downgrades),
            }))
        return 0 if verified.all_available or all(
            c.downgrade is not None for c in verified.downgrades
        ) else 1

    if args.offline:
        if not args.list_schemas:
            parser.error("--list-schemas is required with --offline")
        schemas_by_dataset = list_schemas_offline(args.list_schemas)
        planned = planned_matrix()
        downgrades = {
            ("EQUS.MINI", "mbp-10"): "depth -> <DEPTH_DATASET>",
            ("EQUS.MINI", "status"): "status -> broker (Alpaca) + exchange_calendars (M2)",
        }
        try:
            verified = verify(planned, schemas_by_dataset, downgrades=downgrades)
        except UnverifiableSchema as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if args.write_artifact:
            write_artifact(verified, args.write_artifact)
        else:
            print(_dumps({
                "live_subscription": verified.live_subscription,
                "all_available": verified.all_available,
                "cell_count": len(verified.cells),
                "downgrade_count": len(verified.downgrades),
            }))
        return 0 if verified.all_available or all(
            c.downgrade is not None for c in verified.downgrades
        ) else 1

    parser.error("one of --offline or --live is required")
    return 2  # pragma: no cover (parser.error exits)


if __name__ == "__main__":  # pragma: no cover (CLI entrypoint, exercised via main())
    sys.exit(main())
