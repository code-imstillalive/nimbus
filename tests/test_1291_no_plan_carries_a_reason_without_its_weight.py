"""nimbus #1291: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in
tests/v2/solver/test_calibration_weight_in_plan.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import network as network_mod


class TestEveryPlanConstructionSiteCarriesIt(unittest.TestCase):
    def test_no_Plan_construction_carries_the_reason_without_the_weight(self):
        """Mirrors tests/v2/solver/test_blend_weight_result.py's own
        TestEveryConstructionSiteCarriesIt, one hop further down the pipe:
        LPResult -> Plan instead of the calibrator -> LPResult."""
        src = Path(network_mod.__file__).read_text(encoding="utf-8")
        reason = "calibration_fallback_reason=result.calibration_fallback_reason"
        weight = "calibration_weight_used=result.calibration_weight_used"
        self.assertEqual(
            src.count(reason),
            src.count(weight),
            "every Plan(...) construction carrying the fallback reason from "
            "result must also carry the weight (nimbus #1291)",
        )
