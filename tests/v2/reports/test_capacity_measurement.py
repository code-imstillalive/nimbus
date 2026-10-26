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
        # test_the_module_exposes_no_capacity_measurement
        self.assertFalse(
            hasattr(sw, "_measured_usable_capacity_kwh"),
            "the retracted capacity measurement is back on the module. It "
            "returns capacity x sensor_error, not capacity -- read this "
            "file's docstring before reinstating it.",
        )

        # test_its_threshold_constant_is_gone_too: `_MIN_CAPACITY_RISE_PCT` existed only to make the unsound
        self.assertFalse(hasattr(sw, "_MIN_CAPACITY_RISE_PCT"))


class TestTheFieldIsNoLongerPublished(unittest.TestCase):
    """The attribute is what a household and the cards actually read."""

    def test_it_is_not_in_the_per_row_history_tuple_either(self):
        self.assertNotIn("measured_usable_capacity_kwh", sw._QUALITY_HISTORY_FIELDS)


if __name__ == "__main__":
    unittest.main()
