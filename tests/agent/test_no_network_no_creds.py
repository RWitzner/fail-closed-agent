"""M0 makes no network calls and needs no secrets (supports S1's spirit).

The agent modules import no broker/data SDK, the M0 flows open no socket, and the
suite is green with `.secrets/` absent.
"""
import ast
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


if __name__ == "__main__":
    unittest.main()
