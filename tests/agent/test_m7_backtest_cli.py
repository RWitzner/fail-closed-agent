"""M7 artifact builder CLI tests."""
import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent import __main__ as agent_main
from agent import backtest_builder
from agent.backtest_gate import verify_artifact

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STRATEGY_ID = "directional.momentum_v1"
_RULES_HASH = "rh-m7-cli"
_DATA_PIN = "EQUS.MINI:tbbo:1m:fixture:m7-cli-v1"


def _cli_args(artifacts_dir, *extra):
    return [
        "m7-backtest",
        "--artifacts-dir", str(artifacts_dir),
        "--rules-hash", _RULES_HASH,
        "--data-pin", _DATA_PIN,
        "--created-utc", "2026-06-13T00:00:00.000000Z",
        "--input-manifest-hash", "mh-cli-test",
        "--builder-git-commit", "test",
        "--tier", "fixture",
        *extra,
    ]


def _run_main(args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = agent_main.main(args)
    return rc, stdout.getvalue(), stderr.getvalue()


class TestM7BacktestCli(unittest.TestCase):
    def test_cli_writes_v2_artifact_only_when_thresholds_pass(self):
        with TemporaryDirectory() as tmp:
            rc, _, _ = _run_main(_cli_args(tmp))

            artifact_path = Path(tmp) / f"{_STRATEGY_ID}.json"
            self.assertEqual(rc, 0)
            self.assertTrue(artifact_path.is_file())
            self.assertEqual(verify_artifact(
                _STRATEGY_ID,
                rules_hash=_RULES_HASH,
                data_pin=_DATA_PIN,
                artifacts_dir=tmp,
            ).status, "ok")

        with TemporaryDirectory() as tmp:
            rc, _, err = _run_main(_cli_args(
                tmp, "--fixture-net-pnl-usd", "-1.000000"))

            self.assertEqual(rc, 1)
            self.assertIn("criteria_failed=", err)
            self.assertFalse((Path(tmp) / f"{_STRATEGY_ID}.json").exists())

    def test_cli_refuses_committed_artifacts_dir_without_reviewed_flag(self):
        committed = _REPO_ROOT / "artifacts" / "backtests"
        target = committed / f"{_STRATEGY_ID}.json"
        self.assertFalse(target.exists())

        rc, _, err = _run_main(_cli_args(committed))

        self.assertEqual(rc, 2)
        self.assertIn("fixture builder cannot write committed artifacts/backtests",
                      err)
        self.assertFalse(target.exists())

        rc, _, err = _run_main(_cli_args(committed, "--write-reviewed-artifact"))

        self.assertEqual(rc, 2)
        self.assertIn("fixture builder cannot write committed artifacts/backtests",
                      err)
        self.assertFalse(target.exists())

    def test_fixture_builder_never_writes_production_artifact_path(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            with patch.object(backtest_builder, "PRODUCTION_ARTIFACTS_DIR",
                              production.resolve()):
                with self.assertRaises(backtest_builder.ArtifactWriteRefused):
                    backtest_builder.write_m7_fixture_artifact(
                        artifacts_dir=production,
                        rules_hash=_RULES_HASH,
                        data_pin=_DATA_PIN,
                        created_utc="2026-06-13T00:00:00.000000Z",
                        input_manifest_hash="mh-cli-test",
                        builder_git_commit="test",
                        tier="fixture",
                        fixture_net_pnl_usd="1.900000",
                        allow_reviewed_artifact=False,
                    )

                with self.assertRaises(backtest_builder.ArtifactWriteRefused):
                    backtest_builder.write_m7_fixture_artifact(
                        artifacts_dir=production,
                        rules_hash=_RULES_HASH,
                        data_pin=_DATA_PIN,
                        created_utc="2026-06-13T00:00:00.000000Z",
                        input_manifest_hash="mh-cli-test",
                        builder_git_commit="test",
                        tier="fixture",
                        fixture_net_pnl_usd="1.900000",
                        allow_reviewed_artifact=True,
                    )

            self.assertFalse((production / f"{_STRATEGY_ID}.json").exists())

    def test_runbook_names_exact_paper_evidence_before_m8(self):
        runbook = (_REPO_ROOT / "docs" / "runbooks"
                   / "m7-paper-edge-validation.md")
        text = runbook.read_text(encoding="utf-8")
        for token in (
                "20 full RTH sessions",
                "30 opened-and-closed paper trades",
                "5 traded sessions",
                "unresolved broker reconciliation drift",
                "net_execution_realistic_pnl_usd > 0",
                "active_pnl_usd > 0",
                "Profit factor >= 1.10",
                "P95 broker-vs-modeled realism gap <= 15 bps",
                "Zero live-broker submissions",
                "M8 remains blocked",
        ):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
