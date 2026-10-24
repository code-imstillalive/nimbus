"""The scorer does not measure pack capacity from a power sensor (#1172)
(#1837).
"""

from __future__ import annotations

import pathlib
import unittest

import solver_writer as sw
from _writer_source import writer_source

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-regret-card.js"
)


def _writer_source() -> str:
    # The UNION across solver_writer.py and every module #1298 has extracted
    # out of it -- see tests/_writer_source.py for why reading one file is no
    # longer the same thing as reading "the writer".
    return writer_source()


class TestTheFunctionIsGone(unittest.TestCase):
    def test_the_module_exposes_no_capacity_measurement(self):
        self.assertFalse(
            hasattr(sw, "_measured_usable_capacity_kwh"),
            "the retracted capacity measurement is back on the module. It "
            "returns capacity x sensor_error, not capacity -- read this "
            "file's docstring before reinstating it.",
        )

    def test_its_threshold_constant_is_gone_too(self):
        """`_MIN_CAPACITY_RISE_PCT` existed only to make the unsound
        division look trustworthy by refusing small denominators. A
        bigger denominator does not make the quantity correct."""
        self.assertFalse(hasattr(sw, "_MIN_CAPACITY_RISE_PCT"))


class TestTheFieldIsNoLongerPublished(unittest.TestCase):
    """The attribute is what a household and the cards actually read."""

    def test_it_is_not_in_the_per_row_history_tuple_either(self):
        self.assertNotIn("measured_usable_capacity_kwh", sw._QUALITY_HISTORY_FIELDS)


class TestWhatIsDeliberatelyKEPT(unittest.TestCase):
    """This is a retraction of one unsound measurement, not a rollback of
    everything #1172 touched. A future reader undoing too much is as bad
    as one reinstating the defect."""

    def test_the_soh_derate_still_applies(self):
        """119.72 is the figure the BMS counter independently confirmed
        at 119.7 -- if this stops matching, the confirmation no longer
        applies to what is published."""
        self.assertAlmostEqual(
            sw.resolve_effective_capacity_kwh(
                {
                    "solver_battery_capacity_kwh": 122.2,
                    "solver_battery_soh_percent": 98.0,
                }
            ),
            119.756,
            places=2,
        )


if __name__ == "__main__":
    unittest.main()
