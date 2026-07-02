"""Credentialed Alpaca PAPER account verifier (the M8-checklist broker dry-run,
paper tier) — the Alpaca analogue of ``verify_databento_entitlements``.

Read-only by default: probes account / clock / positions / open orders through
the SAME lazy-SDK surface the broker adapter uses (``TradingClient`` with
``raw_data=True``; the loader + host pin enforce paper-only BEFORE any SDK
import), evaluates fail-closed checks, and writes a REDACTED summary artifact
(flags/counts/last-4 only — never keys, never the full account number) to
``reports/alpaca_paper/verified_account.json``.

The optional ``--allow-order-drill`` exercises the full submit→status→cancel
round trip with a deliberately NON-MARKETABLE 1-share DAY limit (far below any
plausible price, so it can never fill) and cancels it immediately. It talks to
the SDK directly — this is an OPERATOR account-level dry-run outside the agent
loop; the agent's own ``submit_order`` seam stays token-gated and untouched.
Default is OFF; the read-only pass never submits anything.

Offline tests inject a fake client via ``client_factory``; the real SDK import
lives only inside ``_build_real_client`` (never reached offline).
"""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from agent.secrets_runtime import load_alpaca_paper_credentials
from agent.serializer import dumps

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CREDENTIALS = _REPO_ROOT / ".secrets" / "alpaca_paper.json"
_DEFAULT_REPORT = _REPO_ROOT / "reports" / "alpaca_paper" / "verified_account.json"
PAPER_HOST = "https://paper-api.alpaca.markets"
_DRILL_LIMIT = "1.00"   # non-marketable for any large-cap: the drill can never fill


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _decimal_ok(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Decimal(value).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _last4(value) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[-4:]


def _build_real_client(creds: Mapping):  # pragma: no cover - credentialed
    """The ONLY alpaca SDK import site in this module (mirrors the broker
    adapter's FD-M5-5 discipline); reached solely on the credentialed path."""
    from alpaca.trading.client import TradingClient

    return TradingClient(api_key=creds["key_id"],
                         secret_key=creds["secret_key"],
                         paper=True, raw_data=True)


def _order_drill(client, *, symbol: str, now_iso: str) -> dict:
    """Submit a non-marketable 1-share DAY limit, then cancel it. Any failure is
    RECORDED, never raised — the read-only verification stands on its own."""
    client_order_id = f"verify-drill-{now_iso.replace(':', '').replace('.', '')}"
    drill = {"attempted": True, "symbol": symbol,
             "client_order_id": client_order_id, "submitted": False,
             "canceled": False, "final_status": None, "error": None}
    try:  # pragma: no cover start - exercised via injected fakes offline
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        request = LimitOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=_DRILL_LIMIT,
            client_order_id=client_order_id)
    except ImportError:
        # offline fakes: hand the fake client a plain payload instead
        request = {"symbol": symbol, "qty": 1, "side": "buy",
                   "time_in_force": "day", "limit_price": _DRILL_LIMIT,
                   "client_order_id": client_order_id}
    # pragma: no cover end
    try:
        submitted = client.submit_order(order_data=request)
        drill["submitted"] = True
        order_id = (submitted or {}).get("id")
        if order_id:
            client.cancel_order_by_id(order_id)
            drill["canceled"] = True
        final = client.get_order_by_client_id(client_order_id)
        drill["final_status"] = (final or {}).get("status")
    except Exception as exc:  # noqa: BLE001 — recorded, verification continues
        drill["error"] = f"{type(exc).__name__}: {exc}"
    return drill


def verify_alpaca_paper(*, credentials_path=None,
                        client_factory: Optional[Callable] = None,
                        allow_order_drill: bool = False,
                        drill_symbol: str = "AAPL",
                        report_path=None,
                        utc_now_iso: Optional[str] = None) -> dict:
    """Run the verification; returns the redacted summary dict (also written to
    ``report_path`` unless None). ``summary['ok']`` is the verdict."""
    now_iso = utc_now_iso or _utc_now_iso()
    creds = load_alpaca_paper_credentials(
        credentials_path or _DEFAULT_CREDENTIALS)
    failures = []
    if creds["base_url"] != PAPER_HOST:
        # the broker adapter enforces this too; double-locked here pre-SDK
        raise ValueError(
            f"verifier is paper-only: base_url must be {PAPER_HOST!r}")

    client = (client_factory(creds) if client_factory is not None
              else _build_real_client(creds))

    account = client.get_account() or {}
    clock = client.get_clock() or {}
    positions = client.get_all_positions() or []
    orders = client.get_orders() or []

    status = account.get("status")
    if status != "ACTIVE":
        failures.append(f"account_status:{status}")
    for flag in ("account_blocked", "trading_blocked"):
        if account.get(flag):
            failures.append(flag)
    if account.get("currency") != "USD":
        failures.append(f"currency:{account.get('currency')}")
    for field in ("equity", "buying_power", "cash"):
        if not _decimal_ok(account.get(field)):
            failures.append(f"unparseable:{field}")
    if not account.get("account_number"):
        failures.append("missing:account_number")
    for field in ("next_open", "next_close"):
        if not clock.get(field):
            failures.append(f"missing:clock.{field}")

    drill = None
    if allow_order_drill:
        drill = _order_drill(client, symbol=drill_symbol, now_iso=now_iso)
        if drill["error"] is not None or not drill["submitted"]:
            failures.append("order_drill_failed")

    summary = {
        "kind": "alpaca_paper_verification_v1",
        "verified_utc": now_iso,
        "base_url": creds["base_url"],
        "account": {
            "status": status,
            "currency": account.get("currency"),
            "account_number_last4": _last4(account.get("account_number")),
            "pattern_day_trader": bool(account.get("pattern_day_trader")),
            "account_blocked": bool(account.get("account_blocked")),
            "trading_blocked": bool(account.get("trading_blocked")),
            "equity": account.get("equity"),
            "buying_power": account.get("buying_power"),
            "cash": account.get("cash"),
            "daytrading_buying_power": account.get("daytrading_buying_power"),
        },
        "clock": {
            "is_open": bool(clock.get("is_open")),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
        },
        "positions_count": len(positions),
        "open_orders_count": len(orders),
        "order_drill": drill,
        "failures": sorted(failures),
        "ok": not failures,
    }
    if report_path is not None:
        output = Path(report_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(dumps(summary) + "\n", encoding="utf-8")
    return summary


def _main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify the Alpaca PAPER account credentials + API surface "
                    "(read-only by default; redacted summary artifact).")
    parser.add_argument("--credentials", default=str(_DEFAULT_CREDENTIALS))
    parser.add_argument("--report", default=str(_DEFAULT_REPORT))
    parser.add_argument("--allow-order-drill", action="store_true",
                        help="ALSO exercise submit->cancel with a non-"
                             "marketable 1-share DAY limit (paper account)")
    parser.add_argument("--drill-symbol", default="AAPL")
    args = parser.parse_args(argv)

    summary = verify_alpaca_paper(
        credentials_path=args.credentials,
        allow_order_drill=args.allow_order_drill,
        drill_symbol=args.drill_symbol,
        report_path=args.report)
    print(json.dumps({k: summary[k] for k in
                      ("ok", "failures", "positions_count",
                       "open_orders_count")}, indent=1))
    print(f"report: {args.report}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
