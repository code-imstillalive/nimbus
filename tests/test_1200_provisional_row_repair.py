"""nimbus #1200: a day scored before its P2P settlement existed must not
stay that way once the settlement lands.

**The measured defect, on the reference household.** The settlement sensor
gained `2026-09-23` at 09:00 local. That day had been scored at 00:00,
when `real_p2p_dollars` was still 0 -- which also makes the
`real_p2p_volume_kwh > 0.01` bonus gate false, so `j_ref`, `j_ach` and
`j_star` go P2P-blind *together* and the day is priced as though the
household had no P2P arrangement at all. Four consecutive days published
that way:

    date        published EPR     settled export     after rescoring
    2026-09-21        51.9%             $14.91             89.8%
    2026-09-22        38.5%             $12.71             87.7%
    2026-09-23        36.4%             $13.46             87.2%
    2026-09-24        41.6%        (not yet settled)       41.6%

The scorer read WORST on the household's best-earning days, and read
88.6% on 2026-09-20, the one day that genuinely under-exported ($4.01).
Anti-correlated with the money.

**Why #1082 did not already cover this.** #1082 re-scores the currently
published day hourly while it is provisional, and that retry is reachable
only while `latest_date == yesterday_key` -- so it expires at local
midnight. On three consecutive measured days its last firing was 06:00,
three hours before the 09:00 settlement. A repair that has to win a race
against midnight will keep losing it: to a restart, a failover, a slow
provider, or the 06:00 retrain.

So the row itself now records that it is waiting, and the sweep has no
deadline. Two controls in that table above are the reason this is
believable as a fix rather than as inflation, and both are pinned here in
spirit: the five already-correct days did not move at all, and the one day
with no settlement is the one day that did not move.

The properties that carry the fix, each pinned below:

1. **The flag is written only when the day is genuinely provisional**, and
   self-clears when the day is re-scored with real figures.
2. **A report carrying no status at all writes no flag.** Defaulting to
   provisional would mark such rows for a repair that can never succeed --
   one missing field becoming a permanent MILP every cycle.
3. **The sweep pays a solve only once the real settlement entry exists.**
   A day whose settlement never arrives costs zero solves, not one per
   cycle, which is the #773/#757 executor-starvation shape.
4. **Presence, not a non-zero figure.** A settled day of genuinely zero
   export is a real result; reading it as "not arrived" would freeze that
   row for good.
5. **One day per cycle, oldest first**, so a backlog drains in order and
   two MILPs never land in one tick.
6. **The currently published day is left to #1082** -- rescoring it here
   as well would buy two solves for one day in one cycle.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import datetime
from typing import ClassVar
from unittest import mock

import _solver_path  # noqa: F401
import solver_writer


def _entry(status="applied", **extra):
    """A minimal scored-day report, provisional or not as asked."""
    base = {
        "epr": 0.9,
        "j_ref": 5.0,
        "j_ach": -20.0,
        "j_star": -21.0,
        "regret_dollars": 1.0,
    }
    if status is not None:
        base["real_p2p_settlement_status"] = status
    base.update(extra)
    return base


def _now(day=25, hour=10):
    return datetime(2026, 9, day, hour, 0, tzinfo=solver_writer.LOCAL_TZ)


class TestWhatCountsAsProvisional(unittest.TestCase):
    def test_the_two_statuses_that_can_change_with_time_are_provisional(self):
        for status in (
            "no_settlement_entry_for_this_date",
            "settlement_sensor_unreadable",
        ):
            with self.subTest(status=status):
                self.assertIs(
                    solver_writer._settlement_is_provisional(_entry(status)), True
                )

    def test_a_settled_or_permanently_settleable_day_is_not(self):
        for status in (
            "applied",
            "no_sensor_configured",
            "window_is_not_one_local_calendar_day",
        ):
            with self.subTest(status=status):
                self.assertIs(
                    solver_writer._settlement_is_provisional(_entry(status)), False
                )

    def test_no_status_at_all_returns_none_rather_than_guessing(self):
        # Property 2. A pre-#1016 report, or a dict that is not a report.
        self.assertIsNone(solver_writer._settlement_is_provisional(_entry(status=None)))
        self.assertIsNone(solver_writer._settlement_is_provisional({}))

    def test_it_reads_the_same_set_the_same_day_retry_reads(self):
        # Restating the membership test is the drift this scorer keeps
        # recording, so pin that both paths agree on every member.
        for status in solver_writer._PROVISIONAL_SETTLEMENT_STATUSES:
            with self.subTest(status=status):
                self.assertIs(
                    solver_writer._settlement_is_provisional(_entry(status)), True
                )
                self.assertFalse(
                    solver_writer._keep_published_quality_score(
                        {
                            "real_p2p_settlement_status": status,
                            "generated_at": _now(hour=8).isoformat(),
                        },
                        _now(hour=10),
                    )
                )


class TestTheRowRecordsThatItIsWaiting(unittest.TestCase):
    FIELD = "p"

    def test_a_provisional_day_is_flagged(self):
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-24", _entry("no_settlement_entry_for_this_date")
        )
        self.assertEqual(history["2026-09-24"][self.FIELD], 1)

    def test_a_settled_day_carries_no_flag(self):
        # Property 1's other half: the key is absent, not False -- the
        # byte budget is why, and absence is what the sweep reads as
        # "nothing to do".
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-23", _entry("applied")
        )
        self.assertNotIn(self.FIELD, history["2026-09-23"])

    def test_a_report_with_no_status_carries_no_flag(self):
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-23", _entry(status=None)
        )
        self.assertNotIn(self.FIELD, history["2026-09-23"])

    def test_rescoring_a_flagged_day_with_real_figures_clears_the_flag(self):
        # The self-clearing property. Same day, scored twice: provisional
        # first, settled second.
        prior = {
            "history": solver_writer._carry_forward_quality_history(
                {}, "2026-09-23", _entry("no_settlement_entry_for_this_date")
            )
        }
        self.assertEqual(prior["history"]["2026-09-23"][self.FIELD], 1)
        after = solver_writer._carry_forward_quality_history(
            prior, "2026-09-23", _entry("applied")
        )
        self.assertNotIn(self.FIELD, after["2026-09-23"])

    def test_prior_rows_are_not_back_dated(self):
        # Matching the "v" field's own rule: a row written before this
        # existed stays unflagged rather than being asserted about.
        prior = {"history": {"2026-09-01": {"epr": 0.5}}}
        after = solver_writer._carry_forward_quality_history(
            prior, "2026-09-24", _entry("no_settlement_entry_for_this_date")
        )
        self.assertNotIn(self.FIELD, after["2026-09-01"])


class TestTheSettlementPresenceGate(unittest.TestCase):
    CFG: ClassVar[dict] = {"solver_p2p_settlement_history_sensor": "sensor.settlement"}

    def _ha_get(self, history):
        return mock.Mock(return_value={"attributes": {"history": history}})

    def test_a_present_date_is_available(self):
        with mock.patch.object(
            solver_writer,
            "ha_get",
            self._ha_get({"2026-09-23": {"export_cost": 13.46}}),
        ):
            self.assertTrue(
                solver_writer._settlement_entry_exists(self.CFG, "2026-09-23")
            )

    def test_a_settled_day_of_zero_export_still_counts_as_arrived(self):
        # Property 4. Treating a real zero as "not yet" would freeze the
        # row for good.
        with mock.patch.object(
            solver_writer, "ha_get", self._ha_get({"2026-09-23": {"export_cost": 0.0}})
        ):
            self.assertTrue(
                solver_writer._settlement_entry_exists(self.CFG, "2026-09-23")
            )

    def test_an_absent_date_is_not_available(self):
        with mock.patch.object(
            solver_writer, "ha_get", self._ha_get({"2026-09-22": {"export_cost": 1.0}})
        ):
            self.assertFalse(
                solver_writer._settlement_entry_exists(self.CFG, "2026-09-23")
            )

    def test_no_configured_sensor_reads_as_unavailable_without_a_request(self):
        ha_get = mock.Mock()
        with mock.patch.object(solver_writer, "ha_get", ha_get):
            self.assertFalse(solver_writer._settlement_entry_exists({}, "2026-09-23"))
        ha_get.assert_not_called()

    def test_every_read_failure_reads_as_unavailable_rather_than_raising(self):
        for exc in (
            urllib.error.URLError("down"),
            KeyError("history"),
            TypeError("nope"),
            json.JSONDecodeError("bad", "{}", 0),
        ):
            with (
                self.subTest(exc=type(exc).__name__),
                mock.patch.object(solver_writer, "ha_get", mock.Mock(side_effect=exc)),
            ):
                self.assertFalse(
                    solver_writer._settlement_entry_exists(self.CFG, "2026-09-23")
                )


class TestTheRepairSweep(unittest.TestCase):
    CFG: ClassVar[dict] = {"solver_p2p_settlement_history_sensor": "sensor.settlement"}

    def _run(self, history, latest_date, settled, now=None):
        """Drive the sweep against a given table, returning (result, rescore mock)."""
        quality = {"attributes": {"history": history, "latest_date": latest_date}}

        def ha_get(entity_id):
            if entity_id == "sensor.settlement":
                return {"attributes": {"history": {d: {} for d in settled}}}
            return quality

        rescore = mock.Mock(return_value={"rescored": [], "skipped": []})
        with (
            mock.patch.object(solver_writer, "ha_get", side_effect=ha_get),
            mock.patch.object(
                solver_writer, "resolve_real_entity_id", side_effect=lambda e: e
            ),
            mock.patch.object(solver_writer, "rescore_quality_history", rescore),
        ):
            result = solver_writer.repair_provisional_quality_history(
                self.CFG, now or _now()
            )
        return result, rescore

    def test_a_flagged_day_whose_settlement_has_landed_is_rescored(self):
        result, rescore = self._run(
            {"2026-09-23": {"epr": 0.364, "p": 1}},
            latest_date="2026-09-24",
            settled=["2026-09-23"],
        )
        self.assertIsNotNone(result)
        self.assertEqual([r["date"] for r in result["repaired"]], ["2026-09-23"])
        # Named explicitly, so only that day's MILP is paid for.
        self.assertEqual(rescore.call_args.kwargs["only_dates"], {"2026-09-23"})

    def test_a_flagged_day_with_no_settlement_yet_costs_no_solve(self):
        # Property 3 -- the whole reason this is gated on evidence.
        result, rescore = self._run(
            {"2026-09-23": {"epr": 0.364, "p": 1}},
            latest_date="2026-09-24",
            settled=[],
        )
        self.assertIsNone(result)
        rescore.assert_not_called()

    def test_an_unflagged_row_is_never_touched(self):
        # The control: rescoring healthy rows is how a "fix" becomes
        # inflation. The five correct days must not move.
        result, rescore = self._run(
            {"2026-09-20": {"epr": 0.886}, "2026-09-19": {"epr": 0.917}},
            latest_date="2026-09-24",
            settled=["2026-09-19", "2026-09-20"],
        )
        self.assertIsNone(result)
        rescore.assert_not_called()

    def test_the_currently_published_day_is_left_to_1082(self):
        # Property 6.
        result, rescore = self._run(
            {"2026-09-24": {"epr": 0.416, "p": 1}},
            latest_date="2026-09-24",
            settled=["2026-09-24"],
        )
        self.assertIsNone(result)
        rescore.assert_not_called()

    def test_one_day_per_cycle_oldest_first(self):
        # Property 5.
        result, rescore = self._run(
            {
                "2026-09-23": {"epr": 0.364, "p": 1},
                "2026-09-21": {"epr": 0.519, "p": 1},
                "2026-09-22": {"epr": 0.385, "p": 1},
            },
            latest_date="2026-09-24",
            settled=["2026-09-21", "2026-09-22", "2026-09-23"],
        )
        self.assertEqual([r["date"] for r in result["repaired"]], ["2026-09-21"])
        self.assertEqual(rescore.call_count, 1)

    def test_a_day_beyond_the_rescore_ceiling_is_left_alone(self):
        old = "2026-07-01"
        result, rescore = self._run(
            {old: {"epr": 0.4, "p": 1}}, latest_date="2026-09-24", settled=[old]
        )
        self.assertIsNone(result)
        rescore.assert_not_called()

    def test_an_empty_or_missing_table_is_a_quiet_no_op(self):
        for history in ({}, None, "not a dict"):
            with self.subTest(history=history):
                result, rescore = self._run(
                    history, latest_date="2026-09-24", settled=["2026-09-23"]
                )
                self.assertIsNone(result)
                rescore.assert_not_called()

    def test_an_unreadable_quality_sensor_is_a_quiet_no_op(self):
        with (
            mock.patch.object(
                solver_writer, "ha_get", side_effect=urllib.error.URLError("down")
            ),
            mock.patch.object(
                solver_writer, "resolve_real_entity_id", side_effect=lambda e: e
            ),
        ):
            self.assertIsNone(
                solver_writer.repair_provisional_quality_history(self.CFG, _now())
            )


class TestOnlyDatesNarrowsWithoutPayingForTheRest(unittest.TestCase):
    def test_an_unnamed_day_never_reaches_the_oracle(self):
        """The parameter exists to avoid buying 30 MILPs to fix one row, so
        the filter must sit before the solve, not after it."""
        computed: list[str] = []

        def compute(cfg, day_start, day_end, allow_partial):
            computed.append(day_start.date().isoformat())
            return _entry("applied")

        with (
            mock.patch.object(
                solver_writer, "ha_get", return_value={"attributes": {}, "state": "90"}
            ),
            mock.patch.object(
                solver_writer, "_compute_report_for_window", side_effect=compute
            ),
            mock.patch.object(solver_writer, "ha_post_state"),
        ):
            solver_writer.rescore_quality_history(
                {}, _now(day=25), 10, only_dates={"2026-09-21"}
            )

        self.assertEqual(computed, ["2026-09-21"])

    def test_without_only_dates_every_day_in_the_window_is_scored(self):
        # The control: the filter must not change default behaviour.
        computed: list[str] = []

        def compute(cfg, day_start, day_end, allow_partial):
            computed.append(day_start.date().isoformat())
            return _entry("applied")

        with (
            mock.patch.object(
                solver_writer, "ha_get", return_value={"attributes": {}, "state": "90"}
            ),
            mock.patch.object(
                solver_writer, "_compute_report_for_window", side_effect=compute
            ),
            mock.patch.object(solver_writer, "ha_post_state"),
        ):
            solver_writer.rescore_quality_history({}, _now(day=25), 3)

        self.assertEqual(computed, ["2026-09-24", "2026-09-23", "2026-09-22"])


if __name__ == "__main__":
    unittest.main()
