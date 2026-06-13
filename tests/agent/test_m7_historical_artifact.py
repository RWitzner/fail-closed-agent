"""M7 historical reviewed-artifact flow.

This is distinct from the fixture-only ``m7-backtest`` builder: production
``artifacts/backtests`` writes require an explicit reviewed-artifact flag and
must still produce a normal verifier-compatible v2 artifact.
"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.__main__ import main
from agent.backtest_gate import verify_artifact
from agent.backtest_historical import (
    HistoricalArtifactWriteRefused,
    write_m7_historical_artifact,
)


_STRATEGY_ID = "directional.momentum_v1"
_RULES_HASH = "rh-historical-test"
_DATA_PIN = "EQUS.MINI:tbbo:1m:historical:mh-historical-test"


def _historical_rows():
    rows = []
    start_dates = (
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
        "2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16",
        "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22",
        "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
    )
    price_cents = 10_000
    vendor_seq = 1
    for date in start_dates:
        start = datetime.fromisoformat(date + "T14:30:00+00:00")
        for minute in range(90):
            ts_event = start + timedelta(minutes=minute)
            ts_recv = ts_event + timedelta(milliseconds=300)
            # Non-constant positive path: positive momentum and non-zero vol.
            price_cents += 1 + (minute % 3)
            mid = price_cents / 100
            rows.append({
                "dataset": "EQUS.MINI",
                "schema": "tbbo",
                "symbol": "AAPL",
                "instrument_id": 1001,
                "vendor_seq": vendor_seq,
                "ts_event_utc": (
                    ts_event.astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                ),
                "ts_recv_utc": (
                    ts_recv.astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                ),
                "bid_px": f"{mid - 0.01:.6f}",
                "bid_sz": "100",
                "ask_px": f"{mid + 0.01:.6f}",
                "ask_sz": "100",
                "reconnect_epoch": 0,
            })
            vendor_seq += 1
    return rows


def _write_jsonl(path, rows):
    import json

    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


class TestHistoricalArtifactFlow(unittest.TestCase):
    def test_reviewed_historical_flow_can_write_verifier_ok_artifact(self):
        with TemporaryDirectory() as tmp:
            result = write_m7_historical_artifact(
                artifacts_dir=tmp,
                quote_rows=_historical_rows(),
                symbol="AAPL",
                instrument_id=1001,
                rules_hash=_RULES_HASH,
                data_pin=_DATA_PIN,
                created_utc="2026-06-13T12:00:00.000000Z",
                input_manifest_hash="mh-historical-test",
                builder_git_commit="test-commit",
                allow_reviewed_artifact=True,
            )

            self.assertTrue(result.criteria.passed)
            self.assertEqual(result.payload["metrics"]["provenance"]["tier"],
                             "historical_reviewed")
            self.assertEqual(result.artifact_path,
                             Path(tmp) / f"{_STRATEGY_ID}.json")
            self.assertEqual(
                verify_artifact(
                    _STRATEGY_ID,
                    rules_hash=_RULES_HASH,
                    data_pin=_DATA_PIN,
                    artifacts_dir=tmp,
                ).status,
                "ok",
            )

    def test_historical_production_write_requires_reviewed_flag(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            production.mkdir(parents=True)

            with self.assertRaises(HistoricalArtifactWriteRefused):
                write_m7_historical_artifact(
                    artifacts_dir=production,
                    quote_rows=_historical_rows(),
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_DATA_PIN,
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest_hash="mh-historical-test",
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=False,
                    production_artifacts_dir=production,
                )


class TestHistoricalArtifactCli(unittest.TestCase):
    def test_cli_writes_reviewed_historical_artifact(self):
        with TemporaryDirectory() as tmp:
            quotes = Path(tmp) / "quotes.jsonl"
            _write_jsonl(quotes, _historical_rows())
            rc = main([
                "m7-historical-artifact",
                "--quotes-jsonl", str(quotes),
                "--artifacts-dir", tmp,
                "--symbol", "AAPL",
                "--instrument-id", "1001",
                "--rules-hash", _RULES_HASH,
                "--data-pin", _DATA_PIN,
                "--created-utc", "2026-06-13T12:00:00.000000Z",
                "--input-manifest-hash", "mh-historical-test",
                "--builder-git-commit", "test-commit",
                "--allow-reviewed-artifact",
            ])

            self.assertEqual(rc, 0)
            self.assertEqual(
                verify_artifact(
                    _STRATEGY_ID,
                    rules_hash=_RULES_HASH,
                    data_pin=_DATA_PIN,
                    artifacts_dir=tmp,
                ).status,
                "ok",
            )


if __name__ == "__main__":
    unittest.main()
