"""nimbus #1109: the scorer's oracle could charge every member of a
shared-charger group at its own `max_charge_kw` simultaneously.

[#768](https://github.com/code-imstillalive/nimbus/issues/768) lists this
as one of three prerequisites gating its own options:

> The shared-charger/availability gating (#563) has to carry over to the
> oracle too -- an oracle free to charge both EVs on one 25 kW charger
> simultaneously, or charge a car that was genuinely away, would
> overstate achievable value in a way that isn't a fair comparison.

**Half of that carried over.** `_resolve_battery_participant_history()`
-- by its own docstring "the history-based sibling of
`build_extra_batteries()`" -- set `unavailable_period_indices` (#467) but
not `shared_charger_group`/`shared_charger_max_kw`. The live solve path
set both. `network.py` consumes both when present, so the capability was
real and only the wiring was missing on the history side.

Consequence, in the unflattering direction: the oracle's feasible set was
larger than the hardware, so `j_star` was better than anything achievable,
`regret_dollars` was overstated and EPR understated. The scorer reported
the household as having left more on the table than was ever available.

## Why passing the cap through is not the whole fix

Adding a constraint **narrows** the oracle, and #956 is the record of what
that costs when the achieved trajectory falls outside the narrowed set:
the achieved side prices out cheaper than optimal, regret goes negative,
and the comparison becomes invalid rather than merely imprecise.

The household settled that in #956 -- *"go with B"*, widen the oracle to
contain the achieved trajectory rather than clamp the achieved
integration to fit the oracle. This applies that existing decision to the
same class of problem rather than making a new one, in the same shape
`quality_report.py`'s `_widen_export_pin_to_achieved()` already uses for
the P2P export pin.

So the cap becomes `max(configured, largest simultaneous draw the group
actually made)` -- **exactly as wide as the day itself and no wider**.

`TestAWellBehavedDayIsUntouched` is the load-bearing one: on any day whose
real draw stayed under the cap, the result must be identical to passing
the configured value straight through. If that ever stops holding, the
widening has started relaxing real limits on ordinary days, which would
put back the very overstatement this issue is about.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_writer
from solver import elements

ZERO = np.zeros(6)


def _cfg(name, group, cap, *, max_charge_kw=15.0):
    return elements.BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=0.0,
        max_soc_kwh=60.0,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=15.0,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
        shared_charger_group=group,
        shared_charger_max_kw=cap,
    )


def _widen(rows):
    return solver_writer._widen_shared_charger_cap_to_achieved(rows)


def _caps(rows):
    return [c.shared_charger_max_kw for c, _, _, _ in rows]


class TestAWellBehavedDayIsUntouched(unittest.TestCase):
    """The case that must never move. A day whose real draw stayed under
    the cap has to come out identical to passing the configured value
    straight through -- otherwise the widening is relaxing real limits on
    ordinary days and the overstatement this issue is about is back."""

    def test_combined_draw_under_the_cap_leaves_the_cap_alone(self):
        a = np.array([5.0, 5.0, 5.0, 0.0, 0.0, 0.0])
        b = np.array([5.0, 5.0, 0.0, 0.0, 0.0, 0.0])
        out = _widen(
            [(_cfg("ev1", "garage", 25.0), a, ZERO, 0.0),
             (_cfg("ev2", "garage", 25.0), b, ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(_caps(out), [25.0, 25.0])

    def test_draw_exactly_at_the_cap_leaves_it_alone(self):
        """Boundary: peak == cap is not an excursion. #1104 is the recent
        reminder that a boundary comparison deserves its own case."""
        a = np.array([12.5, 0.0, 0.0, 0.0, 0.0, 0.0])
        b = np.array([12.5, 0.0, 0.0, 0.0, 0.0, 0.0])
        out = _widen(
            [(_cfg("ev1", "garage", 25.0), a, ZERO, 0.0),
             (_cfg("ev2", "garage", 25.0), b, ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(_caps(out), [25.0, 25.0])

    def test_the_arrays_and_final_soc_ride_through_unchanged(self):
        """Only the cap may move. The charge/discharge arrays and final
        SoC are the scorer's own measured reconstruction and must not be
        touched by a constraint fix."""
        a = np.array([5.0, 5.0, 0.0, 0.0, 0.0, 0.0])
        d = np.array([0.0, 0.0, 3.0, 0.0, 0.0, 0.0])
        out = _widen([(_cfg("ev1", "garage", 25.0), a, d, 41.5)])
        _cfg_out, charge, discharge, final = out[0]
        np.testing.assert_array_equal(charge, a)
        np.testing.assert_array_equal(discharge, d)
        self.assertEqual(final, 41.5)


class TestADayThatExceededTheCap(unittest.TestCase):
    """The #956 case: narrowing the oracle to a cap the real day already
    broke would push the achieved trajectory outside the oracle's feasible
    set and send regret negative."""

    def test_the_cap_widens_to_the_achieved_peak(self):
        a = np.array([15.0, 15.0, 0.0, 0.0, 0.0, 0.0])
        b = np.array([14.0, 12.0, 0.0, 0.0, 0.0, 0.0])
        out = _widen(
            [(_cfg("ev1", "garage", 25.0), a, ZERO, 0.0),
             (_cfg("ev2", "garage", 25.0), b, ZERO, 0.0)]
        )  # fmt: skip
        # period 0 draws 15 + 14 = 29, the largest simultaneous draw.
        self.assertEqual(_caps(out), [29.0, 29.0])

    def test_every_member_of_the_group_gets_the_same_widened_cap(self):
        """network.py applies the MINIMUM non-None cap across a group, so
        widening only one member would leave the group still constrained
        by the other's un-widened value and achieve nothing."""
        a = np.array([15.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        b = np.array([14.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        out = _widen(
            [(_cfg("ev1", "garage", 25.0), a, ZERO, 0.0),
             (_cfg("ev2", "garage", 25.0), b, ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(len(set(_caps(out))), 1)

    def test_it_widens_to_the_peak_not_the_total(self):
        """The constraint is instantaneous power, not energy. Summing
        across periods instead of taking the per-period max would relax
        the cap enormously on any real day."""
        a = np.array([10.0, 10.0, 10.0, 10.0, 0.0, 0.0])
        b = np.array([10.0, 10.0, 10.0, 10.0, 0.0, 0.0])
        out = _widen(
            [(_cfg("ev1", "garage", 5.0), a, ZERO, 0.0),
             (_cfg("ev2", "garage", 5.0), b, ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(_caps(out), [20.0, 20.0])


class TestWhatIsDeliberatelyLeftAlone(unittest.TestCase):
    def test_a_group_of_one_is_a_per_battery_limit_not_a_shared_one(self):
        """`shared_charger_max_kw` on a lone participant is just a second
        `max_charge_kw`. Widening it would quietly relax a real
        per-battery limit that nothing else re-imposes."""
        out = _widen(
            [(_cfg("ev1", "garage", 10.0), np.array([15.0, 0, 0, 0, 0, 0]), ZERO, 0.0)]
        )
        self.assertEqual(_caps(out), [10.0])

    def test_a_participant_with_no_group_is_untouched(self):
        out = _widen([(_cfg("home", None, None), np.full(6, 99.0), ZERO, 0.0)])
        self.assertEqual(_caps(out), [None])

    def test_a_group_with_no_configured_cap_is_untouched(self):
        """A group name with no cap configured is not a constraint, and
        inventing one from the achieved peak would impose a limit the
        household never set."""
        out = _widen(
            [(_cfg("ev1", "garage", None), np.full(6, 9.0), ZERO, 0.0),
             (_cfg("ev2", "garage", None), np.full(6, 9.0), ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(_caps(out), [None, None])

    def test_two_separate_groups_do_not_pool(self):
        """Two chargers are two constraints. Summing across groups would
        widen each by the other's draw."""
        out = _widen(
            [(_cfg("ev1", "garage", 10.0), np.full(6, 12.0), ZERO, 0.0),
             (_cfg("ev2", "garage", 10.0), np.full(6, 1.0), ZERO, 0.0),
             (_cfg("ev3", "street", 10.0), np.full(6, 30.0), ZERO, 0.0),
             (_cfg("ev4", "street", 10.0), np.full(6, 1.0), ZERO, 0.0)]
        )  # fmt: skip
        self.assertEqual(_caps(out), [13.0, 13.0, 31.0, 31.0])

    def test_an_empty_fleet_does_not_raise(self):
        self.assertEqual(_widen([]), [])


class TestTheScorerActuallySetsTheFields(unittest.TestCase):
    """The whole finding was that the history path did not set them while
    its live-solve sibling did. Source-checked, because driving it needs a
    real subentry with real availability history -- and because the defect
    was an absence, which behaviour cannot demonstrate without that setup.
    """

    def test_the_history_path_sets_both_shared_charger_fields(self):
        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        start = text.index("def _resolve_battery_participant_history(")
        end = text.index("def _widen_shared_charger_cap_to_achieved(")
        body = text[start:end]
        for field in ("shared_charger_group=", "shared_charger_max_kw="):
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    body,
                    "the scorer's participant builder has stopped setting "
                    f"{field} -- its live-solve sibling "
                    "build_extra_batteries() sets it, and without it the "
                    "oracle can charge every member of a shared-charger "
                    "group at once (#1109)",
                )

    def test_the_widening_runs_on_the_way_out(self):
        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertIn(
            "return _widen_shared_charger_cap_to_achieved(results)",
            text,
            "the cap is being passed through un-widened, which narrows the "
            "oracle below what the real day did and reintroduces #956",
        )


if __name__ == "__main__":
    unittest.main()
