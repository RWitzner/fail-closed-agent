"""M4 §M test 2 — account/portfolio read models, parser chokepoint, freshness store.

Invariants: S2 (float/NaN/Inf never reaches a snapshot), R1 (fail-closed staleness),
R12 (clock regression = skew, DATA not exception).
"""
import unittest
from decimal import Decimal

from agent.risk.account_state import (
    ACCOUNT_FRESHNESS_TTL_MS,
    MARK_FRESHNESS_TTL_MS,
    PORTFOLIO_FRESHNESS_TTL_MS,
    AccountInvalid,
    AccountReadProvider,
    AccountStore,
    BrokerAccountRead,
    Mark,
    PortfolioRead,
    parse_account_payload,
    parse_positions_payload,
    portfolio_is_stale,
)
from agent.serializer import BrokerUSD
from tests.lib.fakes import FakeClock
from tests.lib.risk_fixtures import OMIT, FakeAccountProvider, account_payload


def _parse(payload, *, seen_at_ms=0, source="fixture"):
    return parse_account_payload(
        payload, source=source, seen_at_ms=seen_at_ms,
        ts_read_utc="2026-06-08T14:00:00.000000Z")


class TestParseAccountPayload(unittest.TestCase):
    def test_canonical_payload_parses_to_broker_account_read(self):
        read = _parse(account_payload())
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertIsInstance(read.equity, BrokerUSD)
        self.assertIsInstance(read.buying_power, BrokerUSD)
        self.assertEqual(read.equity, Decimal("100000.00"))
        self.assertEqual(read.last_equity, Decimal("100000.00"))
        self.assertEqual(read.cash, Decimal("40000.00"))
        self.assertEqual(read.buying_power, Decimal("200000.00"))
        self.assertEqual(read.maintenance_margin, Decimal("30000.00"))
        self.assertEqual(read.multiplier, Decimal("2"))
        self.assertEqual(read.daytrading_buying_power, Decimal("400000.00"))
        self.assertIs(read.pattern_day_trader, False)
        self.assertEqual(read.daytrade_count, 0)
        self.assertEqual(read.source, "fixture")
        self.assertTrue(read.account_snapshot_id.startswith("as-"))

    def test_missing_required_field_is_account_invalid(self):
        for field in ("equity", "last_equity", "cash", "buying_power", "maintenance_margin"):
            result = _parse(account_payload(**{field: OMIT}))
            self.assertIsInstance(result, AccountInvalid)
            self.assertEqual(result.reason, f"missing_field:{field}")

    def test_float_typed_money_is_account_invalid(self):
        result = _parse(account_payload(cash=100.5))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "float_typed:cash")

    def test_bool_typed_money_is_account_invalid(self):
        result = _parse(account_payload(equity=True))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "bool_typed:equity")

    def test_non_finite_money_is_account_invalid(self):
        result = _parse(account_payload(equity="NaN"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "non_finite:equity")
        result = _parse(account_payload(last_equity=Decimal("Infinity")))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "non_finite:last_equity")

    def test_negative_buying_power_or_maintenance_is_account_invalid(self):
        result = _parse(account_payload(buying_power="-1"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "negative:buying_power")
        result = _parse(account_payload(maintenance_margin="-0.01"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "negative:maintenance_margin")

    def test_unparseable_and_invalid_type_money(self):
        result = _parse(account_payload(cash="not-a-number"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "unparseable:cash")
        result = _parse(account_payload(cash=[1]))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "invalid_type:cash")

    def test_pattern_day_trader_must_be_strict_bool_or_absent(self):
        read = _parse(account_payload(pattern_day_trader=OMIT))
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertIsNone(read.pattern_day_trader)
        result = _parse(account_payload(pattern_day_trader="true"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "non_bool:pattern_day_trader")

    def test_daytrade_count_must_be_int_or_absent(self):
        read = _parse(account_payload(daytrade_count=OMIT))
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertIsNone(read.daytrade_count)
        result = _parse(account_payload(daytrade_count="3"))
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "non_int:daytrade_count")

    def test_m0_spy_payload_parses_to_account_invalid(self):
        # The M0 spy broker account() has only 2 keys (alpaca.py:26) — NOT an
        # account of record (§B).
        spy_payload = {"equity": Decimal("0"), "buying_power": Decimal("0")}
        result = _parse(spy_payload, source="spy")
        self.assertIsInstance(result, AccountInvalid)
        self.assertEqual(result.reason, "missing_field:last_equity")

    def test_out_of_vocab_source_raises(self):
        with self.assertRaises(ValueError):
            _parse(account_payload(), source="binance")

    def test_account_snapshot_id_deterministic_and_stamp_independent(self):
        # M4C-6/F9: same broker numbers re-read => same id; seen_at_ms and
        # ts_read_utc must NOT enter the hash.
        a = parse_account_payload(account_payload(), source="fixture",
                                  seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
        b = parse_account_payload(account_payload(), source="fixture",
                                  seen_at_ms=99999, ts_read_utc="2026-06-09T15:30:00Z")
        self.assertEqual(a.account_snapshot_id, b.account_snapshot_id)
        c = parse_account_payload(account_payload(equity="100000.01"), source="fixture",
                                  seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
        self.assertNotEqual(a.account_snapshot_id, c.account_snapshot_id)


class TestAccountStore(unittest.TestCase):
    def test_never_put_is_missing(self):
        store = AccountStore(clock=FakeClock(start_ms=0))
        read = store.get()
        self.assertEqual(read.status, "missing")
        self.assertIsNone(read.read)
        self.assertIsNone(read.age_ms)
        self.assertIsNone(read.invalid_reason)

    def test_fresh_at_exactly_ttl_stale_at_plus_one(self):
        clock = FakeClock(start_ms=0)
        store = AccountStore(clock=clock)
        store.put(_parse(account_payload(), seen_at_ms=0))
        clock.advance(ACCOUNT_FRESHNESS_TTL_MS)
        read = store.get()
        self.assertEqual(read.status, "fresh")
        self.assertEqual(read.age_ms, ACCOUNT_FRESHNESS_TTL_MS)
        self.assertIsInstance(read.read, BrokerAccountRead)
        clock.advance(1)
        read = store.get()
        self.assertEqual(read.status, "stale")  # strict '>' (FD-M4-22)
        self.assertIsInstance(read.read, BrokerAccountRead)  # stale still carries the read

    def test_clock_regression_is_skew(self):
        # R12: now_ms < seen_at_ms => "skew" — DATA, never an exception.
        clock = FakeClock(start_ms=100)
        store = AccountStore(clock=clock)
        store.put(_parse(account_payload(), seen_at_ms=200))
        read = store.get()
        self.assertEqual(read.status, "skew")
        self.assertIsNone(read.read)

    def test_invalid_put_reads_invalid(self):
        store = AccountStore(clock=FakeClock(start_ms=10))
        store.put(_parse(account_payload(equity="NaN"), seen_at_ms=0))
        read = store.get()
        self.assertEqual(read.status, "invalid")
        self.assertIsNone(read.read)
        self.assertEqual(read.invalid_reason, "non_finite:equity")

    def test_ctor_clamp_raises_on_longer_ttl(self):
        with self.assertRaises(ValueError):
            AccountStore(clock=FakeClock(), ttl_ms=ACCOUNT_FRESHNESS_TTL_MS + 1)
        AccountStore(clock=FakeClock(), ttl_ms=ACCOUNT_FRESHNESS_TTL_MS - 1)  # shorten OK

    def test_latest_unsafe_returns_under_every_degraded_status(self):
        clock = FakeClock(start_ms=0)
        store = AccountStore(clock=clock)
        self.assertIsNone(store.latest_unsafe())  # missing
        good = _parse(account_payload(), seen_at_ms=0)
        store.put(good)
        clock.advance(ACCOUNT_FRESHNESS_TTL_MS + 1)
        self.assertEqual(store.get().status, "stale")
        self.assertIs(store.latest_unsafe(), good)          # stale
        store.put(_parse(account_payload(equity="NaN"), seen_at_ms=clock.now_ms()))
        self.assertEqual(store.get().status, "invalid")
        self.assertIs(store.latest_unsafe(), good)          # invalid keeps last good read
        store.put(_parse(account_payload(), seen_at_ms=clock.now_ms() + 500))
        self.assertEqual(store.get().status, "skew")
        self.assertIsNotNone(store.latest_unsafe())         # skew


class TestPositionsParser(unittest.TestCase):
    def test_parses_sorted_with_zero_qty_dropped(self):
        rows = [
            {"symbol": "MSFT", "qty": "-5", "market_value": "-2100.00"},
            {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
            {"symbol": "ZZZ", "qty": "0", "market_value": "0"},  # LD-R4: dropped
        ]
        pf = parse_positions_payload(rows, source="fixture", seen_at_ms=0)
        self.assertEqual([p.symbol for p in pf.positions], ["AAPL", "MSFT"])
        self.assertEqual(pf.qty_for("AAPL"), Decimal("10"))
        self.assertEqual(pf.qty_for("MSFT"), Decimal("-5"))
        self.assertEqual(pf.qty_for("ZZZ"), Decimal("0"))  # flat is not held
        self.assertIsInstance(pf.positions[0].market_value, BrokerUSD)
        self.assertIs(pf.unreconciled_drift, False)

    def test_duplicate_symbol_raises(self):
        rows = [
            {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
            {"symbol": "AAPL", "qty": "1", "market_value": "190.00"},
        ]
        with self.assertRaises(ValueError):
            parse_positions_payload(rows, source="fixture", seen_at_ms=0)

    def test_missing_market_value_raises(self):
        with self.assertRaises(ValueError):
            parse_positions_payload([{"symbol": "AAPL", "qty": "10"}],
                                    source="fixture", seen_at_ms=0)

    def test_float_or_nonfinite_raises(self):
        with self.assertRaises(ValueError):
            parse_positions_payload(
                [{"symbol": "AAPL", "qty": 10.0, "market_value": "1900.00"}],
                source="fixture", seen_at_ms=0)
        with self.assertRaises(ValueError):
            parse_positions_payload(
                [{"symbol": "AAPL", "qty": "10", "market_value": "NaN"}],
                source="fixture", seen_at_ms=0)

    def test_out_of_vocab_source_raises(self):
        with self.assertRaises(ValueError):
            parse_positions_payload([], source="binance", seen_at_ms=0)


class TestPortfolioIsStale(unittest.TestCase):
    def test_strict_gt_boundary(self):
        self.assertFalse(portfolio_is_stale(0, PORTFOLIO_FRESHNESS_TTL_MS))
        self.assertTrue(portfolio_is_stale(0, PORTFOLIO_FRESHNESS_TTL_MS + 1))

    def test_shorten_only_clamp(self):
        with self.assertRaises(ValueError):
            portfolio_is_stale(0, 0, ttl_ms=PORTFOLIO_FRESHNESS_TTL_MS + 1)
        self.assertTrue(portfolio_is_stale(0, 11, ttl_ms=10))  # shorten OK


class TestMark(unittest.TestCase):
    def test_valid_mark(self):
        mark = Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("190.123456"),
                    seen_at_ms=0, source="quote_mid")
        self.assertEqual(mark.mid, Decimal("190.123456"))

    def test_mark_validation(self):
        with self.assertRaises(ValueError):
            Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("0"),
                 seen_at_ms=0, source="quote_mid")
        with self.assertRaises(ValueError):
            Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("NaN"),
                 seen_at_ms=0, source="quote_mid")
        with self.assertRaises(ValueError):
            Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("190.1234567"),
                 seen_at_ms=0, source="quote_mid")  # off the MID_QUANTUM grid
        with self.assertRaises(ValueError):
            Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("190.12"),
                 seen_at_ms=0, source="last_trade")  # out-of-vocab source

    def test_mark_grid_check_immune_to_ambient_decimal_context(self):
        # harden round, M4-DET-2: the MID_QUANTUM grid check must run under the
        # pinned context (prec=28, ROUND_HALF_EVEN), never the ambient one — a
        # caller-shrunk global context must neither reject a VALID on-grid Mark
        # with InvalidOperation (FD-M4-16: a mark is an optional tightener whose
        # assembly must never crash) nor accept an off-grid one (codebase pattern:
        # test_calibration.py test_persisted_arithmetic_immune_to_ambient_decimal_context).
        import decimal

        original = decimal.getcontext()
        hostile = decimal.Context(prec=3, rounding=decimal.ROUND_UP)
        try:
            decimal.setcontext(hostile)
            mark = Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("191.000000"),
                        seen_at_ms=0, source="quote_mid")  # 9 digits > ambient prec
            self.assertEqual(mark.mid, Decimal("191.000000"))
            with self.assertRaises(ValueError):
                Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("190.1234567"),
                     seen_at_ms=0, source="quote_mid")  # off-grid still rejected
        finally:
            decimal.setcontext(original)


class TestProviderSeam(unittest.TestCase):
    def test_fake_provider_satisfies_protocol_and_scripts_payloads(self):
        provider = FakeAccountProvider(
            account_payloads=[account_payload(), account_payload(equity="99000.00")],
            positions_payloads=[[]],
        )
        self.assertIsInstance(provider, AccountReadProvider)
        first = provider.account_payload()
        second = provider.account_payload()
        self.assertEqual(first["equity"], "100000.00")
        self.assertEqual(second["equity"], "99000.00")
        self.assertEqual(provider.positions_payload(), [])

    def test_ttl_constants_pinned(self):
        self.assertEqual(ACCOUNT_FRESHNESS_TTL_MS, 5000)
        self.assertEqual(PORTFOLIO_FRESHNESS_TTL_MS, 5000)
        self.assertEqual(MARK_FRESHNESS_TTL_MS, 2000)


if __name__ == "__main__":
    unittest.main()
