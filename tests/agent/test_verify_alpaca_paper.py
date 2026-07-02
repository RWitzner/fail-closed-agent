"""agent.verify_alpaca_paper — the credentialed Alpaca paper verifier.

Offline via an injected fake client. Asserts: happy-path verdict + REDACTED
artifact (no key material, no full account number), fail-closed checks
(blocked/currency/unparseable), the order drill is OFF by default and gated,
non-paper base_url refused, and the module imports no alpaca SDK offline.
"""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.verify_alpaca_paper import PAPER_HOST, verify_alpaca_paper

_CREDS = {
    "key_id": "PKTESTKEYID12345",
    "secret_key": "sekritsekritsekritsekrit",
    "base_url": PAPER_HOST,
}


def _write_creds(tmp: Path, creds=None) -> Path:
    path = tmp / "alpaca_paper.json"
    path.write_text(json.dumps(creds or _CREDS), encoding="utf-8")
    return path


def _account(**overrides) -> dict:
    payload = {
        "status": "ACTIVE",
        "currency": "USD",
        "account_number": "PA1234567890",
        "pattern_day_trader": False,
        "account_blocked": False,
        "trading_blocked": False,
        "equity": "100000.00",
        "buying_power": "200000.00",
        "cash": "100000.00",
        "daytrading_buying_power": "400000.00",
    }
    payload.update(overrides)
    return payload


class _FakeClient:
    def __init__(self, account=None, clock=None):
        self._account = account or _account()
        self._clock = clock or {
            "is_open": False,
            "next_open": "2026-07-06T13:30:00Z",
            "next_close": "2026-07-06T20:00:00Z",
        }
        self.submit_calls = []
        self.cancel_calls = []
        self.drill_error = None

    def get_account(self):
        return self._account

    def get_clock(self):
        return self._clock

    def get_all_positions(self):
        return []

    def get_orders(self):
        return []

    def submit_order(self, *, order_data):
        if self.drill_error is not None:
            raise self.drill_error
        coid = (order_data.get("client_order_id")
                if isinstance(order_data, dict)
                else getattr(order_data, "client_order_id", None))
        self.submit_calls.append(coid)
        return {"id": "broker-drill-1", "status": "accepted",
                "client_order_id": coid}

    def cancel_order_by_id(self, order_id):
        self.cancel_calls.append(order_id)

    def get_order_by_client_id(self, client_order_id):
        return {"id": "broker-drill-1", "status": "canceled",
                "client_order_id": client_order_id}


class TestVerifier(unittest.TestCase):
    def _run(self, tmp, client, **kwargs):
        return verify_alpaca_paper(
            credentials_path=_write_creds(Path(tmp)),
            client_factory=lambda creds: client,
            report_path=Path(tmp) / "report" / "verified.json",
            utc_now_iso="2026-07-02T21:00:00.000000Z",
            **kwargs)

    def test_happy_path_ok_and_redacted_report(self):
        client = _FakeClient()
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["failures"], [])
            self.assertEqual(summary["account"]["account_number_last4"],
                             "7890")
            text = (Path(tmp) / "report" / "verified.json").read_text()
            self.assertNotIn("PA1234567890", text)      # full number redacted
            self.assertNotIn(_CREDS["key_id"], text)     # no key material
            self.assertNotIn(_CREDS["secret_key"], text)
            self.assertIn('"account_number_last4":"7890"', text)
        # read-only by default: the drill never ran
        self.assertEqual(client.submit_calls, [])
        self.assertIsNone(summary["order_drill"])

    def test_blocked_account_fails_closed(self):
        client = _FakeClient(account=_account(
            status="SUBMITTED", trading_blocked=True, currency="EUR",
            equity="not-a-number"))
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client)
        self.assertFalse(summary["ok"])
        self.assertIn("account_status:SUBMITTED", summary["failures"])
        self.assertIn("trading_blocked", summary["failures"])
        self.assertIn("currency:EUR", summary["failures"])
        self.assertIn("unparseable:equity", summary["failures"])

    def test_order_drill_gated_and_round_trips(self):
        client = _FakeClient()
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)
        self.assertTrue(summary["ok"])
        drill = summary["order_drill"]
        self.assertTrue(drill["submitted"])
        self.assertTrue(drill["canceled"])
        self.assertEqual(drill["final_status"], "canceled")
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])
        self.assertEqual(len(client.submit_calls), 1)
        self.assertTrue(client.submit_calls[0].startswith("verify-drill-"))

    def test_order_drill_failure_recorded_not_raised(self):
        client = _FakeClient()
        client.drill_error = RuntimeError("submit rejected")
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)
        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertIn("submit rejected", summary["order_drill"]["error"])

    def test_non_paper_base_url_refused(self):
        creds = dict(_CREDS, base_url="https://api.alpaca.markets")
        with TemporaryDirectory() as tmp:
            path = _write_creds(Path(tmp), creds)
            with self.assertRaises(ValueError):
                verify_alpaca_paper(credentials_path=path,
                                    client_factory=lambda c: _FakeClient(),
                                    report_path=None)

    def test_module_imports_no_alpaca_offline(self):
        import agent.verify_alpaca_paper  # noqa: F401

        self.assertNotIn("alpaca", sys.modules)


if __name__ == "__main__":
    unittest.main()
