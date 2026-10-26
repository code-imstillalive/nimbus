"""The battery SoC envelope the solver plans within (#735) (#1837)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import solver_shared
from solver_inputs import battery_soc

CAPACITY = 40.0


def _resolve(*, min_pct=10.0, max_pct=90.0, initial_pct=50.0, efficiency=95.0):
    cfg = {"solver_efficiency_percent": efficiency}
    return battery_soc.resolve_soc_envelope(
        cfg,
        capacity_kwh=CAPACITY,
        min_pct=min_pct,
        max_pct=max_pct,
        initial_pct=initial_pct,
    )


class _WarnStateReset(unittest.TestCase):
    """The warn-once flag is module state and genuinely persists across
    calls — that is its whole point. Every test here resets it, so a
    failure is never inherited from whichever test ran first."""

    def setUp(self):
        battery_soc._HOME_BATTERY_SOC_EXCURSION_WARNED = False
        self.addCleanup(
            setattr, battery_soc, "_HOME_BATTERY_SOC_EXCURSION_WARNED", False
        )


class TestThePhysicalClampIsADifferentClamp(_WarnStateReset):
    def test_a_zero_capacity_battery_does_not_divide_by_zero(self):
        """The percent used in the log message divides by capacity. A
        misconfigured install reading 0 kWh must not crash the solve."""
        cfg = {"solver_efficiency_percent": 95.0}
        got = battery_soc.resolve_soc_envelope(
            cfg, capacity_kwh=0.0, min_pct=10.0, max_pct=90.0, initial_pct=50.0
        )
        self.assertEqual(got.initial_soc_kwh, 0.0)


class TestIssue601WarnOnceThenRecover(_WarnStateReset):
    def test_recovery_logs_once_and_rearms(self):
        """Rearming is the half a naive "log once ever" would miss — a
        second, genuinely new excursion has to warn again."""
        with patch.object(solver_shared, "_LOGGER") as log:
            _resolve(min_pct=20.0, initial_pct=5.0)
            _resolve(min_pct=20.0, initial_pct=50.0)
            _resolve(min_pct=20.0, initial_pct=50.0)
            _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(log.info.call_count, 1)
        self.assertEqual(log.warning.call_count, 2)

    def test_the_physical_clamp_warns_every_time_deliberately(self):
        """Not covered by the #601 dedup, and correctly so: a sensor
        returning nonsense on every cycle is a different, louder problem
        than a battery sitting outside its configured floor."""
        with patch.object(solver_shared, "_LOGGER") as log:
            _resolve(initial_pct=130.0)
            _resolve(initial_pct=130.0)
        # One excursion warning (deduped) plus one physical-clamp warning
        # per call.
        self.assertEqual(log.warning.call_count, 3)


class TestTheResultShape(_WarnStateReset):
    def test_the_percents_are_keyword_only(self):
        """Three bare floats of the same unit. A transposed min/initial
        pair would produce a plausible envelope and a wrong plan."""
        with self.assertRaises(TypeError):
            battery_soc.resolve_soc_envelope({}, CAPACITY, 10.0, 90.0, 50.0)


if __name__ == "__main__":
    unittest.main()
