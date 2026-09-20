"""nimbus #1176: when the load-nowcast skill cannot be computed, say
which gate closed.

Every `load_nowcast_skill_*` field reads `null` on the reference
household's scored report, and nothing on the sensor says why. The metric
that answers *"is the ML load forecaster earning its keep?"* is silent on
the install with the most history, and the only signal was a `DEBUG` line
nobody has enabled.

Worse, one of those lines covered two different causes at once:

    "Only %d of %d periods carried a real recorded sample, or a scenario
     solve failed"

So even with DEBUG on, coverage-below-threshold and a failed scenario
solve were indistinguishable -- and they call for completely different
responses (record more trail samples, versus a solver problem).

## Why it matters

#937 measured naive persistence beating Nimbus's own forecast on 11 of
14 days, mean -$0.71/day. These fields are the live per-day version of
that question. Answering it with `null` every day leaves a household
unable to tell "the forecaster has no skill" from "the check could not
run" from "something is unconfigured" -- and only the last is actionable.

## The split needs no solver-package change

`measured` and `len(grid_times)` are the two figures that debug line
already printed, and `nowcast_skill.DEFAULT_MIN_COVERAGE` is exported.
Coverage below the bar is therefore decidable at the call site; anything
else returning None is the scenario solve, which
`compute_load_nowcast_skill()` swallows deliberately (#366/#373's
"degrade, never wedge").

## The coverage is published even when it is the failure

It is known in that branch and was being discarded. Telling a household
the check did not run, without telling them it missed the bar by 0.31, is
the absence-as-the-only-signal shape this repo keeps recording.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import solver_writer
from solver import nowcast_skill

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 9, 19, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)
GRID = [DAY_START + timedelta(hours=i) for i in range(24)]


def _call(trail, prev, result, measured=24):
    """Drive the real attribute builder with the three inputs that decide
    which gate closes."""

    def _fetch(entity_id, start, end):
        # The trail fetch is the first read; the persistence fetch asks
        # for the PRECEDING day, which is how they are told apart here.
        return prev if start < DAY_START else trail

    with (
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(
            solver_writer,
            "resample_history_mean",
            side_effect=lambda *a, **k: [1.0] * 24,
        ),
        patch.object(
            solver_writer.nowcast_skill,
            "compute_load_nowcast_skill",
            return_value=result,
        ),
    ):
        return solver_writer._load_nowcast_skill_attributes(
            load_sensor="sensor.load",
            load_scale=1.0,
            grid_times=GRID,
            period_hours=1.0,
            periods=object(),
            grid=object(),
            battery=object(),
            solar_real_kw=np.zeros(24),
            load_real_kw=np.ones(24),
            day_start=DAY_START,
            day_end=DAY_END,
        )


def _hist(n):
    return [(DAY_START + timedelta(minutes=10 * i), 1.0) for i in range(n)]


class TestTheKeyIsAlwaysPresent(unittest.TestCase):
    """#589's lesson: a key that appears and vanishes between windows is
    its own bug."""

    def test_the_reason_is_in_the_declared_key_set(self):
        self.assertIn("load_nowcast_skill_reason", solver_writer._NOWCAST_SKILL_KEYS)

    def test_every_blank_path_returns_the_full_key_set(self):
        for label, args in (
            ("no trail", ([], _hist(50), None)),
            ("no persistence", (_hist(50), [], None)),
            ("unusable", (_hist(50), _hist(50), None)),
        ):
            with self.subTest(path=label):
                got = _call(*args)
                self.assertEqual(
                    set(got), set(solver_writer._NOWCAST_SKILL_KEYS), label
                )


class TestEachGateNamesItself(unittest.TestCase):
    def test_a_missing_forecast_trail(self):
        got = _call([], _hist(50), None)
        self.assertEqual(got["load_nowcast_skill_reason"], "no_forecast_trail")

    def test_a_missing_persistence_baseline(self):
        got = _call(_hist(50), [], None)
        self.assertEqual(got["load_nowcast_skill_reason"], "no_persistence_baseline")

    def test_the_two_gates_do_not_share_a_label(self):
        self.assertNotEqual(
            _call([], _hist(50), None)["load_nowcast_skill_reason"],
            _call(_hist(50), [], None)["load_nowcast_skill_reason"],
        )


class TestTheConflatedGateIsSplit(unittest.TestCase):
    """The load-bearing pair. These two shared one debug line and one
    all-null payload, and they call for completely different responses."""

    def test_the_two_causes_do_not_share_a_label(self):
        """They shared one debug line and one all-null payload. Whatever
        each is called, they must differ -- a household fixing recording
        density when the solver failed is fixing the wrong thing."""
        self.assertNotEqual("coverage_below_threshold", "scenario_solve_failed")
        got = _call(_hist(240), _hist(240), None)
        self.assertIn(
            got["load_nowcast_skill_reason"],
            ("coverage_below_threshold", "scenario_solve_failed"),
        )

    def test_full_coverage_with_a_none_result_is_the_solve(self):
        """Every period measured, and the skill still came back None --
        that is the scenario solve, not the recording density."""
        got = _call(_hist(240), _hist(240), None)
        self.assertEqual(
            got["load_nowcast_skill_reason"],
            "scenario_solve_failed",
            "a full-coverage window that still returned None is being "
            "reported as a recording-density problem, which sends a "
            "household to fix the wrong thing",
        )

    def test_the_threshold_comes_from_the_solver_package(self):
        """Retyping 0.5 here is how the call site and the function that
        enforces it would drift apart."""
        self.assertEqual(nowcast_skill.DEFAULT_MIN_COVERAGE, 0.5)
        src = solver_writer.__file__.replace(".pyc", ".py")
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("nowcast_skill.DEFAULT_MIN_COVERAGE", text)


class TestTheCoverageIsPublishedWhenItIsTheFailure(unittest.TestCase):
    def test_a_solve_failure_still_reports_what_was_measured(self):
        got = _call(_hist(240), _hist(240), None)
        self.assertIsNotNone(
            got["load_nowcast_skill_coverage"],
            "the coverage is known in this branch and was being discarded "
            "-- telling a household the check did not run without telling "
            "them how far short it fell is the absence-as-signal failure",
        )
        self.assertIsNotNone(got["load_nowcast_skill_periods_measured"])

    def test_the_earlier_gates_still_report_nothing_they_do_not_know(self):
        """No trail means no coverage was ever computed. Publishing 0.0
        there would assert a measurement that was never made."""
        got = _call([], _hist(50), None)
        self.assertIsNone(got["load_nowcast_skill_coverage"])
        self.assertIsNone(got["load_nowcast_skill_periods_measured"])


class TestASuccessfulComputationSaysNothing(unittest.TestCase):
    def test_the_reason_is_none_when_the_skill_computed(self):
        class _R:
            j_star = -1.0
            j_forecast = -2.0
            j_persistence = -1.5
            value_add_dollars = 0.5
            coverage = 0.98
            n_periods_measured = 23

        got = _call(_hist(240), _hist(240), _R())
        self.assertIsNone(got["load_nowcast_skill_reason"])
        self.assertAlmostEqual(got["load_nowcast_skill_value_add_dollars"], 0.5)
        self.assertAlmostEqual(got["load_nowcast_skill_coverage"], 0.98)


if __name__ == "__main__":
    unittest.main()
