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

from solver import lp as _solver_lp

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
        # test_lpresult_defaults_to_None: None, not "" and not a status string.
        _solver_lp._lex_calibration_failed_until = 0.0
        self.assertIsNone(getattr(lp_mod.LPResult(status="error"), FIELD, "missing"))

        # test_plan_mirrors_it_and_also_defaults_to_None
        _solver_lp._lex_calibration_failed_until = 0.0
        fields = {f.name: f for f in dataclasses.fields(net_mod.Plan)}
        self.assertIn(FIELD, fields, "Plan must mirror LPResult's own field")
        self.assertIsNone(fields[FIELD].default)

        # test_the_infeasible_plan_path_accepts_it: `_infeasible_plan()` is the path a FAILED cycle actually takes --
        _solver_lp._lex_calibration_failed_until = 0.0
        params = inspect.signature(net_mod._infeasible_plan).parameters
        self.assertIn(FIELD, params)
        self.assertIsNone(params[FIELD].default)


class TestTheCalibratorDistinguishesTheTwoReasons(unittest.TestCase):
    """The substance: the source must actually branch on which probe outcome
    happened, not report one label for both."""

    def _calibrator_src(self) -> str:
        return textwrap.dedent(inspect.getsource(lp_mod._calibrate_blend_weight))

    def test_the_probe_records_a_non_optimal_status_separately(self):
        # test_the_probe_records_a_non_optimal_status_separately
        _solver_lp._lex_calibration_failed_until = 0.0
        src = self._calibrator_src()
        self.assertIn(PROBE_NOT_OPTIMAL, src)
        self.assertIn(COST_NOT_PRESERVED, src)

        # test_the_calibrator_warning_reports_both_ends: One label for the pair is not enough to audit: hi and lo can be
        _solver_lp._lex_calibration_failed_until = 0.0
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
        # test_it_names_the_reason_field
        _solver_lp._lex_calibration_failed_until = 0.0
        self.assertIn(f"plan.{FIELD}", self._window())

        # test_it_spells_out_the_symptom_reading
        _solver_lp._lex_calibration_failed_until = 0.0
        window = self._window()
        self.assertIn(PROBE_NOT_OPTIMAL, window)
        self.assertIn("ALREADY failing", window)
        self.assertIn("symptom", window)

        # test_it_spells_out_the_cause_reading
        _solver_lp._lex_calibration_failed_until = 0.0
        window = self._window()
        self.assertIn(COST_NOT_PRESERVED, window)
        self.assertIn("CAUSE", window)

        # test_an_unrecorded_reason_does_not_read_as_a_real_one: An older release's plan, or any path that did not set it, must not
        _solver_lp._lex_calibration_failed_until = 0.0
        self.assertIn("unrecorded", self._window())


if __name__ == "__main__":
    unittest.main(verbosity=2)
