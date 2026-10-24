"""The blend weight actually used reaches the LP result (#1179) (#1837)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import lp as lp_mod


class TestTheFieldExists(unittest.TestCase):
    def test_lpresult_carries_the_weight(self):
        self.assertIn("calibration_weight_used", lp_mod.LPResult.__dataclass_fields__)

    def test_it_defaults_to_None(self):
        """None means "no calibration ran", the same posture as the two
        sibling #1179 fields. A default of 0.0 or 1.0 would be a claim."""
        field = lp_mod.LPResult.__dataclass_fields__["calibration_weight_used"]
        self.assertIsNone(field.default)

    def test_it_sits_beside_its_two_siblings(self):
        """Grouped deliberately -- all three are #1179 diagnostics and are
        read together when an episode recurs."""
        names = list(lp_mod.LPResult.__dataclass_fields__)
        i = names.index("calibration_weight_used")
        neighbourhood = names[max(0, i - 3) : i + 1]
        self.assertIn("calibration_min_weight_fallback", neighbourhood)
        self.assertIn("calibration_fallback_reason", neighbourhood)


if __name__ == "__main__":
    unittest.main(verbosity=2)
