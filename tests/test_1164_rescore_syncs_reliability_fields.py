"""nimbus issue #1164: a rescore must move the reliability fields too,
not only the figures they qualify.

`rescore_quality_history()` syncs the headline to the rescored day
through two explicit lists -- `_QUALITY_HISTORY_FIELDS` (the five each
history row carries) and `_QUALITY_HEADLINE_ONLY_FIELDS`. Every
reliability field was in neither, so a rescore recomputed all of them
and then dropped them, leaving the caveat and the figure it qualifies
describing **different days**.

Same defect class as #1149, found the same way -- by running a real
rescore on a dev install rather than by review.

## Why this one is worse than the field that exposed it

v0.94.400 made `epr_reason` name the SoC cause instead of reading
`null`. On a rescored day it kept reading `null`, which is how this was
noticed. But the field that matters is `epr_reliable`: a stale `True`
asserts a recomputed figure is trustworthy when the recomputation
concluded it is not. **A reliability flag that can be wrong in the
reassuring direction is worse than no flag**, so the direction-specific
test below is the load-bearing one.

`nimbus_version` is here for the same reason in miniature: a rescore
that moves the figures and leaves the stamp makes the sensor misreport
which release produced what is on it -- the one thing #1120 added that
stamp to prevent.

## Why not in `_QUALITY_HISTORY_FIELDS`

That tuple is deliberately narrow: every history row is carried for up
to `_QUALITY_HISTORY_MAX_DAYS` inside one attribute payload, and
`tests/test_quality_report_attribute_size_budget.py` already projects
that payload to its retention cap against the Recorder's own limit.
These belong on top of the sensor, once, not in every row.

Same stubbed-dependency pattern as
test_1149_rescore_syncs_energy_decomposition.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 9, 20, 6, 0, tzinfo=BRISBANE)
YESTERDAY = "2026-09-19"
OLDER = "2026-09-18"

# Everything that qualifies the headline and must travel with it.
RELIABILITY_FIELDS = (
    "epr_reliable",
    "epr_reason",
    "epr_denominator_reason",
    "regret_reliable",
    "soc_discrepancy_reliable",
    "soc_discrepancy_reason",
    "soc_discrepancy_max_pct",
    "soc_discrepancy_mean_pct",
    "nimbus_version",
)


def _entry(epr, **reliability):
    base = {
        "epr": epr,
        "epr_pct": round(epr * 100.0, 2),
        "j_ref": 6.0,
        "j_ach": -24.0,
        "j_star": -25.0,
        "regret_dollars": 1.0,
    }
    base.update(reliability)
    return base


def _stale_attrs():
    """What the sensor carried BEFORE the rescore -- the shape observed
    on the dev install: a reliable-looking headline from an older run."""
    return {
        "state": "54.13",
        "attributes": {
            # The real sensor carries this, and the rescore's headline
            # sync is gated on `key == latest_date` -- without it the
            # sync block never runs and every assertion below would pass
            # or fail for the wrong reason.
            "latest_date": YESTERDAY,
            "epr": 0.5413,
            "epr_pct": 54.13,
            "j_ref": 1.9978,
            "j_ach": -2.3078,
            "j_star": -3.3304,
            "regret_dollars": 3.6485,
            "epr_reliable": True,
            "epr_reason": None,
            "epr_denominator_reason": None,
            "regret_reliable": True,
            "soc_discrepancy_reliable": True,
            "soc_discrepancy_reason": None,
            "soc_discrepancy_max_pct": 19.11,
            "soc_discrepancy_mean_pct": 10.02,
            "nimbus_version": "0.94.391",
            "history": {
                OLDER: {
                    "epr": 0.4681,
                    "j_ref": 4.9168,
                    "j_ach": -6.3004,
                    "j_star": -17.0034,
                    "regret_dollars": 12.7468,
                },
                YESTERDAY: {
                    "epr": 0.5413,
                    "j_ref": 1.9978,
                    "j_ach": -2.3078,
                    "j_star": -3.3304,
                    "regret_dollars": 3.6485,
                },
            },
        },
    }


def _rescored_entry():
    """What the recomputation concluded -- unreliable, with a reason."""
    return _entry(
        0.9175,
        epr_reliable=False,
        epr_reason="achieved_soc_unreliable:disagreement",
        epr_denominator_reason=None,
        regret_reliable=True,
        soc_discrepancy_reliable=False,
        soc_discrepancy_reason="disagreement",
        soc_discrepancy_max_pct=19.12,
        soc_discrepancy_mean_pct=10.02,
        nimbus_version="0.94.401",
    )


def _run(days, existing, side_effect, version="0.94.401"):
    posted = {}

    def _post(entity_id, state, attributes):
        posted["state"] = state
        posted["attributes"] = attributes

    with (
        patch.object(solver_writer, "ha_get", return_value=existing),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        patch.object(solver_writer, "_nimbus_version", return_value=version),
        patch.object(
            solver_writer, "_compute_report_for_window", side_effect=side_effect
        ),
    ):
        result = solver_writer.rescore_quality_history({}, NOW, days)
    return result, posted


class TestTheListItself(unittest.TestCase):
    def test_every_reliability_field_is_declared(self):
        for f in RELIABILITY_FIELDS:
            with self.subTest(field=f):
                self.assertIn(
                    f,
                    solver_writer._QUALITY_HEADLINE_ONLY_FIELDS,
                    f"{f!r} is recomputed by a rescore and then dropped, so the "
                    "headline keeps the previous computation's answer",
                )

    def test_they_are_not_in_the_per_row_tuple(self):
        """A history row is carried for a year inside one attribute
        payload -- see the recorder-cap guard. These belong on top of the
        sensor once, not in every row."""
        for f in RELIABILITY_FIELDS:
            with self.subTest(field=f):
                self.assertNotIn(f, solver_writer._QUALITY_HISTORY_FIELDS)


class TestRescoringThePublishedDay(unittest.TestCase):
    def setUp(self):
        self.result, self.posted = _run(
            1, _stale_attrs(), lambda cfg, s, e, allow_partial: _rescored_entry()
        )
        self.attrs = self.posted["attributes"]

    def test_the_rescore_actually_ran(self):
        self.assertEqual(self.result["rescored_count"], 1)
        self.assertTrue(self.result["latest_date_rescored"])

    def test_a_stale_reliable_flag_does_not_survive(self):
        """The load-bearing one. Before the rescore the sensor asserted
        the figure was trustworthy; the recomputation concluded it is
        not. Keeping the old flag would state the reassuring thing about
        a number that no longer supports it."""
        self.assertIs(
            self.attrs["epr_reliable"],
            False,
            "epr_reliable still says the headline is trustworthy after a "
            "rescore that concluded otherwise -- wrong in the reassuring "
            "direction, which is worse than having no flag",
        )

    def test_the_reason_moves_with_it(self):
        self.assertEqual(
            self.attrs["epr_reason"], "achieved_soc_unreliable:disagreement"
        )

    def test_every_reliability_field_matches_the_recomputation(self):
        expected = _rescored_entry()
        for f in RELIABILITY_FIELDS:
            with self.subTest(field=f):
                self.assertEqual(self.attrs[f], expected[f])

    def test_the_version_stamp_names_the_release_that_recomputed(self):
        """#1120's own point: a rescore that moves the figures and leaves
        the stamp makes the sensor misreport its own provenance.

        This one cannot come from the sync loop. `nimbus_version` is the
        single field in `_QUALITY_HEADLINE_ONLY_FIELDS` that the
        recomputed report never carries -- `_compute_report_for_window()`
        does not set it, the sensor adds it as a fallback only when the
        publish did not -- so the loop had nothing to copy and the
        rescore republished the PREVIOUS publish's value. Observed on the
        dev install under v0.94.401: a history row stamped 0.94.401
        sitting under a headline still claiming 0.94.391. It is now set
        from the running code explicitly.
        """
        self.assertEqual(self.attrs["nimbus_version"], "0.94.401")

    def test_the_stamp_comes_from_the_running_code_not_the_entry(self):
        """Pins the mechanism, not just the value: an entry carrying no
        version at all must still produce a correct stamp, which is the
        real case -- the report never carries one."""
        _result, posted = _run(
            1,
            _stale_attrs(),
            lambda cfg, s, e, allow_partial: _entry(0.9175),
            version="0.94.402",
        )
        self.assertEqual(
            posted["attributes"]["nimbus_version"],
            "0.94.402",
            "the rescue republished the previous publish's stamp -- the "
            "sensor now misreports which release produced its figures",
        )

    def test_the_figures_moved_too(self):
        """Guards the fixture: if the headline figures did not move
        either, the assertions above would pass for the wrong reason."""
        self.assertAlmostEqual(self.attrs["epr"], 0.9175)
        self.assertEqual(self.posted["state"], 91.75)


class TestRescoringAnOlderDayLeavesTheHeadlineAlone(unittest.TestCase):
    """Rescoring a day that is not the published one must not promote its
    reliability verdict onto a headline describing a different day."""

    def test_an_older_days_verdict_does_not_become_the_headline(self):
        def _side(cfg, start, end, allow_partial):
            if start.date().isoformat() == OLDER:
                return _entry(
                    0.1,
                    epr_reliable=False,
                    epr_reason="oracle_beaten",
                    soc_discrepancy_reason="something_else",
                    nimbus_version="0.94.401",
                )
            return None

        _result, posted = _run(2, _stale_attrs(), _side)
        attrs = posted["attributes"]
        self.assertIsNot(
            attrs.get("epr_reason"),
            "oracle_beaten",
            "an older day's reliability verdict was promoted onto the "
            "headline, which describes a different day",
        )
        self.assertEqual(attrs["epr"], 0.5413, "the headline figure moved too")


class TestAMissingFieldIsSurvivable(unittest.TestCase):
    def test_a_recomputation_without_the_field_leaves_what_was_there(self):
        """The sync is `if field in latest_entry` -- an entry that does
        not carry a field must not blank the published one."""
        _result, posted = _run(
            1,
            _stale_attrs(),
            lambda cfg, s, e, allow_partial: _entry(0.9175),
        )
        attrs = posted["attributes"]
        self.assertEqual(attrs["soc_discrepancy_max_pct"], 19.11)
        self.assertIs(attrs["epr_reliable"], True)

    def test_an_install_that_never_had_the_field_does_not_gain_an_empty_one(self):
        existing = _stale_attrs()
        for f in RELIABILITY_FIELDS:
            existing["attributes"].pop(f, None)
        _result, posted = _run(
            1, existing, lambda cfg, s, e, allow_partial: _entry(0.9175)
        )
        for f in RELIABILITY_FIELDS:
            if f == "nimbus_version":
                # Always set from the running code now, so it is the one
                # field an install legitimately GAINS -- see
                # test_the_stamp_comes_from_the_running_code_not_the_entry.
                continue
            with self.subTest(field=f):
                self.assertNotIn(f, posted["attributes"])


if __name__ == "__main__":
    unittest.main()
