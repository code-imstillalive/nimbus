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

import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.nowcast_skill import (
    compute_load_nowcast_skill,
    forecast_value_at_lead_hours,
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


class TestTheBenchmarkDocsClaimStaysTrue(unittest.TestCase):
    """`docs/reference-benchmark.md` makes a factual claim about who
    calls `compute_forecast_regret()`, and that claim went stale silently
    the moment this module started calling it.

    It had said the HACS integration "never calls it -- nothing outside
    `custom_components/nimbus_load/solver/` even imports
    `forecast_regret`". Nothing asserted that, so nothing caught it. This
    is the same stale-documented-claim class as the four guards added on
    2026-09-15 (entity names in a docs table, test-file names in source
    comments, units, and physics in a docstring) -- a claim a reader will
    trust needs something that fails when it stops being true.
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_the_documented_importer_set_is_the_real_one(self):
        """Measures real IMPORT statements, not textual mentions. The
        first version of this test matched any file containing the string
        and so flagged `solver_writer.py`, which only names the module in
        a docstring -- a count measuring the wrong thing, the same error
        four probes made on 2026-09-15. The doc's claim is about who
        CALLS it, so imports are the right proxy; prose is not.
        """
        importers = {
            path.name
            for path in (self.repo / "custom_components" / "nimbus_load").rglob("*.py")
            if re.search(
                r"^\s*(?:from\s+\.?\S*forecast_regret\s+import|import\s+\S*forecast_regret)",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        }
        self.assertEqual(
            importers,
            {"nowcast_skill.py", "reference_benchmark.py"},
            "the set of modules referencing forecast_regret changed. "
            "docs/reference-benchmark.md documents exactly who calls it "
            "and why the field number is not comparable to the "
            "benchmark's -- update that section, then update this test",
        )

    def test_the_doc_tells_a_reader_the_field_number_is_a_different_horizon(self):
        """The specific thing a reader could get wrong: comparing
        `load_nowcast_skill_value_add_dollars` against the benchmark's
        own `nimbus_value_add_dollars` as though they measured the same
        thing. One is one-step-ahead, the other day-ahead."""
        doc = (self.repo / "docs" / "reference-benchmark.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("nowcast_skill", doc)
        self.assertIn("one-step-ahead", doc)


class TestForecastValueAtLeadHours(unittest.TestCase):
    """nimbus #937: the three published `load_forecast_plus_*h_kw`
    scalars are what make error-versus-lead-time measurable from
    ordinary recorder history, since the full `forecast` array is
    deliberately unrecorded. If this picks the wrong period the whole
    curve is quietly shifted, and nobody would notice.
    """

    def setUp(self):
        # Deliberately NON-UNIFORM, matching the real tiered grid (#438):
        # six 5-min periods, then 15-min. A lead time therefore cannot be
        # turned into an index by division.
        self.times = [START + timedelta(minutes=5 * i) for i in range(6)]
        self.times += [
            START + timedelta(minutes=30) + timedelta(minutes=15 * i) for i in range(12)
        ]
        self.vals = np.arange(len(self.times), dtype=float)

    def test_it_returns_the_value_for_the_period_containing_the_target(self):
        """now + 1 h = 60 min. The 15-min tier starts at 30 min, so 60 min
        lands in the period starting at 60 min -- index 8, not the index
        12 a naive 5-min division would give."""
        self.assertEqual(
            forecast_value_at_lead_hours(self.times, self.vals, START, 1.0), 8.0
        )

    def test_a_target_inside_a_period_takes_that_period_not_the_next(self):
        """35 min sits inside the period starting at 30 min. Reading the
        next period would bias every published figure one step into the
        future."""
        self.assertEqual(
            forecast_value_at_lead_hours(self.times, self.vals, START, 35.0 / 60.0),
            6.0,
        )

    def test_an_exact_period_start_takes_that_period(self):
        self.assertEqual(
            forecast_value_at_lead_hours(self.times, self.vals, START, 0.5), 6.0
        )

    def test_zero_lead_is_the_current_period(self):
        self.assertEqual(
            forecast_value_at_lead_hours(self.times, self.vals, START, 0.0), 0.0
        )

    def test_beyond_the_horizon_is_none_not_the_last_period(self):
        """The honest answer. A forecast that does not reach 24 h ahead
        has no 24 h-ahead value, and clamping would publish "the far end
        of whatever we had" under a name claiming a specific lead time --
        corrupting the very curve these scalars exist to measure."""
        self.assertIsNone(
            forecast_value_at_lead_hours(self.times, self.vals, START, 24.0)
        )

    def test_a_target_before_the_grid_is_none(self):
        """Guards a clock-skew / stale-`now` case rather than returning
        period 0 as though it applied."""
        self.assertIsNone(
            forecast_value_at_lead_hours(self.times, self.vals, START, -1.0)
        )

    def test_empty_and_mismatched_inputs_are_none(self):
        self.assertIsNone(forecast_value_at_lead_hours([], np.array([]), START, 1.0))
        self.assertIsNone(
            forecast_value_at_lead_hours(self.times, np.arange(3.0), START, 0.1)
        )

    def test_the_last_period_start_is_reachable(self):
        """Boundary: the final grid point is inside the horizon, so it
        must resolve rather than trip the beyond-horizon guard."""
        total_h = (self.times[-1] - START).total_seconds() / 3600.0
        self.assertEqual(
            forecast_value_at_lead_hours(self.times, self.vals, START, total_h),
            float(len(self.times) - 1),
        )

    def test_it_returns_a_real_float_not_a_numpy_scalar(self):
        """These go straight into a published attribute dict, and a
        numpy float64 is not what HA's own JSON encoder expects."""
        got = forecast_value_at_lead_hours(self.times, self.vals, START, 1.0)
        self.assertIsInstance(got, float)
        self.assertNotIsInstance(got, np.floating)


if __name__ == "__main__":
    unittest.main()
