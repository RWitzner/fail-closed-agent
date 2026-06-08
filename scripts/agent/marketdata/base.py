"""Market-data transport seam (spec §5 Tier 1).

The transport is the pluggable boundary: a real vendor adapter (Databento, M1) or
a `FakeTransport`/`FlakyTransport` in tests. `stream` yields raw vendor messages
for the recorder to parse, so ingest/parse/persist can be driven deterministically
offline.
"""
from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class MarketDataTransport(Protocol):
    def stream(self, symbols) -> AsyncIterator[bytes]:
        """Yield raw market-data messages for the given symbols."""
        ...
