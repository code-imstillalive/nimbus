"""nimbus issue #919: one-step-ahead LOAD nowcast skill -- the pure half.

`forecast_regret.py` has been able to answer "does the ML forecaster beat
persistence" since #273, but no deployed install has ever computed it,
because the forecast *as it was made at the time* was not recoverable
after the fact. #919's decision was to recover it from recorder history.

These tests pin the two things that decision depends on:

1. **Solar is held at truth in every scenario**, so the published delta
   is attributable to LOAD forecast quality alone (Nimbus does not
   forecast solar -- Solcast/Open-Meteo do, and their skill is not this
   project's to claim). The perfect-trail test below is what proves this
   is actually wired that way: if solar were NOT held at truth, a
   perfect load trail would still carry solar-attributable regret and
   that test would fail.

2. **It degrades to None rather than publishing a fabricated number**
   when the window has too little real recorded history.

There is also a test here that exists purely as documentation of a real
near-miss -- `test_a_trail_identical_to_ground_truth_is_the_anchor_
fingerprint`. See its own docstring; it encodes the shape of a bug that
would have published near-perfect skill on every install forever, and
which was caught only by reading the code that writes the sensor rather
than trusting the sensor's name.
"""

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.nowcast_skill import (
    compute_load_nowcast_skill,
    period_sample_coverage,
)

N = 24  # one day, hourly periods
START = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def _grid():
    return GridConfig(
        import_price=np.full(N, 0.30),
        export_price=np.full(N, 0.10),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )


def _battery():
    return BatteryConfig(
        name="battery",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=1.0,
        max_soc_kwh=20.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )


def _periods():
    return PeriodGrid(hours=np.array([1.0] * N), start=START)


def _real_curves():
    """Real-shaped solar hump and a load with morning/evening peaks --
    flat synthetic data would make every forecast free to be wrong."""
    hour = np.arange(N)
    solar = np.clip(8.0 * np.sin((hour - 6.0) / 12.0 * np.pi), 0.0, None)
    load = (
        1.5
        + 1.0 * np.sin(hour / 24.0 * 2 * np.pi)
        + (2.0 * ((hour >= 17) & (hour <= 21)))
    )
    return solar, load


def _score(**overrides):
    solar_real, load_real = _real_curves()
    kwargs = {
        "periods": _periods(),
        "grid": _grid(),
        "battery": _battery(),
        "solar_real_kw": solar_real,
        "load_real_kw": load_real,
        "load_nowcast_kw": load_real.copy(),
        "load_persistence_kw": np.roll(load_real, 2) * 1.1,
        "n_periods_measured": N,
    }
    kwargs.update(overrides)
    return compute_load_nowcast_skill(**kwargs)


class TestPeriodSampleCoverage(unittest.TestCase):
    """Needed because `resample_history_mean()` deliberately never
    reports a gap -- it holds the last real sample instead, which leaves
    a caller unable to tell a measured period from a held-over one."""

    def setUp(self):
        self.grid_times = [START + timedelta(hours=i) for i in range(6)]

    def test_every_period_measured(self):
        samples = [START + timedelta(hours=i, minutes=30) for i in range(6)]
        self.assertEqual(period_sample_coverage(samples, self.grid_times, 1.0), (6, 6))

    def test_a_real_gap_is_counted_as_unmeasured(self):
        """Two periods with no sample at all -- exactly the recorder-purge
        / post-restart shape the coverage floor exists to catch."""
        samples = [START + timedelta(hours=i, minutes=30) for i in (0, 1, 4, 5)]
        self.assertEqual(period_sample_coverage(samples, self.grid_times, 1.0), (4, 6))

    def test_the_window_is_half_open_matching_resample_history_mean(self):
        """A sample landing exactly on a period boundary belongs to the
        period it STARTS, not the one it ends. This has to agree with
        `resample_history_mean()`'s own `[gt, gt + period_hours)` or the
        coverage figure would describe a different set of samples than
        the values being scored."""
        exactly_on_second_boundary = [START + timedelta(hours=1)]
        measured, total = period_sample_coverage(
            exactly_on_second_boundary, self.grid_times, 1.0
        )
        self.assertEqual((measured, total), (1, 6))
        # ...and it is the SECOND period that was measured, not the first.
        self.assertEqual(
            period_sample_coverage(
                exactly_on_second_boundary, [self.grid_times[0]], 1.0
            ),
            (0, 1),
        )

    def test_no_samples_at_all(self):
        self.assertEqual(period_sample_coverage([], self.grid_times, 1.0), (0, 6))

    def test_an_empty_grid_is_zero_of_zero_not_a_crash(self):
        """Guards the division in `compute_load_nowcast_skill()` -- a
        zero-period window must not reach a ZeroDivisionError."""
        self.assertEqual(period_sample_coverage([START], [], 1.0), (0, 0))

    def test_unordered_samples_are_handled(self):
        """Recorder history arrives ordered, but nothing in the signature
        promises it, and a wrong answer here would silently understate
        coverage."""
        samples = [START + timedelta(hours=i, minutes=30) for i in (5, 0, 3, 1)]
        self.assertEqual(period_sample_coverage(samples, self.grid_times, 1.0), (4, 6))

    def test_multiple_samples_in_one_period_count_once(self):
        samples = [START + timedelta(minutes=m) for m in (1, 2, 3, 4, 59)]
        self.assertEqual(period_sample_coverage(samples, self.grid_times, 1.0), (1, 6))


class TestSolarIsHeldAtTruth(unittest.TestCase):
    """The design decision that makes the published number mean "load
    forecast skill" rather than "load skill plus Solcast's skill"."""

    def test_a_perfect_load_trail_has_near_zero_forecast_regret(self):
        """This is the test that actually proves solar is held at truth.
        If solar were passed as anything other than the real measured
        series, a perfect LOAD trail would still leave real
        solar-attributable regret here and this would fail -- so it
        cannot pass for the wrong reason."""
        result = _score()
        assert result is not None
        self.assertAlmostEqual(result.j_forecast, result.j_star, places=4)

    def test_the_oracle_still_cannot_be_beaten(self):
        """The structural bound every regret module in this package
        shares. A violation means the scenarios are not being priced
        against the same ground truth."""
        result = _score(load_nowcast_kw=np.roll(_real_curves()[1], 1))
        assert result is not None
        self.assertLessEqual(result.j_star, result.j_forecast + 1e-6)
        self.assertLessEqual(result.j_star, result.j_persistence + 1e-6)


class TestTheValueAddSignIsReal(unittest.TestCase):
    def test_a_good_trail_beats_a_bad_persistence(self):
        _, load_real = _real_curves()
        result = _score(
            load_nowcast_kw=load_real + 0.05,
            load_persistence_kw=np.roll(load_real, 6) * 1.4,
        )
        assert result is not None
        self.assertGreater(result.value_add_dollars, 0.0)

    def test_a_bad_trail_gives_a_NEGATIVE_value_add_and_it_is_published(self):
        """A negative result is a real answer to #919's question, not an
        error to suppress. An install where persistence wins deserves to
        be told that -- it is the whole reason the number is worth
        publishing at all."""
        _, load_real = _real_curves()
        result = _score(
            load_nowcast_kw=np.roll(load_real, 8) * 1.6,
            load_persistence_kw=load_real + 0.05,
        )
        assert result is not None
        self.assertLess(result.value_add_dollars, 0.0)

    def test_value_add_is_exactly_the_persistence_minus_forecast_delta(self):
        result = _score()
        assert result is not None
        self.assertAlmostEqual(
            result.value_add_dollars,
            result.j_persistence - result.j_forecast,
            places=9,
        )


class TestTheAnchorFingerprint(unittest.TestCase):
    """Documentation of a real near-miss, kept as an executable test.

    The first design for #919 sourced the trail from
    `sensor.nimbus_household_load_total_forecast`'s recorded state. That
    sensor's state is `round(load_kw[0], 3)`, and `solver_inputs/load.py`
    overwrites `load_kw[0]` with the live cross-check reading immediately
    before publishing it (#429's anchor) -- and the cross-check sensor is
    the SAME sensor a quality report uses as its real-load ground truth.

    So that trail is the ground truth echoed back. Confirmed live: three
    hours of recorded history agreed to the cent on every row.
    """

    def test_a_trail_identical_to_ground_truth_is_the_anchor_fingerprint(self):
        """A trail equal to reality produces ZERO forecast regret and
        therefore the maximum possible value-add -- a perfect score, on
        every install, forever.

        That is not evidence of a good forecaster; it is the signature of
        feeding ground truth back in. Anyone who sees `j_forecast ==
        j_star` exactly, on real data, should suspect the trail source
        before celebrating -- real forecasts are never exact.
        """
        result = _score()
        assert result is not None
        self.assertAlmostEqual(result.j_forecast, result.j_star, places=4)
        self.assertAlmostEqual(
            result.value_add_dollars,
            result.j_persistence - result.j_star,
            places=4,
            msg=(
                "a ground-truth trail yields the maximum attainable "
                "value-add; if a real install ever publishes this, the "
                "trail sensor is wrong, not the forecaster brilliant"
            ),
        )


class TestItDegradesInsteadOfFabricating(unittest.TestCase):
    def test_coverage_below_the_floor_returns_none(self):
        self.assertIsNone(_score(n_periods_measured=2))

    def test_coverage_at_the_floor_is_accepted(self):
        """The floor is inclusive -- `< min_coverage` rejects, so exactly
        half measured is still scored."""
        self.assertIsNotNone(_score(n_periods_measured=N // 2))

    def test_a_zero_floor_accepts_anything_measured(self):
        """An explicit opt-out for a diagnostic caller that wants the
        number regardless, without having to reimplement the scoring."""
        self.assertIsNotNone(_score(n_periods_measured=0, min_coverage=0.0))

    def test_mismatched_array_lengths_return_none(self):
        """A trail resampled onto a different grid than the ground truth
        would be compared period-by-period against the wrong instants --
        silently, and the resulting dollars would look plausible."""
        self.assertIsNone(_score(load_nowcast_kw=np.zeros(N - 1)))
        self.assertIsNone(_score(load_persistence_kw=np.zeros(N + 3)))
        self.assertIsNone(_score(solar_real_kw=np.zeros(N - 2)))

    def test_a_zero_period_window_is_unconstructible_so_the_division_is_safe(self):
        """`compute_load_nowcast_skill()` divides by the period count and
        has no zero guard of its own. It does not need one: the invariant
        lives in `PeriodGrid` itself, which refuses an empty grid at
        construction. Pinned here so nobody 'simplifies' that check away
        and turns this division into a live ZeroDivisionError -- and so
        the absence of a guard reads as deliberate rather than missed.
        """
        with self.assertRaises(ValueError):
            PeriodGrid(hours=np.array([]), start=START)

    def test_coverage_is_reported_on_the_result(self):
        """A consumer has to be able to see how much of the window was
        really measured -- a scored window at 0.6 coverage is a weaker
        claim than one at 1.0, and the report should say so rather than
        presenting both as equal."""
        result = _score(n_periods_measured=18)
        assert result is not None
        self.assertAlmostEqual(result.coverage, 0.75)
        self.assertEqual(result.n_periods_measured, 18)
        self.assertEqual(result.n_periods, N)


if __name__ == "__main__":
    unittest.main()
