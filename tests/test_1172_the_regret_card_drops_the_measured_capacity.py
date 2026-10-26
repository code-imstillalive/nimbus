"""nimbus #1172: the regret card no longer tells a household its pack is
smaller than configured, and the writer says where the measurement went. Card
and comment checks, kept under the #1837 decision (group C); what the report
publishes is tested in tests/v2/reports/test_quality_window.py and
tests/v2/reports/test_capacity_measurement.py."""

from __future__ import annotations

import pathlib
import unittest

import _solver_path  # noqa: F401
from _writer_source import writer_source

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
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


class TestTheCardNoLongerTellsHouseholdsTheirPackIsWrong(unittest.TestCase):
    """The card is where the wrong number actually reached someone."""

    @staticmethod
    def _source() -> str:
        return CARD_JS.read_text(encoding="utf-8")

    def test_the_card_does_not_read_the_retracted_field(self):
        # test_the_card_does_not_read_the_retracted_field
        code = [
            line
            for line in self._source().splitlines()
            if "measured_usable_capacity_kwh" in line
            and not line.lstrip().startswith("//")
        ]
        self.assertEqual(code, [], "the card reads the retracted field again")

        # test_the_sentence_itself_is_gone
        self.assertNotIn("would account for", self._source())


class TestWhatIsDeliberatelyKEPT(unittest.TestCase):
    """This is a retraction of one unsound measurement, not a rollback of
    everything #1172 touched. A future reader undoing too much is as bad
    as one reinstating the defect."""

    def test_the_reliability_caveat_still_works(self):
        """v0.94.408's real contribution -- telling a household the EPR
        is flagged -- is independent of the capacity sentence and stays."""
        src = CARD_JS.read_text(encoding="utf-8")
        self.assertIn("attrs.epr_reliable === false", src)


class TestTheReasoningIsRecordedWhereItWillBeRead(unittest.TestCase):
    """A deleted function leaves no trace at the place someone would
    naturally re-add it. The comment block is the trace."""

    def test_the_writer_explains_why_there_is_no_measurement(self):
        # test_the_writer_explains_why_there_is_no_measurement
        src = _writer_source()
        self.assertIn(
            "there is deliberately NO capacity measurement here",
            src,
        )
        self.assertIn("Do not reintroduce this from a power sensor", src)

        # test_it_names_this_test_so_the_guard_is_findable
        self.assertIn(
            "test_1172_the_regret_card_drops_the_measured_capacity",
            _writer_source(),
        )
