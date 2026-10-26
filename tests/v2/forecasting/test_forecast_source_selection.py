"""Persistence as a real fallback: which load forecast to use, the ML
forecast, yesterday's load or a blend, chosen from the trailing day-ahead
record; off, measure-only and the thresholds each pinned (#937) (#1837).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "nimbus_load"
    / "solver"
    / "forecast_source_selection.py"
)


def _load_module():
    """Load by path, with no Home Assistant import anywhere.

    Deliberately not a package import: that executes the package `__init__`,
    which pulls in HA's config-entry machinery. This module has zero HA
    dependencies and this loader is what PROVES it -- if an HA import is ever
    added, this file fails first.
    """
    spec = importlib.util.spec_from_file_location(
        "forecast_source_selection", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `dataclasses` resolves annotations through
    # `sys.modules[cls.__module__]` and raises AttributeError without it.
    sys.modules["forecast_source_selection"] = module
    spec.loader.exec_module(module)
    return module


fss = _load_module()
select_forecast_source = fss.select_forecast_source
blend_load_forecast = fss.blend_load_forecast
ForecastSourceDecision = fss.ForecastSourceDecision

TODAY = date(2026, 9, 13)

#: #937's own published per-day `nimbus_value_add_dollars`, verbatim from the
#: issue body. Used rather than a synthetic series on purpose: the numbers this
#: mechanism has to behave correctly on are the numbers that motivated it, and a
#: rounder fixture would let a sign or an off-by-one hide.
ISSUE_937_SERIES = {
    "2026-08-30": -3.4158,
    "2026-08-31": -0.9705,
    "2026-09-01": +0.2553,
    "2026-09-02": -0.5878,
    "2026-09-03": -1.6414,
    "2026-09-04": -0.7930,
    "2026-09-05": -0.0709,
    "2026-09-06": -1.0909,
    "2026-09-07": -1.0702,
    "2026-09-08": -0.6903,
    "2026-09-09": -0.4531,
    "2026-09-10": +0.7988,
    "2026-09-11": -0.5224,
    "2026-09-12": +0.3200,
}

#: The window that reaches all fourteen of those days from TODAY. 2026-08-30 is
#: 14 days before 2026-09-13, so a 14-day window would exclude it by one day --
#: stated explicitly here because silently using 15 would look like a typo.
ISSUE_937_WINDOW = 15


def _issue_decision(policy: str, **kw):
    return select_forecast_source(
        value_add_by_day=ISSUE_937_SERIES,
        today=TODAY,
        policy=policy,
        trailing_days=ISSUE_937_WINDOW,
        **kw,
    )


class TestOffCannotBeTalkedIntoActing(unittest.TestCase):
    """The default policy, against the most persuasive input that exists.

    This is the safety property the whole design rests on: the reference
    household dispatches a real battery through this code path, and an install
    that has not opted in must be byte-identical to one running the release
    before this existed.
    """

    def test_an_unrecognised_policy_is_off_and_says_so(self):
        # Never silently normalised: an install whose select entity somehow
        # holds a stale option must keep dispatching on the ML forecast AND be
        # diagnosable, rather than looking like a deliberate `off`.
        d = _issue_decision("aggressive")
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.policy, "aggressive")
        self.assertEqual(d.reason, fss.REASON_POLICY_UNRECOGNISED)


class TestTheGatesBeforeAnyPolicyActs(unittest.TestCase):
    """Four reasons to keep the ML forecast that have nothing to do with the
    policy asked for. Each is separately reachable and separately named."""

    def test_a_thin_sample_is_refused_even_when_persistence_won_every_day(self):
        # test_a_thin_sample_is_refused_even_when_persistence_won_every_day: #937's own caveat is that 14 days is already a small sample. Three
        d = select_forecast_source(
            value_add_by_day={
                "2026-09-10": -2.0,
                "2026-09-11": -2.0,
                "2026-09-12": -2.0,
            },
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
        )
        self.assertEqual(d.days_scored, 3)
        self.assertEqual(d.days_persistence_won, 3)
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_INSUFFICIENT_SCORED_DAYS)

        # test_a_forecaster_losing_by_pennies_is_left_alone: The threshold's whole purpose. A mean of -3c/day is a forecaster
        d = select_forecast_source(
            value_add_by_day={f"2026-09-{day:02d}": -0.03 for day in range(5, 13)},
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
        )
        self.assertEqual(d.days_persistence_won, 8)  # every day, technically
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_FORECAST_NOT_LOSING)


class TestTheTrailingWindowIsCalendarBased(unittest.TestCase):
    """A calendar window, not "the most recent N rows".

    Rows are legitimately sparse -- the day-ahead decomposition is written only
    on days that had a snapshot and usable previous-day history -- so
    most-recent-N can reach back months and present a stale verdict as current.
    A calendar window thins the sample instead, `days_scored` says so, and the
    minimum-days gate then refuses to act on it.
    """

    def test_a_stale_record_thins_rather_than_lying(self):
        # test_a_stale_record_thins_rather_than_lying: Fourteen unanimous days, all two months old. Most-recent-N would act
        stale = {f"2026-07-{day:02d}": -2.0 for day in range(1, 15)}
        d = select_forecast_source(
            value_add_by_day=stale, today=TODAY, policy=fss.POLICY_PERSISTENCE
        )
        self.assertEqual(d.days_scored, 0)
        self.assertEqual(d.reason, fss.REASON_NO_TRAILING_RECORD)

        # test_a_future_dated_row_is_not_evidence
        record = dict(ISSUE_937_SERIES)
        record["2026-12-25"] = -99.0
        d = select_forecast_source(
            value_add_by_day=record,
            today=TODAY,
            policy=fss.POLICY_MEASURE,
            trailing_days=ISSUE_937_WINDOW,
        )
        self.assertEqual(d.days_scored, 14)
        self.assertGreater(d.mean_value_add_dollars, -1.0)

        # test_a_zero_or_negative_window_reads_nothing
        d = select_forecast_source(
            value_add_by_day=ISSUE_937_SERIES,
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
            trailing_days=0,
        )
        self.assertEqual(d.reason, fss.REASON_NO_TRAILING_RECORD)


class TestAMalformedRecordCannotDecideAnything(unittest.TestCase):
    """This mapping comes off a published attribute any program can have
    written -- the standalone cron writer did, for the era #937's own figures
    come from. One bad row must not cost the decision, and must not BE the
    decision."""

    def test_unparseable_keys_and_values_are_skipped(self):
        # test_unparseable_keys_and_values_are_skipped
        record = dict(ISSUE_937_SERIES)
        record.update(
            {
                "not-a-date": -50.0,
                "2026-13-45": -50.0,
                "2026-09-11x": -50.0,
            }
        )
        record["2026-09-06"] = "sideways"  # type: ignore[assignment]
        d = select_forecast_source(
            value_add_by_day=record,
            today=TODAY,
            policy=fss.POLICY_MEASURE,
            trailing_days=ISSUE_937_WINDOW,
        )
        self.assertEqual(d.days_scored, 13)  # the 14 minus the unparseable one

        # test_a_non_finite_value_cannot_poison_the_mean
        record = dict(ISSUE_937_SERIES)
        record["2026-09-06"] = float("nan")
        record["2026-09-07"] = float("-inf")
        d = select_forecast_source(
            value_add_by_day=record,
            today=TODAY,
            policy=fss.POLICY_MEASURE,
            trailing_days=ISSUE_937_WINDOW,
        )
        self.assertEqual(d.days_scored, 12)
        self.assertTrue(-5.0 < d.mean_value_add_dollars < 0.0)


class TestTheBlendArithmetic(unittest.TestCase):
    """`blend_load_forecast()` is what actually reaches the LP."""

    def test_zero_weight_needs_no_persistence_array_at_all(self):
        # test_zero_weight_needs_no_persistence_array_at_all: The no-change path must not be able to fail on the shape of an array
        self.assertEqual(blend_load_forecast([1.0, 2.0], [], 0.0), [1.0, 2.0])

        # test_a_length_mismatch_raises_rather_than_truncating: A silently short load array is the shape that costs real money: the
        with self.assertRaises(ValueError):
            blend_load_forecast([1.0, 2.0, 3.0], [1.0, 2.0], 0.5)

        # test_a_weight_outside_zero_to_one_raises
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                blend_load_forecast([1.0], [2.0], bad)


if __name__ == "__main__":
    unittest.main()
