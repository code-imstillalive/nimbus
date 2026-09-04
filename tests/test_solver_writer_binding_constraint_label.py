"""Real regression test for nimbus repo issue #125/#133 (Mark Purcell, a
real independent installer's own confirmation trace, 2026-08-24, direct
follow-up to his day-ahead plan analysis #129): compute_binding_
constraint_label() used to label a nonzero reduced cost on e.g.
battery_discharge_0 as "Battery max discharge power" UNCONDITIONALLY --
but a real, nonzero LP reduced cost fires whenever a variable is pinned
at EITHER of its own bounds (a core LP optimality property), not only
its upper/capacity bound.

Mark's own real repro: his plan showed the battery CHARGING at period 0
(not discharging at all), while this label still reported "Battery max
discharge power" -- his own confirmed configured solver_max_discharge_kw
was 24.0, but the observed discharge across his whole 96h horizon never
exceeded ~4.67 kW, which he (reasonably, given the misleading label)
read as evidence of a second, undiscovered override path. Direct source
reads (network.py's own battery_discharge_{t} construction: `ub=
battery.max_discharge_kw`, no efficiency/SoC derating of the bound
itself; a repo-wide grep for "logger_" finding no second hardcoded
entity slug anywhere) ruled out both of his own suggested mechanisms.
The real explanation: battery_discharge_0's reduced cost was nonzero
because it was pinned at its LOWER bound (0 -- a genuine "not
economical to discharge right now" decision, consistent with the plan
choosing to charge instead), not the 24kW ceiling -- and the OLD label
had no way to say that.

These tests prove the fix directly: the genuine "pinned at the real
ceiling" case keeps the exact original label strings (a real
compatibility guarantee), while the "pinned at zero" case gets its own
new, distinct, honest label -- and Mark's own exact scenario (battery
charging, discharge reduced cost nonzero, discharge value at 0) is
reproduced faithfully as its own dedicated test.
"""

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
import solver_writer


def _fake_plan(
    reduced_costs: dict,
    grid_export_kw=(0.0,),
    grid_import_kw=(0.0,),
    battery_charge_kw=(0.0,),
    battery_discharge_kw=(0.0,),
):
    """A minimal duck-typed stand-in for network.Plan -- only the 5
    attributes compute_binding_constraint_label() actually reads. Same
    pragmatic, stub-based approach as this file's own sibling
    test_solver_writer_max_discharge_resolve.py (which uses a plain
    dict for `cfg` rather than a real config-entry object) -- Python's
    own type hints are not enforced at runtime, and a real network.Plan
    would require constructing an unrelated PeriodGrid/sheddable_loads/
    adequacy_loads just to satisfy its own __init__, none of which this
    function ever touches."""
    return SimpleNamespace(
        reduced_costs=reduced_costs,
        grid_export_kw=list(grid_export_kw),
        grid_import_kw=list(grid_import_kw),
        battery_charge_kw=list(battery_charge_kw),
        battery_discharge_kw=list(battery_discharge_kw),
    )


# Real, representative household config -- matches the shape of the
# actual production numbers referenced throughout this issue thread
# (24.0 kW max discharge, 21.0 kW max charge).
_MAX_CHARGE_KW = 21.0
_MAX_DISCHARGE_KW = 24.0
_IMPORT_LIMIT_KW = 40.0
_EXPORT_LIMIT_KW = 40.0


def _label(plan):
    return solver_writer.compute_binding_constraint_label(
        plan, _EXPORT_LIMIT_KW, _IMPORT_LIMIT_KW, _MAX_CHARGE_KW, _MAX_DISCHARGE_KW
    )


class TestNothingBinding(unittest.TestCase):
    def test_all_zero_reduced_costs_reports_nothing_binding(self):
        plan = _fake_plan(reduced_costs={})
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Nothing currently binding")
        self.assertIsNone(shadow_price)

    def test_reduced_costs_below_epsilon_are_treated_as_zero(self):
        plan = _fake_plan(reduced_costs={"battery_discharge_0": 1e-9})
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Nothing currently binding")
        self.assertIsNone(shadow_price)


class TestGenuinelyAtTheRealCeilingKeepsTheOriginalLabel(unittest.TestCase):
    """Byte-identical to the pre-2026-08-24 label strings -- a real
    compatibility guarantee for this specific case, which was always
    correctly labelled."""

    def test_battery_discharge_at_its_real_max_kw(self):
        plan = _fake_plan(
            reduced_costs={"battery_discharge_0": 0.05},
            battery_discharge_kw=(_MAX_DISCHARGE_KW,),
        )
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Battery max discharge power")
        self.assertEqual(shadow_price, 0.05)

    def test_battery_charge_at_its_real_max_kw(self):
        plan = _fake_plan(
            reduced_costs={"battery_charge_0": 0.03},
            battery_charge_kw=(_MAX_CHARGE_KW,),
        )
        label, _ = _label(plan)
        self.assertEqual(label, "Battery max charge power")

    def test_grid_export_at_its_real_limit_kw(self):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.02},
            grid_export_kw=(_EXPORT_LIMIT_KW,),
        )
        label, _ = _label(plan)
        self.assertEqual(label, "Grid export limit")

    def test_grid_import_at_its_real_limit_kw(self):
        plan = _fake_plan(
            reduced_costs={"grid_import_0": 0.02},
            grid_import_kw=(_IMPORT_LIMIT_KW,),
        )
        label, _ = _label(plan)
        self.assertEqual(label, "Grid import limit")


class TestMarksExactRealRepro(unittest.TestCase):
    """The precise real scenario from nimbus #125/#133: battery
    genuinely charging at period 0, discharge sitting at 0, yet
    battery_discharge_0 carries the dominant nonzero reduced cost.
    Must NOT report "Battery max discharge power" (actively wrong --
    there is no discharge happening, let alone one pinned at 24kW)."""

    def test_discharge_pinned_at_zero_while_battery_is_charging(self):
        plan = _fake_plan(
            reduced_costs={"battery_discharge_0": 0.031, "battery_charge_0": 0.0},
            battery_charge_kw=(3.67,),  # real Mark-reported charge value
            battery_discharge_kw=(0.0,),
        )
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Battery discharge at zero (not economical right now)")
        self.assertNotIn("max discharge power", label)
        self.assertEqual(shadow_price, 0.031)

    def test_charge_pinned_at_zero_gets_the_same_treatment(self):
        # The mirror case -- proves the fix isn't special-cased to
        # discharge only, it's the same real mechanism for all 4 families.
        plan = _fake_plan(
            reduced_costs={"battery_charge_0": 0.012},
            battery_charge_kw=(0.0,),
        )
        label, _ = _label(plan)
        self.assertEqual(label, "Battery charge at zero (not economical right now)")

    def test_grid_export_pinned_at_zero_gets_the_same_treatment(self):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.001},
            grid_export_kw=(0.0,),
        )
        label, _ = _label(plan)
        self.assertEqual(label, "Grid export at zero (not economical right now)")


class TestTieBreakingUnchanged(unittest.TestCase):
    """Multiple families with a nonzero reduced cost simultaneously --
    must still pick whichever has the LARGEST |reduced_cost| magnitude,
    exactly the original tie-breaking rule, now just applied on top of
    the corrected per-family labelling."""

    def test_larger_magnitude_wins_regardless_of_which_bound_each_is_at(self):
        plan = _fake_plan(
            reduced_costs={
                "battery_discharge_0": 0.01,  # smaller magnitude, at zero
                "grid_export_0": 0.5,  # larger magnitude, at its real ceiling
            },
            battery_discharge_kw=(0.0,),
            grid_export_kw=(_EXPORT_LIMIT_KW,),
        )
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Grid export limit")
        self.assertEqual(shadow_price, 0.5)

    def test_negative_reduced_costs_compare_by_magnitude_not_sign(self):
        plan = _fake_plan(
            reduced_costs={
                "battery_charge_0": -0.2,
                "battery_discharge_0": 0.05,
            },
            battery_charge_kw=(_MAX_CHARGE_KW,),
            battery_discharge_kw=(0.0,),
        )
        label, shadow_price = _label(plan)
        self.assertEqual(label, "Battery max charge power")
        self.assertEqual(shadow_price, -0.2)


class TestUnexpectedNeitherBoundDegradesHonestly(unittest.TestCase):
    """Shouldn't happen for a genuinely nonzero reduced cost (LP
    optimality), but must never crash if it somehow does -- represented
    honestly rather than silently mislabelled as either real case."""

    def test_solved_value_strictly_between_zero_and_the_limit(self):
        plan = _fake_plan(
            reduced_costs={"battery_discharge_0": 0.02},
            battery_discharge_kw=(12.0,),  # neither 0 nor 24.0
        )
        label, shadow_price = _label(plan)
        self.assertIn("Battery discharge at 12.00 kW", label)
        self.assertIn("unexpected", label)
        self.assertEqual(shadow_price, 0.02)

    def test_zero_configured_limit_never_divides_or_crashes(self):
        # A real, if unusual, config edge case (e.g. export disabled
        # entirely, limit=0.0) -- limit_kw > 1e-9 must correctly steer
        # this to the "at zero" branch, not attempt a ceiling comparison
        # against a genuinely zero bound.
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.01},
            grid_export_kw=(0.0,),
        )
        label, _ = solver_writer.compute_binding_constraint_label(
            plan, 0.0, _IMPORT_LIMIT_KW, _MAX_CHARGE_KW, _MAX_DISCHARGE_KW
        )
        self.assertEqual(label, "Grid export at zero (not economical right now)")
