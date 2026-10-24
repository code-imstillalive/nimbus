"""nimbus #125/#133: the standalone writer carries the same
binding-constraint branch as the native one. A check that reads the cron
copy's source, kept apart until the decision on source-reading tests
(#1837); the label itself is in
tests/v2/orchestration/test_binding_constraint_label.py."""

import unittest

import _solver_path  # noqa: F401


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    def test_the_standalone_writer_has_the_same_new_branch(self):
        import pathlib

        standalone = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("by P2P export commitment", standalone)
        self.assertIn("fixed_export_kw_now", standalone)
