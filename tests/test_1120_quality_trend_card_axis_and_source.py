"""nimbus #1120: the shipped quality-trend card must not clip the days
worth looking at, and must keep reading the scorer's own table.

`lovelace_add_nimbus_solver_quality_card.py` installs two cards on a real
household's dashboard, and until now nothing tested it at all.

## The defect

The EPR axis was pinned `min: 0, max: 100`. **EPR is not bounded to that
range**, and both ways out of it are the interesting days:

- **Above 100** whenever the scored day beat the oracle -- the
  `oracle_beaten` shape. Measured on the reference household:
  **106.4%** on 2026-09-16, and **115.87** in that install's own recorded
  daily statistics.
- **Below 0** whenever the day did worse than doing nothing at all.
  **-5.76%** on 2026-09-06, recorded in this repo's own worklog.

A fixed axis does not drop those points, which would at least be visible.
It renders them flush against the boundary, so a 106.4% day reads as a
perfect 100% and a -5.76% day reads as a zero. The chart looks fine and
is wrong in the direction that matters: it hides anomalies while
reporting the ordinary days accurately.

## The source

The same card is also the one place this project gets "which data does
the trend read" right, and that is worth pinning rather than assuming.
The household's own hand-edited copy had drifted to recorder **long-term
statistics** on the flattened `sensor.nimbus_quality_epr`, which carries
two defects the shipped card does not: the statistics bucket for day D
holds day **D-1**'s score (the sensor holds "the latest scored day"), and
a bucket spanning a mid-day change **blends** the two values. Reading the
report's own `history` dict, keyed by the real scored date, avoids both.

## The scaling

`history` stores `epr` as a **fraction** (0.9481), while the axis is
labelled `EPR %`. The `* 100` in the data generator is therefore
load-bearing, not cosmetic -- drop it and every point renders at roughly
zero on a percentage axis. Pinned because it is exactly the kind of
detail a well-meaning edit removes.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "real-world-integration"
    / "files"
    / "lovelace_add_nimbus_solver_quality_card.py"
)


def _load_module():
    """Import the installer script by path.

    It lives outside any package and is normally run as a standalone
    deployment script, so a plain import will not find it.
    """
    spec = importlib.util.spec_from_file_location("_quality_card_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestTheEprAxisDoesNotClipRealDays(unittest.TestCase):
    def setUp(self):
        self.card = _load_module().QUALITY_TREND_CHART_CARD
        self.epr_axis = next(axis for axis in self.card["yaxis"] if axis["id"] == "epr")

    def test_there_is_no_hard_ceiling(self):
        self.assertNotIn(
            "max",
            self.epr_axis,
            "the EPR axis has a hard maximum again. EPR exceeds 100 "
            "whenever the day beat the oracle -- 106.4% and 115.87 are "
            "both real readings from the reference household -- and a "
            "fixed ceiling renders those flush against the top, which "
            "reads as a perfect day rather than as the anomaly it is",
        )

    def test_there_is_no_hard_floor(self):
        self.assertNotIn(
            "min",
            self.epr_axis,
            "the EPR axis has a hard minimum again. A day worse than "
            "doing nothing scores NEGATIVE EPR (-5.76% on 2026-09-06, in "
            "this repo's own worklog), and a floor of 0 renders that as a "
            "zero day -- hiding the single most actionable result the "
            "scorer can produce",
        )

    def test_the_regret_axis_is_also_unclamped(self):
        """Regret goes negative by the same mechanism -- it is
        `j_ach - j_star_evaluator`, and beating the oracle makes it
        negative (-$0.79 on 2026-09-16)."""
        regret_axis = next(
            axis for axis in self.card["yaxis"] if axis["id"] == "regret"
        )
        self.assertNotIn("min", regret_axis)
        self.assertNotIn("max", regret_axis)

    def test_the_regret_axis_keeps_cent_resolution(self):
        """Regret lives in the +-$5 range -- $1.67 and -$0.79 are both
        real readings. Whole-dollar axis labels round almost every day to
        0 or -1, so the axis stops distinguishing a good day from a bad
        one while still looking like a working chart."""
        regret_axis = next(
            axis for axis in self.card["yaxis"] if axis["id"] == "regret"
        )
        self.assertEqual(regret_axis["decimals"], 2)


class TestItReadsTheScorersOwnTable(unittest.TestCase):
    """Not recorder statistics. See this file's docstring for the two
    defects that choice avoids."""

    def setUp(self):
        self.card = _load_module().QUALITY_TREND_CHART_CARD

    def test_both_series_read_the_report_entity(self):
        for series in self.card["series"]:
            with self.subTest(series=series["name"]):
                self.assertEqual(
                    series["entity"], "sensor.nimbus_solver_quality_report"
                )

    def test_both_series_use_the_history_attribute(self):
        for series in self.card["series"]:
            with self.subTest(series=series["name"]):
                self.assertIn("entity.attributes.history", series["data_generator"])

    def test_neither_series_uses_recorder_statistics(self):
        """`statistics:` on a series makes apexcharts read long-term
        statistics instead of the data generator -- the exact drift the
        household's own hand-edited card had taken, which put every point
        one day late and blended the upgrade day."""
        for series in self.card["series"]:
            with self.subTest(series=series["name"]):
                self.assertNotIn(
                    "statistics",
                    series,
                    "this series reads recorder statistics. The bucket "
                    "for day D holds day D-1's score, and a bucket "
                    "spanning a mid-day change blends the two values. "
                    "The report's own history dict is keyed by the real "
                    "scored date and has neither problem.",
                )

    def test_the_dates_are_sorted_before_plotting(self):
        """A dict's insertion order is whatever the writer happened to
        use; an unsorted series draws the line backwards across the
        chart."""
        for series in self.card["series"]:
            with self.subTest(series=series["name"]):
                self.assertIn(".sort(", series["data_generator"])


class TestTheFractionToPercentScalingSurvives(unittest.TestCase):
    """`history` stores epr as a fraction (0.9481) against an axis
    labelled `EPR %`."""

    def setUp(self):
        self.card = _load_module().QUALITY_TREND_CHART_CARD

    def test_epr_is_multiplied_by_one_hundred(self):
        epr_series = next(s for s in self.card["series"] if s["name"] == "EPR")
        self.assertIn(
            "* 100",
            epr_series["data_generator"],
            "the fraction-to-percent scaling is gone. `history` stores "
            "epr as 0.9481, so without this every point renders at "
            "roughly zero on a percentage axis",
        )

    def test_regret_is_not_scaled_by_one_hundred(self):
        """Regret is already dollars. The `/ 100` there is a 2dp round,
        paired with its own `* 100` -- a different operation that must not
        be mistaken for the EPR scaling and 'made consistent'."""
        regret = next(s for s in self.card["series"] if s["name"] == "Regret $")
        self.assertIn("/ 100", regret["data_generator"])


if __name__ == "__main__":
    unittest.main()
