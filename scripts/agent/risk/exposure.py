"""M4 §D — PURE Decimal exposure math. No I/O, no clock, no journal.

Poisoned-aggregate posture (FD-M4-17): a held symbol missing from cfg.universe yields
an `UnknownMeta` marker, NEVER a partial sum; a CANDIDATE leg symbol missing from the
universe poisons the sector AND beta projections exactly like a held one (RM-5 — the
always-case on the committed empty universe). Candidate impact is additive-conservative:
|cap_notional| -> gross / per-symbol / sector; signed cap_notional (buy +, sell −) ->
net / beta. Strict '>' breaches; a 0 cap rejects ANY positive exposure (FD-M4-6).
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Optional, Tuple, Union

from agent.risk.account_state import MARK_FRESHNESS_TTL_MS, Mark, PortfolioRead
from agent.risk.reasons import RiskError
from agent.risk.risk_config import RiskConfig

# Persisted-value arithmetic runs under THIS pinned context, never the ambient one
# (M3 harden-round-1 precedent, quote_quality.py:23).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class UnknownMeta:
    kind: str                      # "sector" | "beta"
    symbols: Tuple[str, ...]       # sorted offenders


def leg_cap_notional(leg, mark: Optional[Mark], *, now_ms: int,
                     ttl_ms: int = MARK_FRESHNESS_TTL_MS
                     ) -> Tuple[Optional[Decimal], bool]:
    """FD-M4-16: (qty × max(limit_price, mark.mid), mark_used=True) when a fresh
    (strict '>' ttl_ms) identity-matched mark exists; else (qty × limit_price, False);
    (None, False) when limit_price is None OR limit_price <= 0 (RM-3 — a non-positive
    limit is unpriceable, evaluated before any notional math). ttl_ms may only SHORTEN
    (ValueError if > MARK_FRESHNESS_TTL_MS — FD-M4-22 clamp). Mark identity mismatch
    vs the leg -> RiskError."""
    if ttl_ms > MARK_FRESHNESS_TTL_MS:
        raise ValueError(
            "ttl_ms may only SHORTEN mark freshness (FD-M4-22 clamp): "
            f"{ttl_ms} > MARK_FRESHNESS_TTL_MS={MARK_FRESHNESS_TTL_MS}")
    if mark is not None and (mark.symbol != leg.symbol
                             or mark.instrument_id != leg.instrument_id):
        raise RiskError(
            f"mark identity mismatch: mark ({mark.symbol!r}, {mark.instrument_id}) "
            f"vs leg ({leg.symbol!r}, {leg.instrument_id})")
    limit = leg.limit_price
    if limit is None or limit <= 0:
        return (None, False)   # RM-3: unpriceable — excluded from ALL notionals
    with localcontext(_DECIMAL_CTX):
        if mark is not None and (int(now_ms) - mark.seen_at_ms) <= int(ttl_ms):
            return (leg.qty * max(limit, mark.mid), True)
        return (leg.qty * limit, False)


def gross_exposure(portfolio: PortfolioRead) -> Decimal:
    """Σ |market_value| over held positions."""
    with localcontext(_DECIMAL_CTX):
        total = Decimal("0")
        for position in portfolio.positions:
            total += abs(position.market_value)
        return total


def net_exposure(portfolio: PortfolioRead) -> Decimal:
    """Σ signed market_value over held positions."""
    with localcontext(_DECIMAL_CTX):
        total = Decimal("0")
        for position in portfolio.positions:
            total += position.market_value
        return total


def symbol_exposure(portfolio: PortfolioRead, symbol: str) -> Decimal:
    """|market_value| of the held position in `symbol` (0 if not held)."""
    for position in portfolio.positions:
        if position.symbol == symbol:
            return abs(position.market_value)
    return Decimal("0")


def sector_exposure(portfolio: PortfolioRead, universe
                    ) -> Union[Mapping[str, Decimal], UnknownMeta]:
    """Σ |market_value| per sector (absolute — RM-4: a held short ADDS to its sector).
    ANY held symbol missing from the universe poisons the whole aggregate."""
    offenders = sorted(p.symbol for p in portfolio.positions if p.symbol not in universe)
    if offenders:
        return UnknownMeta(kind="sector", symbols=tuple(offenders))
    with localcontext(_DECIMAL_CTX):
        sectors: dict = {}
        for position in portfolio.positions:
            sector = universe[position.symbol].sector
            sectors[sector] = sectors.get(sector, Decimal("0")) + abs(position.market_value)
        return sectors


def beta_notional(portfolio: PortfolioRead, universe) -> Union[Decimal, UnknownMeta]:
    """Σ signed market_value × beta. ANY held symbol missing from the universe poisons
    the whole aggregate."""
    offenders = sorted(p.symbol for p in portfolio.positions if p.symbol not in universe)
    if offenders:
        return UnknownMeta(kind="beta", symbols=tuple(offenders))
    with localcontext(_DECIMAL_CTX):
        total = Decimal("0")
        for position in portfolio.positions:
            total += position.market_value * universe[position.symbol].beta
        return total


def project_caps(candidate, notionals: Mapping[int, Decimal],
                 portfolio: PortfolioRead, cfg: RiskConfig,
                 *, opening_indexes=None
                 ) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str, str], ...]]:
    """Returns (sorted cap-violation reasons, caps_used rows). `notionals` maps leg
    index -> cap_notional for PRICEABLE OPENING legs only (§D). `opening_indexes`
    (harden round 1, M4-R3) is the FULL set of opening-leg indexes — priceable or
    not — over which universe-membership POISONING is evaluated (FD-M4-17: never a
    partial sum; an unpriceable out-of-universe leg must still poison sector/beta);
    None falls back to the notional keys. `used`/`limit` are the
    projected post-trade values as EXACT canonical Decimal strings (LD-R5 — never
    re-quantized). Rows are sorted by name (M1-build single ordering rule); the caller
    unions margin/loss rows in and re-sorts ONCE."""
    reasons = set()
    rows = []
    with localcontext(_DECIMAL_CTX):
        signed_total = Decimal("0")
        abs_total = Decimal("0")
        per_symbol_add: dict = {}
        for index in sorted(notionals):
            leg = candidate.legs[index]
            notional = notionals[index]
            sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
            signed_total += sign * notional
            abs_total += abs(notional)
            per_symbol_add[leg.symbol] = (
                per_symbol_add.get(leg.symbol, Decimal("0")) + abs(notional))

        # per-symbol position cap
        for symbol in sorted(per_symbol_add):
            used = symbol_exposure(portfolio, symbol) + per_symbol_add[symbol]
            rows.append((f"max_position_usd:{symbol}", str(used),
                         str(cfg.max_position_usd)))
            if used > cfg.max_position_usd:
                reasons.add("position_cap_exceeded")

        # gross
        gross_used = gross_exposure(portfolio) + abs_total
        rows.append(("max_gross_exposure_usd", str(gross_used),
                     str(cfg.max_gross_exposure_usd)))
        if gross_used > cfg.max_gross_exposure_usd:
            reasons.add("gross_exposure_cap_exceeded")

        # net (|used| journaled and compared)
        net_used = net_exposure(portfolio) + signed_total
        rows.append(("max_net_exposure_usd", str(abs(net_used)),
                     str(cfg.max_net_exposure_usd)))
        if abs(net_used) > cfg.max_net_exposure_usd:
            reasons.add("net_exposure_cap_exceeded")

        # sector / beta — poisoned by held OR candidate symbols outside the universe.
        # Poisoning scans ALL opening legs, priceable or not (harden round 1, M4-R3).
        poison_scan = notionals if opening_indexes is None else opening_indexes
        candidate_offenders = sorted({
            candidate.legs[index].symbol for index in poison_scan
            if candidate.legs[index].symbol not in cfg.universe})
        held_sectors = sector_exposure(portfolio, cfg.universe)
        if candidate_offenders or isinstance(held_sectors, UnknownMeta):
            reasons.add("sector_unknown")
        else:
            sector_add: dict = {}
            for index in sorted(notionals):
                leg = candidate.legs[index]
                sector = cfg.universe[leg.symbol].sector
                sector_add[sector] = (
                    sector_add.get(sector, Decimal("0")) + abs(notionals[index]))
            for sector in sorted(sector_add):
                used = held_sectors.get(sector, Decimal("0")) + sector_add[sector]
                rows.append((f"max_sector_exposure_usd:{sector}", str(used),
                             str(cfg.max_sector_exposure_usd)))
                if used > cfg.max_sector_exposure_usd:
                    reasons.add("sector_cap_exceeded")

        held_beta = beta_notional(portfolio, cfg.universe)
        if candidate_offenders or isinstance(held_beta, UnknownMeta):
            reasons.add("beta_unknown")
        else:
            beta_used = held_beta
            for index in sorted(notionals):
                leg = candidate.legs[index]
                sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
                beta_used += sign * notionals[index] * cfg.universe[leg.symbol].beta
            rows.append(("max_abs_beta_notional_usd", str(abs(beta_used)),
                         str(cfg.max_abs_beta_notional_usd)))
            if abs(beta_used) > cfg.max_abs_beta_notional_usd:
                reasons.add("beta_cap_exceeded")

    return tuple(sorted(reasons)), tuple(sorted(rows))
