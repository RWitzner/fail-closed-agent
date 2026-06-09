"""Offline entitlement verifier (contract §K).

Offline mode reads a FAKED ``list-schemas`` JSON (no network, no creds) and turns a
``planned_matrix`` into a ``verified_matrix``. No silent fallback: an unavailable
``(dataset, schema)`` WITHOUT a downgrade note is a hard failure
(``UnverifiableSchema``).

2026-06-09 access constraint: the provisioned key (``.secrets/databento.json``,
user_id ``<databento-user-id>``) entitles HISTORICAL data only; live realtime is a separate
paid subscription, NOT provisioned. The ``verified_matrix`` carries a per-cell
``access`` field and a top-level ``live_subscription: "pending"``.

Credentialed mode is a ``NotImplementedError`` stub never reached offline.
"""
import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from agent.serializer import dumps as _dumps


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


def write_artifact(verified: VerifiedMatrix, out_path) -> None:
    """Write the verified_matrix via agent.serializer.dumps (Decimal-safe, sorted, canonical).

    Includes the per-cell ``access`` field and top-level ``live_subscription``.
    """
    path = Path(out_path)
    payload = {
        "live_subscription": verified.live_subscription,
        "all_available": verified.all_available,
        "cells": [
            {
                "dataset": c.dataset,
                "schema": c.schema,
                "use": c.use,
                "available": c.available,
                "access": c.access,
                "downgrade": c.downgrade,
            }
            for c in verified.cells
        ],
    }
    path.write_text(_dumps(payload) + "\n", encoding="utf-8")


def verify_credentialed(cfg) -> VerifiedMatrix:
    """Tier-2 STUB: lazily ``import databento``, read ``.secrets/databento.json``, call
    the HISTORICAL list-schemas API. Records each verified cell access='historical' and
    live_subscription='pending' (live realtime is the deferred paid subscription, §P 2b).
    Raises NotImplementedError in M1 offline scope and is NEVER reached by an offline test.
    """
    raise NotImplementedError(
        "verify_credentialed lands in M1 tier-2 (historical key required; live is deferred)"
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (argparse, fixed arg array, never shell=True).

    --offline --list-schemas <fixture> -> verified_matrix to stdout / --write-artifact.
    Without --offline -> verify_credentialed (stub). Exit 0 iff every planned cell is
    available OR carries an explicit downgrade note; non-zero otherwise.
    """
    parser = argparse.ArgumentParser(description="Databento entitlement verifier")
    parser.add_argument("--offline", action="store_true",
                        help="Run in offline mode using a faked list-schemas JSON fixture")
    parser.add_argument("--list-schemas", metavar="PATH",
                        help="Path to the faked list-schemas JSON (required with --offline)")
    parser.add_argument("--write-artifact", metavar="OUT_PATH",
                        help="Write the verified_matrix artifact to this path")
    args = parser.parse_args(argv)

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
    else:
        try:
            verify_credentialed(None)
        except NotImplementedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0  # pragma: no cover
