"""M5 §J — `FeeModel` (FD-M5-15; verified rates §0.2 F1/F2). Stdlib only.

Fee rates are CODE CONSTANTS (regulatory facts are not knobs — the M2 §G /
FD-M4-6 discipline; changing a rate = a reviewed commit + version bump):

- `SEC_SECTION31_RATE`: $27.80 per million USD of covered SALE proceeds
  (FY2025 fee-rate advisory figure — PINNED, NOT RE-VERIFIED 2026-06-10, §0.2 F2;
  standing M5-2a obligation to re-verify against sec.gov before the first
  credentialed paper session and bump constant + `FEE_MODEL_VERSION` if changed).
- `TAF_PER_SHARE_SOLD` / `TAF_CAP_PER_TRADE_USD`: FINRA TAF, covered equities,
  sells only (VERIFIED 2026-06-10, finra.org Schedule A §1; §0.2 F1).

Frozen semantics (§J):
- Sells only; buys => the zero assumption (no fee is assessed on a buy).
- Each component is ceil-rounded to the cent (`ROUND_CEILING`): fee assumptions
  round AGAINST us (conservative realistic PnL).
- TAF is capped per trade at $8.30 AFTER the per-share product is ceil-rounded.
- `qty`/`notional` must be positive finite Decimals (float/bool/non-Decimal =>
  ValueError, S2). Validation runs for BOTH sides — a malformed input raises even
  on the zero-fee buy path.

S5-critical split (§J): Alpaca paper charges NO regulatory fees (A6), so these
assumptions enter ONLY `execution_realistic_pnl` — never `broker_account_pnl`
(injecting synthetic fees into the broker side would manufacture permanent M6
reconcile drift).

Documented resolution (byte form of the buy-side zero): §J's pseudocode writes
`FeeAssumption(FEE_MODEL_VERSION, 0, 0, 0)`. The zero is rendered here as
`Decimal("0.00")` — the CENT-scale zero, the same scale every sell-side component
carries after `quantize(CENT, ...)` — so the journaled `fees_*` byte form is
uniform across rows ("0.00", never a mixed "0"/"0.00"). Value-equal to the
contract's literal 0.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

SEC_SECTION31_RATE = Decimal("0.0000278")   # per USD of covered SALE proceeds ($27.80/million;
                                            #   FY2025 advisory figure — provenance + re-verify
                                            #   duty in §0.2 F2)
TAF_PER_SHARE_SOLD = Decimal("0.000166")    # FINRA TAF, covered equities, sells only (VERIFIED
                                            #   2026-06-10, finra.org Schedule A §1)
TAF_CAP_PER_TRADE_USD = Decimal("8.30")     # per-trade cap (VERIFIED 2026-06-10)
FEE_MODEL_VERSION = "reg_fees_v1"
CENT = Decimal("0.01")

_ZERO_USD = Decimal("0.00")                 # CENT-scale zero (see module docstring)
_SIDES = ("buy", "sell")


@dataclass(frozen=True)
class FeeAssumption:
    """One trade's regulatory-fee assumption — modeled-side label input ONLY (S5)."""

    model_version: str            # FEE_MODEL_VERSION
    sec_usd: Decimal
    taf_usd: Decimal
    total_usd: Decimal


def _require_positive_decimal(value, *, field: str) -> Decimal:
    """qty/notional discipline (§J): a positive finite Decimal; float/bool/other
    types => ValueError (S2 — fees are money math, a float never enters)."""
    if isinstance(value, bool):
        raise ValueError(f"{field}: bool not allowed; use Decimal")
    if isinstance(value, float):
        raise ValueError(f"{field}: float not allowed; use Decimal")
    if not isinstance(value, Decimal):
        raise ValueError(
            f"{field}: must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed: {value}")
    if value <= 0:
        raise ValueError(f"{field}: must be > 0, got {value}")
    return value


def fees_for(*, side: str, qty: Decimal, notional: Decimal) -> FeeAssumption:
    """The frozen §J formula.

    buy  -> the zero assumption (no regulatory fee on buys).
    sell -> sec = (notional × SEC_SECTION31_RATE).quantize(CENT, ROUND_CEILING)
            taf = min((qty × TAF_PER_SHARE_SOLD).quantize(CENT, ROUND_CEILING),
                      TAF_CAP_PER_TRADE_USD)
            total = sec + taf
    Ceil-rounding is frozen: fee assumptions round AGAINST us.
    """
    if side not in _SIDES:
        raise ValueError(f"side must be one of {_SIDES}, got {side!r}")
    _require_positive_decimal(qty, field="qty")
    _require_positive_decimal(notional, field="notional")

    if side == "buy":
        return FeeAssumption(
            model_version=FEE_MODEL_VERSION,
            sec_usd=_ZERO_USD,
            taf_usd=_ZERO_USD,
            total_usd=_ZERO_USD,
        )

    sec = (notional * SEC_SECTION31_RATE).quantize(CENT, ROUND_CEILING)
    taf = min((qty * TAF_PER_SHARE_SOLD).quantize(CENT, ROUND_CEILING),
              TAF_CAP_PER_TRADE_USD)
    return FeeAssumption(
        model_version=FEE_MODEL_VERSION,
        sec_usd=sec,
        taf_usd=taf,
        total_usd=sec + taf,
    )
