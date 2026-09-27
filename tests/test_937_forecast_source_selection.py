"""nimbus issue #937 item 4: persistence as a real fallback.

That issue's headline, measured on the reference household's own real data:

    days scored                                 14
    naive persistence beat Nimbus's forecast    11
    mean nimbus_value_add_dollars               -$0.71/day
    worst day                                   -$3.42 (2026-08-30)

and its own item 4, the last of four "what would move this forward" actions and
the only one nothing had been built for:

    **Consider whether persistence deserves to be a real fallback.** If a
    household's own data shows persistence winning consistently, the honest
    product answer might be to use it, or to blend.

## What these tests are actually defending

**That the default cannot act.** `TestOffCannotBeTalkedIntoActing` is the most
important class in this file, and the reason it uses the issue's own real
fourteen-day series rather than a synthetic one: the input that would most
strongly justify switching to persistence is exactly the input the default
policy must ignore. This code sits on the live dispatch path of a household
running real battery dispatch and real P2P money, so "off is a no-op" has to be
a property with a test, not a default with a comment.

**That the escalation gates are ordered and each one is reachable.** There are
five distinct ways this module answers "keep using the ML forecast", and a
diagnostic that goes quiet without saying which gate closed is the defect
`epr_reason` (#1162) and `forecast_regret_reason` (#1307) were both written to
fix. Every gate has a test that pins its own reason code.

**That the blend weight is the evidence.** On #937's own series the weight comes
out at exactly 11/14. If someone later replaces that with a tunable dial, this
test fails and says why in its own name.

Loaded BY PATH, so "this module has zero Home Assistant imports" is proven by
the test running at all rather than asserted in a docstring -- the same pattern
`test_1241_detect_a_wrong_power_sign_convention.py` and
`test_467_calendar_trip_windows.py` use, for the same reason.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
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
    spec = importlib.util.spec_from_file_location("forecast_source_selection", _MODULE_PATH)
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


class TestTheFixtureReallyIsTheIssuesOwnMeasurement(unittest.TestCase):
    """If this drifts, every other test in the file is measuring something
    other than what #937 measured."""

    def test_it_reproduces_the_issues_own_headline(self):
        d = _issue_decision("measure")
        self.assertEqual(d.days_scored, 14)
        self.assertEqual(d.days_persistence_won, 11)
        self.assertAlmostEqual(d.mean_value_add_dollars, -0.71, places=2)


class TestOffCannotBeTalkedIntoActing(unittest.TestCase):
    """The default policy, against the most persuasive input that exists.

    This is the safety property the whole design rests on: the reference
    household dispatches a real battery through this code path, and an install
    that has not opted in must be byte-identical to one running the release
    before this existed.
    """

    def test_the_default_policy_is_off(self):
        # Not a stylistic point. If the default ever became anything else, an
        # upgrade would silently change what a live household dispatches on.
        d = select_forecast_source(value_add_by_day=ISSUE_937_SERIES, today=TODAY)
        self.assertEqual(d.policy, fss.POLICY_OFF)
        self.assertEqual(d.source, fss.SOURCE_ML)
        self.assertEqual(d.persistence_weight, 0.0)

    def test_off_ignores_a_record_that_screams_persistence(self):
        d = _issue_decision(fss.POLICY_OFF)
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_POLICY_OFF)

    def test_off_does_not_even_read_the_record(self):
        """`days_scored` comes back 0, not 14.

        Deliberate, and worth a test of its own: it is the observable proof that
        the short-circuit happens BEFORE the evidence is consulted, which is what
        makes the no-op claim about the whole code path rather than only about
        the returned weight.
        """
        d = _issue_decision(fss.POLICY_OFF)
        self.assertEqual(d.days_scored, 0)
        self.assertEqual(d.days_persistence_won, 0)
        self.assertIsNone(d.mean_value_add_dollars)

    def test_an_unrecognised_policy_is_off_and_says_so(self):
        # Never silently normalised: an install whose select entity somehow
        # holds a stale option must keep dispatching on the ML forecast AND be
        # diagnosable, rather than looking like a deliberate `off`.
        d = _issue_decision("aggressive")
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.policy, "aggressive")
        self.assertEqual(d.reason, fss.REASON_POLICY_UNRECOGNISED)

    def test_every_policy_name_the_select_offers_is_understood(self):
        # The entity's options and this module's branches are two lists that
        # must not drift -- the #837 silent-no-op shape, one layer up.
        for policy in fss.POLICIES:
            d = _issue_decision(policy)
            self.assertNotEqual(
                d.reason,
                fss.REASON_POLICY_UNRECOGNISED,
                f"{policy!r} is offered but not understood",
            )


class TestMeasureIsNotActing(unittest.TestCase):
    """`measure` is the honest middle position: publish the recommendation,
    change nothing. It is the state #937 was effectively already in, made
    deliberate and visible."""

    def test_measure_keeps_the_ml_forecast(self):
        d = _issue_decision(fss.POLICY_MEASURE)
        self.assertEqual(d.source, fss.SOURCE_ML)
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_PERSISTENCE_FAVOURED_MEASURE_ONLY)

    def test_measure_still_publishes_the_full_evidence(self):
        """The whole point of the mode. A weight of 0.0 next to an empty
        evidence set would be indistinguishable from `off`."""
        d = _issue_decision(fss.POLICY_MEASURE)
        self.assertEqual(d.days_scored, 14)
        self.assertEqual(d.days_persistence_won, 11)
        self.assertLess(d.mean_value_add_dollars, -0.5)


class TestTheGatesBeforeAnyPolicyActs(unittest.TestCase):
    """Four reasons to keep the ML forecast that have nothing to do with the
    policy asked for. Each is separately reachable and separately named."""

    def test_an_empty_record_is_not_evidence(self):
        d = select_forecast_source(
            value_add_by_day={}, today=TODAY, policy=fss.POLICY_PERSISTENCE
        )
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_NO_TRAILING_RECORD)

    def test_a_thin_sample_is_refused_even_when_persistence_won_every_day(self):
        """#937's own caveat is that 14 days is already a small sample. Three
        days, even three unanimous ones, is a verdict about three days."""
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

    def test_a_winning_forecaster_is_left_alone(self):
        d = select_forecast_source(
            value_add_by_day={
                f"2026-09-{day:02d}": +0.5 for day in range(5, 13)
            },
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
        )
        self.assertEqual(d.days_persistence_won, 0)
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_FORECAST_NOT_LOSING)

    def test_a_forecaster_losing_by_pennies_is_left_alone(self):
        """The threshold's whole purpose. A mean of -3c/day is a forecaster
        indistinguishable from persistence, and #937's own series has three days
        inside +-10c -- not a reason to change what a live battery dispatches
        on."""
        d = select_forecast_source(
            value_add_by_day={
                f"2026-09-{day:02d}": -0.03 for day in range(5, 13)
            },
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
        )
        self.assertEqual(d.days_persistence_won, 8)  # every day, technically
        self.assertEqual(d.persistence_weight, 0.0)
        self.assertEqual(d.reason, fss.REASON_FORECAST_NOT_LOSING)

    def test_exactly_at_the_threshold_is_not_losing(self):
        # A boundary that has to be decided somewhere; pinned so it is decided
        # once rather than re-derived from the comparison operator.
        d = select_forecast_source(
            value_add_by_day={
                f"2026-09-{day:02d}": -0.10 for day in range(5, 13)
            },
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
            loss_threshold_dollars=0.10,
        )
        self.assertEqual(d.reason, fss.REASON_FORECAST_NOT_LOSING)

    def test_a_negative_threshold_is_read_as_a_magnitude(self):
        """A household (or a future number entity) handing this -0.10 meant the
        same thing as 0.10. Reading it literally would invert the gate and let
        a WINNING forecaster be replaced by persistence."""
        d = _issue_decision(fss.POLICY_PERSISTENCE, loss_threshold_dollars=-0.10)
        self.assertEqual(d.persistence_weight, 1.0)
        winners = select_forecast_source(
            value_add_by_day={
                f"2026-09-{day:02d}": +0.5 for day in range(5, 13)
            },
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
            loss_threshold_dollars=-0.10,
        )
        self.assertEqual(winners.persistence_weight, 0.0)


class TestActingOnTheIssuesOwnNumbers(unittest.TestCase):
    """What the two acting policies do with the measurement that motivated
    them."""

    def test_persistence_takes_over_outright(self):
        d = _issue_decision(fss.POLICY_PERSISTENCE)
        self.assertEqual(d.source, fss.SOURCE_PERSISTENCE)
        self.assertEqual(d.persistence_weight, 1.0)
        self.assertEqual(d.reason, fss.REASON_PERSISTENCE_FAVOURED_FULL)

    def test_the_blend_weight_is_the_fraction_of_days_persistence_won(self):
        """11/14 on #937's own data -- the evidence, not a dial.

        If this ever becomes configurable, this test fails and its name says
        what was given up: a dial invites picking the number that produces a
        liked answer, and no measurement supports any particular value.
        """
        d = _issue_decision(fss.POLICY_BLEND)
        self.assertEqual(d.source, fss.SOURCE_BLEND)
        self.assertAlmostEqual(d.persistence_weight, 11 / 14, places=12)
        self.assertEqual(d.reason, fss.REASON_PERSISTENCE_FAVOURED_BLEND)

    def test_a_blend_only_reaches_one_if_persistence_won_every_day(self):
        d = select_forecast_source(
            value_add_by_day={
                f"2026-09-{day:02d}": -1.0 for day in range(5, 13)
            },
            today=TODAY,
            policy=fss.POLICY_BLEND,
        )
        self.assertEqual(d.persistence_weight, 1.0)
        # And the LABEL follows the weight, never the branch.
        self.assertEqual(d.source, fss.SOURCE_PERSISTENCE)

    def test_the_source_label_never_contradicts_the_weight(self):
        for policy in fss.POLICIES:
            d = _issue_decision(policy)
            if d.persistence_weight == 0.0:
                self.assertEqual(d.source, fss.SOURCE_ML)
            elif d.persistence_weight >= 1.0:
                self.assertEqual(d.source, fss.SOURCE_PERSISTENCE)
            else:
                self.assertEqual(d.source, fss.SOURCE_BLEND)


class TestTheTrailingWindowIsCalendarBased(unittest.TestCase):
    """A calendar window, not "the most recent N rows".

    Rows are legitimately sparse -- the day-ahead decomposition is written only
    on days that had a snapshot and usable previous-day history -- so
    most-recent-N can reach back months and present a stale verdict as current.
    A calendar window thins the sample instead, `days_scored` says so, and the
    minimum-days gate then refuses to act on it.
    """

    def test_days_outside_the_window_are_not_counted(self):
        d = select_forecast_source(
            value_add_by_day=ISSUE_937_SERIES,
            today=TODAY,
            policy=fss.POLICY_MEASURE,
            trailing_days=7,
        )
        # A 7-day window from 2026-09-13 reaches back to 09-07 inclusive, and
        # the series has no row for 09-13 itself (today is not yet scored), so
        # six rows: 09-07 through 09-12.
        self.assertEqual(d.days_scored, 6)

    def test_a_stale_record_thins_rather_than_lying(self):
        """Fourteen unanimous days, all two months old. Most-recent-N would act
        on them; a calendar window sees nothing and refuses."""
        stale = {f"2026-07-{day:02d}": -2.0 for day in range(1, 15)}
        d = select_forecast_source(
            value_add_by_day=stale, today=TODAY, policy=fss.POLICY_PERSISTENCE
        )
        self.assertEqual(d.days_scored, 0)
        self.assertEqual(d.reason, fss.REASON_NO_TRAILING_RECORD)

    def test_a_future_dated_row_is_not_evidence(self):
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

    def test_a_zero_or_negative_window_reads_nothing(self):
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

    def test_a_non_finite_value_cannot_poison_the_mean(self):
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

    def test_a_record_of_nothing_but_garbage_is_no_record(self):
        d = select_forecast_source(
            value_add_by_day={"nope": "nope"},  # type: ignore[dict-item]
            today=TODAY,
            policy=fss.POLICY_PERSISTENCE,
        )
        self.assertEqual(d.reason, fss.REASON_NO_TRAILING_RECORD)


class TestTheBlendArithmetic(unittest.TestCase):
    """`blend_load_forecast()` is what actually reaches the LP."""

    def test_zero_weight_returns_the_ml_values_unchanged(self):
        ml = [1.0, 2.0, 3.0]
        out = blend_load_forecast(ml, [9.0, 9.0, 9.0], 0.0)
        self.assertEqual(out, ml)

    def test_zero_weight_needs_no_persistence_array_at_all(self):
        """The no-change path must not be able to fail on the shape of an array
        it does not use -- a length check firing at weight 0.0 would turn a
        no-op into a crash on the live dispatch path."""
        self.assertEqual(blend_load_forecast([1.0, 2.0], [], 0.0), [1.0, 2.0])

    def test_full_weight_returns_persistence(self):
        self.assertEqual(
            blend_load_forecast([1.0, 2.0, 3.0], [9.0, 8.0, 7.0], 1.0),
            [9.0, 8.0, 7.0],
        )

    def test_a_partial_weight_interpolates_elementwise(self):
        out = blend_load_forecast([0.0, 10.0], [10.0, 0.0], 0.25)
        self.assertAlmostEqual(out[0], 2.5)
        self.assertAlmostEqual(out[1], 7.5)

    def test_a_length_mismatch_raises_rather_than_truncating(self):
        """A silently short load array is the shape that costs real money: the
        periods past the end become whatever the LP's padding does, on an input
        the household believes is a forecast."""
        with self.assertRaises(ValueError):
            blend_load_forecast([1.0, 2.0, 3.0], [1.0, 2.0], 0.5)

    def test_a_weight_outside_zero_to_one_raises(self):
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                blend_load_forecast([1.0], [2.0], bad)

    def test_the_result_is_a_plain_list(self):
        """The caller applies its own in-place period-0 live anchor to this --
        a measured meter reading, which must survive whatever the policy did."""
        out = blend_load_forecast([1.0, 2.0], [3.0, 4.0], 0.5)
        self.assertIsInstance(out, list)
        out[0] = 99.0  # must not raise


class TestTheModuleStaysPure(unittest.TestCase):
    """Loading by path already proves there is no HA import at runtime. This
    asserts it at the source level too, so the intent survives someone adding
    an import that happens to be installed in the test environment."""

    def test_it_imports_nothing_from_home_assistant_or_numpy(self):
        tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            self.assertFalse(
                name.startswith("homeassistant") or name.startswith("numpy"),
                f"{name} would end this module's portability",
            )


if __name__ == "__main__":
    unittest.main()
