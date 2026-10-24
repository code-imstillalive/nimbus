"""The minimum-weight fallback says why it fired, since its two reasons point
in opposite directions (#1179) (#1837).
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod

FIELD = "calibration_fallback_reason"
PROBE_NOT_OPTIMAL = "probe_not_optimal"
COST_NOT_PRESERVED = "cost_not_preserved"


class TestTheFieldExistsOnBothDataclasses(unittest.TestCase):
    """It has to survive the whole trip: LPResult -> Plan -> the log line."""

    def test_lpresult_defaults_to_None(self):
        """None, not "" and not a status string.

        Unlike the sibling bool -- where "did not take the fallback" is equally
        true whether calibration ran or not -- there is no honest reason to
        report when no fallback happened. None says that.
        """
        self.assertIsNone(getattr(lp_mod.LPResult(status="error"), FIELD, "missing"))

    def test_plan_mirrors_it_and_also_defaults_to_None(self):
        fields = {f.name: f for f in dataclasses.fields(net_mod.Plan)}
        self.assertIn(FIELD, fields, "Plan must mirror LPResult's own field")
        self.assertIsNone(fields[FIELD].default)

    def test_the_infeasible_plan_path_accepts_it(self):
        """`_infeasible_plan()` is the path a FAILED cycle actually takes --
        the only path where this field has any use at all. A field that
        reaches every plan except the failing ones would be useless."""
        params = inspect.signature(net_mod._infeasible_plan).parameters
        self.assertIn(FIELD, params)
        self.assertIsNone(params[FIELD].default)


class TestTheCalibratorDistinguishesTheTwoReasons(unittest.TestCase):
    """The substance: the source must actually branch on which probe outcome
    happened, not report one label for both."""

    def _calibrator_src(self) -> str:
        return textwrap.dedent(inspect.getsource(lp_mod._calibrate_blend_weight))

    def test_the_probe_records_a_non_optimal_status_separately(self):
        src = self._calibrator_src()
        self.assertIn(PROBE_NOT_OPTIMAL, src)
        self.assertIn(COST_NOT_PRESERVED, src)

    def test_the_calibrator_warning_reports_both_ends(self):
        """One label for the pair is not enough to audit: hi and lo can be
        rejected for different reasons, and that combination is itself a
        finding."""
        src = self._calibrator_src()
        i = src.find("no blend weight ")
        self.assertGreater(i, -1, "the calibrator warning moved or was renamed")
        window = src[i : i + 1600]
        self.assertIn("hi=%s", window)
        self.assertIn("lo=%s", window)
        self.assertIn("#1179", window)


class TestTheFailureLineExplainsWhatTheReasonMeans(unittest.TestCase):
    """A label nobody can interpret is not observability. Whoever reads this
    line at 03:00 during the next episode should not have to find this issue
    to know which way the causality runs."""

    def _window(self) -> str:
        # nimbus #1304 (spec 005): the #757 failure warning moved to
        # solver_publish.py with publish_plan. writer_source() is the union
        # across solver_writer.py and every module #1298 has extracted -- the
        # claim is "the writer warns", not "one file contains the string".
        from _writer_source import writer_source

        src = writer_source()
        i = src.find("solve did not complete after")
        assert i > -1, "the #757 failure warning moved or was renamed"
        return src[max(0, i - 3000) : i + 1400]

    def test_it_names_the_reason_field(self):
        self.assertIn(f"plan.{FIELD}", self._window())

    def test_it_spells_out_the_symptom_reading(self):
        window = self._window()
        self.assertIn(PROBE_NOT_OPTIMAL, window)
        self.assertIn("ALREADY failing", window)
        self.assertIn("symptom", window)

    def test_it_spells_out_the_cause_reading(self):
        window = self._window()
        self.assertIn(COST_NOT_PRESERVED, window)
        self.assertIn("CAUSE", window)

    def test_an_unrecorded_reason_does_not_read_as_a_real_one(self):
        """An older release's plan, or any path that did not set it, must not
        silently render as a blank or as one of the two real verdicts."""
        self.assertIn("unrecorded", self._window())


if __name__ == "__main__":
    unittest.main(verbosity=2)
