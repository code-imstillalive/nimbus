"""nimbus #1314: the household-mode tuning seam runs before the thermal
branch reads its band, so a mode's band preset reaches the LP. A check that
reads source text, kept apart until the decision on source-reading tests
(#1837); the band presets themselves are in tests/v2/entities/test_thermal_band_modes.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import _solver_path  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A realistic hot-water subentry's own thermal fields, as
# `_resolve_controllable_load_tuning()` hands them on. Real key names,
# taken from const.py, not invented for the test.
_BASE_THERMAL_DATA = {
    "controllable_load_name": "Hot Water",
    "controllable_load_kind": "thermal",
    "thermal_max_power_kw": 3.0,
    "thermal_target_temperature_c": 60.0,
    "thermal_comfort_floor_c": 50.0,
    "thermal_comfort_floor_cost": 0.40,
    "thermal_earliest_hour": 9.0,
    "thermal_deadline_hour": 17.0,
}

_N = 24


class TestTheBandReachesTheSolverThroughTheOneExistingSeam(unittest.TestCase):
    def test_the_tuning_resolve_precedes_the_thermal_target_read(self):
        import inspect

        from custom_components.nimbus_load import solver_writer as sw

        src = inspect.getsource(sw.build_controllable_loads)
        resolve_at = src.index("data = _resolve_controllable_load_tuning(subentry.data")
        # The READ, not the name: this function opens with a local import
        # block that mentions every CONF_ constant it uses, so a bare
        # name match finds the import and proves nothing. Found the hard
        # way -- the first version of this assertion failed on exactly
        # that.
        for read in (
            "data.get(CONF_THERMAL_TARGET_TEMPERATURE_C)",
            "data.get(CONF_THERMAL_COMFORT_FLOOR_C)",
            "data.get(CONF_THERMAL_COMFORT_FLOOR_COST)",
        ):
            with self.subTest(read=read):
                self.assertLess(
                    resolve_at,
                    src.index(read),
                    "the thermal branch reads this field before the "
                    "household-mode seam has run -- the band preset would "
                    "never reach the LP",
                )
