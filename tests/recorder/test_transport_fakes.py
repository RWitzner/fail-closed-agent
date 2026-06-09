"""Transport seam + fault-injection fakes (M1 §A, §F2, §N).

Offline-only: importing the Databento transport must NOT pull the real
`databento` SDK into `sys.modules` and must open no socket. The credentialed
build path is a tier-2 stub that raises `NotImplementedError` offline.
"""
import asyncio
import socket
import sys
import unittest
from unittest import mock

from agent.marketdata.base import MarketDataTransport
from agent.marketdata.databento import (
    DatabentoConfig,
    DatabentoTransport,
)
from tests.lib.fakes import FlakyTransport, TransportDisconnected


def _fake_raw_source(messages):
    async def _source(symbols):
        for message in messages:
            yield message

    return _source


async def _collect(transport, symbols):
    out = []
    async for msg in transport.stream(symbols):
        out.append(msg)
    return out


def _config(symbols=("AAPL", "MSFT")):
    return DatabentoConfig(
        dataset="EQUS.MINI",
        schema="tbbo",
        symbols=tuple(symbols),
    )


class TestDatabentoTransportSeam(unittest.TestCase):
    def test_databento_transport_is_a_marketdata_transport(self):
        transport = DatabentoTransport(
            _config(), raw_source=_fake_raw_source([])
        )
        self.assertIsInstance(transport, MarketDataTransport)

    def test_streams_injected_bytes_in_order(self):
        frames = [b'{"a":1}', b'{"b":2}', b'{"c":3}']
        transport = DatabentoTransport(
            _config(), raw_source=_fake_raw_source(frames)
        )
        got = asyncio.run(_collect(transport, ["AAPL"]))
        self.assertEqual(got, frames)

    def test_unsubscribed_symbol_raises(self):
        sentinel = {"accessed": False}

        def _guarded_source(symbols):  # pragma: no cover - must never run
            sentinel["accessed"] = True

            async def _gen(_symbols):
                if False:
                    yield b""

            return _gen(symbols)

        transport = DatabentoTransport(_config(), raw_source=_guarded_source)

        async def _drive():
            async for _ in transport.stream(["TSLA"]):
                pass

        with self.assertRaises(ValueError):
            asyncio.run(_drive())
        self.assertFalse(sentinel["accessed"])

    def test_empty_symbols_raises(self):
        transport = DatabentoTransport(
            _config(), raw_source=_fake_raw_source([b"x"])
        )

        async def _drive():
            async for _ in transport.stream([]):
                pass

        with self.assertRaises(ValueError):
            asyncio.run(_drive())


class TestNoNetworkNoCreds(unittest.TestCase):
    def test_import_adds_no_databento_to_sys_modules_and_no_socket(self):
        import agent.marketdata.databento  # noqa: F401

        self.assertNotIn("databento", sys.modules)

        transport = DatabentoTransport(
            _config(), raw_source=_fake_raw_source([b'{"q":1}'])
        )
        # Build the event loop OUTSIDE the patch (asyncio's self-pipe socketpair
        # is loop plumbing, not transport behavior — mirrors test_no_network_no_creds
        # which patches socket only around the M0 flow, not the runner).
        loop = asyncio.new_event_loop()
        try:
            with mock.patch(
                "socket.socket",
                side_effect=AssertionError("transport must not open sockets"),
            ):
                got = loop.run_until_complete(_collect(transport, ["AAPL"]))
        finally:
            loop.close()
        self.assertEqual(got, [b'{"q":1}'])
        self.assertNotIn("databento", sys.modules)

    def test_credentialed_build_is_notimplemented_offline(self):
        # raw_source is None -> credentialed (tier-2) path; offline must refuse.
        transport = DatabentoTransport(_config())
        with self.assertRaises(NotImplementedError):
            transport._build_real_client()
        self.assertNotIn("databento", sys.modules)

    def test_credentialed_stream_is_notimplemented_offline(self):
        transport = DatabentoTransport(_config())

        async def _drive():
            async for _ in transport.stream(["AAPL"]):
                pass

        with self.assertRaises(NotImplementedError):
            asyncio.run(_drive())
        self.assertNotIn("databento", sys.modules)


class TestFlakyTransport(unittest.TestCase):
    def _flaky_frames(self):
        # Mirrors §L.4 flaky_transport_gap.jsonl: data, disconnect, reconnect, data.
        return [
            {
                "dataset": "EQUS.MINI",
                "schema": "mbp-10",
                "instrument_id": 1001,
                "symbol": "AAPL",
                "vendor_seq": 2001,
            },
            {"_control": "disconnect", "after_seq": 2001},
            {"_control": "reconnect"},
            {
                "dataset": "EQUS.MINI",
                "schema": "mbp-10",
                "instrument_id": 1001,
                "symbol": "AAPL",
                "vendor_seq": 2004,
            },
        ]

    def test_flaky_transport_disconnect_raises_transport_disconnected(self):
        flaky = FlakyTransport(self._flaky_frames())

        async def _first_segment():
            out = []
            agen = flaky.stream(["AAPL"])
            try:
                async for msg in agen:
                    out.append(msg)
            except TransportDisconnected:
                return out, True
            return out, False

        first, disconnected = asyncio.run(_first_segment())
        # Pre-disconnect data frame yielded as json bytes; control row not yielded.
        self.assertEqual(len(first), 1)
        self.assertIsInstance(first[0], bytes)
        self.assertIn(b'"vendor_seq":2001', first[0])
        self.assertTrue(disconnected)

        # A subsequent stream() call resumes AFTER the matching reconnect row.
        second = asyncio.run(_collect(flaky, ["AAPL"]))
        self.assertEqual(len(second), 1)
        self.assertIn(b'"vendor_seq":2004', second[0])

    def test_flaky_transport_is_a_marketdata_transport(self):
        self.assertIsInstance(FlakyTransport([]), MarketDataTransport)

    def test_control_unaware_yields_only_data_frames(self):
        flaky = FlakyTransport(self._flaky_frames(), control_aware=False)
        got = asyncio.run(_collect(flaky, ["AAPL"]))
        # Both data frames, no disconnect, no control rows yielded.
        self.assertEqual(len(got), 2)
        self.assertTrue(all(isinstance(m, bytes) for m in got))
        self.assertNotIn(b"_control", got[0])
        self.assertNotIn(b"_control", got[1])


if __name__ == "__main__":
    unittest.main()
