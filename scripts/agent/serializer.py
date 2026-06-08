"""Deterministic, Decimal-strict serialization (spec §7, invariant S2).

Every persisted row passes through `dumps`: canonical JSON (sorted keys, compact
separators), Decimal rendered as a string, floats forbidden entirely, non-finite
Decimals rejected. `BrokerUSD` and `ModeledUSD` are distinct money newtypes so the
broker ledger (position-of-record) is type-incompatible with the modeled
execution-realism value and a modeled price can never be written into a broker
field (`as_broker_usd`).
"""
import hashlib
import json
from decimal import Decimal


class BrokerUSD(Decimal):
    """Money on the broker ledger (position-of-record). Distinct from ModeledUSD."""

    __slots__ = ()


class ModeledUSD(Decimal):
    """Money from the Databento-depth execution-realism model — label/scoring only."""

    __slots__ = ()


def _reject_floats(obj):
    if isinstance(obj, float):
        raise ValueError("float not allowed in serialized rows; use Decimal")
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_floats(key)
            _reject_floats(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _reject_floats(value)


def _default(obj):
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError(f"non-finite Decimal not allowed: {obj!r}")
        return str(obj)
    raise TypeError(f"type not serializable: {type(obj).__name__}")


def dumps(row) -> str:
    """Canonical, byte-stable JSON for a journal row. Rejects floats / non-finite."""
    _reject_floats(row)
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=_default)


def row_hash(row) -> str:
    """sha256 hex of the canonical serialization — replayable, diffable."""
    return hashlib.sha256(dumps(row).encode("utf-8")).hexdigest()


def as_broker_usd(value) -> BrokerUSD:
    """Guard for broker-ledger money fields: only a finite `BrokerUSD` is accepted."""
    if not isinstance(value, BrokerUSD):
        raise TypeError("broker ledger field requires BrokerUSD")
    if not value.is_finite():
        raise ValueError("non-finite BrokerUSD")
    return value
