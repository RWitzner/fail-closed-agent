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
        "calendar_fixture",
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

    def test_live_provider_missing_lib_fails_closed_offline(self):
        from agent.market_calendar import (
            CalendarError,
            ExchangeCalendarsScheduleProvider,
        )
        provider = ExchangeCalendarsScheduleProvider()
        # sys.modules[name] = None forces ImportError deterministically whether
        # or not the lib is installed in the running env (bare checkout OR .venv).
        with mock.patch.dict(sys.modules, {"exchange_calendars": None}):
            with self.assertRaises(CalendarError):
                provider._build_calendar()

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


class TestM5ExecOfflinePurityAndImportGuard(unittest.TestCase):
    """M5 §R test 11 (S1, R11): socket-blocked import of every M5 module + a
    FULL orchestrator observe run; `alpaca` (the SDK, FD-M5-5) never in
    sys.modules; the §3 per-module import whitelist (module scope) + the
    pure-family forbidden-import/token rule (any scope); subprocess-isolated
    fresh imports proving the lazy-broker discipline; `.secrets/` never read
    (loaders take explicit injected paths only)."""

    # The FULL §3 NEW-module list (repo-relative under scripts/agent/).
    _M5_MODULES = (
        "exec_reasons.py", "execution_config.py", "order_pricing.py",
        "execution_realism.py", "fees.py", "paper_book.py", "exec_ledger.py",
        "backtest_gate.py", "run_lock.py", "secrets_runtime.py",
        "orchestrator.py", "__main__.py", "broker/order_state.py",
        "broker/fake.py", "broker/flatten_proxy.py",
        "marketdata/replay_feed.py", "strategies/synthetic.py",
    )

    # §3 per-module allowed agent.*/recorder.* imports AT MODULE SCOPE (the
    # data-driven table; None = the orchestrator special row, tested apart).
    # Documented errata honored: replay_feed imports agent.execution_realism
    # for DepthSnapshot (whitelisted); execution_realism imports
    # agent.execution_config for its §B-homed code constants
    # (DIVERGENCE_ALERT_BPS / DEPTH_FRESHNESS_TTL_MS); fake.py's
    # SyntheticConfinementError import from agent.broker.alpaca lives INSIDE
    # `_place` (function scope), so the module-scope whitelist never sees it.
    _ALLOWED_MODULE_SCOPE = {
        "exec_reasons.py": frozenset(),
        "execution_config.py": frozenset({"agent.serializer"}),
        "order_pricing.py": frozenset(
            {"agent.quote_quality", "agent.exec_reasons"}),
        "execution_realism.py": frozenset(
            {"agent.serializer", "agent.quote_quality", "agent.exec_reasons",
             "agent.execution_config"}),
        "fees.py": frozenset(),
        "paper_book.py": frozenset(
            {"agent.serializer", "agent.quote_quality", "agent.exec_reasons",
             "agent.fees"}),
        "exec_ledger.py": frozenset(
            {"agent.exec_reasons", "agent.journal", "agent.serializer",
             "recorder.persistence"}),
        "backtest_gate.py": frozenset({"agent.serializer"}),
        "run_lock.py": frozenset(),
        "secrets_runtime.py": frozenset(),
        "orchestrator.py": None,        # special row (broker-limited, below)
        "__main__.py": frozenset(
            {"agent.config", "agent.orchestrator", "agent.secrets_runtime"}),
        "broker/order_state.py": frozenset(
            {"agent.serializer", "agent.exec_reasons"}),
        "broker/fake.py": frozenset(
            {"agent.broker.base", "agent.broker.order_state",
             "agent.exec_reasons"}),
        "broker/flatten_proxy.py": frozenset(
            {"agent.broker.base", "agent.order_pricing",
             "agent.exec_reasons"}),
        "marketdata/replay_feed.py": frozenset(
            {"agent.quote_quality", "agent.bar_series",
             "agent.execution_realism", "recorder.persistence",
             "recorder.event_row", "recorder.book_state",
             "recorder.book_hash"}),
        "strategies/synthetic.py": frozenset(
            {"agent.candidate", "agent.strategy"}),
    }

    # §3 bullet 1: the pure pricing/labeling/booking family — must not import
    # the order-capable modules at ANY scope, nor reference the mint/submit
    # tokens, nor importlib/__import__ at all.
    _PURE_FAMILY = (
        "exec_reasons.py", "execution_config.py", "order_pricing.py",
        "execution_realism.py", "fees.py", "paper_book.py", "exec_ledger.py",
        "backtest_gate.py", "run_lock.py", "secrets_runtime.py",
        "marketdata/replay_feed.py",
    )
    _FORBIDDEN_MODULE_PREFIXES = (
        "agent.broker", "agent.execution_preflight", "agent.kill_switch",
        "agent.arming",
    )
    _FORBIDDEN_TOKENS = frozenset({
        "submit_order", "mint_open_token", "mint_reduce_only_token",
        "OrderIntent", "OpenPreflightToken", "ReduceOnlyPreflightToken",
        "PreflightToken", "require_token", "consume", "importlib",
        "__import__",
    })

    _BANNED_SYS_MODULES = ("alpaca", "databento", "exchange_calendars",
                           "pandas", "numpy")

    def _agent_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "scripts" / "agent"

    @staticmethod
    def _import_name(rel: str) -> str:
        return "agent." + rel[:-3].replace("/", ".")

    @staticmethod
    def _module_scope_imports(tree) -> list:
        """Module names imported at module scope (functions excluded; class
        bodies count as module scope). `from agent import x` maps to agent.x."""
        out = []

        def walk(node, in_func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, True)
                    continue
                if not in_func and isinstance(child,
                                              (ast.Import, ast.ImportFrom)):
                    if isinstance(child, ast.Import):
                        out.extend(alias.name for alias in child.names)
                    else:
                        module = child.module or ""
                        if module == "agent":
                            out.extend(f"agent.{alias.name}"
                                       for alias in child.names)
                        else:
                            out.append(module)
                walk(child, in_func)

        walk(tree, False)
        return out

    # ---------------------------------------------------------- socket block

    def test_m5_modules_import_under_socket_block_with_no_heavy_sdk(self):
        import importlib as _importlib

        with mock.patch("socket.socket",
                        side_effect=AssertionError("M5 must not open sockets")):
            for rel in self._M5_MODULES:
                _importlib.import_module(self._import_name(rel))
            # the GROWN broker modules ride the same guarantee.
            _importlib.import_module("agent.broker.alpaca")
            _importlib.import_module("agent.broker.base")
            _importlib.import_module("agent.execution_preflight")
        # FD-M5-5: `alpaca` (the SDK) is in the banned set — first, explicitly.
        self.assertNotIn("alpaca", sys.modules)
        for banned in self._BANNED_SYS_MODULES:
            self.assertNotIn(banned, sys.modules)

    def test_full_orchestrator_observe_run_opens_no_socket(self):
        import shutil
        import tempfile

        from agent.execution_preflight import unbind_runtime
        from tests.lib.exec_fixtures import (
            ExecPipeline,
            committed_assembled_config,
        )

        tmp = Path(tempfile.mkdtemp(prefix="m5-purity-observe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(unbind_runtime)
        pipeline = None
        with mock.patch("socket.socket",
                        side_effect=AssertionError("M5 must not open sockets")):
            try:
                pipeline = ExecPipeline(
                    journal_dir=tmp, run_id="run-m5-purity-observe",
                    config=committed_assembled_config())
                for index in range(50, 56):
                    pipeline.tick_on_bar(index)
            finally:
                if pipeline is not None:
                    pipeline.close()
        self.assertEqual(pipeline.orch.mode, "observe")
        self.assertIsNone(pipeline.orch.broker)
        self.assertGreater(len(pipeline.rows("decisions")), 0)
        for banned in self._BANNED_SYS_MODULES:
            self.assertNotIn(banned, sys.modules)

    # ------------------------------------------------------------- AST guard

    def test_section3_module_scope_import_whitelist(self):
        for rel, allowed in sorted(self._ALLOWED_MODULE_SCOPE.items()):
            if allowed is None:
                continue  # the orchestrator special row has its own test
            source_path = self._agent_dir() / rel
            self.assertTrue(source_path.is_file(), rel)
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            offending = []
            for name in self._module_scope_imports(tree):
                if not (name.startswith("agent") or name.startswith("recorder")):
                    continue  # stdlib is always allowed
                if not any(name == ok or name.startswith(ok + ".")
                           for ok in allowed):
                    offending.append(name)
            self.assertEqual(
                offending, [],
                f"{rel}: module-scope imports outside its §3 row: {offending}")

    def test_orchestrator_module_scope_broker_imports_limited(self):
        # §3/M5C-T10 special row: agent.broker.base + agent.broker.order_state
        # ONLY at module scope; alpaca/fake/flatten_proxy stay lazy (function
        # scope), so an observe run never imports them.
        source_path = self._agent_dir() / "orchestrator.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        allowed = {"agent.broker.base", "agent.broker.order_state"}
        offending = [
            name for name in self._module_scope_imports(tree)
            if (name == "agent.broker" or name.startswith("agent.broker."))
            and name not in allowed
        ]
        self.assertEqual(offending, [])

    def test_pure_family_forbidden_imports_and_tokens_any_scope(self):
        for rel in self._PURE_FAMILY:
            source_path = self._agent_dir() / rel
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            violations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._prefix_forbidden(alias.name):
                            violations.append(f"import {alias.name}")
                        if alias.name == "importlib" or alias.name.startswith(
                                "importlib."):
                            violations.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._prefix_forbidden(module):
                        violations.append(f"from {module} import ...")
                    if module == "importlib" or module.startswith("importlib."):
                        violations.append(f"from {module} import ...")
                    if module == "agent":
                        for alias in node.names:
                            if self._prefix_forbidden(f"agent.{alias.name}"):
                                violations.append(
                                    f"from agent import {alias.name}")
                    for alias in node.names:
                        if alias.name in self._FORBIDDEN_TOKENS:
                            violations.append(f"imported token {alias.name}")
                        if (alias.asname
                                and alias.asname in self._FORBIDDEN_TOKENS):
                            violations.append(f"alias token {alias.asname}")
                elif isinstance(node, ast.Name):
                    if node.id in self._FORBIDDEN_TOKENS:
                        violations.append(f"name {node.id}")
                elif isinstance(node, ast.Attribute):
                    if node.attr in self._FORBIDDEN_TOKENS:
                        violations.append(f"attribute .{node.attr}")
            self.assertEqual(violations, [],
                             f"{rel}: §3 pure-family violations: {violations}")

    def _prefix_forbidden(self, module_name: str) -> bool:
        if not module_name:
            return False
        return any(module_name == prefix
                   or module_name.startswith(prefix + ".")
                   for prefix in self._FORBIDDEN_MODULE_PREFIXES)

    def test_strategies_and_preflight_keep_closed_import_set(self):
        # strategies/ (synthetic AND calibration_probe) keep the M3 FD-12
        # closed set at ANY scope; execution_preflight must not import
        # agent.broker* at any scope (no cycle — the ladder consumes
        # Candidate/Leg, never OrderIntent).
        cases = (
            ("strategies/synthetic.py", self._FORBIDDEN_MODULE_PREFIXES),
            ("strategies/calibration_probe.py",
             self._FORBIDDEN_MODULE_PREFIXES),
            ("execution_preflight.py", ("agent.broker",)),
        )
        for rel, prefixes in cases:
            tree = ast.parse(
                (self._agent_dir() / rel).read_text(encoding="utf-8"))
            offending = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name == p
                               or alias.name.startswith(p + ".")
                               for p in prefixes):
                            offending.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if any(module == p or module.startswith(p + ".")
                           for p in prefixes):
                        offending.append(module)
                    if module == "agent":
                        for alias in node.names:
                            name = f"agent.{alias.name}"
                            if any(name == p or name.startswith(p + ".")
                                   for p in prefixes):
                                offending.append(name)
            self.assertEqual(offending, [], rel)

    def test_alpaca_sdk_import_only_inside_build_real_client(self):
        # FD-M5-5/§3: `alpaca` (the SDK) appears ONLY inside a function named
        # `_build_real_client`, in exactly the two allow-listed modules (the
        # broker adapter + the credentialed paper-account verifier), and never
        # at module scope anywhere under scripts/agent/.
        sites = []

        def walk(node, func_name, rel):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name, rel)
                    continue
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    names = ([alias.name for alias in child.names]
                             if isinstance(child, ast.Import)
                             else [child.module or ""])
                    for name in names:
                        if name == "alpaca" or name.startswith("alpaca."):
                            sites.append((rel, func_name))
                walk(child, func_name, rel)

        agent_dir = self._agent_dir()
        for source_path in sorted(agent_dir.rglob("*.py")):
            if "__pycache__" in source_path.parts:
                continue
            rel = source_path.relative_to(agent_dir).as_posix()
            walk(ast.parse(source_path.read_text(encoding="utf-8")), None, rel)
        self.assertTrue(sites, "expected the lazy SDK import to exist")
        self.assertEqual(
            sorted(set(sites)),
            [("broker/alpaca.py", "_build_real_client"),
             ("verify_alpaca_paper.py", "_build_real_client")],
            f"alpaca SDK import outside the allow-listed sites: {sites}")

    # ---------------------------------------------- subprocess fresh imports

    def _run_isolated(self, code: str) -> None:
        import subprocess

        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        env = dict(os.environ)
        env["PYTHONPATH"] = scripts_dir
        argv = [sys.executable, "-c", code]   # fixed command array, no shell
        completed = subprocess.run(argv, env=env, capture_output=True,
                                   text=True)
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout!r} "
                         f"stderr={completed.stderr!r}")

    def test_subprocess_pure_family_pulls_no_order_capable_module(self):
        imports = "".join(
            f"import {self._import_name(rel)}\n" for rel in self._PURE_FAMILY)
        code = (
            "import sys\n"
            + imports
            + "banned = [k for k in sys.modules\n"
            "          if k in ('agent.execution_preflight',\n"
            "                   'agent.kill_switch', 'agent.arming',\n"
            "                   'alpaca')\n"
            "          or k == 'agent.broker'\n"
            "          or k.startswith('agent.broker.')\n"
            "          or k.startswith('alpaca.')]\n"
            "assert not banned, f'order-capable modules imported: {banned}'\n"
        )
        self._run_isolated(code)

    def test_subprocess_orchestrator_import_keeps_heavy_brokers_lazy(self):
        # M5C-T10 at import time: a fresh `import agent.orchestrator` (and the
        # CLI module) pulls base+order_state ONLY — never alpaca/fake/
        # flatten_proxy, never the SDK.
        code = (
            "import sys\n"
            "import agent.orchestrator\n"
            "import agent.__main__\n"
            "assert 'agent.broker.base' in sys.modules\n"
            "assert 'agent.broker.order_state' in sys.modules\n"
            "for banned in ('agent.broker.alpaca', 'agent.broker.fake',\n"
            "               'agent.broker.flatten_proxy', 'alpaca'):\n"
            "    assert banned not in sys.modules, banned\n"
        )
        self._run_isolated(code)

    def test_subprocess_fake_broker_import_pulls_no_alpaca_module(self):
        # The fake.py erratum: SyntheticConfinementError is imported from
        # agent.broker.alpaca INSIDE _place — so importing fake.py alone must
        # not pull agent.broker.alpaca.
        code = (
            "import sys\n"
            "import agent.broker.fake\n"
            "assert 'agent.broker.alpaca' not in sys.modules\n"
            "assert 'alpaca' not in sys.modules\n"
        )
        self._run_isolated(code)

    # -------------------------------------------------- .secrets/ never read

    def test_secrets_loaders_take_explicit_paths_only(self):
        import inspect

        from agent.secrets_runtime import (
            load_alpaca_paper_credentials,
            load_run_gates,
        )

        for loader in (load_run_gates, load_alpaca_paper_credentials):
            parameters = inspect.signature(loader).parameters
            self.assertIn("path", parameters, loader.__name__)
            self.assertIs(parameters["path"].default, inspect.Parameter.empty,
                          f"{loader.__name__}: path must have NO default "
                          "(R11: no implicit .secrets/ read)")

    def test_secrets_loaders_touch_only_the_injected_tmp_paths(self):
        import json
        import shutil
        import tempfile

        from agent.secrets_runtime import (
            load_alpaca_paper_credentials,
            load_run_gates,
        )

        tmp = Path(tempfile.mkdtemp(prefix="m5-secrets-spy-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        gates_path = tmp / "run_gates.json"
        gates_path.write_text(
            json.dumps({"enabled": True, "paper_trading": {"enabled": True}}),
            encoding="utf-8")
        creds_path = tmp / "alpaca_paper.json"
        creds_path.write_text(json.dumps({
            "key_id": "k", "secret_key": "s",
            "base_url": "https://paper-api.alpaca.markets"}), encoding="utf-8")

        accessed = []
        real_read_text = Path.read_text

        def read_text_spy(self_path, *args, **kwargs):
            accessed.append(Path(self_path))
            return real_read_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text_spy):
            reading = load_run_gates(gates_path)
            credentials = load_alpaca_paper_credentials(creds_path)
        self.assertTrue(reading["enabled"])
        self.assertEqual(credentials["key_id"], "k")
        self.assertTrue(accessed, "the spy must have observed the reads")
        for path in accessed:
            self.assertNotIn(".secrets", str(path))
            self.assertEqual(Path(path).parent, tmp,
                             f"loader read outside the injected dir: {path}")


class TestM6ReconcileOfflinePurityAndImportGuard(unittest.TestCase):
    """M6 W5: the reconcile pure modules stay offline-pure and the CLI entry
    keeps its module-scope import budget while adding `agent reconcile`."""

    _PURE_RECONCILE_FILES = ("broker_reconcile.py", "reconcile_ledger.py")
    _FORBIDDEN_MODULE_PREFIXES = (
        "agent.broker", "agent.execution_preflight", "agent.kill_switch",
        "agent.arming",
    )
    _FORBIDDEN_TOKENS = frozenset({
        "submit_order", "mint_open_token", "mint_reduce_only_token",
        "OrderIntent", "OpenPreflightToken", "ReduceOnlyPreflightToken",
        "PreflightToken", "require_token", "consume", "importlib",
        "__import__",
    })
    _BANNED_SYS_MODULES = ("alpaca", "databento", "exchange_calendars",
                           "pandas", "numpy")

    def _agent_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "scripts" / "agent"

    @staticmethod
    def _module_scope_imports(tree) -> list:
        out = []

        def walk(node, in_func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, True)
                    continue
                if not in_func and isinstance(child,
                                              (ast.Import, ast.ImportFrom)):
                    if isinstance(child, ast.Import):
                        out.extend(alias.name for alias in child.names)
                    else:
                        module = child.module or ""
                        if module == "agent":
                            out.extend(f"agent.{alias.name}"
                                       for alias in child.names)
                        else:
                            out.append(module)
                walk(child, in_func)

        walk(tree, False)
        return out

    def _prefix_forbidden(self, module_name: str) -> bool:
        if not module_name:
            return False
        return any(module_name == prefix
                   or module_name.startswith(prefix + ".")
                   for prefix in self._FORBIDDEN_MODULE_PREFIXES)

    def _run_isolated(self, code: str) -> None:
        import subprocess

        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        env = dict(os.environ)
        env["PYTHONPATH"] = scripts_dir
        argv = [sys.executable, "-c", code]
        completed = subprocess.run(argv, env=env, capture_output=True,
                                   text=True)
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout!r} "
                         f"stderr={completed.stderr!r}")

    def test_reconcile_modules_import_under_socket_block_with_no_heavy_sdk(self):
        import importlib as _importlib

        with mock.patch("socket.socket",
                        side_effect=AssertionError("M6 must not open sockets")):
            _importlib.import_module("agent.broker_reconcile")
            _importlib.import_module("agent.reconcile_ledger")
            _importlib.import_module("agent.__main__")
        for banned in self._BANNED_SYS_MODULES:
            self.assertNotIn(banned, sys.modules)

    def test_pure_reconcile_family_forbidden_imports_and_tokens_any_scope(self):
        for rel in self._PURE_RECONCILE_FILES:
            source_path = self._agent_dir() / rel
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            violations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._prefix_forbidden(alias.name):
                            violations.append(f"import {alias.name}")
                        if alias.name == "importlib" or alias.name.startswith(
                                "importlib."):
                            violations.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._prefix_forbidden(module):
                        violations.append(f"from {module} import ...")
                    if module == "importlib" or module.startswith("importlib."):
                        violations.append(f"from {module} import ...")
                    if module == "agent":
                        for alias in node.names:
                            if self._prefix_forbidden(f"agent.{alias.name}"):
                                violations.append(
                                    f"from agent import {alias.name}")
                    for alias in node.names:
                        if alias.name in self._FORBIDDEN_TOKENS:
                            violations.append(f"imported token {alias.name}")
                        if (alias.asname
                                and alias.asname in self._FORBIDDEN_TOKENS):
                            violations.append(f"alias token {alias.asname}")
                elif isinstance(node, ast.Name):
                    if node.id in self._FORBIDDEN_TOKENS:
                        violations.append(f"name {node.id}")
                elif isinstance(node, ast.Attribute):
                    if node.attr in self._FORBIDDEN_TOKENS:
                        violations.append(f"attribute .{node.attr}")
            self.assertEqual(violations, [],
                             f"{rel}: M6 pure-family violations: {violations}")

    def test_reconcile_pure_family_subprocess_pulls_no_order_capable_module(self):
        code = (
            "import sys\n"
            "import agent.broker_reconcile\n"
            "import agent.reconcile_ledger\n"
            "banned = [k for k in sys.modules\n"
            "          if k in ('agent.execution_preflight',\n"
            "                   'agent.kill_switch', 'agent.arming',\n"
            "                   'alpaca')\n"
            "          or k == 'agent.broker'\n"
            "          or k.startswith('agent.broker.')\n"
            "          or k.startswith('alpaca.')]\n"
            "assert not banned, f'order-capable modules imported: {banned}'\n"
        )
        self._run_isolated(code)

    def test_cli_module_scope_import_budget_stays_closed(self):
        tree = ast.parse((self._agent_dir() / "__main__.py")
                         .read_text(encoding="utf-8"))
        allowed = {"agent.config", "agent.orchestrator",
                   "agent.secrets_runtime"}
        offending = []
        for name in self._module_scope_imports(tree):
            if not name.startswith("agent"):
                continue
            if not any(name == ok or name.startswith(ok + ".")
                       for ok in allowed):
                offending.append(name)
        self.assertEqual(offending, [])

    def test_cmd_reconcile_uses_cli_owned_explicit_secret_paths(self):
        source = (self._agent_dir() / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('credentials_path=_SECRETS / "alpaca_paper.json"', source)
        self.assertIn('run_gates_path=_SECRETS / "run_gates.json"', source)
        self.assertNotIn("load_alpaca_paper_credentials()", source)
        self.assertNotIn("load_run_gates()", source)

    def test_orchestrator_still_has_no_wall_clock_or_sleep_calls(self):
        tree = ast.parse((self._agent_dir() / "orchestrator.py")
                         .read_text(encoding="utf-8"))
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"time", "datetime"}:
                        violations.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module in {"time", "datetime"}:
                    violations.append(f"from {node.module} import ...")
            elif isinstance(node, ast.Attribute):
                if node.attr in {"sleep", "time", "monotonic", "now", "utcnow"}:
                    violations.append(f"attribute .{node.attr}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
