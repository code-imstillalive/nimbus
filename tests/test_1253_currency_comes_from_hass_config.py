"""nimbus issue #1253: the flattened children resolve currency from
`hass.config.currency` instead of hardcoding AUD.

## Measured before changing anything, which is what the issue asks for

Reference household's production install, 2026-09-26, read-only via REST:

```
nimbus sensors with unit AUD or AUD/kWh ....... 38
  of those carrying state_class ............... 38   (all 'measurement')
  of those free to change without a repair .... 0
by unit: AUD 25, AUD/kWh 13
```

The issue hoped this might be "a 3-repair event". It is not — every one of them
carries long-term statistics, and the issue's own "~20 children" estimate was
low (37 rows in the table).

**And the measurement that makes it safe anyway:** `GET /api/config` returns
`currency: 'AUD'`. So on this install the property returns the same string the
constant did — HA sees no unit change, raises no statistics repair, breaks no
series. The issue said that case "should be verified, not assumed". It is now
verified.

A non-AUD install does see all 38 change. That is the correct one-time cost:
those units are currently wrong, the statistics behind them were recorded under
a false label, and no version of "fix the mislabel" avoids changing the label
once.

## Why sentinels rather than a constructor argument

`create_flattened_entities()` and its five siblings take `(entry, sw_version)`
with no `hass`. Threading `hass` through six factory signatures to resolve a
string at construction is more surface than resolving it where it is read. The
entity has `self.hass` by the time HA asks for the unit.

The sentinel values are deliberately **not plausible currencies**
(`__nimbus_currency__`), so one that ever escaped resolution would be
conspicuous in the UI rather than quietly wrong. `TestTheSentinelsAreNotUnits`
pins that.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor_flattened as sf

_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "sensor_flattened.py"
).read_text(encoding="utf-8")


class _Config:
    def __init__(self, currency):
        self.currency = currency


class _Hass:
    def __init__(self, currency):
        self.config = _Config(currency)


class _Entry:
    entry_id = "hub1"


def _sensor(spec, currency="AUD", with_hass=True):
    ent = sf._FlattenedAttributeSensor(_Entry(), "0.0.0", spec)
    if with_hass:
        ent.hass = _Hass(currency)
    return ent


def _spec_with_unit(unit):
    """A real spec from the live table, so this tests the shipped rows rather
    than a hand-built stand-in."""
    for spec in sf.FLATTENED_ATTRS:
        if spec.unit_of_measurement == unit:
            return spec
    raise AssertionError(f"no spec in FLATTENED_ATTRS uses {unit!r}")


class TestTheHardcodeIsGone(unittest.TestCase):
    def test_no_spec_row_anywhere_still_carries_a_literal_AUD_unit(self):
        """Asserted on the RUNTIME TABLES, not by searching the text.

        Two earlier versions of this grepped the source and both tripped over
        prose -- first a stale lead-in comment, then the property's own
        docstring, which necessarily discusses AUD to explain why the constant
        went away. A text search cannot tell a hardcode from an explanation of
        a hardcode, and an assertion that punishes the explanation pushes the
        reasoning out of the file. What actually matters is that no shipped row
        still declares AUD, and that is directly observable.
        """
        tables = {
            name: value
            for name, value in vars(sf).items()
            if name.startswith("FLATTENED_ATTRS") and isinstance(value, (list, tuple))
        }
        self.assertTrue(tables, "no FLATTENED_ATTRS* tables found to check")
        for name, table in tables.items():
            for spec in table:
                with self.subTest(table=name, row=spec.entity_id_suffix):
                    self.assertNotIn(spec.unit_of_measurement, ("AUD", "AUD/kWh", "$"))

    def test_the_module_defines_no_AUD_constant(self):
        self.assertFalse(
            [n for n in vars(sf) if "_AUD" in n],
            "the _AUD constants should be gone, replaced by the sentinels",
        )

    def test_the_table_still_has_both_currency_row_kinds(self):
        """Guards against a substitution that silently dropped rows."""
        units = [s.unit_of_measurement for s in sf.FLATTENED_ATTRS]
        self.assertGreater(units.count(sf._CURRENCY), 0)
        self.assertGreater(units.count(sf._CURRENCY_PER_KWH), 0)


class TestTheSentinelsAreNotUnits(unittest.TestCase):
    """If a sentinel ever escapes resolution it must be obvious, not plausible.
    A sentinel of "AUD" would have been indistinguishable from success."""

    def test_neither_sentinel_looks_like_a_currency(self):
        for sentinel in (sf._CURRENCY, sf._CURRENCY_PER_KWH):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, ("AUD", "USD", "EUR", "GBP", "$"))
                self.assertTrue(sentinel.startswith("__"))


class TestItResolvesAgainstHassConfig(unittest.TestCase):
    def test_a_plain_currency_row_returns_the_configured_currency(self):
        ent = _sensor(_spec_with_unit(sf._CURRENCY), currency="USD")
        self.assertEqual(ent.native_unit_of_measurement, "USD")

    def test_a_per_kwh_row_composes_it(self):
        ent = _sensor(_spec_with_unit(sf._CURRENCY_PER_KWH), currency="EUR")
        self.assertEqual(ent.native_unit_of_measurement, "EUR/kWh")

    def test_an_AUD_install_sees_EXACTLY_the_old_value(self):
        """The whole safety argument. Verified live: production reads
        currency 'AUD', so HA sees no unit change, no statistics repair, and
        none of its 38 affected series break."""
        self.assertEqual(
            _sensor(
                _spec_with_unit(sf._CURRENCY), currency="AUD"
            ).native_unit_of_measurement,
            "AUD",
        )
        self.assertEqual(
            _sensor(
                _spec_with_unit(sf._CURRENCY_PER_KWH), currency="AUD"
            ).native_unit_of_measurement,
            "AUD/kWh",
        )

    def test_a_non_currency_row_passes_through_untouched(self):
        kwh = next(
            s
            for s in sf.FLATTENED_ATTRS
            if s.unit_of_measurement not in (None, sf._CURRENCY, sf._CURRENCY_PER_KWH)
        )
        ent = _sensor(kwh, currency="USD")
        self.assertEqual(ent.native_unit_of_measurement, kwh.unit_of_measurement)


class TestTheAwkwardCases(unittest.TestCase):
    def test_a_falsy_currency_is_returned_as_is_not_defaulted(self):
        """Matches sensor.py's three existing call sites exactly, which do
        `return self._hass.config.currency` with no fallback. A default here
        would re-introduce the hardcode with a different string."""
        self.assertIsNone(
            _sensor(
                _spec_with_unit(sf._CURRENCY), currency=None
            ).native_unit_of_measurement
        )

    def test_a_falsy_currency_never_produces_the_string_None_per_kwh(self):
        """The specific ugly failure an f-string would cause: 'None/kWh'."""
        got = _sensor(
            _spec_with_unit(sf._CURRENCY_PER_KWH), currency=None
        ).native_unit_of_measurement
        self.assertIsNone(got)
        self.assertNotEqual(got, "None/kWh")

    def test_before_HA_registers_the_entity_the_sentinel_is_returned(self):
        """There is no `hass` to ask yet. Returning the sentinel is honest and
        conspicuous; returning a guessed currency would not be."""
        ent = _sensor(_spec_with_unit(sf._CURRENCY), with_hass=False)
        self.assertEqual(ent.native_unit_of_measurement, sf._CURRENCY)


class TestTheAttrDoesNotShadowTheProperty(unittest.TestCase):
    """The one way this change could silently do nothing: HA's Entity base
    prefers `_attr_native_unit_of_measurement` when it is set, so leaving the
    __init__ assignment in place for currency rows would republish the
    sentinel as the unit and the property would never be consulted."""

    def test_currency_rows_do_not_set_the_attr(self):
        ent = _sensor(_spec_with_unit(sf._CURRENCY))
        self.assertNotIn("_attr_native_unit_of_measurement", ent.__dict__)

    def test_non_currency_rows_still_do(self):
        kwh = next(
            s
            for s in sf.FLATTENED_ATTRS
            if s.unit_of_measurement not in (None, sf._CURRENCY, sf._CURRENCY_PER_KWH)
        )
        ent = _sensor(kwh)
        self.assertEqual(
            ent.__dict__.get("_attr_native_unit_of_measurement"),
            kwh.unit_of_measurement,
        )

    def test_the_guard_is_on_the_source(self):
        src = inspect.getsource(sf._FlattenedAttributeSensor.__init__)
        i = src.index("_attr_native_unit_of_measurement")
        self.assertIn("_CURRENCY", src[max(0, i - 300) : i])


if __name__ == "__main__":
    unittest.main(verbosity=2)
