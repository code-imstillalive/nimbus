"""nimbus #1167: a rescore must publish what the daily scorer would have
published for that day -- all of it, not a list of field names.

## The defect class this closes

Four instances in two days, all the same shape: a field is added to the
quality report, the rescore path is not updated, and the field is
recomputed and then silently discarded.

===================  ===========================================
#1149                `energy_decomposition`
#1164                the eight reliability fields
v0.94.402            `nimbus_version`
#1167 (this)         the ENTIRE hourly and diagnostic payload
===================  ===========================================

Each earlier fix added names to a hand-maintained tuple. The names were
never the problem; enumerating was.

## What it looked like

Dev install, v0.94.402, after a `rescore_history` that reported success
and wrote a correctly stamped row::

    regret_dollars       2.0674   <- the rescored day
    sum(hourly_regret)   3.6485   <- the previous computation

Those are the same quantity computed two ways, disagreeing by 76%,
because one moved and the other did not. `hourly_regret` is what
`nimbus-regret-card.js` reads, so the card rendered one day's hours
under another day's total -- with no error, no warning, and internally
plausible numbers on both halves.

## The rule, stated once

`publish_daily_quality_report()` has always spread the recomputed report
wholesale (`**day_entry`). The rescore is now the same. So the two paths
make the same promise: **the headline describes the day that was just
scored, entirely.**

## Why the sweep below is the load-bearing test

`TestEveryPublishedFieldSurvivesARescore` drives a rescore whose
recomputed report carries a deliberately DIFFERENT value for every key,
and asserts every one of them reaches the published attributes. It does
not enumerate the report's fields -- it discovers them -- so a field
added tomorrow is covered without anyone remembering to add it here.

That is the whole point. A test that listed the fields would need the
same maintenance the code just stopped needing, and would fail the same
way.
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

# Keys a rescore legitimately does NOT take from the recomputed day, each
# with the reason. Anything else appearing here later is a decision, not
# a drift.
EXEMPT = {
    # Merged across every rescored day by _carry_forward_quality_history();
    # `latest_entry` is one day and must not replace the table.
    "history",
    # Never carried by the recomputed report -- set explicitly from the
    # running code instead. See the #1164 follow-up.
    "nimbus_version",
}


def _report(marker: float):
    """A recomputed report whose every value is distinguishable from the
    previously-published one, so 'it arrived' cannot pass by coincidence."""
    return {
        "epr": 0.9175,
        "epr_pct": 91.75,
        "j_ref": 5.3144,
        "j_ach": -17.6635,
        "j_star": -17.8664,
        "regret_dollars": 2.0674,
        "hourly_regret": {str(h): marker for h in range(24)},
        "j_ach_hourly": {
            f"2026-09-19T{h:02d}:00": {"soc_pct": marker} for h in range(3)
        },
        "achieved_energy_in_kwh": marker,
        "achieved_energy_out_kwh": marker,
        "achieved_energy_by_battery": {"home": {"in_kwh": marker}},
        "soc_discrepancy_hourly": [{"hour": "2026-09-19T00:00", "gap_pct": marker}],
        "soc_discrepancy_max_pct": marker,
        "epr_reliable": False,
        "epr_reason": "achieved_soc_unreliable:disagreement",
        "real_p2p_dollars": marker,
        "tracking_fidelity": marker,
        "load_nowcast_skill_coverage": marker,
        "energy_decomposition": {"achieved": {"charge_kwh": marker}},
    }


def _stale_attrs():
    """The previously-published payload, every value deliberately wrong
    for the day about to be rescored."""
    stale = {k: 1.0 for k in _report(1.0)}
    stale.update(
        {
            "latest_date": YESTERDAY,
            "epr": 0.5413,
            "epr_pct": 54.13,
            "regret_dollars": 3.6485,
            "hourly_regret": {str(h): 9.9 for h in range(24)},
            "epr_reliable": True,
            "epr_reason": None,
            "nimbus_version": "0.94.391",
            "history": {
                YESTERDAY: {
                    "epr": 0.5413,
                    "j_ref": 1.9978,
                    "j_ach": -2.3078,
                    "j_star": -3.3304,
                    "regret_dollars": 3.6485,
                }
            },
        }
    )
    return {"state": "54.13", "attributes": stale}


def _run(existing, entry, version="0.94.403", days=1):
    posted = {}

    def _post(entity_id, state, attributes):
        posted["state"] = state
        posted["attributes"] = attributes

    with (
        patch.object(solver_writer, "ha_get", return_value=existing),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        patch.object(solver_writer, "_nimbus_version", return_value=version),
        patch.object(
            solver_writer,
            "_compute_report_for_window",
            side_effect=lambda cfg, s, e, allow_partial: entry,
        ),
    ):
        result = solver_writer.rescore_quality_history({}, NOW, days)
    return result, posted


class TestEveryPublishedFieldSurvivesARescore(unittest.TestCase):
    """The load-bearing test. Discovers the report's fields rather than
    listing them, so tomorrow's new field is covered automatically."""

    def setUp(self):
        self.entry = _report(42.0)
        self.result, self.posted = _run(_stale_attrs(), self.entry)
        self.attrs = self.posted["attributes"]

    def test_the_rescore_actually_ran(self):
        self.assertEqual(self.result["rescored_count"], 1)
        self.assertTrue(self.result["latest_date_rescored"])

    def test_every_recomputed_field_reaches_the_headline(self):
        missing = {
            k: (self.attrs.get(k), v)
            for k, v in self.entry.items()
            if k not in EXEMPT and self.attrs.get(k) != v
        }
        self.assertEqual(
            missing,
            {},
            "a rescore recomputed these fields and published something else "
            "instead -- the headline is describing two different days at "
            "once. Either the rescore must carry the field, or it belongs "
            "in this file's EXEMPT set with a reason.",
        )

    def test_the_sweep_is_not_vacuous(self):
        """Every asserted field must genuinely have differed from what was
        published before, or the test above proves nothing."""
        stale = _stale_attrs()["attributes"]
        undifferentiated = [
            k for k, v in self.entry.items() if k not in EXEMPT and stale.get(k) == v
        ]
        self.assertEqual(
            undifferentiated,
            [],
            "these fixture fields are identical before and after, so the "
            "sweep would pass whether or not the rescore carried them",
        )
        self.assertGreater(len(self.entry), 12, "the fixture stopped being broad")


class TestTheSpecificRegression(unittest.TestCase):
    """#1167's own symptom, named so a future reader finds it by the
    thing they observed rather than by the mechanism."""

    def test_the_hourly_series_matches_the_headline_it_sits_under(self):
        entry = _report(42.0)
        entry["hourly_regret"] = {str(h): 2.0674 / 24 for h in range(24)}
        _result, posted = _run(_stale_attrs(), entry)
        attrs = posted["attributes"]
        self.assertAlmostEqual(
            sum(attrs["hourly_regret"].values()),
            attrs["regret_dollars"],
            places=3,
            msg="the Regret card's hours do not sum to the regret printed "
            "above them -- one of the two is from a different computation",
        )


class TestWhatMustNotMove(unittest.TestCase):
    def test_the_history_table_is_not_replaced_by_one_day(self):
        _result, posted = _run(_stale_attrs(), _report(42.0))
        self.assertIn(YESTERDAY, posted["attributes"]["history"])

    def test_the_version_stamp_comes_from_the_running_code(self):
        _result, posted = _run(_stale_attrs(), _report(42.0), version="0.94.403")
        self.assertEqual(posted["attributes"]["nimbus_version"], "0.94.403")

    def test_the_state_channel_follows_the_rescored_day(self):
        _result, posted = _run(_stale_attrs(), _report(42.0))
        self.assertEqual(posted["state"], 91.75)

    def test_fields_the_report_does_not_carry_are_left_alone(self):
        """A rescore must not blank attributes that belong to other
        publishers (unit, state_class, friendly_name)."""
        existing = _stale_attrs()
        existing["attributes"]["unit_of_measurement"] = "%"
        existing["attributes"]["state_class"] = "measurement"
        _result, posted = _run(existing, _report(42.0))
        self.assertEqual(posted["attributes"]["unit_of_measurement"], "%")
        self.assertEqual(posted["attributes"]["state_class"], "measurement")


class TestRescoringAnOlderDayLeavesTheHeadlineAlone(unittest.TestCase):
    def test_an_older_day_does_not_become_the_headline(self):
        """The wholesale spread is gated on `key == latest_date`. Losing
        that gate would promote an older day's entire payload onto a
        headline describing a different day -- a worse version of the bug
        being fixed."""
        older = "2026-09-18"
        existing = _stale_attrs()
        existing["attributes"]["history"][older] = {"epr": 0.4681}

        def _side(cfg, start, end, allow_partial):
            if start.date().isoformat() == older:
                return _report(999.0)
            return None

        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(solver_writer, "ha_post_state", side_effect=lambda *a: None),
            patch.object(solver_writer, "_nimbus_version", return_value="0.94.403"),
            patch.object(
                solver_writer, "_compute_report_for_window", side_effect=_side
            ),
        ):
            posted = {}

            def _post(entity_id, state, attributes):
                posted["attributes"] = attributes

            with patch.object(solver_writer, "ha_post_state", side_effect=_post):
                solver_writer.rescore_quality_history({}, NOW, 2)

        self.assertEqual(
            posted["attributes"]["regret_dollars"],
            3.6485,
            "an older day's payload was promoted onto the headline",
        )


if __name__ == "__main__":
    unittest.main()
