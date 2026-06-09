"""Databento `MarketDataTransport` implementation behind the M0 seam (M1 §A).

Offline-complete: streaming is driven by an **injected** ``raw_source`` (an async
iterator of raw vendor frames). The real ``databento`` SDK is imported **lazily**,
only inside the credentialed (tier-2) build path, which no offline test reaches —
so importing this module and streaming offline never pulls ``databento`` into
``sys.modules`` and opens no socket. Mirrors ``agent/broker/alpaca.py``, which
imports no SDK at module scope.
"""
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Sequence


@dataclass(frozen=True)
class DatabentoConfig:
    """Frozen subscription config. One schema per subscription (spec §5.1)."""

    dataset: str  # exact pinned code, e.g. "EQUS.MINI" (placeholder OK offline)
    schema: str  # exactly ONE schema per subscription (no mixed query)
    symbols: tuple  # tuple[str], order-preserved as given
    heartbeat_timeout_ms: int = 30000
    backoff_base_ms: int = 250
    backoff_cap_ms: int = 30000  # always-on reconnect backoff CAP
    disconnect_alert_ms: int = 60000  # prolonged-disconnect -> data_quality_alert


class DatabentoAuthError(RuntimeError):
    """Credential / entitlement failure. LIVE path only — never raised offline."""


class DatabentoTransport:
    """``MarketDataTransport`` for Databento.

    OFFLINE: driven by an injected ``raw_source`` (a callable taking ``symbols``
    and returning an async iterator of raw ``bytes`` frames). The real SDK is
    imported LAZILY only when ``raw_source is None`` (credentialed path), so
    importing this module and streaming offline never pulls ``databento`` into
    ``sys.modules`` (keeps ``test_no_network_no_creds`` green). Mirrors
    ``agent/broker/alpaca.py``, which imports no SDK at module scope.
    """

    def __init__(
        self,
        config: DatabentoConfig,
        *,
        raw_source: Optional[Callable[[Sequence[str]], "AsyncIterator[bytes]"]] = None,
        credentials_loader: Optional[Callable[[], str]] = None,
    ) -> None:
        self._config = config
        self._raw_source = raw_source
        self._credentials_loader = credentials_loader

    @property
    def config(self) -> DatabentoConfig:
        return self._config

    async def stream(self, symbols) -> "AsyncIterator[bytes]":
        """ASYNC GENERATOR (mirrors ``FakeTransport.stream``).

        Yields raw vendor frames (bytes) for the recorder to parse — NO parsing,
        NO timestamping, NO ordering change (pure pass-through offline).

        - ``symbols`` is validated to be a subset of ``config.symbols``; an
          unsubscribed symbol (or empty ``symbols``) raises ``ValueError`` BEFORE
          any source access (fail-closed, no silent widening).
        - If ``raw_source`` is set: delegate to it (offline) — zero SDK import,
          zero socket.
        - Else: lazily build a real client (credentialed path). This branch is the
          tier-2 STUB and raises ``NotImplementedError`` offline (never reached).
        """
        requested = tuple(symbols)
        if not requested:
            raise ValueError("stream requires a non-empty symbol subset")
        subscribed = set(self._config.symbols)
        unsubscribed = [s for s in requested if s not in subscribed]
        if unsubscribed:
            raise ValueError(
                "symbols not in subscription "
                f"(fail-closed, no silent widening): {unsubscribed}"
            )

        if self._raw_source is not None:
            async for frame in self._raw_source(requested):
                yield frame
            return

        # Credentialed (tier-2) path: lazily build a real client. Offline this
        # raises NotImplementedError and is never reached by an offline test.
        client = self._build_real_client()
        async for frame in client.stream(requested):  # pragma: no cover - tier-2
            yield frame

    def _build_real_client(self):
        """Tier-2 stub: lazily ``import databento`` and read ``.secrets/``.

        In M1 offline scope this raises ``NotImplementedError``. Per the
        2026-06-09 access constraint, the entitled key (``.secrets/databento.json``)
        is HISTORICAL only; a live realtime client is a deferred paid subscription.
        """
        raise NotImplementedError(
            "credentialed Databento client lands in M1 tier-2"
        )
