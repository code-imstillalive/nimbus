"""Real regression test, live incident (Mark Purcell, an independent
installer, 2026-08-24): the native solver_runtime.py loop crashed 20
consecutive times over 24 minutes with

    ValueError: Invalid SoC bounds: 0 < min_soc(0.0) <= max_soc(40.0)
    <= capacity(40.0) required

elements.BatteryConfig.__post_init__ requires a STRICTLY positive
min_soc_kwh -- a deliberate LP-level degeneracy/safety floor. But the
dashboard's own "Battery Min SoC" number entity allows dragging all the
way down to 0% (number.py's own native_min_value=0, deliberately --
_cfg_num's own docstring, and test_solver_writer_cfg_defaults.py, both
already name "min_soc_percent set to 0% by an installer as a temporary
bypass" as a real, legitimate use case). Nothing stopped that genuine
0% from reaching the constructor and crashing every single solve.

resolve_min_soc_kwh() closes the gap: same "absorb real reality rather
than propagate a ValueError every minute" discipline already proven for
resolve_max_discharge_kw() (nimbus #125) and the initial_soc_kwh clamp
in main() (2026-08-23) -- a tiny relative floor (0.05% of capacity)
keeps a 0% intent honoured as "effectively no reserve" while staying
strictly positive and therefore solvable.
"""

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from solver.elements import BatteryConfig


class TestMarksExactRealRepro(unittest.TestCase):
    """The precise, real numbers from Mark's own live crash report."""

    def test_zero_percent_no_longer_raises(self):
        # Baseline, pre-fix behaviour if this floor didn't exist: a bare
        # 0.0 kWh min_soc handed straight to BatteryConfig genuinely
        # does raise -- documents the real crash this function exists
        # to prevent from ever reaching that constructor.
        with self.assertRaises(ValueError):
            BatteryConfig(
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=0.0,
                max_soc_kwh=40.0,
                max_charge_kw=21.0,
                max_discharge_kw=24.0,
                charge_efficiency=0.975,
                discharge_efficiency=0.975,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.15,
            )

    def test_resolve_min_soc_kwh_floors_zero_percent_to_something_positive(self):
        result = solver_writer.resolve_min_soc_kwh(
            min_pct=0.0, capacity_kwh=40.0, max_soc_kwh=40.0
        )
        self.assertGreater(result, 0.0)
        # 0.05% of 40 kWh = 0.02 kWh -- effectively no reserve, not a
        # coincidentally-large substitute value.
        self.assertAlmostEqual(result, 0.02, places=6)

    def test_floored_value_constructs_a_valid_batteryconfig(self):
        # The actual end-to-end proof: Mark's real numbers (capacity 40,
        # min_pct 0, max_soc 40), run through the real fix, must produce
        # a BatteryConfig that does NOT raise.
        min_soc_kwh = solver_writer.resolve_min_soc_kwh(
            min_pct=0.0, capacity_kwh=40.0, max_soc_kwh=40.0
        )
        try:
            bc = BatteryConfig(
                capacity_kwh=40.0,
                initial_soc_kwh=min_soc_kwh,  # worst case: SoC right at the new floor
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=40.0,
                max_charge_kw=21.0,
                max_discharge_kw=24.0,
                charge_efficiency=0.975,
                discharge_efficiency=0.975,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.15,
            )
        except ValueError as e:
            self.fail(f"BatteryConfig raised on the floored min_soc value: {e}")
        self.assertEqual(bc.min_soc_kwh, min_soc_kwh)


class TestNormalConfiguredValuesAreUnaffected(unittest.TestCase):
    """A genuinely nonzero, sane Min SoC (this repo's own reference
    household's real 5% default, or any other real value) must pass
    through completely unchanged -- this fix must never quietly nudge a
    real, intentional floor."""

    def test_five_percent_passes_through_unchanged(self):
        result = solver_writer.resolve_min_soc_kwh(
            min_pct=5.0, capacity_kwh=40.0, max_soc_kwh=40.0
        )
        self.assertEqual(result, 2.0)  # 5% of 40, exactly, no clamping

    def test_a_tiny_but_genuinely_positive_percent_is_not_touched_either(self):
        # 0.01% of 40 kWh = 0.004 kWh -- smaller than the 0.02 kWh floor
        # a true 0% would get clamped to, but it's already > 0, so the
        # fix must leave it exactly as configured, not "round it up" to
        # match the floor.
        result = solver_writer.resolve_min_soc_kwh(
            min_pct=0.01, capacity_kwh=40.0, max_soc_kwh=40.0
        )
        self.assertAlmostEqual(result, 0.004, places=6)


class TestPathologicalMaxSocAlsoNearZero(unittest.TestCase):
    """Defensive edge case, not observed in the wild: if Max SoC is ALSO
    at or near 0%, the floor must not push min_soc_kwh above max_soc_kwh
    and reintroduce a different invariant violation."""

    def test_floor_is_capped_by_max_soc_kwh(self):
        result = solver_writer.resolve_min_soc_kwh(
            min_pct=0.0, capacity_kwh=40.0, max_soc_kwh=0.01
        )
        self.assertLessEqual(result, 0.01)
        self.assertGreater(result, 0.0)


class TestWarnsOnlyWhenActuallyClamped(unittest.TestCase):
    """A real, sane Min SoC must never print a spurious warning -- the
    same discipline already proven for resolve_max_discharge_kw()'s own
    fallback warnings."""

    def test_no_warning_for_a_normal_value(self):
        with patch("builtins.print") as mock_print:
            solver_writer.resolve_min_soc_kwh(
                min_pct=5.0, capacity_kwh=40.0, max_soc_kwh=40.0
            )
            mock_print.assert_not_called()

    def test_warning_fires_when_actually_clamped(self):
        with patch("builtins.print") as mock_print:
            solver_writer.resolve_min_soc_kwh(
                min_pct=0.0, capacity_kwh=40.0, max_soc_kwh=40.0
            )
            mock_print.assert_called_once()
            (msg,), _kwargs = mock_print.call_args
            self.assertIn("WARN", msg)
            self.assertIn("Min SoC", msg)


if __name__ == "__main__":
    unittest.main()
