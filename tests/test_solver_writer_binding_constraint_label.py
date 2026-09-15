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


def _label(plan, period_0_hours=1.0):
    # period_0_hours=1.0 (nimbus issue #662's own correction, `val /
    # period_0_hours`) keeps every existing assertion below unchanged --
    # dividing by 1.0 is a no-op. See TestPeriod0HoursCorrection below
    # for dedicated coverage of the real, non-1.0 case.
    return solver_writer.compute_binding_constraint_label(
        plan,
        _EXPORT_LIMIT_KW,
        _IMPORT_LIMIT_KW,
        _MAX_CHARGE_KW,
        _MAX_DISCHARGE_KW,
        period_0_hours,
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
            plan, 0.0, _IMPORT_LIMIT_KW, _MAX_CHARGE_KW, _MAX_DISCHARGE_KW, 1.0
        )
        self.assertEqual(label, "Grid export at zero (not economical right now)")


class TestPeriod0HoursCorrection(unittest.TestCase):
    """nimbus issue #662 (Mark Purcell): the 4 variable families here
    have their own LP objective coefficients scaled by period 0's own
    duration, so their raw reduced cost is "$ per kW of bound" -- the
    real $/kWh marginal value needs dividing by period_0_hours, the
    exact same correction #662 itself found missing on shadow_price/
    energy_shadow_price_now (network.py's own power_balance_t0 row
    dual). Verified directly against #662's own real numbers: a raw
    value of 0.0186 at a real 5-minute (5/60 h) period recovers 0.223,
    matching the real contemporaneous import price of 0.2134.
    """

    def test_five_minute_period_divides_by_five_sixtieths(self):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.0186},
            grid_export_kw=(_EXPORT_LIMIT_KW,),
        )
        _label_out, shadow_price = _label(plan, period_0_hours=5 / 60)
        self.assertAlmostEqual(shadow_price, 0.223, places=3)

    def test_one_hour_period_is_a_no_op(self):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.05},
            grid_export_kw=(_EXPORT_LIMIT_KW,),
        )
        _label_out, shadow_price = _label(plan, period_0_hours=1.0)
        self.assertEqual(shadow_price, 0.05)


class TestP2PPinnedExport(unittest.TestCase):
    """nimbus issue #921, found live rather than reasoned about.

    A real install running a P2P export block of 12 kW over 17:00-24:00
    published, at 19:05 and again at 19:18:

        Grid export at 12.00 kW (unexpected -- neither its 0 nor
        40.00 kW bound)

    Nothing was unexpected. `p2p_export.grid_export_bounds()` pins
    grid_export[t] to `lb == ub == 12.0` for every period under a real
    commitment, so the variable genuinely is at a bound -- LP optimality
    is satisfied exactly as the "shouldn't happen" branch's own comment
    reasons. What that comment did not say is that it assumed 0 and
    `limit_kw` are the ONLY bounds, and the P2P pin is a third one.

    The consequence was worse than a wrong string: the message fired for
    every period of every evening block -- the household's highest-value
    hours, and the ones someone is most likely to be inspecting this
    field to understand -- telling a correctly-configured install its
    solver was in a state that should not occur.
    """

    _PIN_KW = 12.0

    def _pinned_plan(self, value_kw=None):
        return _fake_plan(
            reduced_costs={"grid_export_0": 0.05},
            grid_export_kw=(self._PIN_KW if value_kw is None else value_kw,),
        )

    def test_the_live_misreport_is_reproduced_without_the_pin(self):
        """Mutation check, and the exact observed string. Without the new
        argument the old behaviour must still be reachable -- otherwise
        the test below proves nothing about what changed."""
        label, _ = _label(self._pinned_plan())
        self.assertEqual(
            label,
            "Grid export at 12.00 kW (unexpected -- neither its 0 nor 40.00 kW bound)",
        )

    def test_a_p2p_pin_is_named_rather_than_called_unexpected(self):
        label, _ = solver_writer.compute_binding_constraint_label(
            self._pinned_plan(),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN_KW,
        )
        self.assertEqual(
            label, "Grid export pinned at 12.00 kW by P2P export commitment"
        )
        self.assertNotIn("unexpected", label)

    def test_a_period_with_no_commitment_reads_nan_and_falls_through(self):
        """`fixed_export_kw` is an array over the whole horizon with NaN
        in every uncommitted period, so period 0 is routinely NaN on an
        install that has P2P configured at all. That must not swallow a
        genuinely unexplained value."""
        label, _ = solver_writer.compute_binding_constraint_label(
            self._pinned_plan(),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            float("nan"),
        )
        self.assertIn("unexpected", label)

    def test_a_value_that_is_not_the_pin_is_still_unexpected(self):
        """The guard this branch sits next to exists to surface bounds
        nothing in the function models. Narrowing it to the pin keeps
        that guard live for the next one."""
        label, _ = solver_writer.compute_binding_constraint_label(
            self._pinned_plan(value_kw=7.5),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN_KW,
        )
        self.assertIn("unexpected", label)

    def test_a_pin_at_the_export_limit_keeps_the_original_ceiling_label(self):
        """Order matters: the ceiling branch is checked first and its
        wording is a documented compatibility guarantee (#125/#133). A
        commitment that happens to equal the export limit must not
        quietly re-word it."""
        label, _ = solver_writer.compute_binding_constraint_label(
            self._pinned_plan(value_kw=_EXPORT_LIMIT_KW),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            _EXPORT_LIMIT_KW,
        )
        self.assertEqual(label, "Grid export limit")

    def test_a_zero_commitment_is_reported_as_the_commitment(self):
        """nimbus issue #951 (Mark Purcell, 48-hour IV&V #950).

        **This test previously asserted the opposite**, and its stated
        reasoning was: *"A 0 kW commitment and a 'not economical'
        decision look identical in the solved value. The existing, more
        specific economic label wins, unchanged."*

        That was wrong, not merely stale. The premise is true only of the
        **solved value** — and the function is not limited to the solved
        value. It already has `fixed_export_kw_now` in hand, and already
        uses it to disambiguate every other pin magnitude. Declining to
        use it for exactly one value left a residual of the bug class
        #921 was filed to eliminate.

        And 0.0 is a real, reachable commitment rather than a contrived
        one: `fetch_p2p_fixed_export_kw()` deliberately pins export to
        0.0 for `solver_post_window_self_consume_hours` after midnight on
        any block configured with `end_hour=24`, mirroring the real
        automation's own self-consume window. So a correctly-configured
        overnight P2P household was being told its solver saw no economic
        reason to export, during the exact hours export was
        deterministically forbidden.
        """
        label, _ = solver_writer.compute_binding_constraint_label(
            self._pinned_plan(value_kw=0.0),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            0.0,
        )
        self.assertEqual(
            label, "Grid export pinned at 0.00 kW by P2P export commitment"
        )

    def test_a_genuine_not_economical_zero_is_untouched_by_that_change(self):
        """The regression the #951 reorder had to not cause, and the
        reason it is safe.

        A period with no commitment never reaches the zero-check carrying
        0.0: `fetch_p2p_fixed_export_kw()` returns None when no block is
        configured at all, and defaults an unmatched period to
        `float("nan")`. Both are rejected by the pin branch's own guards,
        so a real "not worth exporting right now" still reads as one.

        Covering both shapes explicitly, because the safety of hoisting
        the pin check rests entirely on them.
        """
        for fixed_export in (None, float("nan")):
            with self.subTest(fixed_export_kw_now=fixed_export):
                label, _ = solver_writer.compute_binding_constraint_label(
                    self._pinned_plan(value_kw=0.0),
                    _EXPORT_LIMIT_KW,
                    _IMPORT_LIMIT_KW,
                    _MAX_CHARGE_KW,
                    _MAX_DISCHARGE_KW,
                    1.0,
                    fixed_export,
                )
                self.assertEqual(
                    label, "Grid export at zero (not economical right now)"
                )

    def test_the_pin_only_applies_to_grid_export(self):
        """`fixed_export_kw` bounds one variable. A battery value that
        coincidentally equals the pin must not borrow its explanation."""
        plan = _fake_plan(
            reduced_costs={"battery_discharge_0": 0.05},
            battery_discharge_kw=(self._PIN_KW,),
        )
        label, _ = solver_writer.compute_binding_constraint_label(
            plan,
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN_KW,
        )
        self.assertIn("unexpected", label)
        self.assertNotIn("P2P", label)


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    """#357's lesson: the standalone/cron writer carries its own copy of
    this function, and a fix landing in only one of them is how the two
    drifted last time. Checks the fix is present in both, by behaviour
    of the source rather than by a byte-comparison of files that do have
    real, deliberate differences elsewhere."""

    def test_the_standalone_writer_has_the_same_new_branch(self):
        import pathlib

        standalone = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("by P2P export commitment", standalone)
        self.assertIn("fixed_export_kw_now", standalone)


class TestThePinMatchSurvivesRealSolverResidual(unittest.TestCase):
    """nimbus issue #921, second pass -- the first fix did not work.

    v0.94.324 shipped this branch with `abs(solved - pin) <= 1e-6`, and
    on the very install it was written for it never fired. The published
    attribute still read

        Grid export at 12.00 kW (unexpected -- neither its 0 nor
        40.00 kW bound)

    with a 12.0 kW commitment genuinely in force on that period,
    confirmed by reading the plan's own pinned run (17:00-24:00 local,
    period 0 inside it) and by matching the module's own log line
    numbers against the merged source to rule out a stale deploy.

    The mistake was the shape of the test, not the arithmetic. A small
    synthetic LP returns a pinned variable at exactly its bound -- I
    checked, and got `delta = 0.000e+00`, which is what made `1e-6` look
    safe. That does not generalise to one variable out of a ~12,000-
    column two-phase MIP.

    The fix is a real tolerance rather than a float-equality epsilon.
    Still two-sided: `grid_export_bounds()` returns `(pin, pin)`, so a
    value well *below* the commitment is as impossible as one well
    above it, and #694's price-spike override -- which relaxes the
    bounds to `(pin, export_limit_kw)` at t=0 -- is precisely a case
    where export legitimately rises above the commitment and must not
    be called pinned. Both remain "unexpected", which is the point of
    that branch.
    """

    _PIN = 12.0

    def _label_with_pin(self, solved_kw, pin=None):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.05},
            grid_export_kw=(solved_kw,),
        )
        return solver_writer.compute_binding_constraint_label(
            plan,
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN if pin is None else pin,
        )[0]

    def test_a_solver_residual_above_the_pin_still_reads_as_pinned(self):
        """The regression. 1e-6 rejected this; the install lived here."""
        self.assertEqual(
            self._label_with_pin(12.0004),
            "Grid export pinned at 12.00 kW by P2P export commitment",
        )

    def test_a_solver_residual_below_the_pin_still_reads_as_pinned(self):
        self.assertEqual(
            self._label_with_pin(11.9996),
            "Grid export pinned at 12.00 kW by P2P export commitment",
        )

    def test_an_exact_hit_still_reads_as_pinned(self):
        self.assertEqual(
            self._label_with_pin(12.0),
            "Grid export pinned at 12.00 kW by P2P export commitment",
        )

    def test_the_label_reports_the_commitment_not_the_solved_value(self):
        """12.006 would render as "12.01" if the solved value were used.
        The household configured 12, and that is the number that means
        something to them."""
        self.assertIn("12.00 kW", self._label_with_pin(12.006))

    def test_a_value_well_above_the_pin_is_not_called_pinned(self):
        """#694's price-spike override lifts the ceiling to the export
        limit at t=0, so export legitimately rises above the commitment.
        That is not 'pinned' and must not be reported as such."""
        label = self._label_with_pin(25.0)
        self.assertNotIn("pinned", label)
        self.assertIn("unexpected", label)

    def test_the_tolerance_is_far_below_anything_actionable(self):
        """A guard on the constant itself: widen it to where it could mask
        a real dispatch difference and this fails."""
        self.assertLessEqual(solver_writer._PIN_MATCH_TOLERANCE_KW, 0.05)
        self.assertGreater(solver_writer._PIN_MATCH_TOLERANCE_KW, 0.0)


class TestTheFallbackMessageCarriesTheCommitment(unittest.TestCase):
    """The v0.94.324 miss was undiagnosable from the published attribute:
    "no commitment this period" and "a commitment the comparison
    rejected" produced byte-identical text. The fallback now names the
    commitment when there is one."""

    def _label(self, pin):
        plan = _fake_plan(
            reduced_costs={"grid_export_0": 0.05},
            grid_export_kw=(25.0,),  # well above any pin -> falls through
        )
        return solver_writer.compute_binding_constraint_label(
            plan,
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            pin,
        )[0]

    def test_it_names_the_commitment_when_one_is_in_force(self):
        self.assertEqual(
            self._label(12.0),
            "Grid export at 25.00 kW (unexpected -- neither its 0 nor "
            "40.00 kW bound, P2P commitment 12.00 kW)",
        )

    def test_it_stays_exactly_as_before_when_there_is_no_commitment(self):
        """Unchanged wording for the no-P2P case -- this string has been
        published for a long time and installs read it."""
        self.assertEqual(
            self._label(None),
            "Grid export at 25.00 kW (unexpected -- neither its 0 nor 40.00 kW bound)",
        )

    def test_a_nan_period_is_treated_as_no_commitment(self):
        self.assertEqual(
            self._label(float("nan")),
            "Grid export at 25.00 kW (unexpected -- neither its 0 nor 40.00 kW bound)",
        )
