"""nimbus issue #956: a negative regret must not publish as a score.

The oracle is a bound by construction, and this codebase says so in at
least three places -- `forecast_regret.py`'s own docstring, and two
separate test files that use *"a perfect-foresight oracle can never be
beaten"* as a premise for unrelated assertions. That claim is true, for
two trajectories drawn from **the same feasible set**.

The achieved trajectory is not. It is a cumulative integration of real
battery-power history, bound by nothing, while the oracle is an LP bound
by `min_soc_kwh`/`max_soc_kwh`. When the reconstruction drifts below the
LP's own floor, it can sell energy the oracle is structurally forbidden
to touch -- and then price out cheaper than optimal.

**Confirmed on the reference household's own 2026-09-15 report**, and
the real numbers are used as this file's primary fixture rather than
invented ones:

    Min SoC configured      2.0 %      (number.nimbus_solver_battery_min_soc_percent)
    capacity                122.2 kWh
    oracle SoC floor        exactly 2.0 % for two consecutive hours
    achieved SoC minimum    0.4429 %   <- 1.90 kWh below a floor the oracle cannot cross
    regret_dollars          -0.7307
    epr                     103.66 %

**The distinction this file exists to pin.** #571's `out_of_range` flag
tests the achieved trajectory against `[0, 100]` -- physical
possibility -- and read `False` on all 24 hours of that day, because
0.4429 is comfortably inside `[0, 100]`. The envelope that was actually
violated is the LP's `[min_pct, max_pct]`. Clamping to `[0, 100]`, the
remedy #956's own body first proposed, would not have changed that day
at all. Two different questions; only one of them explains the defect.

The guard deliberately only MEASURES and REPORTS. Clamping the
integration used for costing would change `j_ach` -- the headline
achieved-cost figure -- on every install, which #956 records as a
household decision rather than a patch.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer

# The reference household's own configuration for the confirmed day.
_MIN_PCT = 2.0
_MAX_PCT = 100.0
_CAPACITY_KWH = 122.2

# The real achieved SoC trajectory from that report's `j_ach_hourly`,
# hours 00:00 through 23:00 local.
_REAL_ACHIEVED_SOC_PCT = [
    16.0733, 15.199, 14.2864, 13.3699, 12.4797, 5.6226,
    4.3405, 4.6783, 6.3345, 9.9114, 14.8908, 19.4014,
    35.5793, 59.646, 77.5374, 79.8896, 79.8366, 71.8059,
    60.1527, 48.7484, 37.182, 25.1736, 12.9144, 0.4429,
]  # fmt: skip

_REAL_REGRET_DOLLARS = -0.7307


def _hourly(soc_pcts):
    return {
        f"2026-09-15T{h:02d}:00:00+10:00": {"soc_pct": v}
        for h, v in enumerate(soc_pcts)
    }


def _stats(
    soc_pcts, regret, min_pct=_MIN_PCT, max_pct=_MAX_PCT, capacity=_CAPACITY_KWH
):
    return solver_writer._achieved_feasibility_stats(
        _hourly(soc_pcts),
        regret,
        min_pct=min_pct,
        max_pct=max_pct,
        capacity_kwh=capacity,
    )


class TestTheRealReferenceHouseholdDay(unittest.TestCase):
    """The day that produced the issue, priced exactly as it was."""

    def setUp(self):
        self.stats = _stats(_REAL_ACHIEVED_SOC_PCT, _REAL_REGRET_DOLLARS)

    def test_the_negative_regret_is_called_unreliable(self):
        self.assertFalse(
            self.stats["regret_reliable"],
            "regret_dollars -0.7307 means the achieved dispatch priced out "
            "cheaper than perfect foresight -- not a result, proof the "
            "comparison was invalid",
        )

    def test_the_achieved_trajectory_is_outside_the_lp_envelope(self):
        self.assertFalse(self.stats["achieved_within_lp_soc_bounds"])
        self.assertAlmostEqual(self.stats["achieved_soc_min_pct"], 0.4429, places=4)

    def test_the_energy_below_the_floor_is_reported_in_kwh(self):
        """1.557 pt below a 2.0% floor on a 122.2 kWh battery. The kWh
        figure is the one that makes the cost effect legible -- this is
        energy the oracle was structurally forbidden from selling."""
        self.assertAlmostEqual(self.stats["achieved_below_floor_kwh"], 1.9028, places=3)
        self.assertEqual(self.stats["achieved_above_ceiling_kwh"], 0.0)

    def test_the_reason_names_the_envelope_breach(self):
        self.assertEqual(
            self.stats["epr_reason"], "oracle_beaten_achieved_outside_lp_soc_bounds"
        )

    def test_this_day_passes_a_zero_to_one_hundred_check(self):
        """**The point of the whole issue.** Every value in the real
        trajectory is inside [0, 100], which is why #571's own
        `out_of_range` flag read False on all 24 hours while the day was
        genuinely infeasible for the oracle it was compared against.

        If this ever starts failing, the fixture has stopped being the
        real day and the file's central claim goes with it.
        """
        self.assertTrue(all(0.0 <= v <= 100.0 for v in _REAL_ACHIEVED_SOC_PCT))
        inside_physical_only = _stats(
            _REAL_ACHIEVED_SOC_PCT, _REAL_REGRET_DOLLARS, min_pct=0.0, max_pct=100.0
        )
        self.assertTrue(
            inside_physical_only["achieved_within_lp_soc_bounds"],
            "against a [0, 100] envelope this day looks perfectly fine -- "
            "which is exactly why clamping to [0, 100] would not have fixed it",
        )


class TestAHealthyDayIsLeftAlone(unittest.TestCase):
    def test_positive_regret_inside_the_envelope_reports_nothing(self):
        stats = _stats([20.0, 50.0, 80.0, 30.0], 1.2345)
        self.assertTrue(stats["regret_reliable"])
        self.assertTrue(stats["achieved_within_lp_soc_bounds"])
        self.assertIsNone(stats["epr_reason"])
        self.assertEqual(stats["achieved_below_floor_kwh"], 0.0)

    def test_exactly_zero_regret_is_reliable(self):
        """`regret == 0` is the achieved dispatch matching the oracle
        exactly -- the best possible real result, not a defect. The test
        is `>= 0`, and an off-by-one to `> 0` would flag a perfect day as
        broken."""
        self.assertTrue(_stats([20.0, 50.0], 0.0)["regret_reliable"])

    def test_an_envelope_breach_alone_does_not_flag_the_regret(self):
        """Being outside the LP envelope is a real reconstruction
        problem, but on its own it is #949/#571's business. This flag is
        specifically about the bound violation, so a day that drifts
        below the floor and STILL reports a positive regret is left
        reliable here -- flagging it would overstate what was measured.
        """
        stats = _stats([0.5, 50.0], 0.5)
        self.assertTrue(stats["regret_reliable"])
        self.assertFalse(stats["achieved_within_lp_soc_bounds"])
        self.assertIsNone(stats["epr_reason"])


class TestNegativeRegretWithoutAnEnvelopeBreach(unittest.TestCase):
    def test_it_gets_its_own_reason_rather_than_the_shared_one(self):
        """If regret goes negative while the achieved trajectory stayed
        inside the LP's envelope, the mechanism verified on the
        reference household does NOT explain it, and something else
        does. Worth a distinct label rather than one that asserts a
        cause this day has no evidence for.
        """
        stats = _stats([20.0, 50.0, 80.0], -0.25)
        self.assertFalse(stats["regret_reliable"])
        self.assertTrue(stats["achieved_within_lp_soc_bounds"])
        self.assertEqual(stats["epr_reason"], "oracle_beaten")


class TestDegenerateInputs(unittest.TestCase):
    def test_no_trajectory_still_reports_the_regret_verdict(self):
        """The regret test needs no SoC history at all, so losing the
        trajectory must not lose the finding -- the envelope fields go
        None, the verdict stands."""
        stats = solver_writer._achieved_feasibility_stats(
            {}, -0.5, min_pct=2.0, max_pct=100.0, capacity_kwh=122.2
        )
        self.assertFalse(stats["regret_reliable"])
        self.assertEqual(stats["epr_reason"], "oracle_beaten")
        self.assertIsNone(stats["achieved_within_lp_soc_bounds"])
        self.assertIsNone(stats["achieved_below_floor_kwh"])

    def test_zero_capacity_does_not_divide_by_it(self):
        stats = _stats([10.0, 20.0], -0.5, capacity=0.0)
        self.assertFalse(stats["regret_reliable"])
        self.assertIsNone(stats["achieved_soc_min_pct"])

    def test_rows_without_a_soc_are_skipped_not_fatal(self):
        hourly = {"a": {"soc_pct": 10.0}, "b": {"battery_kw": 1.0}}
        stats = solver_writer._achieved_feasibility_stats(
            hourly, 1.0, min_pct=2.0, max_pct=100.0, capacity_kwh=122.2
        )
        self.assertEqual(stats["achieved_soc_min_pct"], 10.0)


class TestEprReliabilityCombination(unittest.TestCase):
    """A definite 'no' from either signal must win over an 'unknown'."""

    def test_negative_regret_overrides_an_unknown_soc_verdict(self):
        self.assertIs(solver_writer._epr_reliability(None, False), False)

    def test_negative_regret_overrides_a_healthy_soc_verdict(self):
        self.assertIs(solver_writer._epr_reliability(True, False), False)

    def test_an_unknown_soc_verdict_survives_a_clean_regret(self):
        """#533's own None-means-unknown must not be quietly promoted to
        True just because the regret half found nothing wrong -- they
        answer different questions."""
        self.assertIsNone(solver_writer._epr_reliability(None, True))

    def test_the_soc_verdict_still_decides_when_regret_is_clean(self):
        self.assertIs(solver_writer._epr_reliability(False, True), False)
        self.assertIs(solver_writer._epr_reliability(True, True), True)


if __name__ == "__main__":
    unittest.main()
