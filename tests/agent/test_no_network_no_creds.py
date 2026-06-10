"""M0 makes no network calls and needs no secrets (supports S1's spirit).

The agent modules import no broker/data SDK, the M0 flows open no socket, and the
suite is green with `.secrets/` absent.
"""
import ast
import os
import socket
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


class TestNoSdkImported(unittest.TestCase):
    def test_no_broker_or_data_sdk_in_sys_modules(self):
        import agent.broker.alpaca  # noqa: F401
        import agent.marketdata.base  # noqa: F401

        self.assertNotIn("alpaca", sys.modules)
        self.assertNotIn("databento", sys.modules)

    def test_m2_status_modules_import_no_heavy_sdk(self):
        # M2 §J offline-purity guard: importing the M2 status/CA chain must NOT pull in any
        # broker/data/calendar SDK. A top-level `import exchange_calendars` would crash
        # `unittest discover` at COLLECTION and fail ALL tests — so this is BUILD-BLOCKING.
        import agent.corporate_actions  # noqa: F401
        import agent.status_ledger  # noqa: F401

        self.assertNotIn("alpaca", sys.modules)
        self.assertNotIn("databento", sys.modules)
        self.assertNotIn("exchange_calendars", sys.modules)

    def test_all_six_m2_modules_import_no_heavy_sdk(self):
        # M2 §J: importing the FULL M2 module set must pull in NO broker/data/calendar
        # SDK. A module-scope `import exchange_calendars` would crash `unittest discover`
        # at COLLECTION and fail ALL tests — so this is BUILD-BLOCKING.
        import agent.market_calendar  # noqa: F401
        import agent.market_state  # noqa: F401
        import agent.market_state_cache  # noqa: F401
        import agent.corporate_actions  # noqa: F401
        import agent.status_ledger  # noqa: F401
        import agent.session_liveness  # noqa: F401

        self.assertNotIn("alpaca", sys.modules)
        self.assertNotIn("databento", sys.modules)
        self.assertNotIn("exchange_calendars", sys.modules)


class TestNoSocketOpened(unittest.TestCase):
    def test_m0_flows_open_no_socket(self):
        from agent.broker.alpaca import AlpacaPaperBroker
        from agent.broker.base import OrderIntent
        from agent.execution_preflight import mint_reduce_only_token
        from agent.serializer import dumps

        with mock.patch("socket.socket", side_effect=AssertionError("M0 must not open sockets")):
            dumps({"x": Decimal("1.0")})
            broker = AlpacaPaperBroker()
            held = types.SimpleNamespace(symbol="AAPL", qty=Decimal("3"))
            intent = OrderIntent(
                symbol="AAPL", side="sell", qty=Decimal("1"), is_reducing=True, intent_id="r"
            )
            token = mint_reduce_only_token(held, intent)
            broker.submit_order(intent, token)
        # Reaching here without AssertionError proves no socket was created.
        self.assertEqual(len(broker.submitted), 1)


class TestM2OfflinePurity(unittest.TestCase):
    """M2 §J: the calendar/session layer stays offline-pure — the heavy
    `exchange_calendars` lib is reached ONLY via the lazy live-provider build path,
    never at module scope, and the M2 decision flows open no socket."""

    _M2_MODULES = [
        "market_calendar", "market_state", "market_state_cache",
        "corporate_actions", "status_ledger", "session_liveness",
    ]

    def _agent_dir(self):
        return Path(__file__).resolve().parents[2] / "scripts" / "agent"

    def _calendar_fixture(self):
        import json
        fpath = Path(__file__).resolve().parents[1] / "fixtures" / "calendar" / "nyse_2026_schedule.json"
        return json.loads(fpath.read_text(encoding="utf-8"))

    def test_no_module_scope_import_of_exchange_calendars(self):
        # AST guard: no M2 module may import exchange_calendars at module/class scope.
        # The only legitimate import lives INSIDE a function (the lazy live build path),
        # so we descend into functions but only flag imports NOT nested in a function.
        for name in self._M2_MODULES:
            src = (self._agent_dir() / f"{name}.py").read_text(encoding="utf-8")
            offending = []

            def walk(node, in_func):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk(child, True)
                        continue
                    if not in_func and isinstance(child, (ast.Import, ast.ImportFrom)):
                        names = (
                            [a.name for a in child.names]
                            if isinstance(child, ast.Import)
                            else [child.module or ""]
                        )
                        if any("exchange_calendars" in (m or "") for m in names):
                            offending.append(name)
                    walk(child, in_func)

            walk(ast.parse(src), False)
            self.assertEqual(
                offending, [],
                f"{name}.py imports exchange_calendars at module scope (crashes collection)")

    def test_fixture_provider_imports_no_exchange_calendars(self):
        from agent.market_calendar import FixtureScheduleProvider, MarketCalendar
        fixture = self._calendar_fixture()
        cal = MarketCalendar(FixtureScheduleProvider(fixture, pin=fixture["pin"]))
        cal.phase_at("2026-06-15T13:30:00.000000Z")
        self.assertNotIn("exchange_calendars", sys.modules)

    def test_live_provider_build_is_notimplemented_offline(self):
        from agent.market_calendar import ExchangeCalendarsScheduleProvider
        provider = ExchangeCalendarsScheduleProvider()
        with self.assertRaises(NotImplementedError):
            provider._build_calendar()
        self.assertNotIn("exchange_calendars", sys.modules)

    def test_m2_flows_open_no_socket(self):
        from agent.corporate_actions import (
            CaProvenance, CaSource, CaType, DurableId, SourceObservation, cross_validate,
        )
        from agent.market_calendar import SessionPhase
        from agent.market_state import StatusFlags, TradabilityDecider, TradabilityInputs
        from agent.market_state_cache import MarketStateCache
        from tests.lib.fakes import FakeClock

        with mock.patch("socket.socket", side_effect=AssertionError("M2 must not open sockets")):
            # decide (fail-closed: UNKNOWN status + no NBBO -> NOT_TRADABLE)
            TradabilityDecider().decide(TradabilityInputs(
                symbol="AAPL", instrument_id=1001, ts_utc="2026-06-15T13:30:00.000000Z",
                session_date_et="2026-06-15", session_phase=SessionPhase.RTH,
                status=StatusFlags(symbol="AAPL"), nbbo=None, ca_blackout=False))
            # cache (missing entry -> safe default)
            MarketStateCache(clock=FakeClock(start_ms=0)).get("AAPL", 1001, "2026-06-15")
            # CA cross-validate (two independent confirming sources)
            did = DurableId(cusip="TESTAAPL1", figi="BBG000B9XRY4", ticker="AAPL")
            cross_validate((
                SourceObservation(
                    source=CaSource.ALPACA, durable_id=did, ca_type=CaType.SPLIT,
                    ex_date_et="2026-07-01", factor=Decimal("4.00000000"), cash_amount=None,
                    provenance=CaProvenance(
                        source=CaSource.ALPACA, source_ca_id="ALP-1",
                        announced_ts_utc="2026-06-20T12:00:00.000000Z",
                        ts_recv_utc="2026-06-20T12:00:01.000000Z")),
                SourceObservation(
                    source=CaSource.DATA_VENDOR, durable_id=did, ca_type=CaType.SPLIT,
                    ex_date_et="2026-07-01", factor=Decimal("4.00000000"), cash_amount=None,
                    provenance=CaProvenance(
                        source=CaSource.DATA_VENDOR, source_ca_id="DV-9",
                        announced_ts_utc="2026-06-20T12:05:00.000000Z",
                        ts_recv_utc="2026-06-20T12:05:01.000000Z")),
            ))
        # Reaching here without AssertionError proves no socket was created.
        self.assertNotIn("exchange_calendars", sys.modules)


class TestM3OfflinePurity(unittest.TestCase):
    """M3 §M.11: importing every M3 module pulls in NO vendor SDK / plotting / ML
    lib, and a minimal feature -> snapshot -> forecast -> score path opens no
    socket (design §9)."""

    def test_m3_modules_import_no_heavy_sdk(self):
        import agent.signal_config  # noqa: F401
        import agent.quote_quality  # noqa: F401
        import agent.bar_series  # noqa: F401
        import agent.feature_engine  # noqa: F401
        import agent.signal_snapshot  # noqa: F401
        import agent.strategy  # noqa: F401
        import agent.candidate  # noqa: F401
        import agent.forecast  # noqa: F401
        import agent.calibration  # noqa: F401
        import agent.calibration_report  # noqa: F401
        import agent.strategies.calibration_probe  # noqa: F401

        for banned in ("alpaca", "databento", "exchange_calendars",
                       "matplotlib", "plotly", "pandas", "numpy", "scipy",
                       "sklearn", "torch"):
            self.assertNotIn(banned, sys.modules)

    def test_m3_minimal_path_opens_no_socket(self):
        from tempfile import TemporaryDirectory

        from tests.lib.signal_fixtures import quotes_session_v1
        from tests.lib.signal_pipeline import SignalPipeline

        with mock.patch("socket.socket",
                        side_effect=AssertionError("M3 must not open sockets")):
            with TemporaryDirectory() as tmpdir:
                pipeline = SignalPipeline(quote_rows=quotes_session_v1(),
                                          journal_dir=tmpdir, run_id="run-purity")
                decisions = pipeline.tick_on_bar(50)
                stats = pipeline.resolve(now_utc="2026-06-15T20:30:00.000000Z")
                report = pipeline.report()
        self.assertEqual(len(decisions), 2)
        self.assertGreaterEqual(stats.scored, 1)
        self.assertEqual(report["funnel"]["forecasts"], 2)


class TestM4RiskOfflinePurityAndImportGuard(unittest.TestCase):
    """M4 §M test 11 (S1, R11): socket-blocked import of every risk/ module, no
    heavy SDK in sys.modules, the FD-M4-24 AST import/token guard with the SOLE
    risk_kill.py exemption (agent.kill_switch only), and a subprocess-isolated
    fresh-import check proving no order-capable module lands in sys.modules."""

    _RISK_MODULES = (
        "reasons", "account_state", "risk_config", "exposure", "risk_ledger",
        "loss_limits", "intraday_margin", "pdt_compat", "locate", "risk_kill",
        "can_open",
    )
    _FORBIDDEN_MODULE_PREFIXES = (
        "agent.broker", "agent.execution_preflight", "agent.kill_switch",
        "agent.arming",
    )
    _FORBIDDEN_TOKENS = frozenset({
        "submit_order", "mint_open_token", "mint_reduce_only_token", "OrderIntent",
        "OpenPreflightToken", "ReduceOnlyPreflightToken", "PreflightToken",
        "require_token", "consume", "importlib", "__import__",
    })

    def _risk_dir(self):
        return Path(__file__).resolve().parents[2] / "scripts" / "agent" / "risk"

    def _module_forbidden(self, module_name: str, source_file: str) -> bool:
        if module_name is None:
            return False
        # SOLE exemption (FD-M4-24): risk_kill.py may import agent.kill_switch.
        if source_file == "risk_kill.py" and module_name == "agent.kill_switch":
            return False
        return any(module_name == prefix or module_name.startswith(prefix + ".")
                   for prefix in self._FORBIDDEN_MODULE_PREFIXES)

    def test_risk_modules_import_under_socket_block_with_no_heavy_sdk(self):
        import importlib as _importlib

        with mock.patch("socket.socket",
                        side_effect=AssertionError("M4 must not open sockets")):
            for name in self._RISK_MODULES:
                _importlib.import_module(f"agent.risk.{name}")
        for banned in ("alpaca", "databento", "exchange_calendars",
                       "pandas", "numpy"):
            self.assertNotIn(banned, sys.modules)

    def test_fd_m4_24_ast_import_and_token_guard(self):
        source_files = sorted(self._risk_dir().glob("*.py"))
        self.assertEqual(
            sorted(p.name for p in source_files),
            sorted(["__init__.py"] + [f"{m}.py" for m in self._RISK_MODULES]))
        for source_path in source_files:
            source_file = source_path.name
            violations = []
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._module_forbidden(alias.name, source_file):
                            violations.append(f"import {alias.name}")
                        if alias.name == "importlib" or alias.name.startswith(
                                "importlib."):
                            violations.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._module_forbidden(module, source_file):
                        violations.append(f"from {module} import ...")
                    if module == "importlib" or module.startswith("importlib."):
                        violations.append(f"from {module} import ...")
                    if module == "agent":
                        for alias in node.names:
                            if self._module_forbidden(f"agent.{alias.name}",
                                                      source_file):
                                violations.append(f"from agent import {alias.name}")
                    for alias in node.names:
                        if alias.name in self._FORBIDDEN_TOKENS:
                            violations.append(f"imported token {alias.name}")
                        if alias.asname and alias.asname in self._FORBIDDEN_TOKENS:
                            violations.append(f"alias token {alias.asname}")
                elif isinstance(node, ast.Name):
                    if node.id in self._FORBIDDEN_TOKENS:
                        violations.append(f"name {node.id}")
                elif isinstance(node, ast.Attribute):
                    if node.attr in self._FORBIDDEN_TOKENS:
                        violations.append(f"attribute .{node.attr}")
            self.assertEqual(violations, [],
                             f"{source_file}: FD-M4-24 violations: {violations}")

    def test_risk_kill_imports_only_killswitch_from_the_actuator(self):
        # The exemption is NARROW: agent.kill_switch, and only the KillSwitch name.
        source_path = self._risk_dir() / "risk_kill.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        actuator_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agent.kill_switch":
                actuator_imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "agent.kill_switch":
                        actuator_imports.append(alias.name)
        self.assertEqual(actuator_imports, ["KillSwitch"])

    def test_subprocess_isolated_fresh_import_pulls_no_order_capable_module(self):
        import subprocess

        modules = [m for m in self._RISK_MODULES if m != "risk_kill"]
        code = (
            "import sys\n"
            + "".join(f"import agent.risk.{m}\n" for m in modules)
            + "banned = [k for k in sys.modules\n"
            "          if k in ('agent.execution_preflight', 'agent.arming',\n"
            "                   'agent.kill_switch')\n"
            "          or k == 'agent.broker' or k.startswith('agent.broker.')]\n"
            "assert not banned, f'order-capable modules imported: {banned}'\n"
        )
        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        env = dict(os.environ)
        env["PYTHONPATH"] = scripts_dir
        argv = [sys.executable, "-c", code]   # fixed command array, no shell
        completed = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout!r} stderr={completed.stderr!r}")

    def test_risk_kill_subprocess_pulls_actuator_but_never_arming(self):
        import subprocess

        code = (
            "import sys\n"
            "import agent.risk.risk_kill\n"
            "assert 'agent.kill_switch' in sys.modules\n"  # the sanctioned delegation
            "assert 'agent.arming' not in sys.modules\n"
        )
        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        env = dict(os.environ)
        env["PYTHONPATH"] = scripts_dir
        argv = [sys.executable, "-c", code]
        completed = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout!r} stderr={completed.stderr!r}")


if __name__ == "__main__":
    unittest.main()
