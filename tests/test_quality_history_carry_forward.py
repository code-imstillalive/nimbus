"""nimbus issue #994 -- the integration was destroying the scored-day
table the Regret card is built to trust.

`publish_daily_quality_report()` builds a fresh attributes dict every
time it scores a day, and `ha_post_state` replaces attributes wholesale.
Nothing put `history` back, because nothing in this integration ever put
it there: it came from the standalone retrospective writer, and
`nimbus-regret-card.js` treats it as authoritative -- "the bible", per
the household's own 2026-09-05 instruction.

So every new score wiped it, and the card fell back per date to
re-scoring that day live. It says so in its own sub-header ("not yet in
table, showing live estimate"), which is the only reason this was ever
visible -- there is no error, no warning, and no `unavailable` sensor.

The two paths do not agree. Measured on a real install for 2026-09-15:

    j_star   -$9.45 live   vs   -$15.17 in the table
    j_ref     $4.43 live   vs     $4.79 in the table
    EPR      117.8% card   vs    103.66% sensor

`j_ref` differing is the diagnostic part: it is the battery-idle
baseline, so two scorers agreeing on their inputs agree on it whatever
they do with the battery. They did not.

This was also not new. The card's own header comment records EPR 71.5%
vs the table's 89.9% on 2026-09-04 with "real root cause not yet found".
The card was never comparing two scorers -- the table had been wiped, so
only the fallback was left.

These tests exercise the real `_carry_forward_quality_history()`.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer


def _entry(epr: float) -> dict:
    """A day_entry the size the real one is -- including the three
    24-row hourly reconstructions, so the trimming tests below prove the
    table stays small for the real payload rather than a toy one."""
    return {
        "epr": epr,
        "epr_pct": epr * 100,
        "j_ref": 4.7899,
        "j_ach": -15.8621,
        "j_star": -15.4645,
        "regret_dollars": -0.3977,
        "j_ref_hourly": {str(h): {"load_kw": 1.0} for h in range(24)},
        "j_ach_hourly": {str(h): {"load_kw": 1.0} for h in range(24)},
        "j_star_hourly": {str(h): {"load_kw": 1.0} for h in range(24)},
    }


class TestQualityHistoryCarryForward(unittest.TestCase):
    def test_a_first_ever_publish_still_produces_a_table(self):
        """No prior attributes at all -- a fresh install, or a read that
        failed. One entry, not a crash and not an empty dict, because an
        empty table is exactly the state that makes the card fall back
        forever."""
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(list(history), ["2026-09-15"])
        self.assertAlmostEqual(history["2026-09-15"]["epr"], 1.0196)

    def test_prior_days_survive_a_new_score(self):
        """The actual #994 defect. Pre-fix the new publish replaced the
        whole attributes dict and these days ceased to exist."""
        prior = {
            "history": {
                "2026-09-13": {"epr": 0.9234, "j_star": -10.0},
                "2026-09-14": {"epr": 0.9917, "j_star": -11.0},
            }
        }
        history = solver_writer._carry_forward_quality_history(
            prior, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(sorted(history), ["2026-09-13", "2026-09-14", "2026-09-15"])
        # Untouched, byte for byte -- entries written by the standalone
        # writer must not be reshaped by this function.
        self.assertEqual(history["2026-09-13"], {"epr": 0.9234, "j_star": -10.0})

    def test_only_the_five_card_fields_are_stored(self):
        """The three 24-row hourly reconstructions must never enter the
        table -- at ~5 KB per day they would pass the recorder's 16 KB
        cap (#944) within a week and cost the entity every attribute it
        has, not just this one."""
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(
            sorted(history["2026-09-15"]),
            ["epr", "j_ach", "j_ref", "j_star", "regret_dollars"],
        )

    def test_the_table_is_trimmed_to_the_retention_window(self):
        prior = {
            "history": {
                f"2026-{m:02d}-{d:02d}": {"epr": 0.9}
                for m in (6, 7, 8)
                for d in range(1, 29)
            }
        }
        self.assertGreater(
            len(prior["history"]), solver_writer._QUALITY_HISTORY_MAX_DAYS
        )
        history = solver_writer._carry_forward_quality_history(
            prior, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(len(history), solver_writer._QUALITY_HISTORY_MAX_DAYS)

    def test_trimming_drops_the_oldest_and_always_keeps_today(self):
        """ISO dates sort lexicographically, so "oldest" is a real
        comparison rather than string luck -- and the day just scored is
        the one the card asks for first."""
        prior = {
            "history": {
                f"2026-{m:02d}-{d:02d}": {"epr": 0.9}
                for m in (6, 7, 8)
                for d in range(1, 29)
            }
        }
        history = solver_writer._carry_forward_quality_history(
            prior, "2026-09-15", _entry(1.0196)
        )
        self.assertIn("2026-09-15", history)
        self.assertNotIn("2026-06-01", history)
        # 84 prior days + today, trimmed to 60, so the 25 oldest go and
        # the oldest survivor is a real, checkable date rather than
        # whatever happened to be left.
        self.assertEqual(min(history), "2026-06-26")

    def test_a_malformed_prior_table_costs_only_its_bad_entries(self):
        """This dict may have been written by another program entirely.
        One bad row must not cost the whole table -- losing it is what
        sends the card back to the fallback scorer."""
        prior = {
            "history": {
                "2026-09-13": {"epr": 0.9234},
                "2026-09-14": "not a dict",
                7: {"epr": 0.5},
            }
        }
        history = solver_writer._carry_forward_quality_history(
            prior, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(sorted(history), ["2026-09-13", "2026-09-15"])

    def test_a_non_dict_history_attribute_is_ignored_not_fatal(self):
        for junk in (None, "", [], 0, "history"):
            with self.subTest(junk=junk):
                history = solver_writer._carry_forward_quality_history(
                    {"history": junk}, "2026-09-15", _entry(1.0196)
                )
                self.assertEqual(list(history), ["2026-09-15"])

    def test_rescoring_the_same_day_replaces_rather_than_duplicates(self):
        """A rescore of an already-scored day -- exactly what
        `nimbus_load.compute_quality_report` does -- must update that
        day's row, not add a second one."""
        prior = {"history": {"2026-09-15": {"epr": 1.0387}}}
        history = solver_writer._carry_forward_quality_history(
            prior, "2026-09-15", _entry(1.0196)
        )
        self.assertEqual(list(history), ["2026-09-15"])
        self.assertAlmostEqual(history["2026-09-15"]["epr"], 1.0196)

    def test_a_partial_day_entry_stores_what_it_has(self):
        """A scorer that could not produce every field must still land a
        row -- a missing key is not a reason to lose the day."""
        history = solver_writer._carry_forward_quality_history(
            {}, "2026-09-15", {"epr": 0.5, "j_ref": 1.0}
        )
        self.assertEqual(sorted(history["2026-09-15"]), ["epr", "j_ref"])


if __name__ == "__main__":
    unittest.main()
