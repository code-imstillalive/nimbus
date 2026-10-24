"""nimbus #923: the standalone writer compares the gated battery, not the
fleet aggregate, in its fixed-export charge backstop. A check that reads the
cron copy's source, kept apart until the decision on source-reading tests
(#1837); the clamp itself is in
tests/v2/orchestration/test_fixed_export_charge_clamp.py."""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401

NAN = float("nan")


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    def test_the_standalone_writer_also_compares_the_gated_battery(self):
        import pathlib

        standalone = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def resolve_fixed_export_charge_clamp(", standalone)
        self.assertIn("plan.batteries[0].charge_kw", standalone)
        self.assertNotIn(
            "_violation_mask = _fixed_mask & (plan.battery_charge_kw > 0.05)",
            standalone,
        )
