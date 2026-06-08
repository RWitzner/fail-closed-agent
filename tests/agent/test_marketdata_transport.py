"""MarketDataTransport seam + deterministic FakeTransport (spec §5 Tier 1)."""
import asyncio
import unittest

from agent.marketdata.base import MarketDataTransport
from tests.lib.fakes import FakeTransport


async def _collect(transport, symbols):
    out = []
    async for msg in transport.stream(symbols):
        out.append(msg)
    return out


class TestFakeTransport(unittest.TestCase):
    def test_fake_is_a_marketdata_transport(self):
        self.assertIsInstance(FakeTransport([]), MarketDataTransport)

    def test_streams_scripted_messages_in_order(self):
        msgs = [b"a", b"b", b"c"]
        got = asyncio.run(_collect(FakeTransport(msgs), ["AAPL"]))
        self.assertEqual(got, msgs)

    def test_empty_script_yields_nothing(self):
        self.assertEqual(asyncio.run(_collect(FakeTransport([]), ["AAPL"])), [])


if __name__ == "__main__":
    unittest.main()
