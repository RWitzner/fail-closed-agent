"""Config loading, tighten-only merge, and rules-hash provenance (spec §4.7).

An authoritative overlay may only *tighten* the base: booleans combine with AND
(a gate can be turned off but never on), numeric caps take the minimum (lowered
but never raised), dicts recurse, and overlay-only keys are ignored (no new
permissions). `rules_hash` canonically hashes the effective config.
"""
import hashlib
import json
from pathlib import Path


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rules_hash(config) -> str:
    # allow_nan=False so an accidental non-finite config value raises loudly rather
    # than hashing to an invalid-JSON NaN/Infinity literal.
    canon = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def tighten_only_merge(base, authoritative):
    if isinstance(base, dict) and isinstance(authoritative, dict):
        out = {}
        for key, base_value in base.items():
            if key in authoritative:
                out[key] = tighten_only_merge(base_value, authoritative[key])
            else:
                out[key] = base_value
        return out
    if isinstance(base, bool) and isinstance(authoritative, bool):
        return base and authoritative
    if (
        isinstance(base, (int, float))
        and not isinstance(base, bool)
        and isinstance(authoritative, (int, float))
        and not isinstance(authoritative, bool)
    ):
        return min(base, authoritative)
    # type mismatch or unhandled kind: the overlay cannot loosen, so keep base.
    return base
