"""nimbus issue #452: AEMO's own 30-minute forecast against AEMO's own
realised 5-minute prices for the same window.

Mark Purcell's instruction, after confirming what his install actually
publishes: *"If the 5 minute actual interval price and 30 minute
forecasts are all you have, then you should work with that. Compare the
30 minute forecast with the relevant 6x 5 minute intervals for a like
with like comparison."*

The fixtures below use the exact attribute shape he pasted from his own
`sensor.aemo_nem_qld1_current_30min_forecast` — ISO strings with a real
offset, prices as floats in $/kWh — rather than an invented one.

The cases that matter are the ones a plausible implementation gets wrong:

- **The bucket is found by containment, not `forecast[0]`.** That entity
  does appear to list the current window first, and relying on it would
  work until a refresh landed mid-window or a feed kept one elapsed
  bucket — then it would compare the wrong half-hour against the right
  actuals and report a disagreement that is purely an alignment bug.
- **The upper bound is `now`, not the window end.** Averaging against a
  window that has not finished is the whole point; using `end` would
  silently include nothing and change the sample count.
- **A naive datetime is refused, not assumed.** Comparing naive against
  aware raises, and quietly assuming UTC would produce a confidently
  wrong bucket half the time.
- **`n_samples` is reported.** One sample and six produce equally
  confident-looking means; that difference is the early-window weakness
  and it must be visible to the caller.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

import _solver_path  # noqa: F401
from aemo_crosscheck import (
    compare_forecast_to_actuals,
    mean_realised_price,
    select_current_bucket,
)

AEST = timezone(timedelta(hours=10))


def _entry(start_h, start_m, price, minutes=30):
    start = datetime(2026, 9, 13, start_h, start_m, tzinfo=AEST)
    return {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=minutes)).isoformat(),
        "price": price,
    }


# The real shape, from Mark's own paste.
REAL_FORECAST = [
    _entry(12, 0, -0.0085),
    _entry(12, 30, -0.01),
    _entry(13, 0, -0.012),
    _entry(13, 30, -0.012),
]


def _samples(start_h, start_m, values, step_min=5):
    start = datetime(2026, 9, 13, start_h, start_m, tzinfo=AEST)
    return [(start + timedelta(minutes=i * step_min), v) for i, v in enumerate(values)]


class TestBucketSelection(unittest.TestCase):
    def test_the_bucket_containing_now_is_chosen(self):
        now = datetime(2026, 9, 13, 12, 43, tzinfo=AEST)
        start, _end, price = select_current_bucket(REAL_FORECAST, now)
        self.assertEqual(start.hour, 12)
        self.assertEqual(start.minute, 30)
        self.assertEqual(price, -0.01)

    def test_it_is_not_simply_forecast_zero(self):
        """The distinguishing case: `now` is in the third bucket, so a
        `forecast[0]` implementation returns the wrong window and the
        wrong price."""
        now = datetime(2026, 9, 13, 13, 15, tzinfo=AEST)
        start, _end, price = select_current_bucket(REAL_FORECAST, now)
        self.assertEqual((start.hour, start.minute), (13, 0))
        self.assertEqual(price, -0.012)
        self.assertNotEqual(price, REAL_FORECAST[0]["price"])

    def test_an_already_elapsed_leading_bucket_is_skipped(self):
        """A feed that keeps one finished window at the head must not
        win — this is the real-world shape that breaks `forecast[0]`."""
        now = datetime(2026, 9, 13, 12, 35, tzinfo=AEST)
        start, _end, _price = select_current_bucket(REAL_FORECAST, now)
        self.assertEqual((start.hour, start.minute), (12, 30))

    def test_the_window_is_half_open_at_the_start(self):
        now = datetime(2026, 9, 13, 12, 30, 0, tzinfo=AEST)
        start, _end, _price = select_current_bucket(REAL_FORECAST, now)
        self.assertEqual((start.hour, start.minute), (12, 30))

    def test_the_window_is_half_open_at_the_end(self):
        """13:00:00 belongs to the 13:00 bucket, never to both."""
        now = datetime(2026, 9, 13, 13, 0, 0, tzinfo=AEST)
        start, _end, _price = select_current_bucket(REAL_FORECAST, now)
        self.assertEqual((start.hour, start.minute), (13, 0))

    def test_a_time_outside_every_bucket_returns_none(self):
        now = datetime(2026, 9, 13, 20, 0, tzinfo=AEST)
        self.assertIsNone(select_current_bucket(REAL_FORECAST, now))

    def test_empty_and_none(self):
        now = datetime(2026, 9, 13, 12, 43, tzinfo=AEST)
        self.assertIsNone(select_current_bucket([], now))
        self.assertIsNone(select_current_bucket(None, now))


class TestMalformedEntries(unittest.TestCase):
    NOW = datetime(2026, 9, 13, 12, 43, tzinfo=AEST)

    def test_a_naive_timestamp_is_refused_not_assumed(self):
        """Assuming UTC (or local) would produce a confidently wrong
        bucket half the time; comparing naive against aware raises."""
        bad = [
            {
                "start_time": "2026-09-13T12:30:00",
                "end_time": "2026-09-13T13:00:00",
                "price": -0.01,
            }
        ]
        self.assertIsNone(select_current_bucket(bad, self.NOW))

    def test_a_malformed_entry_does_not_block_a_good_one(self):
        entries = [{"start_time": "not a time"}, *REAL_FORECAST]
        start, _end, _price = select_current_bucket(entries, self.NOW)
        self.assertEqual((start.hour, start.minute), (12, 30))

    def test_a_zero_width_window_is_skipped(self):
        bad = _entry(12, 30, -0.01, minutes=0)
        start, _end, _price = select_current_bucket([bad, *REAL_FORECAST], self.NOW)
        self.assertEqual((start.hour, start.minute), (12, 30))
        self.assertIsNone(select_current_bucket([bad], self.NOW))

    def test_a_boolean_price_is_rejected(self):
        """`bool` is an `int` subclass in Python and would otherwise sail
        through as 0.0/1.0 — a real hazard reading attribute dicts."""
        bad = dict(_entry(12, 30, -0.01), price=True)
        self.assertIsNone(select_current_bucket([bad], self.NOW))

    def test_a_missing_price_is_skipped(self):
        bad = {k: v for k, v in _entry(12, 30, -0.01).items() if k != "price"}
        self.assertIsNone(select_current_bucket([bad], self.NOW))

    def test_a_non_dict_entry_is_skipped(self):
        start, _end, _price = select_current_bucket(
            ["nonsense", *REAL_FORECAST], self.NOW
        )
        self.assertEqual((start.hour, start.minute), (12, 30))


class TestRealisedMean(unittest.TestCase):
    START = datetime(2026, 9, 13, 12, 30, tzinfo=AEST)
    END = datetime(2026, 9, 13, 13, 0, tzinfo=AEST)

    def test_mean_and_count(self):
        samples = _samples(12, 30, [0.10, 0.20, 0.30])
        mean, n = mean_realised_price(samples, self.START, self.END)
        self.assertAlmostEqual(mean, 0.20)
        self.assertEqual(n, 3)

    def test_samples_outside_the_window_are_excluded(self):
        samples = _samples(12, 0, [9.0, 9.0, 9.0, 9.0, 9.0, 9.0]) + _samples(
            12, 30, [0.10, 0.20]
        )
        mean, n = mean_realised_price(samples, self.START, self.END)
        self.assertAlmostEqual(mean, 0.15)
        self.assertEqual(n, 2)

    def test_a_sample_on_the_end_boundary_belongs_to_the_next_window(self):
        samples = [(self.END, 99.0), *_samples(12, 30, [0.10])]
        mean, n = mean_realised_price(samples, self.START, self.END)
        self.assertAlmostEqual(mean, 0.10)
        self.assertEqual(n, 1)

    def test_a_naive_sample_is_skipped(self):
        naive = datetime(2026, 9, 13, 12, 40)  # noqa: DTZ001 -- naive on purpose
        samples = [(naive, 99.0), *_samples(12, 30, [0.10])]
        mean, n = mean_realised_price(samples, self.START, self.END)
        self.assertAlmostEqual(mean, 0.10)
        self.assertEqual(n, 1)

    def test_no_samples(self):
        self.assertEqual(mean_realised_price([], self.START, self.END), (None, 0))
        self.assertEqual(mean_realised_price(None, self.START, self.END), (None, 0))


class TestTheWholeCheck(unittest.TestCase):
    def test_agreement_is_not_flagged(self):
        now = datetime(2026, 9, 13, 12, 50, tzinfo=AEST)
        samples = _samples(12, 30, [-0.011, -0.009, -0.010, -0.010])
        got = compare_forecast_to_actuals(REAL_FORECAST, samples, now, 0.10)
        self.assertIsNotNone(got)
        self.assertFalse(got["flagged"])
        self.assertEqual(got["forecast_price"], -0.01)
        self.assertEqual(got["n_samples"], 4)

    def test_a_real_disagreement_is_flagged(self):
        """Forecast says −1¢/kWh, dispatch actually came in at $2/kWh —
        the kind of divergence this check exists to surface."""
        now = datetime(2026, 9, 13, 12, 50, tzinfo=AEST)
        samples = _samples(12, 30, [2.0, 2.0, 2.0, 2.0])
        got = compare_forecast_to_actuals(REAL_FORECAST, samples, now, 0.10)
        self.assertTrue(got["flagged"])
        self.assertAlmostEqual(got["disagreement_dollars"], 2.01, places=4)

    def test_the_upper_bound_is_now_not_the_window_end(self):
        """Only the elapsed part of an in-progress window counts. A
        sample timestamped after `now` (a clock skew, a future-dated
        reading) must not be averaged in."""
        now = datetime(2026, 9, 13, 12, 40, tzinfo=AEST)
        samples = _samples(12, 30, [0.10, 0.10, 99.0, 99.0])
        got = compare_forecast_to_actuals(REAL_FORECAST, samples, now, 0.10)
        self.assertEqual(got["n_samples"], 2)
        self.assertAlmostEqual(got["realised_mean_price"], 0.10)

    def test_early_in_the_window_it_still_answers_but_says_so(self):
        """Weakest at minute 0-5 with a single sample. It answers, and
        reports n_samples so a caller can weight it."""
        now = datetime(2026, 9, 13, 12, 33, tzinfo=AEST)
        got = compare_forecast_to_actuals(
            REAL_FORECAST, _samples(12, 30, [0.05]), now, 0.10
        )
        self.assertEqual(got["n_samples"], 1)

    def test_min_samples_suppresses_a_weak_comparison_when_asked(self):
        now = datetime(2026, 9, 13, 12, 33, tzinfo=AEST)
        got = compare_forecast_to_actuals(
            REAL_FORECAST, _samples(12, 30, [0.05]), now, 0.10, min_samples=3
        )
        self.assertIsNone(got)

    def test_no_bucket_and_no_samples_both_return_none(self):
        outside = datetime(2026, 9, 13, 20, 0, tzinfo=AEST)
        inside = datetime(2026, 9, 13, 12, 50, tzinfo=AEST)
        self.assertIsNone(
            compare_forecast_to_actuals(
                REAL_FORECAST, _samples(12, 30, [0.1]), outside, 0.1
            )
        )
        self.assertIsNone(compare_forecast_to_actuals(REAL_FORECAST, [], inside, 0.1))

    def test_the_window_is_reported_so_a_reader_can_check_alignment(self):
        now = datetime(2026, 9, 13, 12, 50, tzinfo=AEST)
        got = compare_forecast_to_actuals(
            REAL_FORECAST, _samples(12, 30, [0.0]), now, 0.10
        )
        self.assertTrue(got["window_start"].startswith("2026-09-13T12:30"))
        self.assertTrue(got["window_end"].startswith("2026-09-13T13:00"))

    def test_no_retail_offset_is_applied(self):
        """Unlike the shipped current-interval check, both sides here are
        AEMO's own wholesale numbers — an offset term would be wrong."""
        now = datetime(2026, 9, 13, 12, 50, tzinfo=AEST)
        got = compare_forecast_to_actuals(
            REAL_FORECAST, _samples(12, 30, [-0.01]), now, 0.10
        )
        self.assertAlmostEqual(got["disagreement_dollars"], 0.0)

    def test_a_utc_now_against_an_aest_forecast_still_aligns(self):
        """Aware datetimes compare across zones correctly; this pins that
        nothing in the path is doing naive wall-clock arithmetic."""
        now_utc = datetime(2026, 9, 13, 2, 50, tzinfo=UTC)  # 12:50 AEST
        got = compare_forecast_to_actuals(
            REAL_FORECAST, _samples(12, 30, [-0.01]), now_utc, 0.10
        )
        self.assertIsNotNone(got)
        self.assertTrue(got["window_start"].startswith("2026-09-13T12:30"))


if __name__ == "__main__":
    unittest.main()
