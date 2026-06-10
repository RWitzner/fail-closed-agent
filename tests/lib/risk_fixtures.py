"""M4 §L — fixture builders for the risk-core tests.

Pure: no wall clock, no randomness; Decimal-string money in Alpaca wire shape so the
M5 adapter is a pass-through. Builders that need not-yet-imported risk modules import
them function-locally so this module stays import-light.
"""
import json
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

OMIT = object()  # sentinel: account_payload(field=OMIT) deletes the key


def account_payload(**overrides) -> dict:
    """Canonical Alpaca-shaped account dict (§L); overrides delete (OMIT) / replace keys."""
    payload = {
        "equity": "100000.00",
        "last_equity": "100000.00",
        "cash": "40000.00",
        "buying_power": "200000.00",
        "maintenance_margin": "30000.00",
        "multiplier": "2",
        "daytrading_buying_power": "400000.00",
        "pattern_day_trader": False,
        "daytrade_count": 0,
    }
    for key, value in overrides.items():
        if value is OMIT:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


class FakeAccountProvider:
    """Scripted AccountReadProvider double (the LD8 seam). Successive calls return the
    scripted payloads in order; the last payload repeats once the script is exhausted."""

    def __init__(self, *, account_payloads=None, positions_payloads=None):
        self._account_payloads = list(account_payloads or [account_payload()])
        self._positions_payloads = list(positions_payloads or [[]])
        self._account_i = 0
        self._positions_i = 0

    def account_payload(self) -> dict:
        i = min(self._account_i, len(self._account_payloads) - 1)
        self._account_i += 1
        return self._account_payloads[i]

    def positions_payload(self) -> list:
        i = min(self._positions_i, len(self._positions_payloads) - 1)
        self._positions_i += 1
        return self._positions_payloads[i]


_PORTFOLIO_ROWS = {
    "flat": [],
    "long_short": [
        {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
        {"symbol": "MSFT", "qty": "-5", "market_value": "-2100.00"},
    ],
    "long_only": [
        {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
    ],
    "dup_symbol": [
        {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
        {"symbol": "AAPL", "qty": "1", "market_value": "190.00"},
    ],
    "zero_qty_row": [
        {"symbol": "AAPL", "qty": "10", "market_value": "1900.00"},
        {"symbol": "MSFT", "qty": "-5", "market_value": "-2100.00"},
        {"symbol": "ZERO", "qty": "0", "market_value": "0"},
    ],
}


def portfolio_fixture(name: str, *, seen_at_ms: int = 0, stale: bool = False):
    """Parsed PortfolioRead for a named fixture ("dup_symbol" raises at parse)."""
    from agent.risk.account_state import parse_positions_payload

    return parse_positions_payload(_PORTFOLIO_ROWS[name], source="fixture",
                                   seen_at_ms=seen_at_ms, stale=stale)


def margin_calendar_fixture() -> dict:
    """The committed contiguous margin-window calendar (§L)."""
    path = _FIXTURES / "calendar" / "nyse_margin_window_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def margin_calendar_provider():
    from agent.market_calendar import FixtureScheduleProvider

    fixture = margin_calendar_fixture()
    return FixtureScheduleProvider(fixture, pin=fixture["pin"])


def margin_day(observations, *, session_date_et: str = "2026-06-08",
               source: str = "fixture"):
    """Build MarginObservation sequences from (equity, maintenance, after_iml_reducing,
    eod) string tuples for ONE session date (§L)."""
    from agent.risk.account_state import parse_account_payload
    from agent.risk.intraday_margin import observation_from_read

    built = []
    for i, (equity, maintenance, after_iml_reducing, eod) in enumerate(observations):
        read = parse_account_payload(
            account_payload(equity=equity, last_equity=equity,
                            maintenance_margin=maintenance),
            source=source, seen_at_ms=i,
            ts_read_utc=f"{session_date_et}T14:{i:02d}:00.000000Z")
        built.append(observation_from_read(
            read, session_date_et=session_date_et,
            after_iml_reducing=after_iml_reducing, eod=eod))
    return built


def deficit_boundary_cases():
    """The FD-M4-14 triples: (equity, amount, expected_minor)."""
    return [
        ("18000", "900", True),       # 5% binds: 900 == min(900, 1000) -> minor
        ("18000", "900.01", False),
        ("100000", "1000", True),     # $1,000 binds
        ("100000", "1000.01", False),
    ]


def freeze_timeline() -> dict:
    """Hand-computed key dates for D=2026-06-08 over nyse_margin_window_v1 (§L).

    bd5 counts 06-09,10,11,12,15; bd15 crosses the 06-19 Juneteenth holiday and lands
    on 06-30 (06-29 without it). effective_from = first business day after the bd5
    close; expires_on = effective_from + 90 calendar days (EXCLUSIVE end).
    """
    return {
        "deficit_date": "2026-06-08",
        "bd1": "2026-06-09",
        "bd2": "2026-06-10",
        "bd3": "2026-06-11",
        "bd4": "2026-06-12",
        "bd5": "2026-06-15",
        "bd6": "2026-06-16",
        "bd15": "2026-06-30",
        "effective_from": "2026-06-16",
        "expires_on": "2026-09-14",   # 2026-06-16 + 90 days
    }


def pdt_payloads() -> dict:
    return {
        "pdt_flagged": account_payload(pattern_day_trader=True, daytrade_count=4),
        "pdt_clean": account_payload(pattern_day_trader=False),
        "pdt_fields_absent": account_payload(pattern_day_trader=OMIT,
                                             daytrade_count=OMIT,
                                             daytrading_buying_power=OMIT),
        "rejection_pdt_code": {
            "code": 40310100,
            "message": "trade denied due to pattern day trading protection",
        },
        "rejection_pdt_marker_only": {
            "code": None,
            "message": "Order rejected: insufficient Day Trading Buying Power",
        },
        "rejection_other": {
            "code": 40010001,
            "message": "insufficient buying power",
        },
    }


def marks_fixture(*, now_ms: int = 10_000) -> dict:
    """Fresh / stale / identity-mismatched Marks on the MID_QUANTUM grid + unpriceable
    legs (zero / negative limit_price — RM-3)."""
    from decimal import Decimal

    from agent.candidate import Leg
    from agent.risk.account_state import MARK_FRESHNESS_TTL_MS, Mark

    return {
        "now_ms": now_ms,
        "fresh": Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("191.000000"),
                      seen_at_ms=now_ms - MARK_FRESHNESS_TTL_MS, source="quote_mid"),
        "stale": Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("191.000000"),
                      seen_at_ms=now_ms - MARK_FRESHNESS_TTL_MS - 1, source="quote_mid"),
        "mismatched_symbol": Mark(symbol="MSFT", instrument_id=1001,
                                  mid=Decimal("191.000000"), seen_at_ms=now_ms,
                                  source="quote_mid"),
        "mismatched_instrument": Mark(symbol="AAPL", instrument_id=9999,
                                      mid=Decimal("191.000000"), seen_at_ms=now_ms,
                                      source="quote_mid"),
        "leg": Leg(symbol="AAPL", instrument_id=1001, side="buy",
                   qty=Decimal("10"), limit_price=Decimal("190.00")),
        "leg_zero_limit": Leg(symbol="AAPL", instrument_id=1001, side="buy",
                              qty=Decimal("10"), limit_price=Decimal("0")),
        "leg_negative_limit": Leg(symbol="AAPL", instrument_id=1001, side="buy",
                                  qty=Decimal("10"), limit_price=Decimal("-1")),
        "leg_no_limit": Leg(symbol="AAPL", instrument_id=1001, side="buy",
                            qty=Decimal("10"), limit_price=None),
    }


def verdict_fixture(symbol: str, tradability=None, *, stale_default: bool = False,
                    instrument_id: int = 1001, session_date_et: str = "2026-06-08"):
    """Constructed M2 Verdicts incl. the literal MarketStateCache.safe_default_verdict."""
    from agent.market_state import (
        HaltState, LuldState, SessionState, SsrState, Tradability, Verdict,
    )
    from agent.market_state_cache import MarketStateCache

    if stale_default:
        return MarketStateCache.safe_default_verdict(symbol, instrument_id, session_date_et)
    tradability = tradability if tradability is not None else Tradability.TRADABLE
    return Verdict(
        symbol=symbol,
        instrument_id=instrument_id,
        session_state=SessionState.RTH,
        tradability=tradability,
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=True,
        short_allowed=(tradability == Tradability.TRADABLE),
        reasons=() if tradability == Tradability.TRADABLE else ("fixture_restriction",),
        ca_blackout=False,
        session_date_et=session_date_et,
    )


def gates_on_fixture_config() -> dict:
    """NON-COMMITTED: gates identity-True + committed-shaped zero caps / empty universe
    (the second-wall canary input, §L)."""
    return {
        "agent_rules": {"enabled": True, "paper_trading": {"enabled": True}},
        "risk_rules": {
            "live_trading": {"enabled": False, "max_live_position_usd": 0},
            "caps": {
                "max_position_usd": 0,
                "max_gross_exposure_usd": 0,
                "max_net_exposure_usd": 0,
                "max_daily_loss_usd": 0,
                "max_drawdown_usd": 0,
                "max_sector_exposure_usd": 0,
                "max_abs_beta_notional_usd": 0,
            },
            "risk": {"short_selling": {"enabled": False}, "universe": {}},
        },
    }


def permissive_fixture_config() -> dict:
    """NON-COMMITTED: gates identity-True, NONZERO integer caps, a small universe WITH
    sector/beta metadata — the passing-path / strict-'>' boundary input (§L, M3-build)."""
    return {
        "agent_rules": {"enabled": True, "paper_trading": {"enabled": True}},
        "risk_rules": {
            "live_trading": {"enabled": False, "max_live_position_usd": 0},
            "caps": {
                "max_position_usd": 10000,
                "max_gross_exposure_usd": 50000,
                "max_net_exposure_usd": 50000,
                "max_daily_loss_usd": 1000,
                "max_drawdown_usd": 2000,
                "max_sector_exposure_usd": 20000,
                "max_abs_beta_notional_usd": 30000,
            },
            "risk": {
                "short_selling": {"enabled": False},
                "universe": {
                    "AAPL": {"sector": "tech", "beta": "1.2"},
                    "MSFT": {"sector": "tech", "beta": "1.1"},
                    "XOM": {"sector": "energy", "beta": "0.9"},
                },
            },
        },
    }
