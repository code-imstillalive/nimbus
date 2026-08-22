"""_cfg_num / _cfg_int and the initial-SoC clamp -- two related fixes,
same 2026-08-23 real, live root cause.

Bug 1 (numeric-default footgun): every place in solver_writer.py that
built a numeric default with the pattern ``float(cfg.get(key) or NUM)``
silently overrode a user-provided 0.0 with the hardcoded NUM, because
0.0 is falsy in Python. Setting the dashboard number entity
``number.nimbus_solver_battery_min_soc_percent`` to 0% as a temporary
workaround was byte-identical to leaving it unset -- the solver kept
using 5.0 and kept crashing.

Bug 2 (initial-SoC vs min-SoC): elements.BatteryConfig raises
ValueError when initial_soc_kwh falls outside [min_soc_kwh,
max_soc_kwh]. That invariant correctly protects a user-typed static
config, but the writer's own initial_soc comes from a LIVE sensor --
which genuinely can (and did, 27+ times in one window) read below
min_soc_percent for legitimate transient reasons (fault, cold pack,
fresh install starting empty). A live-sensor reading should not crash
the entire solve every minute.

These tests prove: (1) 0.0 is preserved through _cfg_num, distinct
from missing/None; (2) _cfg_int does the same for hour-of-day
boundaries; (3) BatteryConfig still enforces its invariant on the
low side; (4) the clamped initial (equal to min or max) constructs a
valid BatteryConfig -- the code path the writer now takes for a
below-floor live reading.
"""
import unittest

import _solver_path  # noqa: F401
import solver_writer
from solver.elements import BatteryConfig


class TestCfgNum(unittest.TestCase):
    def test_returns_default_when_missing(self):
        self.assertEqual(solver_writer._cfg_num({}, "k", 5.0), 5.0)

    def test_returns_default_when_none(self):
        self.assertEqual(solver_writer._cfg_num({"k": None}, "k", 5.0), 5.0)

    def test_preserves_intentional_zero(self):
        # The whole point of this helper -- 0.0 must survive, not be
        # replaced by the fallback default.
        self.assertEqual(solver_writer._cfg_num({"k": 0.0}, "k", 5.0), 0.0)
        self.assertEqual(solver_writer._cfg_num({"k": 0}, "k", 5.0), 0.0)

    def test_preserves_positive_value(self):
        self.assertEqual(solver_writer._cfg_num({"k": 3.5}, "k", 5.0), 3.5)

    def test_coerces_string_number(self):
        # A bridge-sensor attribute occasionally arrives as a numeric
        # string; the old ``float(cfg.get(...) or NUM)`` handled that
        # implicitly, so this helper must too.
        self.assertEqual(solver_writer._cfg_num({"k": "2.5"}, "k", 5.0), 2.5)


class TestCfgInt(unittest.TestCase):
    def test_returns_default_when_missing(self):
        self.assertEqual(solver_writer._cfg_int({}, "k", 7), 7)

    def test_returns_default_when_none(self):
        self.assertEqual(solver_writer._cfg_int({"k": None}, "k", 7), 7)

    def test_preserves_intentional_zero(self):
        # Real live case: hour-of-day block boundaries -- 0 means
        # midnight, a legitimate value that must not be replaced.
        self.assertEqual(solver_writer._cfg_int({"k": 0}, "k", 7), 0)


class TestBatteryConfigSoCClamp(unittest.TestCase):
    """Prove the clamped-initial-SoC path constructs a valid
    BatteryConfig without raising. The clamp itself lives inside
    solver_writer.main() and can't be unit-tested standalone (it needs
    a live HA to fetch from), but its output -- initial_soc_kwh equal
    to min_soc_kwh, or equal to max_soc_kwh -- must be a legal
    BatteryConfig input on both boundaries.
    """

    def _base_kwargs(self):
        return dict(
            capacity_kwh=40.0,
            min_soc_kwh=2.0,   # 5% of 40
            max_soc_kwh=40.0,  # 100% of 40
            max_charge_kw=21.0,
            max_discharge_kw=24.0,
            charge_efficiency=0.975,
            discharge_efficiency=0.975,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.15,
        )

    def test_initial_below_min_still_raises_without_clamp(self):
        # Baseline: the invariant IS the crash the writer used to
        # propagate -- documents current behavior of elements.py.
        kwargs = self._base_kwargs()
        kwargs["initial_soc_kwh"] = 0.04  # 0.1% -- below the 2.0 kWh floor
        with self.assertRaises(ValueError):
            BatteryConfig(**kwargs)

    def test_clamp_to_min_constructs_successfully(self):
        # The value solver_writer now passes when live SoC reads below
        # the floor: initial_soc_kwh == min_soc_kwh.
        kwargs = self._base_kwargs()
        kwargs["initial_soc_kwh"] = kwargs["min_soc_kwh"]
        try:
            bc = BatteryConfig(**kwargs)
        except ValueError as e:
            self.fail(f"BatteryConfig raised on clamp-to-min value: {e}")
        self.assertEqual(bc.initial_soc_kwh, kwargs["min_soc_kwh"])

    def test_clamp_to_max_constructs_successfully(self):
        # Symmetric case: initial_soc_kwh == max_soc_kwh, the value
        # solver_writer passes when live SoC reads above the ceiling
        # (e.g. someone configured max=95% but the pack briefly touches
        # 96% during a hardware ramp).
        kwargs = self._base_kwargs()
        kwargs["initial_soc_kwh"] = kwargs["max_soc_kwh"]
        try:
            bc = BatteryConfig(**kwargs)
        except ValueError as e:
            self.fail(f"BatteryConfig raised on clamp-to-max value: {e}")
        self.assertEqual(bc.initial_soc_kwh, kwargs["max_soc_kwh"])


if __name__ == "__main__":
    unittest.main()
