"""The calibration weight used is carried from the LP result onto the plan
(#1291) (#1837).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import network as network_mod


class TestPlanCarriesTheField(unittest.TestCase):
    def test_plan_dataclass_has_the_field(self):
        self.assertIn(
            "calibration_weight_used",
            network_mod.Plan.__dataclass_fields__,
            "Plan must carry calibration_weight_used, the same as its sibling "
            "calibration_fallback_reason, so a failed solve's blend weight "
            "actually reaches something a household or log line can read",
        )


if __name__ == "__main__":
    unittest.main()
