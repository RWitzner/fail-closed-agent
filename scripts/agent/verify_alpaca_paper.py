"""Credentialed Alpaca PAPER account verifier (the M8-checklist broker dry-run,
paper tier) — the Alpaca analogue of ``verify_databento_entitlements``.

Read-only by default: probes account / clock / positions / open orders through
the SAME lazy-SDK surface the broker adapter uses (``TradingClient`` with
``raw_data=True``; the loader + host pin enforce paper-only BEFORE any SDK
import), evaluates fail-closed checks, and writes a summary artifact to
``reports/alpaca_paper/verified_account.json``. Redaction contract: NEVER key
material, NEVER the full account number (last-4 only); paper-account BALANCES
(equity/buying power/cash) ARE included — they are fake-money account values,
not secrets, and the arming checklist needs them (``reports/`` is gitignored).

The optional ``--allow-order-drill`` exercises the full submit→status→cancel
round trip with a 1-share DAY limit intended to be non-marketable for the
constrained large-cap drill symbol, then cancels it immediately. A fill is a
hard verification failure, not an excluded outcome. It talks to the SDK directly
— this is an OPERATOR account-level dry-run outside the agent loop; the agent's
own ``submit_order`` seam stays token-gated and untouched. Default is OFF; the
read-only pass never submits anything.

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
_DRILL_LIMIT = "1.00"   # intended non-marketable limit for the large-cap drill
_DRILL_OBSERVATION_ATTEMPTS = 3
_SAFE_DRILL_TERMINAL = frozenset({"canceled", "rejected", "expired"})
_SAFE_DRILL_ALREADY_TERMINAL = _SAFE_DRILL_TERMINAL - {"canceled"}
_DRILL_FILL_STATUSES = frozenset({"filled", "partially_filled"})


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


def _plain_drill_request(*, symbol: str, client_order_id: str) -> dict:
    """The injected-fake request shape (offline tests); the credentialed path
    builds the real SDK ``LimitOrderRequest`` inside ``_build_real_client``."""
    return {"symbol": symbol, "qty": 1, "side": "buy",
            "time_in_force": "day", "limit_price": _DRILL_LIMIT,
            "client_order_id": client_order_id}


def _build_real_client(creds: Mapping):  # pragma: no cover - credentialed
    """The ONLY alpaca SDK import site in this module (the broker adapter's
    FD-M5-5 one-site discipline, allow-listed in the AST guard); reached solely
    on the credentialed path. Returns ``(client, drill_request_factory)`` so no
    other function needs an SDK import."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    client = TradingClient(api_key=creds["key_id"],
                           secret_key=creds["secret_key"],
                           paper=True, raw_data=True)

    def drill_request(*, symbol: str, client_order_id: str):
        return LimitOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=_DRILL_LIMIT,
            client_order_id=client_order_id)

    return client, drill_request


def _order_drill(client, request_factory, *, symbol: str, now_iso: str) -> dict:
    """Submit an intended non-marketable 1-share DAY limit, then cancel it. Any
    failure is RECORDED, never raised — the read-only verification stands."""
    client_order_id = f"verify-drill-{now_iso.replace(':', '').replace('.', '')}"
    drill = {"attempted": True, "symbol": symbol,
             "client_order_id": client_order_id, "submitted": False,
             "canceled": False, "final_status": None,
             "terminal_verified": False, "error": None}
    order_id = None
    submit_attempted = False
    last_lookup_error = None
    last_cancel_error = None

    def record_error(exc) -> None:
        if drill["error"] is None:
            drill["error"] = f"{type(exc).__name__}: {exc}"

    def validate_observation(payload, *, allow_missing_id: bool):
        """Bind every observed order to the requested client id and one stable
        broker id. An empty lookup is delayed visibility, not an order."""
        nonlocal order_id
        if payload is None:
            return None, False
        if not isinstance(payload, Mapping):
            raise ValueError("order observation must be a mapping")
        if not payload:
            return None, False
        observed_client_id = payload.get("client_order_id")
        if observed_client_id != client_order_id:
            raise ValueError(
                "client_order_id mismatch: expected "
                f"{client_order_id!r}, got {observed_client_id!r}")
        observed_order_id = payload.get("id")
        if observed_order_id is None and allow_missing_id:
            return payload.get("status"), False
        if (not isinstance(observed_order_id, str)
                or not observed_order_id.strip()):
            raise ValueError("missing broker order id")
        if order_id is not None and observed_order_id != order_id:
            raise ValueError(
                "broker order id changed: expected "
                f"{order_id!r}, got {observed_order_id!r}")
        # Identity is sufficient for best-effort cleanup even when later
        # status/fill fields are malformed. Never bind a mismatched id above.
        order_id = observed_order_id
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("missing order status")
        if status in _DRILL_FILL_STATUSES:
            raise ValueError(f"order drill observed fill status {status!r}")
        filled_qty = payload.get("filled_qty")
        if not _decimal_ok(filled_qty):
            raise ValueError("missing or invalid filled_qty")
        if Decimal(filled_qty) != Decimal("0"):
            raise ValueError(f"nonzero filled_qty: {filled_qty!r}")
        return status, True

    def attempt_cancel() -> None:
        nonlocal last_cancel_error
        if order_id is None or drill["canceled"]:
            return
        try:
            client.cancel_order_by_id(order_id)
            drill["canceled"] = True
            last_cancel_error = None
        except Exception as exc:  # noqa: BLE001 — retry on next live observation
            last_cancel_error = exc

    try:
        request = request_factory(symbol=symbol, client_order_id=client_order_id)
        submit_attempted = True
        submitted = client.submit_order(order_data=request)
        drill["submitted"] = True
        status, identity_bound = validate_observation(
            submitted, allow_missing_id=True)
        if identity_bound:
            if status in _SAFE_DRILL_ALREADY_TERMINAL:
                drill["final_status"] = status
                drill["terminal_verified"] = True
            else:
                attempt_cancel()
    except Exception as exc:  # noqa: BLE001 — recorded, verification continues
        record_error(exc)

    if submit_attempted and not drill["terminal_verified"]:
        for _ in range(_DRILL_OBSERVATION_ATTEMPTS):
            try:
                recovered = client.get_order_by_client_id(client_order_id)
            except Exception as exc:  # noqa: BLE001 — bounded delayed visibility
                last_lookup_error = exc
                continue
            try:
                status, identity_bound = validate_observation(
                    recovered, allow_missing_id=False)
            except Exception as exc:  # noqa: BLE001 — unsafe identity is terminal
                record_error(exc)
                break
            if not identity_bound:
                continue
            drill["final_status"] = status
            if status in _SAFE_DRILL_ALREADY_TERMINAL:
                drill["terminal_verified"] = True
                break
            if status == "canceled":
                if drill["canceled"]:
                    drill["terminal_verified"] = True
                    break
                attempt_cancel()
                if not drill["canceled"]:
                    break
                continue
            # Any newly visible nonterminal order is canceled immediately. A
            # previously successful cancel is not repeated while finality is
            # still propagating; the remaining bounded reads observe it.
            attempt_cancel()

    # One bounded cleanup retry for a broker id already identity-bound above.
    # A successful request is not terminal evidence; the verdict still requires
    # a verified safe terminal status from an observation.
    if (not drill["terminal_verified"]
            and order_id is not None
            and not drill["canceled"]):
        attempt_cancel()

    if not drill["terminal_verified"] and drill["error"] is None:
        if last_cancel_error is not None and not drill["canceled"]:
            record_error(last_cancel_error)
        elif last_lookup_error is not None and drill["final_status"] is None:
            record_error(last_lookup_error)
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

    if client_factory is not None:
        client = client_factory(creds)
        drill_request_factory = _plain_drill_request
    else:  # pragma: no cover - credentialed
        client, drill_request_factory = _build_real_client(creds)

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
        if (not isinstance(drill_symbol, str) or not drill_symbol.isalpha()
                or not drill_symbol.isupper() or len(drill_symbol) > 5):
            # keep the drill on plain US large-cap tickers: the $1.00 limit is
            # non-marketable only for symbols trading above $1.
            raise ValueError(
                f"drill_symbol must be a 1-5 letter uppercase ticker, got "
                f"{drill_symbol!r}")
        drill = _order_drill(client, drill_request_factory,
                             symbol=drill_symbol, now_iso=now_iso)
        terminal_ok = (
            drill["final_status"] in _SAFE_DRILL_ALREADY_TERMINAL
            or (drill["final_status"] == "canceled" and drill["canceled"])
        )
        drill_ok = (drill["error"] is None and drill["submitted"]
                    and drill["terminal_verified"] and terminal_ok)
        if not drill_ok:
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
