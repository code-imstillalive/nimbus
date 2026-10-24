"""nimbus #919: docs/reference-benchmark.md names exactly the modules that
import forecast_regret, and tells a reader the field number is one step
ahead. Checks that read the package source and the docs, kept apart until
the decision on source-reading tests (#1837); the nowcast skill itself is in
tests/v2/solver/test_nowcast_skill.py."""

import re
import unittest
from datetime import UTC, datetime
from pathlib import Path

import _solver_path  # noqa: F401

N = 24  # one day, hourly periods
START = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


class TestTheBenchmarkDocsClaimStaysTrue(unittest.TestCase):
    """`docs/reference-benchmark.md` makes a factual claim about who
    calls `compute_forecast_regret()`, and that claim went stale silently
    the moment this module started calling it.

    It had said the HACS integration "never calls it -- nothing outside
    `custom_components/nimbus_load/solver/` even imports
    `forecast_regret`". Nothing asserted that, so nothing caught it. This
    is the same stale-documented-claim class as the four guards added on
    2026-09-15 (entity names in a docs table, test-file names in source
    comments, units, and physics in a docstring) -- a claim a reader will
    trust needs something that fails when it stops being true.
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_the_documented_importer_set_is_the_real_one(self):
        """Measures real IMPORT statements, not textual mentions. The
        first version of this test matched any file containing the string
        and so flagged `solver_writer.py`, which only names the module in
        a docstring -- a count measuring the wrong thing, the same error
        four probes made on 2026-09-15. The doc's claim is about who
        CALLS it, so imports are the right proxy; prose is not.
        """
        importers = {
            path.name
            for path in (self.repo / "custom_components" / "nimbus_load").rglob("*.py")
            if re.search(
                r"^\s*(?:from\s+\.?\S*forecast_regret\s+import|import\s+\S*forecast_regret)",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        }
        self.assertEqual(
            importers,
            # solver_writer.py joined on 2026-09-26 (#937 stage 3): it calls
            # compute_forecast_regret() directly for the DAY-AHEAD
            # decomposition, alongside nowcast_skill.py's one-step-ahead
            # measurement. The doc's caller table now records the horizon of
            # each, because that is what decides whether a field number is
            # comparable to this benchmark's.
            {"nowcast_skill.py", "reference_benchmark.py", "solver_writer.py"},
            "the set of modules referencing forecast_regret changed. "
            "docs/reference-benchmark.md documents exactly who calls it, at "
            "which horizon, and why a field number may or may not be "
            "comparable to the benchmark's -- update that section, then "
            "update this test",
        )

    def test_the_doc_tells_a_reader_the_field_number_is_a_different_horizon(self):
        """The specific thing a reader could get wrong: comparing
        `load_nowcast_skill_value_add_dollars` against the benchmark's
        own `nimbus_value_add_dollars` as though they measured the same
        thing. One is one-step-ahead, the other day-ahead."""
        doc = (self.repo / "docs" / "reference-benchmark.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("nowcast_skill", doc)
        self.assertIn("one-step-ahead", doc)
