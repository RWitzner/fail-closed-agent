"""Track B status plane — fail-closed state machine + pump + source gating.

Pins the design-doc rules: unknown symbol ⇒ None; stale/disconnected stream ⇒
None for EVERY symbol; reconnect requires re-observation (epoch bump);
same-instant conflicts resolve most-restrictive; malformed events drop their
symbol; LULD bands decay to UNKNOWN; transitions are drainable for
journaling; the Alpaca source is UNVERIFIED-fail-closed."""
import unittest

from agent.market_state import HaltState, LuldState, SsrState
from agent.marketdata.status_plane import (
    LiveStatusProvider,
    alpaca_status_source,
    pump_status_events,
)

_TS0 = "2026-07-06T13:30:00.000000Z"
_TS1 = "2026-07-06T14:00:00.000000Z"
_TS2 = "2026-07-06T14:05:00.000000Z"


class _Clock:
    def __init__(self, start_ms=1_000):
        self.ms = start_ms

    def now_ms(self):
        return self.ms


def _baseline(symbol="AAPL", ts=_TS0, **overrides):
    event = {
        "symbol": symbol, "ts_event_utc": ts, "source": "alpaca",
        "halt": "none", "halt_reason": "none",
        "luld": "normal",
        "luld_band": {"reference_px": "100", "lower_px": "95",
                      "upper_px": "105", "tier": "tier1", "doubled": False},
        "ssr": "inactive",
    }
    event.update(overrides)
    return event


class TestLiveStatusProvider(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.provider = LiveStatusProvider(self.clock)

    def test_never_observed_symbol_is_none(self):
        self.assertIsNone(self.provider("AAPL", _TS1))
        self.provider.ingest(_baseline("MSFT"))
        self.assertIsNone(self.provider("AAPL", _TS1))

    def test_baseline_yields_healthy_flags(self):
        self.provider.ingest(_baseline())
        flags = self.provider("AAPL", _TS1)
        self.assertEqual(flags.halt, HaltState.NONE)
        self.assertEqual(flags.luld, LuldState.NORMAL)
        self.assertEqual(flags.ssr, SsrState.INACTIVE)
        self.assertEqual(flags.source, "alpaca")
        self.assertEqual(str(flags.luld_band.lower_px), "95")

    def test_halt_overlay_keeps_other_fields(self):
        self.provider.ingest(_baseline())
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "halted", "halt_reason": "news_pending"})
        flags = self.provider("AAPL", _TS2)
        self.assertEqual(flags.halt, HaltState.HALTED)
        self.assertEqual(flags.luld, LuldState.NORMAL)
        self.assertEqual(flags.ssr, SsrState.INACTIVE)

    def test_out_of_order_older_event_is_ignored(self):
        self.provider.ingest(_baseline())
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS2, "source": "alpaca",
            "halt": "halted", "halt_reason": "volatility"})
        self.provider.ingest({          # late delivery of an OLDER resume
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "none", "halt_reason": "none"})
        self.assertEqual(self.provider("AAPL", _TS2).halt, HaltState.HALTED)

    def test_same_instant_conflict_resolves_most_restrictive(self):
        self.provider.ingest(_baseline())
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "halted", "halt_reason": "volatility"})
        self.provider.ingest({          # same instant, less restrictive
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "none", "halt_reason": "none"})
        self.assertEqual(self.provider("AAPL", _TS2).halt, HaltState.HALTED)

    def test_stale_stream_blocks_every_symbol(self):
        self.provider.ingest(_baseline())
        self.clock.ms += 15_001
        self.assertIsNone(self.provider("AAPL", _TS1))

    def test_disconnect_requires_reobservation(self):
        self.provider.ingest(_baseline())
        self.provider.mark_disconnected(reason="test")
        self.assertIsNone(self.provider("AAPL", _TS1))
        self.provider.ingest(_baseline("MSFT", ts=_TS1))
        self.assertIsNone(self.provider("AAPL", _TS1))   # not re-observed
        self.assertIsNotNone(self.provider("MSFT", _TS1))
        self.provider.ingest(_baseline(ts=_TS1))
        self.assertIsNotNone(self.provider("AAPL", _TS1))

    def test_band_decay_degrades_luld_to_unknown(self):
        self.provider.ingest(_baseline())
        self.clock.ms += 300_001
        self.provider.ingest({          # keeps the stream alive
            "symbol": "MSFT", "ts_event_utc": _TS1, "source": "alpaca",
            "ssr": "inactive"})
        flags = self.provider("AAPL", _TS2)
        self.assertEqual(flags.luld, LuldState.UNKNOWN)
        self.assertIsNone(flags.luld_band)
        self.assertEqual(flags.halt, HaltState.NONE)     # halt keeps event semantics

    def test_malformed_event_drops_symbol(self):
        self.provider.ingest(_baseline())
        self.assertIsNotNone(self.provider("AAPL", _TS1))
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "not-a-halt-state", "halt_reason": "none"})
        self.assertIsNone(self.provider("AAPL", _TS2))
        rows = self.provider.drain_transitions()
        self.assertTrue(any(r["field"] == "malformed" and r["symbol"] == "AAPL"
                            for r in rows))

    def test_transitions_are_drainable_with_provenance(self):
        self.provider.ingest(_baseline())
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "halt": "halted", "halt_reason": "news_pending"})
        rows = self.provider.drain_transitions()
        halt_rows = [r for r in rows if r["field"] == "halt"]
        self.assertEqual([(r["from"], r["to"]) for r in halt_rows],
                         [(None, "none"), ("none", "halted")])
        self.assertTrue(all(r["source"] == "alpaca" and r["seen_at_ms"] == 1_000
                            for r in halt_rows))
        self.assertEqual(self.provider.drain_transitions(), [])

    def test_ssr_persists_across_luld_updates(self):
        self.provider.ingest(_baseline(ssr="active"))
        self.provider.ingest({
            "symbol": "AAPL", "ts_event_utc": _TS1, "source": "alpaca",
            "luld": "limit",
            "luld_band": {"reference_px": "100", "lower_px": "98",
                          "upper_px": "102", "tier": "tier1",
                          "doubled": False}})
        flags = self.provider("AAPL", _TS2)
        self.assertEqual(flags.ssr, SsrState.ACTIVE)
        self.assertEqual(flags.luld, LuldState.LIMIT)


class TestPumpAndSource(unittest.TestCase):
    def test_pump_ingests_then_marks_disconnected_on_exhaustion(self):
        clock = _Clock()
        provider = LiveStatusProvider(clock)
        pump_status_events([_baseline()], provider)
        self.assertFalse(provider.connected)
        self.assertIsNone(provider("AAPL", _TS1))   # post-exhaustion: blocked
        rows = provider.drain_transitions()
        self.assertTrue(any(r["field"] == "stream"
                            and r["detail"] == "source_exhausted"
                            for r in rows))

    def test_pump_never_raises_on_source_error(self):
        clock = _Clock()
        provider = LiveStatusProvider(clock)

        def exploding():
            yield _baseline()
            raise RuntimeError("stream died")

        pump_status_events(exploding(), provider)
        self.assertFalse(provider.connected)
        rows = provider.drain_transitions()
        self.assertTrue(any(r["field"] == "stream"
                            and "stream died" in (r.get("detail") or "")
                            for r in rows))

    def test_alpaca_source_is_unverified_fail_closed(self):
        with self.assertRaisesRegex(NotImplementedError, "UNVERIFIED"):
            alpaca_status_source(symbols=["AAPL"])
        with self.assertRaisesRegex(NotImplementedError, "UNVERIFIED"):
            alpaca_status_source(symbols=["AAPL"],
                                 allow_unverified_status=True)

    def test_injected_stream_factory_is_the_offline_seam(self):
        events = [_baseline()]
        source = alpaca_status_source(symbols=["AAPL"],
                                      stream_factory=lambda: iter(events))
        clock = _Clock()
        provider = LiveStatusProvider(clock)
        pump_status_events(source, provider)
        self.assertEqual(len(provider.drain_transitions()) >= 3, True)


if __name__ == "__main__":
    unittest.main()
