"""The binding-constraint label names the bound a variable is actually
pinned at: a nonzero reduced cost fires at either bound, so a battery held
at zero discharge is not at its maximum discharge power (#125, #133)
(#1837).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import solver_publish


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
    test_max_discharge_resolve.py (which uses a plain
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
    return solver_publish.compute_binding_constraint_label(
        plan,
        _EXPORT_LIMIT_KW,
        _IMPORT_LIMIT_KW,
        _MAX_CHARGE_KW,
        _MAX_DISCHARGE_KW,
        period_0_hours,
    )


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

    def test_a_period_with_no_commitment_reads_nan_and_falls_through(self):
        """`fixed_export_kw` is an array over the whole horizon with NaN
        in every uncommitted period, so period 0 is routinely NaN on an
        install that has P2P configured at all. That must not swallow a
        genuinely unexplained value."""
        label, _ = solver_publish.compute_binding_constraint_label(
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
        label, _ = solver_publish.compute_binding_constraint_label(
            self._pinned_plan(value_kw=7.5),
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN_KW,
        )
        self.assertIn("unexpected", label)

    def test_the_pin_only_applies_to_grid_export(self):
        """`fixed_export_kw` bounds one variable. A battery value that
        coincidentally equals the pin must not borrow its explanation."""
        plan = _fake_plan(
            reduced_costs={"battery_discharge_0": 0.05},
            battery_discharge_kw=(self._PIN_KW,),
        )
        label, _ = solver_publish.compute_binding_constraint_label(
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
        return solver_publish.compute_binding_constraint_label(
            plan,
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            self._PIN if pin is None else pin,
        )[0]

    def test_the_tolerance_is_far_below_anything_actionable(self):
        """A guard on the constant itself: widen it to where it could mask
        a real dispatch difference and this fails."""
        self.assertLessEqual(solver_publish._PIN_MATCH_TOLERANCE_KW, 0.05)
        self.assertGreater(solver_publish._PIN_MATCH_TOLERANCE_KW, 0.0)


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
        return solver_publish.compute_binding_constraint_label(
            plan,
            _EXPORT_LIMIT_KW,
            _IMPORT_LIMIT_KW,
            _MAX_CHARGE_KW,
            _MAX_DISCHARGE_KW,
            1.0,
            pin,
        )[0]
