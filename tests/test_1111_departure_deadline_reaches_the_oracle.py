"""nimbus #1111: the scorer's oracle ignored a participant's departure
requirement, so it could leave the car empty at 07:00 and arbitrage
overnight instead.

The second instance of the drift #1109 is about, and found by the same
method rather than by stumbling on it again: diffing the `BatteryConfig`
keyword sets of `build_extra_batteries()` (live solve) against
`_resolve_battery_participant_history()` (the scorer's oracle), two
functions the code itself calls siblings.

```
on live but NOT history:   available
                           unavailable_until_period_index
                           must_have_soc_by_period_index      <- this
                           must_have_soc_kwh                  <- this
```

Two of those four are deliberate and documented: `available` and
`unavailable_until_period_index` are whole-horizon *"is this car away
right now"* concepts, correctly superseded for an already-elapsed day by
the per-period mask #467 added. **`must_have_soc_*` was neither set nor
mentioned** -- no code, no comment explaining an omission.

`elements.py` is explicit that it is a **hard** constraint:

> HARD, not soft: a real EV genuinely needs a real SoC by a real time,
> not a priced preference.

So on a day where the household needed the car at 80% by 07:00 to drive
to work, the oracle was free to leave it empty and bank an overnight
arbitrage the household could never have accepted -- making `j_star`
better than achievable, `regret_dollars` overstated and EPR understated.
Same direction as #1109: the scorer told households they left more on
the table than was ever available to them.

## Why it is widened rather than simply passed through

A constraint narrows the oracle, and #956 is the record of what that
costs when the achieved trajectory falls outside the narrowed set:
regret goes negative and the comparison is invalid rather than imprecise.

Here the failure is **ordinary, not exotic**. A household that did not
plug the car in that night, or plugged it in late, has a day that misses
its own departure target. Binding the oracle to a target the real day
missed would put the achieved trajectory outside the oracle's feasible
set on exactly those days -- which are the common ones.

So the requirement is `min(configured, the SoC the day actually reached
by that period)`, as demanding as the day itself and no more. On a day
that met its target, that is the configured value and nothing changes --
which `TestADayThatMetItsTarget` pins.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
import solver_writer

# Deliberately UTC-stamped, because `_local()` is what the live path uses
# and this must match it. Brisbane is UTC+10, so local hour 7 is index 21
# of a window starting midnight UTC -- if that ever reads as index 7, the
# timezone conversion has been dropped and #1076's class is back.
START = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
GRID_TIMES = [START + timedelta(hours=i) for i in range(24)]
PERIOD_HOURS = [1.0] * 24

CAPACITY_KWH = 60.0
EFFICIENCY = 0.97
INITIAL_SOC_KWH = 20.0
DEPARTURE_HOUR = 7
REQUIRED_PCT = 80.0
CONFIGURED_KWH = CAPACITY_KWH * REQUIRED_PCT / 100.0  # 48.0


def _deadline(charge, **over):
    kwargs = {
        "grid_times": GRID_TIMES,
        "period_hours": PERIOD_HOURS,
        "departure_hour": DEPARTURE_HOUR,
        "must_have_soc_pct": REQUIRED_PCT,
        "capacity_kwh": CAPACITY_KWH,
        "initial_soc_kwh": INITIAL_SOC_KWH,
        "actual_charge_kw": np.asarray(charge, dtype=float),
        "actual_discharge_kw": np.zeros(len(charge)),
        "efficiency": EFFICIENCY,
    }
    kwargs.update(over)
    return solver_writer._participant_departure_deadline(**kwargs)


class TestTheDeadlineIndexIsLocal(unittest.TestCase):
    def test_the_departure_hour_is_resolved_in_local_time(self):
        """07:00 Brisbane against a UTC-stamped window is index 21, not 7.
        Reading `.hour` straight off the timestamp would give 7 and bind
        the constraint fourteen hours early -- the #1076 class, in the
        one place where getting it wrong silently changes what the oracle
        is allowed to do."""
        idx, _kwh = _deadline([0.0] * 24)
        self.assertEqual(idx, 21)


class TestADayThatMetItsTarget(unittest.TestCase):
    """The case that must not move: a day which genuinely reached its
    departure SoC has to bind the oracle to the CONFIGURED value. If the
    widening ever softens this, the oracle regains the freedom this issue
    is about."""

    def test_the_configured_requirement_is_used_unchanged(self):
        charged_hard = [10.0] * 7 + [0.0] * 17
        idx, kwh = _deadline(charged_hard)
        self.assertEqual(idx, 21)
        self.assertAlmostEqual(kwh, CONFIGURED_KWH, places=6)

    def test_it_never_demands_more_than_configured(self):
        """Even a day that massively overshot is only held to what the
        household actually asked for."""
        _idx, kwh = _deadline([20.0] * 24)
        self.assertAlmostEqual(kwh, CONFIGURED_KWH, places=6)


class TestADayThatMissedItsTarget(unittest.TestCase):
    """The #956 case, and the ordinary one: the car was not plugged in,
    or was plugged in late."""

    def test_an_unplugged_night_widens_down_to_the_starting_soc(self):
        _idx, kwh = _deadline([0.0] * 24)
        self.assertAlmostEqual(kwh, INITIAL_SOC_KWH, places=6)
        self.assertLess(kwh, CONFIGURED_KWH)

    def test_a_partial_charge_widens_to_what_it_reached(self):
        """2 kW for the seven periods before the deadline: 20 + 7 x 2 x
        0.97 = 33.58 kWh."""
        _idx, kwh = _deadline([2.0] * 7 + [0.0] * 17)
        self.assertAlmostEqual(kwh, 33.58, places=2)

    def test_charge_after_the_deadline_does_not_count(self):
        """Energy that arrived after departure cannot have satisfied a
        departure requirement. Integrating the whole day instead of the
        prefix would let a car charged at noon look ready at 07:00."""
        _idx, kwh = _deadline([0.0] * 22 + [20.0, 20.0])
        self.assertAlmostEqual(kwh, INITIAL_SOC_KWH, places=6)

    def test_discharge_before_the_deadline_is_debited(self):
        idx, kwh = solver_writer._participant_departure_deadline(
            grid_times=GRID_TIMES,
            period_hours=PERIOD_HOURS,
            departure_hour=DEPARTURE_HOUR,
            must_have_soc_pct=REQUIRED_PCT,
            capacity_kwh=CAPACITY_KWH,
            initial_soc_kwh=INITIAL_SOC_KWH,
            actual_charge_kw=np.zeros(24),
            actual_discharge_kw=np.array([5.0] + [0.0] * 23),
            efficiency=EFFICIENCY,
        )
        self.assertEqual(idx, 21)
        self.assertLess(kwh, INITIAL_SOC_KWH)

    def test_it_never_goes_negative(self):
        """A reconstruction that discharges more than the pack held would
        otherwise produce a negative requirement, which `BatteryConfig`
        would reject and which means nothing anyway."""
        _idx, kwh = solver_writer._participant_departure_deadline(
            grid_times=GRID_TIMES,
            period_hours=PERIOD_HOURS,
            departure_hour=DEPARTURE_HOUR,
            must_have_soc_pct=REQUIRED_PCT,
            capacity_kwh=CAPACITY_KWH,
            initial_soc_kwh=INITIAL_SOC_KWH,
            actual_charge_kw=np.zeros(24),
            actual_discharge_kw=np.full(24, 50.0),
            efficiency=EFFICIENCY,
        )
        self.assertGreaterEqual(kwh, 0.0)


class TestTheNotConfiguredCases(unittest.TestCase):
    """All real, expected no-ops rather than errors -- matching the live
    path's own posture for each."""

    def test_neither_half_set(self):
        self.assertEqual(
            _deadline([0.0] * 24, departure_hour=None, must_have_soc_pct=None),
            (None, None),
        )

    def test_only_the_hour_set_is_treated_as_neither(self):
        """The live path logs once and treats a half-configured pair as
        unset. The scorer must agree, or the two disagree about what the
        household configured."""
        self.assertEqual(_deadline([0.0] * 24, must_have_soc_pct=None), (None, None))

    def test_only_the_percent_set_is_treated_as_neither(self):
        self.assertEqual(_deadline([0.0] * 24, departure_hour=None), (None, None))

    def test_a_departure_hour_outside_this_window(self):
        """A short window that never reaches the departure hour. The live
        path calls this 'a real, expected no-op (short manual solve...)
        not an error' and this matches."""
        idx, kwh = _deadline(
            [0.0] * 5, grid_times=GRID_TIMES[:5], period_hours=PERIOD_HOURS[:5]
        )
        self.assertIsNone(idx)
        self.assertIsNone(kwh)


class TestTheScorerActuallyWiresItUp(unittest.TestCase):
    """The finding was an absence. Source-checked for the same reason
    #1109's equivalent is: driving it needs a real subentry with real
    availability history, and an absence cannot be demonstrated
    behaviourally without that setup.
    """

    def test_the_history_path_sets_both_fields(self):
        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        start = text.index("def _resolve_battery_participant_history(")
        end = text.index("def _participant_departure_deadline(")
        body = text[start:end]
        for field in ("must_have_soc_by_period_index=", "must_have_soc_kwh="):
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    body,
                    "the scorer's participant builder has stopped setting "
                    f"{field} -- its live-solve sibling sets it, and without "
                    "it the oracle may leave a participant empty at its own "
                    "departure deadline and bank the arbitrage (#1111)",
                )

    def test_both_siblings_now_agree_except_where_documented(self):
        """The systematic check that found this. Every remaining
        difference must have a written reason: two whole-horizon
        availability concepts the history path replaces with a per-period
        mask, that mask itself, and #1161's unobserved-period record --
        which cannot have a live-path equivalent, since a live solve reads
        current state rather than reconstructing it.
        """
        import ast

        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        def fields(name):
            fn = next(
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            found = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    called = (
                        f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    )
                    if called == "BatteryConfig":
                        found |= {k.arg for k in node.keywords if k.arg}
            return found

        live = fields("build_extra_batteries")
        history = fields("_resolve_battery_participant_history")
        self.assertEqual(
            live - history,
            {"available", "unavailable_until_period_index"},
            "a BatteryConfig field is set for the live solve but not for "
            "the scorer's oracle. Unless it is a whole-horizon 'is this "
            "car away right now' concept -- which an already-elapsed day "
            "expresses per-period instead -- the oracle is being given "
            "freedom the household did not have. That is #1109 and #1111, "
            "twice, both in the same direction.",
        )
        self.assertEqual(
            history - live,
            {"unavailable_period_indices", "stale_history_period_indices"},
            "a BatteryConfig field is set by the scorer's participant "
            "builder and not by its live-solve sibling. Both current "
            "entries are reconstruction-only concepts and that is the bar: "
            "`unavailable_period_indices` is the per-period form of an "
            "availability question the live path asks about right now, and "
            "`stale_history_period_indices` (#1161) records which periods "
            "had no recorded power behind them -- a live solve reads "
            "current state and has no history that could be stale. "
            "Anything else appearing here means the oracle is being handed "
            "a constraint the real solve never had.",
        )


if __name__ == "__main__":
    unittest.main()
