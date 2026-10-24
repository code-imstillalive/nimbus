"""nimbus #1172: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in tests/v2/reports/test_capacity_measurement.py."""

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


class TestTheFunctionIsGone(unittest.TestCase):
    def test_no_call_survives_anywhere_in_the_writer(self):
        src = _writer_source()
        calls = [
            line
            for line in src.splitlines()
            if "_measured_usable_capacity_kwh(" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(calls, [])


class TestTheFieldIsNoLongerPublished(unittest.TestCase):
    """The attribute is what a household and the cards actually read."""

    def test_the_report_does_not_publish_it(self):
        src = _writer_source()
        published = [
            line
            for line in src.splitlines()
            if '"measured_usable_capacity_kwh"' in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            published,
            [],
            "`measured_usable_capacity_kwh` is being published again",
        )


class TestTheCardNoLongerTellsHouseholdsTheirPackIsWrong(unittest.TestCase):
    """The card is where the wrong number actually reached someone."""

    @staticmethod
    def _source() -> str:
        return CARD_JS.read_text(encoding="utf-8")

    def test_the_card_does_not_read_the_retracted_field(self):
        code = [
            line
            for line in self._source().splitlines()
            if "measured_usable_capacity_kwh" in line
            and not line.lstrip().startswith("//")
        ]
        self.assertEqual(code, [], "the card reads the retracted field again")

    def test_the_sentence_itself_is_gone(self):
        self.assertNotIn("would account for", self._source())


class TestWhatIsDeliberatelyKEPT(unittest.TestCase):
    """This is a retraction of one unsound measurement, not a rollback of
    everything #1172 touched. A future reader undoing too much is as bad
    as one reinstating the defect."""

    def test_the_configured_capacity_is_still_published(self):
        self.assertIn('"configured_usable_capacity_kwh"', _writer_source())

    def test_it_is_still_the_effective_capacity_not_the_nameplate(self):
        src = _writer_source()
        self.assertIn('"configured_usable_capacity_kwh": round(capacity_kwh, 1)', src)
        self.assertIn("capacity_kwh = resolve_effective_capacity_kwh(cfg)", src)

    def test_the_reliability_caveat_still_works(self):
        """v0.94.408's real contribution -- telling a household the EPR
        is flagged -- is independent of the capacity sentence and stays."""
        src = CARD_JS.read_text(encoding="utf-8")
        self.assertIn("attrs.epr_reliable === false", src)


class TestTheReasoningIsRecordedWhereItWillBeRead(unittest.TestCase):
    """A deleted function leaves no trace at the place someone would
    naturally re-add it. The comment block is the trace."""

    def test_the_writer_explains_why_there_is_no_measurement(self):
        src = _writer_source()
        self.assertIn(
            "there is deliberately NO capacity measurement here",
            src,
        )
        self.assertIn("Do not reintroduce this from a power sensor", src)

    def test_it_names_this_test_so_the_guard_is_findable(self):
        self.assertIn(
            "test_1172_the_retracted_capacity_stays_retracted",
            _writer_source(),
        )
