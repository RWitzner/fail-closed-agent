"""M2 §C — MarketStateCache freshness-gated NON-BLOCKING cache tests (contract §J
test_market_state_cache.py).

Offline, stdlib-only. The cache is a PURE function of explicit injected inputs
(no wall clock, hidden state, or network) — the ms-clock seam is the injected
``FakeClock`` (tests/lib/fakes.py), the SAME seam ``HeartbeatMonitor`` uses
(status.py:162). On staleness or a miss it degrades to the MOST-RESTRICTIVE
``safe_default_verdict`` — it NEVER blocks on a refresh and NEVER serves a stale
"tradable". The boundary is strict ``>`` (fresh at exactly ``ttl_ms``, stale at
``ttl_ms+1``) mirroring ``status.py:178``.

Named cases (contract §J):
  - fresh entry within ttl returned; boundary strict '>' (status.py:178),
  - stale -> safe default; missing -> safe default (fail-closed/non-blocking),
  - safe default has EVERY enum field most-restrictive (S7-7),
  - get is non-blocking (no inline refresh),
  - refresh_set unions open positions (held symbol outside the universe stays in),
  - instrument_id mismatch -> safe default (MED-7),
  - ttl is NOT config-overlayable (§G trap),
  - ttl override cannot LOOSEN (HIGH-3 clamp): ttl_ms > DEFAULT raises ValueError;
    a smaller ttl is accepted.
"""
import unittest
from decimal import Decimal

from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)
from agent.market_state_cache import (
    DEFAULT_FRESHNESS_TTL_MS,
    CacheEntry,
    MarketStateCache,
)

from tests.lib.fakes import FakeClock


def _tradable_verdict(symbol="AAPL", instrument_id=1001, session_date_et="2026-06-09"):
    """A TRADABLE verdict (the opposite of the safe default) so a served stale
    'tradable' would be unmistakable."""
    return Verdict(
        symbol=symbol,
        instrument_id=instrument_id,
        session_state=SessionState.RTH,
        tradability=Tradability.TRADABLE,
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=True,
        short_allowed=True,
        reasons=(),
        ca_blackout=False,
        session_date_et=session_date_et,
    )


class TestFreshness(unittest.TestCase):
    def test_fresh_entry_returned_within_ttl(self):
        clock = FakeClock(start_ms=1000)
        cache = MarketStateCache(clock=clock)
        verdict = _tradable_verdict()
        cache.put(verdict)
        clock.advance(DEFAULT_FRESHNESS_TTL_MS - 1)  # still fresh
        got = cache.get("AAPL", 1001, "2026-06-09")
        self.assertEqual(got, verdict)
        self.assertEqual(got.tradability, Tradability.TRADABLE)

    def test_boundary_is_strict_greater_than(self):
        # Fresh at exactly ttl_ms, stale at ttl_ms+1 (mirrors status.py:178 strict '>').
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        verdict = _tradable_verdict()
        cache.put(verdict, now_ms=0)
        # exactly ttl_ms elapsed -> still fresh
        got = cache.get("AAPL", 1001, "2026-06-09", now_ms=DEFAULT_FRESHNESS_TTL_MS)
        self.assertEqual(got.tradability, Tradability.TRADABLE)
        # ttl_ms + 1 -> stale -> safe default
        got = cache.get("AAPL", 1001, "2026-06-09", now_ms=DEFAULT_FRESHNESS_TTL_MS + 1)
        self.assertEqual(got.tradability, Tradability.NOT_TRADABLE)
        self.assertIn("cache_stale_safe_default", got.reasons)


class TestDegradeToSafe(unittest.TestCase):
    def test_stale_entry_degrades_to_safe_default(self):
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        cache.put(_tradable_verdict(), now_ms=0)
        clock.advance(DEFAULT_FRESHNESS_TTL_MS + 5)  # stale
        got = cache.get("AAPL", 1001, "2026-06-09")
        self.assertEqual(got, MarketStateCache.safe_default_verdict("AAPL", 1001, "2026-06-09"))
        self.assertEqual(got.tradability, Tradability.NOT_TRADABLE)

    def test_missing_entry_is_safe_default(self):
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        got = cache.get("MSFT", 4242, "2026-06-09")
        self.assertEqual(got, MarketStateCache.safe_default_verdict("MSFT", 4242, "2026-06-09"))
        self.assertEqual(got.tradability, Tradability.NOT_TRADABLE)
        # The default carries the REQUESTED identity + session date.
        self.assertEqual(got.symbol, "MSFT")
        self.assertEqual(got.instrument_id, 4242)
        self.assertEqual(got.session_date_et, "2026-06-09")

    def test_safe_default_every_enum_field_most_restrictive(self):
        # S7-7: EVERY enum field pinned to its most-restrictive member; session_state
        # is the honest 'we don't know' UNKNOWN (LOW-1), NOT HALTED.
        v = MarketStateCache.safe_default_verdict("AAPL", 1001, "2026-06-09")
        self.assertEqual(v.session_state, SessionState.UNKNOWN)
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
        self.assertEqual(v.halt, HaltState.UNKNOWN)
        self.assertEqual(v.luld, LuldState.UNKNOWN)
        self.assertEqual(v.ssr, SsrState.UNKNOWN)
        self.assertFalse(v.two_sided_nbbo)
        self.assertFalse(v.short_allowed)
        self.assertTrue(v.ca_blackout)
        self.assertEqual(v.reasons, ("cache_stale_safe_default",))


class TestNonBlocking(unittest.TestCase):
    def test_get_is_non_blocking(self):
        # get() NEVER computes/refreshes inline. A miss returns the safe default
        # WITHOUT mutating the cache (no entry materialized) and without any
        # injected refresh callback being invoked (there is none).
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        # Miss -> safe default; a subsequent is_fresh stays False (nothing stored).
        cache.get("AAPL", 1001, "2026-06-09")
        self.assertFalse(cache.is_fresh("AAPL"))
        self.assertEqual(cache.stale_symbols(["AAPL"]), ("AAPL",))


class TestRefreshSet(unittest.TestCase):
    def test_refresh_set_unions_open_positions(self):
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        out = cache.refresh_set(
            candidate_symbols=["AAPL", "MSFT"],
            open_position_symbols=["GME", "AAPL"],  # GME outside the universe, AAPL dup
        )
        # Union, deduped, SORTED for determinism. Held GME stays in even though it
        # left the candidate universe.
        self.assertEqual(out, ("AAPL", "GME", "MSFT"))


class TestInstrumentIdMismatch(unittest.TestCase):
    def test_instrument_id_mismatch_returns_safe_default(self):
        # MED-7: entry stored for (AAPL, 1001); get("AAPL", 2002, ...) is a MISS.
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        cache.put(_tradable_verdict(symbol="AAPL", instrument_id=1001), now_ms=0)
        got = cache.get("AAPL", 2002, "2026-06-09", now_ms=0)
        self.assertEqual(
            got, MarketStateCache.safe_default_verdict("AAPL", 2002, "2026-06-09")
        )
        self.assertEqual(got.tradability, Tradability.NOT_TRADABLE)
        self.assertEqual(got.instrument_id, 2002)
        # The matching instrument is still served fresh.
        match = cache.get("AAPL", 1001, "2026-06-09", now_ms=0)
        self.assertEqual(match.tradability, Tradability.TRADABLE)


class TestTtlClamp(unittest.TestCase):
    def test_ttl_not_config_overlayable(self):
        # §G trap: ttl_ms is a CODE CONSTANT. The default ctor uses it; there is no
        # config overlay path. A larger TTL would be staler == dangerous, so the
        # only way to set ttl is the test-only ctor override, which cannot loosen.
        clock = FakeClock(start_ms=0)
        cache = MarketStateCache(clock=clock)
        self.assertEqual(DEFAULT_FRESHNESS_TTL_MS, 2000)
        # Default ctor pins ttl to the code constant (no overlay applied).
        cache.put(_tradable_verdict(), now_ms=0)
        self.assertTrue(cache.is_fresh("AAPL", now_ms=DEFAULT_FRESHNESS_TTL_MS))
        self.assertFalse(cache.is_fresh("AAPL", now_ms=DEFAULT_FRESHNESS_TTL_MS + 1))

    def test_ttl_override_cannot_loosen(self):
        # HIGH-3 clamp: ttl_ms > DEFAULT raises ValueError; a smaller ttl is accepted.
        clock = FakeClock(start_ms=0)
        with self.assertRaises(ValueError):
            MarketStateCache(clock=clock, ttl_ms=DEFAULT_FRESHNESS_TTL_MS + 1)
        # A SHORTER (tighter) ttl is allowed.
        cache = MarketStateCache(clock=clock, ttl_ms=DEFAULT_FRESHNESS_TTL_MS - 1)
        cache.put(_tradable_verdict(), now_ms=0)
        # Fresh at exactly the tighter ttl, stale one ms later.
        self.assertTrue(cache.is_fresh("AAPL", now_ms=DEFAULT_FRESHNESS_TTL_MS - 1))
        self.assertFalse(cache.is_fresh("AAPL", now_ms=DEFAULT_FRESHNESS_TTL_MS))
        # Exactly DEFAULT is allowed (<=).
        MarketStateCache(clock=clock, ttl_ms=DEFAULT_FRESHNESS_TTL_MS)


class TestCacheEntry(unittest.TestCase):
    def test_cache_entry_is_frozen(self):
        entry = CacheEntry(verdict=_tradable_verdict(), refreshed_at_ms=123)
        self.assertEqual(entry.refreshed_at_ms, 123)
        with self.assertRaises(Exception):
            entry.refreshed_at_ms = 999  # frozen


if __name__ == "__main__":
    unittest.main()
