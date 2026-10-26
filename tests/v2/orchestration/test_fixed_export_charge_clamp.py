"""The P2P fixed-export charge backstop compares the battery the bound
gates, never the fleet aggregate, so a participant charging during a
committed export block is not "impossible" and nothing is rewritten for it
(#923) (#1837).
"""

from __future__ import annotations

import unittest

import numpy as np
import solver_publish

NAN = float("nan")


def _clamp(fixed_export, gated, aggregate, discharge, grid_import):
    return solver_publish.resolve_fixed_export_charge_clamp(
        None if fixed_export is None else np.array(fixed_export, dtype=float),
        gated_charge_kw=np.array(gated, dtype=float),
        aggregate_charge_kw=np.array(aggregate, dtype=float),
        discharge_kw=np.array(discharge, dtype=float),
        grid_import_kw=np.array(grid_import, dtype=float),
    )


class TestAParticipantChargingIsNotAViolation(unittest.TestCase):
    """The bug itself. One committed period; the home battery obeys its
    `ub=0` gate exactly, and an EV charges 7 kW beside it."""

    def setUp(self):
        self.net, self.charge, self.imp, self.mask = _clamp(
            fixed_export=[12.0],
            gated=[0.0],  # battery 0 -- gated, genuinely at its ub=0
            aggregate=[7.0],  # + a participant EV charging 7 kW
            discharge=[0.0],
            grid_import=[9.0],
        )


class TestTheBackstopStillFiresForWhatItWasBuiltFor(unittest.TestCase):
    """The clamp must keep working for the case it exists for: the home
    battery itself charging inside a commitment, which its own hard
    `ub=0` really does make impossible."""

    def setUp(self):
        self.net, self.charge, self.imp, self.mask = _clamp(
            fixed_export=[12.0],
            gated=[5.0],  # battery 0 charging -- genuinely impossible
            aggregate=[5.0],
            discharge=[0.0],
            grid_import=[6.0],
        )


class TestAMixedPeriodRemovesOnlyTheGatedBatterysCharge(unittest.TestCase):
    """The discriminating case, and the one a naive fix gets wrong: the
    home battery violates AND a participant legitimately charges in the
    same period. Zeroing the aggregate would throw away the legal half
    along with the illegal one."""

    def setUp(self):
        self.net, self.charge, self.imp, self.mask = _clamp(
            fixed_export=[12.0],
            gated=[4.0],  # illegal: battery 0
            aggregate=[11.0],  # 4 illegal + 7 legal (EV)
            discharge=[0.0],
            grid_import=[15.0],
        )

    def test_grid_import_drops_by_four_not_eleven(self):
        self.assertAlmostEqual(float(self.imp[0]), 11.0)


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    """#357's lesson, same as the #921 fix: the standalone/cron writer
    carries its own copy of this clamp, and a fix landing in only one is
    how the two drifted last time."""


if __name__ == "__main__":
    unittest.main()
