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

    def test_nothing_is_flagged(self):
        self.assertFalse(self.mask.any())

    def test_the_evs_charge_survives_into_the_published_plan(self):
        """Pre-fix this read 0.0 -- a real 7 kW draw erased."""
        self.assertAlmostEqual(float(self.charge[0]), 7.0)

    def test_net_battery_still_reflects_the_charging(self):
        self.assertAlmostEqual(float(self.net[0]), -7.0)


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

    def test_it_is_flagged(self):
        self.assertTrue(self.mask.all())

    def test_the_impossible_charge_is_removed(self):
        self.assertAlmostEqual(float(self.charge[0]), 0.0)

    def test_grid_import_is_reduced_by_it(self):
        self.assertAlmostEqual(float(self.imp[0]), 1.0)


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

    def test_it_is_flagged(self):
        self.assertTrue(self.mask.all())

    def test_only_the_gated_batterys_four_kw_is_removed(self):
        self.assertAlmostEqual(float(self.charge[0]), 7.0)

    def test_grid_import_drops_by_four_not_eleven(self):
        self.assertAlmostEqual(float(self.imp[0]), 11.0)

    def test_net_battery_matches_the_corrected_charge(self):
        """The published figures have to stay mutually consistent --
        net_battery is what the dashboard's battery line reads."""
        self.assertAlmostEqual(float(self.net[0]), -7.0)


class TestUncommittedPeriodsAreUntouched(unittest.TestCase):
    """`fixed_export_kw` is NaN wherever there is no commitment, which is
    most of a real horizon. Charging there is ordinary behaviour."""

    def test_a_nan_period_is_never_flagged(self):
        _net, charge, imp, mask = _clamp(
            fixed_export=[NAN],
            gated=[9.0],
            aggregate=[9.0],
            discharge=[0.0],
            grid_import=[9.0],
        )
        self.assertFalse(mask.any())
        self.assertAlmostEqual(float(charge[0]), 9.0)
        self.assertAlmostEqual(float(imp[0]), 9.0)

    def test_only_the_committed_periods_of_a_mixed_horizon_are_touched(self):
        _net, charge, _imp, mask = _clamp(
            fixed_export=[NAN, 12.0, NAN, 12.0],
            gated=[3.0, 3.0, 3.0, 0.0],
            aggregate=[3.0, 3.0, 3.0, 0.0],
            discharge=[0.0, 0.0, 0.0, 0.0],
            grid_import=[5.0, 5.0, 5.0, 5.0],
        )
        self.assertEqual(list(mask), [False, True, False, False])
        self.assertEqual([float(v) for v in charge], [3.0, 0.0, 3.0, 0.0])


class TestNoCommitmentAtAll(unittest.TestCase):
    """A household with no P2P arrangement passes `fixed_export_kw=None`.
    The clamp must be a pure pass-through, and must still return a real
    `net_battery` -- every caller uses that return value now."""

    def test_none_passes_everything_through_and_still_computes_net(self):
        net, charge, imp, mask = _clamp(
            fixed_export=None,
            gated=[6.0],
            aggregate=[6.0],
            discharge=[1.0],
            grid_import=[8.0],
        )
        self.assertFalse(mask.any())
        self.assertAlmostEqual(float(charge[0]), 6.0)
        self.assertAlmostEqual(float(imp[0]), 8.0)
        self.assertAlmostEqual(float(net[0]), -5.0)


class TestTheThresholdAndTheFloors(unittest.TestCase):
    def test_lp_degeneracy_noise_below_the_threshold_is_ignored(self):
        """The 0.05 kW threshold is deliberate -- a few watts of LP
        degeneracy noise is not a violation worth rewriting a plan
        over."""
        _net, _charge, _imp, mask = _clamp(
            fixed_export=[12.0],
            gated=[0.04],
            aggregate=[0.04],
            discharge=[0.0],
            grid_import=[1.0],
        )
        self.assertFalse(mask.any())

    def test_grid_import_never_goes_negative(self):
        """Import can be smaller than the charge being removed when the
        charge was solar-funded. A negative import would be meaningless
        and would publish as one."""
        _net, _charge, imp, _mask = _clamp(
            fixed_export=[12.0],
            gated=[9.0],
            aggregate=[9.0],
            discharge=[0.0],
            grid_import=[2.0],
        )
        self.assertGreaterEqual(float(imp[0]), 0.0)

    def test_kept_charge_never_goes_negative(self):
        """Defensive: if the gated array ever exceeded the aggregate
        (it should not), the kept remainder must not publish negative
        charge."""
        _net, charge, _imp, _mask = _clamp(
            fixed_export=[12.0],
            gated=[9.0],
            aggregate=[4.0],
            discharge=[0.0],
            grid_import=[9.0],
        )
        self.assertGreaterEqual(float(charge[0]), 0.0)


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    """#357's lesson, same as the #921 fix: the standalone/cron writer
    carries its own copy of this clamp, and a fix landing in only one is
    how the two drifted last time."""


if __name__ == "__main__":
    unittest.main()
