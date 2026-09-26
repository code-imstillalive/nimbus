"""nimbus issue #1253, fourth site: the wizard's own money labels.

## The site the issue did not list

#1253 names three places currency was answered differently — parent sensors
(runtime), flattened children (hardcoded `AUD`), and the cron writer (`"$"`).
A fourth existed: the **config-flow selector labels**, the unit shown beside a
number input.

| file | was |
|---|---|
| `flows/battery_participant_subentry.py` | `$/kWh` |
| `flows/controllable_load_subentry.py` | `$/kWh` and `$` |

## Why it is worth fixing despite being the cheapest

No entity, no registry row, no long-term statistic — so none of the
38-entity migration risk that makes the flattened children awkward.

And it is the site a **new household sees first**, before any sensor exists.
A household in Berlin configuring a deferrable load is asked what waiting is
worth "in $".

## Why per-call factories rather than module constants

The currency is not known at import time, and both `_schema()` functions are
called from a flow method that has `self.hass`. Adding an optional `currency`
parameter is a smaller change than threading `hass` through, and keeps the
default behaviour (no currency → no unit) available to any other caller.

## The falsy case

The unit is **omitted entirely** when HA has no currency configured. An absent
unit is honest; substituting a default would re-introduce the hardcode with a
different string — the same posture `sensor.py`'s own three currency call
sites take (`return self._hass.config.currency`, no fallback).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.flows import (
    battery_participant_subentry as bps,
)
from custom_components.nimbus_load.flows import (
    controllable_load_subentry as cls_,
)

_FLOW_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load" / "flows"
)


def _unit(sel) -> str | None:
    """The unit a NumberSelector will render, through whichever attribute the
    stub or the real selector exposes."""
    config = getattr(sel, "config", None) or getattr(sel, "_config", None) or {}
    if isinstance(config, dict):
        return config.get("unit_of_measurement")
    return getattr(config, "unit_of_measurement", None)


class TestNoHardcodedDollarSurvives(unittest.TestCase):
    def test_no_flow_module_sets_a_dollar_unit(self):
        offenders = [
            p.name
            for p in _FLOW_DIR.glob("*.py")
            if 'unit_of_measurement="$' in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_the_old_module_constants_are_gone(self):
        for mod in (bps, cls_):
            with self.subTest(module=mod.__name__):
                self.assertFalse(
                    [n for n in vars(mod) if n.startswith("_DOLLAR")],
                    "the hardcoded selector constants should be replaced by "
                    "per-call factories (#1253)",
                )


class TestTheLabelFollowsTheHousehold(unittest.TestCase):
    def test_per_kwh_uses_the_configured_currency(self):
        for mod in (bps, cls_):
            with self.subTest(module=mod.__name__):
                self.assertEqual(
                    _unit(mod._currency_per_kwh_selector("EUR")), "EUR/kWh"
                )

    def test_a_plain_money_field_uses_it_too(self):
        self.assertEqual(_unit(cls_._currency_selector("GBP")), "GBP")

    def test_an_AUD_household_sees_what_it_saw_before_in_meaning(self):
        """The old label was "$" for everyone. An AUD household now sees
        "AUD" rather than "$" -- a real change, and the correct one: "$" is
        ambiguous across at least a dozen currencies."""
        self.assertEqual(_unit(cls_._currency_selector("AUD")), "AUD")
        self.assertEqual(_unit(cls_._currency_per_kwh_selector("AUD")), "AUD/kWh")


class TestTheFalsyCase(unittest.TestCase):
    """No currency configured must mean NO unit, never a guessed one."""

    def test_no_currency_means_no_unit_at_all(self):
        for sel in (
            cls_._currency_selector(None),
            cls_._currency_per_kwh_selector(None),
            bps._currency_per_kwh_selector(None),
        ):
            with self.subTest(sel=sel):
                self.assertIsNone(_unit(sel))

    def test_it_never_renders_the_string_None_per_kwh(self):
        """The specific ugly failure an unguarded f-string would cause."""
        self.assertNotEqual(_unit(cls_._currency_per_kwh_selector(None)), "None/kWh")

    def test_an_empty_string_is_treated_as_absent(self):
        self.assertIsNone(_unit(cls_._currency_per_kwh_selector("")))


class TestTheWaitingCostKeepsItsOwnShape(unittest.TestCase):
    """#769: this field is a plain money value, not a per-kWh rate -- the
    household reasons in "what is waiting worth to me". It also carries a
    0.05 step. Both survive the refactor."""

    def test_it_is_not_a_per_kwh_field(self):
        self.assertEqual(_unit(cls_._waiting_cost_selector("AUD")), "AUD")

    def test_it_keeps_its_step(self):
        sel = cls_._waiting_cost_selector("AUD")
        config = getattr(sel, "config", None) or getattr(sel, "_config", None) or {}
        step = (
            config.get("step")
            if isinstance(config, dict)
            else getattr(config, "step", None)
        )
        self.assertEqual(step, 0.05)


class TestTheSchemasAcceptCurrency(unittest.TestCase):
    def test_both_schema_builders_take_it_optionally(self):
        """Optional so any other caller keeps working -- and so the default
        stays "no unit" rather than a guess."""
        import inspect

        for mod in (bps, cls_):
            params = inspect.signature(mod._schema).parameters
            with self.subTest(module=mod.__name__):
                self.assertIn("currency", params)
                self.assertIsNone(params["currency"].default)

    def test_both_call_sites_pass_the_real_configured_currency(self):
        import inspect

        for mod in (bps, cls_):
            src = inspect.getsource(mod)
            with self.subTest(module=mod.__name__):
                self.assertIn("self.hass.config.currency", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
