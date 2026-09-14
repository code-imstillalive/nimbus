"""nimbus issue #610 (Mark Purcell: "the published attributes do not say
which [a learned rate from a default]") applied to #481's new ambient
coefficient.

#481 (PR #864) learns `thermal_loss_coeff_per_h`, persists it to the
`load_run_state` Store, and applies it inside
`project_temperature_forecast()` in place of the flat idle decay -- but
it was never added to the sensor's published attributes. So on a live
install there was no way to tell whether the ambient path was actually
in force, or whether it had quietly fallen back to flat decay, which is
precisely the gap #610 already closed for the other two rates.

That mattered immediately: #769's "is this resolved by #800?" observation
window depends on being able to read the learned rates off a real solve,
and `thermal_loss_coeff_per_h` was the one field that couldn't be read.

Tests the real `extra_state_attributes` property rather than grepping
the source, so it keeps holding if the dict is rebuilt or moved.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import sensor as nimbus_sensor
from custom_components.nimbus_load.load_run_state import LoadRunState


def _build_sensor(state: LoadRunState):
    """Construct the real sensor with the minimum stub surface its
    __init__ touches, then hand it a coordinator returning `state`."""
    cls = nimbus_sensor.NimbusControllableLoadTemperatureForecastSensor
    obj = cls.__new__(cls)
    obj._subentry = SimpleNamespace(subentry_id="sub1", title="Hot Water", data={})
    obj.coordinator = SimpleNamespace(get=lambda _sid: state)
    return obj


def _state(**overrides) -> LoadRunState:
    base = {
        "thermal_rates_source": "learned",
        "thermal_heating_rate_c_per_kwh": 7.2,
        "thermal_idle_decay_c_per_hour": 0.41,
        "thermal_loss_coeff_per_h": 0.0123,
        "plan_temperature_forecast": [],
    }
    base.update(overrides)
    return LoadRunState(**base)


class TestThermalRatesArePublished(unittest.TestCase):
    def test_all_four_thermal_fields_are_published(self):
        """#610's contract: a household must be able to see both what the
        rates ARE and whether they were learned, without reading the log
        or the Store."""
        attrs = _build_sensor(_state()).extra_state_attributes
        for key in (
            "thermal_rates_source",
            "heating_rate_c_per_kwh",
            "idle_decay_c_per_hour",
            "thermal_loss_coeff_per_h",
        ):
            with self.subTest(key=key):
                self.assertIn(key, attrs)

    def test_published_values_are_the_real_state_values(self):
        attrs = _build_sensor(_state()).extra_state_attributes
        self.assertEqual(attrs["thermal_rates_source"], "learned")
        self.assertAlmostEqual(attrs["heating_rate_c_per_kwh"], 7.2)
        self.assertAlmostEqual(attrs["idle_decay_c_per_hour"], 0.41)
        self.assertAlmostEqual(attrs["thermal_loss_coeff_per_h"], 0.0123)

    def test_an_absent_loss_coefficient_publishes_none_not_zero(self):
        """None means "no ambient-scaled coefficient in force -- the flat
        idle decay is being used instead". Coercing that to 0.0 would
        read as "this tank loses no heat at all", a different and wrong
        claim, and would make the ambient path look active when it isn't.
        """
        attrs = _build_sensor(_state(thermal_loss_coeff_per_h=None))
        attrs = attrs.extra_state_attributes
        self.assertIn("thermal_loss_coeff_per_h", attrs)
        self.assertIsNone(attrs["thermal_loss_coeff_per_h"])

    def test_the_fallback_case_is_still_distinguishable(self):
        """The whole point of #610: "fallback" must be visible as such,
        with the generic constants shown rather than hidden."""
        attrs = _build_sensor(
            _state(
                thermal_rates_source="fallback",
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                thermal_loss_coeff_per_h=None,
            )
        ).extra_state_attributes
        self.assertEqual(attrs["thermal_rates_source"], "fallback")
        self.assertAlmostEqual(attrs["heating_rate_c_per_kwh"], 8.0)
        self.assertIsNone(attrs["thermal_loss_coeff_per_h"])


if __name__ == "__main__":
    unittest.main()
