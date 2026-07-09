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
        self.lookup_calls = []
        self.events = []
        self.drill_error = None
        self.submit_response = {
            "id": "broker-drill-1",
            "status": "accepted",
            "filled_qty": "0",
        }
        self.cancel_responses = None
        self._cancel_i = 0
        self.final_status = "canceled"
        self.lookup_responses = None
        self._lookup_i = 0

    def get_account(self):
        return self._account

    def get_clock(self):
        return self._clock

    def get_all_positions(self):
        return []

    def get_orders(self):
        return []

    def submit_order(self, *, order_data):
        coid = (order_data.get("client_order_id")
                if isinstance(order_data, dict)
                else getattr(order_data, "client_order_id", None))
        self.submit_calls.append(coid)
        self.events.append(("submit", coid))
        if self.drill_error is not None:
            raise self.drill_error
        return dict(self.submit_response, client_order_id=coid)

    def cancel_order_by_id(self, order_id):
        self.cancel_calls.append(order_id)
        self.events.append(("cancel", order_id))
        if self.cancel_responses is not None:
            i = min(self._cancel_i, len(self.cancel_responses) - 1)
            self._cancel_i += 1
            response = self.cancel_responses[i]
            if isinstance(response, BaseException):
                raise response
            return response

    def get_order_by_client_id(self, client_order_id):
        self.lookup_calls.append(client_order_id)
        self.events.append(("lookup", client_order_id))
        if self.lookup_responses is not None:
            i = min(self._lookup_i, len(self.lookup_responses) - 1)
            self._lookup_i += 1
            response = self.lookup_responses[i]
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                response = response(client_order_id)
            return response
        return {"id": "broker-drill-1", "status": self.final_status,
                "filled_qty": "0",
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

    def test_order_drill_missing_submit_id_recovers_by_client_id_and_cancels(self):
        client = _FakeClient()
        client.submit_response = {"status": "accepted"}
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertTrue(summary["ok"])
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])
        self.assertEqual(summary["order_drill"]["final_status"], "canceled")
        coid = summary["order_drill"]["client_order_id"]
        self.assertEqual(
            client.events,
            [("submit", coid), ("lookup", coid),
             ("cancel", "broker-drill-1"), ("lookup", coid)])

    def test_order_drill_nonterminal_final_status_fails(self):
        for status in (None, "new", "pending", "pending_cancel"):
            with self.subTest(status=status):
                client = _FakeClient()
                client.final_status = status
                with TemporaryDirectory() as tmp:
                    summary = self._run(tmp, client, allow_order_drill=True)
                self.assertFalse(summary["ok"])
                self.assertIn("order_drill_failed", summary["failures"])

    def test_order_drill_fill_is_never_ok(self):
        client = _FakeClient()
        client.final_status = "filled"
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])

    def test_order_drill_safe_terminal_statuses_are_ok(self):
        for status in ("canceled", "rejected", "expired"):
            with self.subTest(status=status):
                client = _FakeClient()
                client.final_status = status
                with TemporaryDirectory() as tmp:
                    summary = self._run(tmp, client, allow_order_drill=True)
                self.assertTrue(summary["ok"])
                self.assertEqual(summary["order_drill"]["final_status"], status)

    def test_order_drill_submit_exception_recovers_and_cancels_but_fails(self):
        client = _FakeClient()
        client.drill_error = TimeoutError("submit outcome unknown")
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertIn("submit outcome unknown", summary["order_drill"]["error"])
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])
        self.assertEqual(summary["order_drill"]["final_status"], "canceled")

    def test_order_drill_delayed_visibility_cancels_when_order_appears(self):
        client = _FakeClient()
        client.drill_error = TimeoutError("submit outcome unknown")
        client.lookup_responses = [
            {},
            lambda coid: {
                "id": "broker-drill-1", "status": "new",
                "filled_qty": "0",
                "client_order_id": coid,
            },
            lambda coid: {
                "id": "broker-drill-1", "status": "canceled",
                "filled_qty": "0",
                "client_order_id": coid,
            },
        ]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        drill = summary["order_drill"]
        coid = drill["client_order_id"]
        self.assertFalse(summary["ok"])
        self.assertIn("submit outcome unknown", drill["error"])
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])
        self.assertEqual(drill["final_status"], "canceled")
        self.assertEqual(
            client.events,
            [("submit", coid), ("lookup", coid), ("lookup", coid),
             ("cancel", "broker-drill-1"), ("lookup", coid)])

    def test_order_drill_retries_known_id_cancel_after_empty_observations(self):
        client = _FakeClient()
        client.cancel_responses = [TimeoutError("cancel transient"), None]
        client.lookup_responses = [{}, {}, {}]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        drill = summary["order_drill"]
        self.assertEqual(client.cancel_calls,
                         ["broker-drill-1", "broker-drill-1"])
        coid = drill["client_order_id"]
        self.assertEqual(
            client.events,
            [("submit", coid), ("cancel", "broker-drill-1"),
             ("lookup", coid), ("lookup", coid), ("lookup", coid),
             ("cancel", "broker-drill-1")])
        self.assertTrue(drill["canceled"])
        self.assertFalse(drill["terminal_verified"])
        self.assertIsNone(drill["final_status"])
        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])

    def test_order_drill_known_id_with_invalid_status_still_cancels(self):
        for response in (
                {"id": "broker-drill-1", "filled_qty": "0"},
                {"id": "broker-drill-1", "status": 123,
                 "filled_qty": "0"}):
            with self.subTest(response=response):
                client = _FakeClient()
                client.submit_response = response
                client.lookup_responses = [{}, {}, {}]
                with TemporaryDirectory() as tmp:
                    summary = self._run(
                        tmp, client, allow_order_drill=True)

                self.assertFalse(summary["ok"])
                self.assertEqual(client.cancel_calls, ["broker-drill-1"])
                self.assertIn("missing order status",
                              summary["order_drill"]["error"])

    def test_order_drill_safe_terminal_with_nonzero_fill_qty_fails(self):
        client = _FakeClient()
        client.lookup_responses = [lambda coid: {
            "id": "broker-drill-1", "status": "canceled",
            "filled_qty": "0.5", "client_order_id": coid,
        }]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertIn("filled_qty", summary["order_drill"]["error"])

    def test_order_drill_safe_terminal_requires_valid_fill_qty(self):
        for filled_qty in (None, "", "NaN", "not-a-number"):
            with self.subTest(filled_qty=filled_qty):
                client = _FakeClient()

                def terminal(coid, value=filled_qty):
                    response = {
                        "id": "broker-drill-1", "status": "canceled",
                        "client_order_id": coid,
                    }
                    if value is not None:
                        response["filled_qty"] = value
                    return response

                client.lookup_responses = [terminal]
                with TemporaryDirectory() as tmp:
                    summary = self._run(
                        tmp, client, allow_order_drill=True)

                self.assertFalse(summary["ok"])
                self.assertIn("order_drill_failed", summary["failures"])
                self.assertIn("filled_qty",
                              summary["order_drill"]["error"])

    def test_order_drill_partially_filled_is_hard_failure(self):
        client = _FakeClient()
        client.submit_response = {
            "id": "broker-drill-1", "status": "partially_filled",
        }
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertIn("partially_filled", summary["order_drill"]["error"])
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])

    def test_order_drill_recovered_status_without_id_cannot_pass(self):
        client = _FakeClient()
        client.submit_response = {"status": "accepted"}
        client.lookup_responses = [
            lambda coid: {"status": "canceled", "client_order_id": coid},
        ]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertEqual(client.cancel_calls, [])
        self.assertIn("missing broker order id", summary["order_drill"]["error"])

    def test_order_drill_recovered_identity_mismatch_never_cancels(self):
        client = _FakeClient()
        client.submit_response = {"status": "accepted"}
        client.lookup_responses = [{
            "id": "broker-drill-1", "status": "new",
            "client_order_id": "some-other-order",
        }]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertEqual(client.cancel_calls, [])
        self.assertIn("client_order_id mismatch",
                      summary["order_drill"]["error"])

    def test_order_drill_recovered_id_must_stay_stable(self):
        client = _FakeClient()
        client.lookup_responses = [
            lambda coid: {
                "id": "broker-drill-2", "status": "canceled",
                "client_order_id": coid,
            },
        ]
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)

        self.assertFalse(summary["ok"])
        self.assertEqual(client.cancel_calls, ["broker-drill-1"])
        self.assertIn("broker order id changed",
                      summary["order_drill"]["error"])

    def test_order_drill_already_rejected_or_expired_needs_no_cancel(self):
        for status in ("rejected", "expired"):
            with self.subTest(status=status):
                client = _FakeClient()
                client.submit_response = {
                    "id": "broker-drill-1", "status": status,
                    "filled_qty": "0",
                }
                with TemporaryDirectory() as tmp:
                    summary = self._run(tmp, client, allow_order_drill=True)

                self.assertTrue(summary["ok"])
                self.assertEqual(client.cancel_calls, [])
                self.assertEqual(client.lookup_calls, [])
                self.assertEqual(summary["order_drill"]["final_status"],
                                 status)

    def test_order_drill_failure_recorded_not_raised(self):
        client = _FakeClient()
        client.drill_error = RuntimeError("submit rejected")
        with TemporaryDirectory() as tmp:
            summary = self._run(tmp, client, allow_order_drill=True)
        self.assertFalse(summary["ok"])
        self.assertIn("order_drill_failed", summary["failures"])
        self.assertIn("submit rejected", summary["order_drill"]["error"])

    def test_drill_symbol_clamped(self):
        client = _FakeClient()
        with TemporaryDirectory() as tmp:
            for bad in ("aapl", "TOOLONGX", "BRK.B", ""):
                with self.subTest(symbol=bad):
                    with self.assertRaises(ValueError):
                        self._run(tmp, client, allow_order_drill=True,
                                  drill_symbol=bad)
        self.assertEqual(client.submit_calls, [])

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
