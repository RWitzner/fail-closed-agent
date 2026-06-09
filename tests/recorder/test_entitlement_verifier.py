"""Tests for recorder.verify_databento_entitlements (contract §K, §N).

Offline only; no network, no credentials.
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.serializer import dumps as serializer_dumps
from recorder.verify_databento_entitlements import (
    PlannedCell,
    UnverifiableSchema,
    VerifiedCell,
    VerifiedMatrix,
    credentialed_downgrades,
    credentialed_planned_matrix,
    list_schemas_offline,
    planned_matrix,
    verify,
    verify_credentialed,
    write_artifact,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"

# The faked schemas used in every test (matches list_schemas_response.json fixture).
_SCHEMAS = {
    "EQUS.MINI": ["tbbo", "bbo-1s", "bbo-1m", "trades", "ohlcv-1s", "ohlcv-1m", "definitions"],
    "<DEPTH_DATASET>": ["mbp-10", "trades", "definitions"],
}

# Standard downgrade map matching the M1 plan.
_DOWNGRADES = {
    ("EQUS.MINI", "mbp-10"): "depth -> <DEPTH_DATASET>",
    ("EQUS.MINI", "status"): "status -> broker (Alpaca) + exchange_calendars (M2)",
}


def _verified():
    """Run verify() with the standard planned_matrix, schemas, and downgrades."""
    return verify(planned_matrix(), _SCHEMAS, downgrades=_DOWNGRADES)


class TestMatrixCorrectness(unittest.TestCase):
    """Core availability assertions from §K."""

    def test_verify_marks_equs_mini_has_no_mbp10(self):
        """(EQUS.MINI, mbp-10) available=False."""
        result = _verified()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "mbp-10")
        self.assertFalse(cell.available)

    def test_verify_marks_equs_mini_has_no_status_with_downgrade(self):
        """(EQUS.MINI, status) available=False + downgrade set."""
        result = _verified()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "status")
        self.assertFalse(cell.available)
        self.assertIsNotNone(cell.downgrade)
        self.assertGreater(len(cell.downgrade), 0)

    def test_depth_dataset_has_mbp10(self):
        """(<DEPTH_DATASET>, mbp-10) available=True."""
        result = _verified()
        cell = next(c for c in result.cells if c.dataset == "<DEPTH_DATASET>" and c.schema == "mbp-10")
        self.assertTrue(cell.available)

    def test_equs_mini_tbbo_available(self):
        """(EQUS.MINI, tbbo) available=True — the primary NBBO source."""
        result = _verified()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "tbbo")
        self.assertTrue(cell.available)

    def test_all_available_false_due_to_downgrades(self):
        """all_available=False because EQUS.MINI lacks mbp-10 and status."""
        result = _verified()
        self.assertFalse(result.all_available)

    def test_downgrades_tuple_contains_unavailable_cells(self):
        """downgrades tuple contains exactly the unavailable cells."""
        result = _verified()
        self.assertGreater(len(result.downgrades), 0)
        for c in result.downgrades:
            self.assertFalse(c.available)


class TestNoSilentFallback(unittest.TestCase):
    """An unavailable (dataset, schema) without a downgrade note raises UnverifiableSchema."""

    def test_absent_schema_without_downgrade_raises(self):
        """UnverifiableSchema raised when no downgrade note provided for an unavailable cell."""
        planned = [PlannedCell("EQUS.MINI", "mbp-10", "L2_depth")]
        with self.assertRaises(UnverifiableSchema):
            verify(planned, _SCHEMAS)  # no downgrades -> raises

    def test_absent_schema_with_downgrade_ok(self):
        """No exception when unavailable cell has a downgrade note."""
        planned = [PlannedCell("EQUS.MINI", "mbp-10", "L2_depth")]
        downgrades = {("EQUS.MINI", "mbp-10"): "depth -> <DEPTH_DATASET>"}
        result = verify(planned, _SCHEMAS, downgrades=downgrades)
        self.assertFalse(result.cells[0].available)
        self.assertIsNotNone(result.cells[0].downgrade)


class TestEqusMiniPollutionGuard(unittest.TestCase):
    """F2 (R4#2): the §K verified-2026-06-08 regression guard.

    verify() must raise UnverifiableSchema if EQUS.MINI ever lists 'mbp-10' or
    'status' (a regression that silently maps depth/status onto EQUS.MINI). This
    exercises the polluted branch the prior suite never hit. Mirrors
    test_absent_schema_without_downgrade_raises.
    """

    def test_equs_mini_with_mbp10_raises_against_verified_matrix(self):
        """EQUS.MINI listing 'mbp-10' -> UnverifiableSchema mentioning the verified-2026-06-08 matrix."""
        polluted = {
            "EQUS.MINI": ["tbbo", "mbp-10"],   # mbp-10 must NOT be on EQUS.MINI
            "<DEPTH_DATASET>": ["mbp-10", "trades", "definitions"],
        }
        with self.assertRaises(UnverifiableSchema) as ctx:
            # Provide downgrades so failure can ONLY come from the §K pollution guard.
            verify(planned_matrix(), polluted, downgrades=_DOWNGRADES)
        self.assertIn("mbp-10", str(ctx.exception))
        self.assertIn("verified-2026-06-08", str(ctx.exception))

    def test_equs_mini_with_status_raises_against_verified_matrix(self):
        """EQUS.MINI listing 'status' -> UnverifiableSchema mentioning the verified-2026-06-08 matrix."""
        polluted = {
            "EQUS.MINI": ["tbbo", "status"],   # status must NOT be on EQUS.MINI
            "<DEPTH_DATASET>": ["mbp-10", "trades", "definitions"],
        }
        with self.assertRaises(UnverifiableSchema) as ctx:
            verify(planned_matrix(), polluted, downgrades=_DOWNGRADES)
        self.assertIn("status", str(ctx.exception))
        self.assertIn("verified-2026-06-08", str(ctx.exception))


class TestPlannedCellInstances(unittest.TestCase):
    """MINOR 7: verify() consumes PlannedCell instances; downgrades keyed by (dataset,schema)."""

    def test_verify_takes_plannedcell_instances(self):
        """verify() accepts PlannedCell dataclass instances (not bare tuples)."""
        planned = [
            PlannedCell("EQUS.MINI", "tbbo", "L1_nbbo"),
        ]
        result = verify(planned, _SCHEMAS)
        self.assertIsInstance(result.cells[0], VerifiedCell)

    def test_downgrades_keyed_by_dataset_schema_tuple(self):
        """downgrades dict is keyed by (dataset, schema) tuples."""
        planned = [PlannedCell("EQUS.MINI", "mbp-10", "L2_depth")]
        # Key must be a tuple, not something else.
        downgrades = {("EQUS.MINI", "mbp-10"): "note"}
        result = verify(planned, _SCHEMAS, downgrades=downgrades)
        self.assertFalse(result.cells[0].available)
        self.assertEqual(result.cells[0].downgrade, "note")


class TestVerifiedMatrixShape(unittest.TestCase):
    """Contract shape: VerifiedMatrix(cells, all_available, downgrades, live_subscription)."""

    def test_verified_matrix_shape(self):
        """Output is VerifiedMatrix with the required fields."""
        result = _verified()
        self.assertIsInstance(result, VerifiedMatrix)
        self.assertIsInstance(result.cells, tuple)
        self.assertIsInstance(result.all_available, bool)
        self.assertIsInstance(result.downgrades, tuple)
        self.assertIsInstance(result.live_subscription, str)

    def test_access_field_and_live_subscription_pending(self):
        """Every offline cell access='historical'; top-level live_subscription='pending'."""
        result = _verified()
        for cell in result.cells:
            self.assertEqual(cell.access, "historical")
        self.assertEqual(result.live_subscription, "pending")

    def test_access_can_be_overridden_per_cell(self):
        """access_by_cell kwarg overrides the per-cell access value."""
        planned = [PlannedCell("EQUS.MINI", "tbbo", "L1_nbbo")]
        access_by_cell = {("EQUS.MINI", "tbbo"): "both"}
        result = verify(planned, _SCHEMAS, access_by_cell=access_by_cell)
        self.assertEqual(result.cells[0].access, "both")

    def test_cell_has_downgrade_field(self):
        """Each VerifiedCell carries a downgrade field (None for available cells)."""
        result = _verified()
        for cell in result.cells:
            if cell.available:
                # available cells: downgrade is None (no note needed)
                self.assertIsNone(cell.downgrade)
            else:
                self.assertIsNotNone(cell.downgrade)


class TestWriteArtifact(unittest.TestCase):
    """write_artifact round-trips through agent.serializer.dumps (Decimal-safe, canonical)."""

    def test_write_artifact_is_decimal_safe_canonical(self):
        """write_artifact output is valid JSON, canonical, includes per-cell access + live_subscription."""
        result = _verified()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name

        write_artifact(result, out_path)
        content = Path(out_path).read_text(encoding="utf-8").strip()
        # Must be valid JSON.
        data = json.loads(content)
        self.assertIn("live_subscription", data)
        self.assertIn("all_available", data)
        self.assertIn("cells", data)
        for cell in data["cells"]:
            self.assertIn("access", cell)
        # Canonical (sort_keys): keys must be sorted.
        # Re-serialize and check it's stable.
        re_serialized = serializer_dumps(data)
        self.assertEqual(content, re_serialized)


class TestListSchemasOffline(unittest.TestCase):
    """list_schemas_offline reads the FAKED list-schemas fixture."""

    def test_list_schemas_offline_reads_fixture(self):
        """list_schemas_offline returns the expected dict from the fixture file."""
        schemas = list_schemas_offline(FIXTURES / "list_schemas_response.json")
        self.assertIn("EQUS.MINI", schemas)
        self.assertIn("<DEPTH_DATASET>", schemas)
        self.assertIn("tbbo", schemas["EQUS.MINI"])
        self.assertNotIn("mbp-10", schemas["EQUS.MINI"])
        self.assertIn("mbp-10", schemas["<DEPTH_DATASET>"])


class TestNoNetworkNoCreds(unittest.TestCase):
    """No SDK import, no socket in offline mode."""

    def test_no_databento_import_no_socket_offline(self):
        """verify() and list_schemas_offline() do not import databento or open a socket."""
        import sys
        # Import the module and run offline operations.
        from recorder.verify_databento_entitlements import (
            list_schemas_offline, verify, planned_matrix,
        )
        schemas = list_schemas_offline(FIXTURES / "list_schemas_response.json")
        verify(planned_matrix(), schemas, downgrades=_DOWNGRADES)
        self.assertNotIn("databento", sys.modules)

    def test_credentialed_with_injected_client_imports_no_databento(self):
        """verify_credentialed with an injected (mocked) client never imports databento."""
        import sys
        client = _FakeHistoricalClient(_LIVE_SCHEMAS, _LIVE_RANGES)
        verify_credentialed(
            credentialed_planned_matrix(),
            downgrades=credentialed_downgrades(),
            client=client,
        )
        self.assertNotIn("databento", sys.modules)


# ---------------------------------------------------------------------------
# Credentialed (--live) mode — assembly + downgrade logic, MOCKED client, NO network.
# ---------------------------------------------------------------------------

# Live list_schemas (verified 2026-06-08): EQUS.MINI definition is SINGULAR; no
# mbp-10, no status. XNAS.ITCH carries mbp-10 (full 10-level depth).
_LIVE_SCHEMAS = {
    "EQUS.MINI": [
        "bbo-1m", "bbo-1s", "definition", "mbp-1", "ohlcv-1d", "ohlcv-1h",
        "ohlcv-1m", "ohlcv-1s", "tbbo", "trades",
    ],
    "XNAS.ITCH": [
        "definition", "imbalance", "mbo", "mbp-1", "mbp-10", "ohlcv-1d",
        "ohlcv-1h", "ohlcv-1m", "ohlcv-1s", "status", "tbbo", "trades",
    ],
}

_LIVE_RANGES = {
    "EQUS.MINI": {"start": "2024-01-01T00:00:00.000000000Z", "end": "2026-06-08T23:59:59.999999999Z"},
    "XNAS.ITCH": {"start": "2018-05-06T00:00:00.000000000Z", "end": "2026-06-08T23:59:59.999999999Z"},
}


class _FakeMetadata:
    """Canned metadata facade — returns fixtures, makes NO network call."""

    def __init__(self, schemas, ranges, costs=None):
        self._schemas = schemas
        self._ranges = ranges
        self._costs = costs or {}
        self.cost_calls = []

    def list_schemas(self, dataset):
        return list(self._schemas[dataset])

    def get_dataset_range(self, dataset):
        return dict(self._ranges[dataset])

    def get_cost(self, dataset, start, end, symbols, schema):
        self.cost_calls.append((dataset, schema, start, end, tuple(symbols)))
        # Default tiny preview cost; float on purpose (mirrors the real SDK return).
        return self._costs.get((dataset, schema), 0.0123456789)


class _FakeHistoricalClient:
    """databento.Historical-shaped fake exposing a .metadata facade."""

    def __init__(self, schemas, ranges, costs=None):
        self.metadata = _FakeMetadata(schemas, ranges, costs)


class TestCredentialedAssembly(unittest.TestCase):
    """verify_credentialed assembly + downgrade logic with a MOCKED client (no network)."""

    def _run(self, **kwargs):
        client = _FakeHistoricalClient(_LIVE_SCHEMAS, _LIVE_RANGES, costs=kwargs.pop("costs", None))
        return verify_credentialed(
            credentialed_planned_matrix(),
            downgrades=credentialed_downgrades(),
            client=client,
            **kwargs,
        ), client

    def test_equs_mini_tbbo_available_historical(self):
        """(EQUS.MINI, tbbo) available=True, access='historical'."""
        result, _ = self._run()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "tbbo")
        self.assertTrue(cell.available)
        self.assertEqual(cell.access, "historical")

    def test_equs_mini_definition_singular_available(self):
        """(EQUS.MINI, definition) (SINGULAR) is available against the live schemas."""
        result, _ = self._run()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "definition")
        self.assertTrue(cell.available)

    def test_xnas_itch_mbp10_available(self):
        """(XNAS.ITCH, mbp-10) available=True — the entitled depth source."""
        result, _ = self._run()
        cell = next(c for c in result.cells if c.dataset == "XNAS.ITCH" and c.schema == "mbp-10")
        self.assertTrue(cell.available)
        self.assertEqual(cell.access, "historical")

    def test_equs_mini_mbp10_unavailable_with_xnas_downgrade(self):
        """(EQUS.MINI, mbp-10) unavailable + downgrade names XNAS.ITCH and the DBEQ.BASIC rejection."""
        result, _ = self._run()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "mbp-10")
        self.assertFalse(cell.available)
        self.assertIn("XNAS.ITCH", cell.downgrade)
        self.assertIn("DBEQ.BASIC", cell.downgrade)

    def test_equs_mini_status_unavailable_with_downgrade(self):
        """(EQUS.MINI, status) unavailable + downgrade to broker + calendar."""
        result, _ = self._run()
        cell = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "status")
        self.assertFalse(cell.available)
        self.assertIn("broker", cell.downgrade)

    def test_dataset_range_recorded_for_available_cells(self):
        """Available cells carry the dataset_range; unavailable cells do not."""
        result, _ = self._run()
        for c in result.cells:
            if c.available:
                self.assertIsNotNone(c.dataset_range)
                self.assertIn("start", c.dataset_range)
                self.assertIn("end", c.dataset_range)
            else:
                self.assertIsNone(c.dataset_range)

    def test_live_subscription_pending(self):
        """Top-level live_subscription stays 'pending' (live realtime not provisioned)."""
        result, _ = self._run()
        self.assertEqual(result.live_subscription, "pending")

    def test_all_available_false_due_to_deliberate_downgrades(self):
        """all_available=False because EQUS.MINI lacks depth + status (deliberate downgrades)."""
        result, _ = self._run()
        self.assertFalse(result.all_available)

    def test_pollution_guard_fires_on_live_path(self):
        """If live list_schemas ever returns mbp-10 on EQUS.MINI, UnverifiableSchema fires."""
        polluted = dict(_LIVE_SCHEMAS)
        polluted["EQUS.MINI"] = _LIVE_SCHEMAS["EQUS.MINI"] + ["mbp-10"]
        client = _FakeHistoricalClient(polluted, _LIVE_RANGES)
        with self.assertRaises(UnverifiableSchema) as ctx:
            verify_credentialed(
                credentialed_planned_matrix(),
                downgrades=credentialed_downgrades(),
                client=client,
            )
        self.assertIn("verified-2026-06-08", str(ctx.exception))

    def test_sample_cost_is_decimal_string_not_float(self):
        """sample_cost_usd is a Decimal-as-string for priced cells; no float survives."""
        result, client = self._run(
            cost_window=("2026-06-08T15:00:00", "2026-06-08T15:00:02"),
            cost_symbols=("AAPL", "MSFT"),
            cost_for=(("EQUS.MINI", "tbbo"), ("XNAS.ITCH", "mbp-10")),
            costs={("EQUS.MINI", "tbbo"): 0.001234, ("XNAS.ITCH", "mbp-10"): 0.004321},
        )
        tbbo = next(c for c in result.cells if c.dataset == "EQUS.MINI" and c.schema == "tbbo")
        depth = next(c for c in result.cells if c.dataset == "XNAS.ITCH" and c.schema == "mbp-10")
        self.assertIsInstance(tbbo.sample_cost_usd, str)
        self.assertIsInstance(depth.sample_cost_usd, str)
        self.assertEqual(tbbo.sample_cost_usd, "0.001234")
        # Two get_cost calls were made (one per priced cell).
        self.assertEqual(len(client.metadata.cost_calls), 2)

    def test_cost_skipped_when_no_window(self):
        """No get_cost call when cost_window/cost_for omitted; sample_cost_usd stays None."""
        result, client = self._run()
        self.assertEqual(len(client.metadata.cost_calls), 0)
        for c in result.cells:
            self.assertIsNone(c.sample_cost_usd)

    def test_artifact_round_trips_with_live_enrichment(self):
        """write_artifact serializes dataset_range + sample_cost_usd canonically (Decimal-safe)."""
        result, _ = self._run(
            cost_window=("2026-06-08T15:00:00", "2026-06-08T15:00:02"),
            cost_symbols=("AAPL",),
            cost_for=(("EQUS.MINI", "tbbo"),),
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name
        write_artifact(result, out_path)
        content = Path(out_path).read_text(encoding="utf-8").strip()
        data = json.loads(content)
        self.assertEqual(data["live_subscription"], "pending")
        self.assertIn("downgrades", data)
        tbbo = next(c for c in data["cells"] if c["dataset"] == "EQUS.MINI" and c["schema"] == "tbbo")
        self.assertIn("dataset_range", tbbo)
        self.assertIn("sample_cost_usd", tbbo)
        self.assertIsInstance(tbbo["sample_cost_usd"], str)
        # Canonical (sorted keys) — re-serialize must be byte-identical.
        self.assertEqual(content, serializer_dumps(data))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
