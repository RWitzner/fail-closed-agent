"""S2: Decimal-strict serialization; no float/NaN/Inf in rows; BrokerUSD vs ModeledUSD.

The serializer is the single seam where rows become bytes. It must be byte-stable
(canonical JSON), forbid floats entirely (money/size are Decimal), reject
non-finite Decimals, and keep the broker ledger type incompatible with the
modeled value so a modeled price can never be written into a broker field.
"""
import unittest
from decimal import Decimal

from agent.serializer import (
    BrokerUSD,
    ModeledUSD,
    as_broker_usd,
    dumps,
    row_hash,
)


class TestCanonicalSerialization(unittest.TestCase):
    def test_decimal_serialized_as_string(self):
        self.assertEqual(dumps({"x": Decimal("1.50")}), '{"x":"1.50"}')

    def test_keys_sorted_and_compact_separators(self):
        self.assertEqual(dumps({"b": 2, "a": 1}), '{"a":1,"b":2}')

    def test_nested_decimal_serialized(self):
        self.assertEqual(
            dumps({"o": {"q": Decimal("3"), "p": Decimal("0.01")}}),
            '{"o":{"p":"0.01","q":"3"}}',
        )


class TestFloatAndNonFiniteRejected(unittest.TestCase):
    def test_top_level_float_rejected(self):
        with self.assertRaises(ValueError):
            dumps({"price": 1.5})

    def test_nested_float_rejected(self):
        with self.assertRaises(ValueError):
            dumps({"legs": [{"size": 2.0}]})

    def test_nan_decimal_rejected(self):
        with self.assertRaises(ValueError):
            dumps({"price": Decimal("NaN")})

    def test_inf_decimal_rejected(self):
        with self.assertRaises(ValueError):
            dumps({"price": Decimal("Infinity")})


class TestRowHash(unittest.TestCase):
    def test_hash_is_deterministic(self):
        self.assertEqual(row_hash({"a": 1, "b": 2}), row_hash({"a": 1, "b": 2}))

    def test_hash_is_key_order_independent(self):
        self.assertEqual(row_hash({"a": 1, "b": 2}), row_hash({"b": 2, "a": 1}))

    def test_hash_is_sha256_hex(self):
        h = row_hash({"a": 1})
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises if not hex


class TestBrokerVsModeledNewtypes(unittest.TestCase):
    def test_broker_and_modeled_are_distinct_types(self):
        self.assertNotIsInstance(ModeledUSD("1"), BrokerUSD)
        self.assertNotIsInstance(BrokerUSD("1"), ModeledUSD)

    def test_as_broker_usd_accepts_broker_usd(self):
        self.assertEqual(as_broker_usd(BrokerUSD("1.25")), Decimal("1.25"))

    def test_modeled_price_cannot_write_broker_field(self):
        with self.assertRaises(TypeError):
            as_broker_usd(ModeledUSD("1.25"))

    def test_plain_decimal_cannot_write_broker_field(self):
        with self.assertRaises(TypeError):
            as_broker_usd(Decimal("1.25"))

    def test_float_cannot_write_broker_field(self):
        with self.assertRaises(TypeError):
            as_broker_usd(1.25)

    def test_non_finite_broker_usd_rejected(self):
        with self.assertRaises(ValueError):
            as_broker_usd(BrokerUSD("NaN"))
        with self.assertRaises(ValueError):
            as_broker_usd(BrokerUSD("Infinity"))


if __name__ == "__main__":
    unittest.main()
