"""M7c credentialed historical backfill + cross-sectional input-manifest builder.

This produces the two reviewed inputs the ``m7-historical-cross-sectional-artifact``
writer consumes: the per-symbol normalized quote JSONL and ONE hash-bound multi-symbol
manifest (predeclared universe block + per-symbol ``{instrument_id, row_count,
quote_rows_sha256}`` data binding).

Split by testability:

- PURE + offline-complete: ``normalize_quote_event(s)`` (recorder ``QuoteEvent`` →
  normalized row), ``derive_session_windows`` / ``instrument_ids_from_rows`` (derive the
  calendar + per-symbol ids from the rows), ``build_cross_sectional_input_manifest`` (emits
  a manifest that ``validate_historical_cross_sectional_manifest`` accepts; the per-symbol
  ``quote_rows_sha256`` and the body ``manifest_hash`` use the SAME primitives the validator
  recomputes with), and ``cross_sectional_data_pin``. These are fully unit-tested and the
  round-trip build → validate is pinned.
- LIVE seam (``pull_normalized_window``): iterates the window/universe and decodes
  Databento ``bbo-1m`` into recorder ``QuoteEvent`` then normalized rows. The decode +
  normalize + write orchestration is offline-tested through an injected
  ``quote_event_source``; the REAL ``databento`` SDK is imported lazily and the raw
  ``timeseries.get_range`` DBN-record adapter (``_dbn_bbo1m_record_to_event``) is the only
  piece that cannot be exercised offline — it is flagged for live (tier-2b) verification
  and must never be trusted as reviewed evidence until run against the live Historical API.

No offline path imports ``databento`` or reads ``.secrets/``; tests pass a fake source.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping as TypingMapping, Sequence

from agent.fees import FEE_MODEL_VERSION
from agent.serializer import dumps, row_hash
from agent.backtest_historical import (
    _MANIFEST_VERSION,
    _NORMALIZER_ID,
    _PRICING_MODEL_VERSION,
    _REALISM_GAP_MODEL_VERSION,
    _quote_rows_sha256,
)

DEFAULT_INTERVAL = "1m"
DEFAULT_SOURCE_ID_PREFIX = "historical"
DEFAULT_HORIZON = "30m"
DEFAULT_LATENCY_BUDGET_MS = 250
DEFAULT_SLIPPAGE_CAP_BPS = "25"
DEFAULT_RTH_OPEN_UTC_TIME = "13:30:00.000000Z"
DEFAULT_RTH_CLOSE_UTC_TIME = "20:00:00.000000Z"
DEFAULT_RAW_SOURCE = "historical-databento"
DEFAULT_RTH_WINDOW = ("13:30:00", "20:00:00")  # UTC HH:MM:SS (EDT regular RTH)
_DEFAULT_SECRETS = (
    Path(__file__).resolve().parents[2] / ".secrets" / "databento.json")

# Databento DBN fixed-point: integer prices are USD * 1e-9 (design §M1; never float).
_DBN_PRICE_SCALE = Decimal("1e-9")
_DBN_PRICE_QUANTUM = Decimal("0.0001")  # 4dp Reg-NMS sub-penny tick (matches recorder)
_DBN_UNDEF_PRICE = 9223372036854775807  # INT64_MAX sentinel = empty/undefined level


# --------------------------------------------------------------------------- normalize

def normalize_quote_event(event, *, dataset: str, schema: str) -> dict:
    """Map a recorder ``QuoteEvent`` to a normalized historical quote row.

    Prices/sizes are emitted as canonical Decimal strings (the ``QuoteEvent`` already
    quantized them at parse), so the row round-trips through the serializer's
    Decimal-strict grid. ``vendor_seq`` falls back to ``0`` where the schema (e.g.
    ``bbo-1m``) carries no per-venue sequence.
    """
    prov = event.provenance
    vendor_seq = prov.vendor_seq if prov.vendor_seq is not None else 0
    return {
        "dataset": dataset,
        "schema": schema,
        "symbol": prov.symbol,
        "instrument_id": prov.instrument_id,
        "vendor_seq": vendor_seq,
        "ts_event_utc": prov.ts_event_utc,
        "ts_recv_utc": prov.ts_recv_utc,
        "bid_px": str(event.bid_px),
        "bid_sz": str(event.bid_sz),
        "ask_px": str(event.ask_px),
        "ask_sz": str(event.ask_sz),
        "reconnect_epoch": prov.reconnect_epoch,
    }


def normalize_quote_events(events: Iterable[object], *, dataset: str,
                           schema: str) -> list:
    return [normalize_quote_event(e, dataset=dataset, schema=schema) for e in events]


# ----------------------------------------------------------------- derive from rows

def session_dates_from_rows(
        symbol_rows: TypingMapping[str, Sequence[dict]]) -> tuple:
    """Distinct session dates across every symbol's rows (``ts_event_utc[:10]``).

    RTH ``bbo-1m`` events fall inside the UTC trading day, so the UTC date is the ET
    session date for this tier.
    """
    dates = set()
    for rows in symbol_rows.values():
        for row in rows:
            ts = row.get("ts_event_utc")
            if isinstance(ts, str) and len(ts) >= 10:
                dates.add(ts[:10])
    return tuple(sorted(dates))


def derive_session_windows(
        symbol_rows: TypingMapping[str, Sequence[dict]], *,
        rth_open_utc_time: str = DEFAULT_RTH_OPEN_UTC_TIME,
        rth_close_utc_time: str = DEFAULT_RTH_CLOSE_UTC_TIME) -> dict:
    """Regular-RTH session windows for every session date present in the rows.

    NOTE: assumes a regular 13:30–20:00 UTC RTH session; early-close days would be
    mis-stated. The credentialed run should override ``sessions`` from the pinned market
    calendar when the window can contain a half day.
    """
    return {
        date: {
            "rth_open_utc": f"{date}T{rth_open_utc_time}",
            "rth_close_utc": f"{date}T{rth_close_utc_time}",
        }
        for date in session_dates_from_rows(symbol_rows)
    }


def instrument_ids_from_rows(
        symbol_rows: TypingMapping[str, Sequence[dict]]) -> dict:
    """Per-symbol instrument id, requiring exactly one distinct int id per symbol."""
    out: dict = {}
    for symbol, rows in symbol_rows.items():
        ids = {row.get("instrument_id") for row in rows}
        ids.discard(None)
        if len(ids) != 1:
            raise ValueError(
                f"symbol {symbol!r} must have exactly one instrument_id, "
                f"got {sorted(i for i in ids if isinstance(i, int))}")
        (instrument_id,) = tuple(ids)
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
            raise ValueError(f"symbol {symbol!r} instrument_id must be an int")
        out[symbol] = instrument_id
    return out


# ----------------------------------------------------------------- manifest builder

def build_cross_sectional_input_manifest(
        *, symbol_rows: TypingMapping[str, Sequence[dict]],
        universe: Sequence[str], dataset: str, schema: str,
        hypothesis_id: str, selection_rule: str, calendar_pin: str,
        instrument_ids: TypingMapping[str, int] | None = None,
        sessions: TypingMapping[str, TypingMapping[str, str]] | None = None,
        blackout_session_dates_et: Sequence[str] = (),
        latency_budget_ms: int = DEFAULT_LATENCY_BUDGET_MS,
        slippage_cap_bps=DEFAULT_SLIPPAGE_CAP_BPS,
        horizon: str = DEFAULT_HORIZON,
        fee_model_version: str = FEE_MODEL_VERSION,
        pricing_model_version: str = _PRICING_MODEL_VERSION,
        realism_gap_model_version: str = _REALISM_GAP_MODEL_VERSION,
        normalizer_id: str = _NORMALIZER_ID,
        raw_source: str = DEFAULT_RAW_SOURCE) -> dict:
    """Build ONE hash-bound cross-sectional input manifest from normalized rows.

    The per-symbol ``quote_rows_sha256`` and the body ``manifest_hash`` are computed with
    the exact primitives ``validate_historical_cross_sectional_manifest`` recomputes with,
    so ``build → validate`` round-trips. The universe symbol set, the per-symbol data
    block, and ``symbol_rows`` must match exactly. The data pin is derived (not stored).
    """
    universe_t = tuple(universe)
    if len(set(universe_t)) != len(universe_t):
        raise ValueError("universe must not contain duplicate symbols")
    rows_by_symbol = {sym: tuple(rows) for sym, rows in symbol_rows.items()}
    if set(rows_by_symbol) != set(universe_t):
        missing = sorted(set(universe_t) - set(rows_by_symbol))
        extra = sorted(set(rows_by_symbol) - set(universe_t))
        raise ValueError(
            "symbol_rows must cover exactly the universe symbols "
            f"(missing={missing}, extra={extra})")
    # Fail-closed regardless of whether ids are derived or supplied: every universe
    # symbol must carry at least one quote row (an empty symbol would otherwise hash to
    # an empty sha256 and only fail late in the validator's quote-quality pass).
    empty = sorted(sym for sym in universe_t if not rows_by_symbol[sym])
    if empty:
        raise ValueError(
            f"every universe symbol must have at least one quote row; empty: {empty}")
    if instrument_ids is None:
        instrument_ids = instrument_ids_from_rows(rows_by_symbol)
    elif set(instrument_ids) != set(universe_t):
        raise ValueError("instrument_ids must cover exactly the universe symbols")
    if sessions is None:
        sessions = derive_session_windows(rows_by_symbol)
    else:
        # A custom sessions map MUST cover every session date in the rows, else the
        # harness silently falls back to a default 13:30-20:00 window for the missing
        # date (a reproducibility/anti-lookahead footgun on early-close days).
        missing_sessions = sorted(
            set(session_dates_from_rows(rows_by_symbol)) - set(sessions))
        if missing_sessions:
            raise ValueError(
                "custom sessions map is missing session dates present in the rows: "
                f"{missing_sessions}")

    body = {
        "v": _MANIFEST_VERSION,
        "dataset": dataset,
        "schema": schema,
        "interval": DEFAULT_INTERVAL,
        "source_id_prefix": DEFAULT_SOURCE_ID_PREFIX,
        "normalizer_id": normalizer_id,
        "raw_source": raw_source,
        "universe": {
            "hypothesis_id": hypothesis_id,
            "selection_rule": selection_rule,
            "symbols": list(universe_t),
        },
        "symbols": {
            sym: {
                "instrument_id": instrument_ids[sym],
                "row_count": len(rows_by_symbol[sym]),
                "quote_rows_sha256": _quote_rows_sha256(rows_by_symbol[sym]),
            }
            for sym in universe_t
        },
        "calendar": {
            "calendar_pin": calendar_pin,
            "sessions": dict(sessions),
        },
        "corporate_actions": {
            "blackout_session_dates_et": list(blackout_session_dates_et),
        },
        "execution": {
            "latency_budget_ms": latency_budget_ms,
            "slippage_cap_bps": str(slippage_cap_bps),
            "horizon": horizon,
            "fee_model_version": fee_model_version,
            "pricing_model_version": pricing_model_version,
            "realism_gap_model_version": realism_gap_model_version,
        },
    }
    body["manifest_hash"] = row_hash(body)
    return body


def cross_sectional_data_pin(manifest: TypingMapping[str, object]) -> str:
    """Universe-level data pin bound to the manifest hash (no symbol suffix)."""
    return (
        f"{manifest['dataset']}:{manifest['schema']}:{manifest['interval']}:"
        f"{manifest['source_id_prefix']}:{manifest['manifest_hash']}"
    )


def write_quote_rows_jsonl(path, rows: Sequence[dict]) -> Path:
    """Write normalized rows as canonical one-row-per-line JSONL (matches the loader)."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dumps(row) + "\n")
    return output


# ----------------------------------------------------------------- live pull seam

def _dbn_int(value, *, field: str, symbol: str) -> int:
    """Require a strict int (rejects None/float/bool) so the speculative live adapter
    fails closed rather than silently truncating untrusted data."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{symbol}: bbo-1m {field} must be an int (got {value!r})")
    return value


def _dbn_price(raw) -> Decimal:
    """Scale a DBN int 1e-9 fixed-point price to a 4dp USD Decimal (never float)."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"DBN price must be int 1e-9 fixed-point (got {raw!r})")
    return (Decimal(raw) * _DBN_PRICE_SCALE).quantize(_DBN_PRICE_QUANTUM)


def _ns_to_iso_utc(ns: int) -> str:
    """Nanosecond epoch → ISO-8601 UTC microsecond string (deterministic, no float)."""
    micros = ns // 1000
    secs, micro = divmod(micros, 1_000_000)
    base = datetime.fromtimestamp(secs, tz=timezone.utc)
    return base.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:06d}Z"


def _dbn_bbo1m_record_to_event_dict(record, *, symbol: str) -> dict:
    """Adapt one live ``bbo-1m`` DBN record to the recorder ``parse`` input dict.

    tier-2b-UNVERIFIED: the DBN field layout (``*_00`` top-of-book, int 1e-9 prices,
    ``UNDEF_PRICE`` empty side, ``ts_recv``/``sequence`` names) is assumed from the design
    note and MUST be verified against the live ``timeseries.get_range`` records before any
    reviewed artifact. Fails closed on missing/one-sided/non-positive/non-int fields; never
    uses ``to_df`` (floats). Returns the recorder ``parse`` record dict plus the ISO
    ``ts_recv_utc`` (which ``parse`` takes as a kwarg, not from the record body).
    """
    bid_px = getattr(record, "bid_px_00", None)
    ask_px = getattr(record, "ask_px_00", None)
    if (bid_px is None or ask_px is None
            or bid_px == _DBN_UNDEF_PRICE or ask_px == _DBN_UNDEF_PRICE):
        raise ValueError(f"{symbol}: one-sided/undefined bbo-1m record dropped")
    bid_sz = _dbn_int(getattr(record, "bid_sz_00", None), field="bid_sz_00", symbol=symbol)
    ask_sz = _dbn_int(getattr(record, "ask_sz_00", None), field="ask_sz_00", symbol=symbol)
    if bid_sz <= 0 or ask_sz <= 0:
        raise ValueError(
            f"{symbol}: non-positive bbo-1m size dropped (bid={bid_sz}, ask={ask_sz})")
    return {
        "symbol": symbol,
        "instrument_id": _dbn_int(
            getattr(record, "instrument_id", None), field="instrument_id", symbol=symbol),
        "vendor_seq": _dbn_int(
            getattr(record, "sequence", 0), field="sequence", symbol=symbol),
        "ts_event": _ns_to_iso_utc(
            _dbn_int(getattr(record, "ts_event", None), field="ts_event", symbol=symbol)),
        "ts_recv_utc": _ns_to_iso_utc(
            _dbn_int(getattr(record, "ts_recv", None), field="ts_recv", symbol=symbol)),
        "bid_px": str(_dbn_price(bid_px)),
        "ask_px": str(_dbn_price(ask_px)),
        "bid_sz": str(bid_sz),
        "ask_sz": str(ask_sz),
    }


def _within_rth(row: dict, rth_window) -> bool:
    """Keep only rows whose minute boundary (``ts_recv_utc`` time-of-day) is inside RTH.

    bbo-1m records carry their minute boundary in ``ts_recv``; the historical harness
    treats every bar as RTH (no time-of-day gate), so extended-hours bars MUST be filtered
    here or they would be modeled as tradable.
    """
    if rth_window is None:
        return True
    open_t, close_t = rth_window
    time_of_day = row["ts_recv_utc"][11:19]
    return open_t <= time_of_day <= close_t


def pull_normalized_window(
        *, dataset: str, schema: str, universe: Sequence[str],
        start_utc: str, end_utc: str, quote_event_source=None,
        credentials_path=None, rth_window=DEFAULT_RTH_WINDOW) -> dict:
    """Pull + normalize a window of ``bbo-1m`` quotes per universe symbol.

    Returns ``{symbol: [normalized_row, ...]}`` filtered to RTH. The decode/normalize/order
    orchestration is offline-testable through an injected ``quote_event_source(symbol) ->
    Iterable[QuoteEvent]``. With no source, the real Databento Historical client is built
    lazily (credentialed) — never reached offline (the import is lazy inside the source).
    """
    if quote_event_source is None:
        quote_event_source = _live_quote_event_source(
            dataset=dataset, schema=schema, start_utc=start_utc,
            end_utc=end_utc, credentials_path=credentials_path)
    out: dict = {}
    for symbol in universe:
        rows = normalize_quote_events(
            quote_event_source(symbol), dataset=dataset, schema=schema)
        out[symbol] = [row for row in rows if _within_rth(row, rth_window)]
    return out


def _live_quote_event_source(*, dataset, schema, start_utc, end_utc,
                             credentials_path):  # pragma: no cover - tier-2b live
    """Credentialed live source: lazy ``databento`` import + ``.secrets`` read, then a
    per-symbol ``timeseries.get_range`` pull decoded through ``_dbn_bbo1m_record_to_event_dict``
    and the recorder ``parse`` (proven int-1e-9 quantization). One-sided/undefined/malformed
    records are dropped (fail-closed per record, not per pull). The DBN field layout was
    verified against the live ``EQUS.MINI`` ``bbo-1m`` ``BBOMsg`` (flat ``*_00`` top-of-book,
    int 1e-9 prices). Never reached by an offline test (the import is lazy here)."""
    import databento  # noqa: WPS433  (lazy by contract — keeps no-network tests green)
    from recorder.event import MalformedRecord, NonFinitePrice, PrecisionLoss, parse

    secrets = Path(credentials_path) if credentials_path else _DEFAULT_SECRETS
    api_key = json.loads(secrets.read_text(encoding="utf-8"))["api_key"]
    client = databento.Historical(api_key)

    def source(symbol):
        store = client.timeseries.get_range(
            dataset=dataset, schema=schema, symbols=[symbol],
            start=start_utc, end=end_utc, stype_in="raw_symbol")
        events = []
        for record in store:
            try:
                record_dict = _dbn_bbo1m_record_to_event_dict(record, symbol=symbol)
            except ValueError:
                continue  # one-sided / undefined / non-positive size -> drop the bar
            ts_recv_utc = record_dict.pop("ts_recv_utc")
            try:
                events.append(parse(
                    record_dict, dataset=dataset, schema=schema,
                    reconnect_epoch=0, ts_recv_utc=ts_recv_utc))
            except (MalformedRecord, NonFinitePrice, PrecisionLoss):
                continue  # vendor decode anomaly -> drop the bar (fail-closed)
        return events

    return source
