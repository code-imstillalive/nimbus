"""nimbus issue #735 stage 5: `resolve_soc_envelope()` in
`solver_inputs/battery_soc.py`.

**Before this extraction, nothing in the suite touched this block.** A
grep for the excursion warning, the physical-clamp warning, and
`_HOME_BATTERY_SOC_EXCURSION_WARNED` returned no test file at all — so
two deliberate, hard-won behaviours were guarded only by the comments
beside them:

- **#328: the starting SoC is passed through honestly, never clamped to
  the configured floor/ceiling.** The clamp this replaced reported a
  *fictional* in-range SoC to the LP, making planned throughput,
  `total_cost`, the next cycle's starting assumption and the quality
  report's EPR all quietly wrong by the clamped gap. That is invisible
  in any output — which is exactly why a comment was never enough.
- **#601: warn once, then DEBUG, then log a recovery.** The 8 Sep day at
  0% would have logged ~800 lines overnight.

The two clamps are the thing most likely to be "simplified" into one:
the configured floor/ceiling is a **soft** preference the LP costs
recovery toward, while `[0, capacity_kwh]` is a **physical** bound that
`elements.BatteryConfig` genuinely rejects — and whose absence caused a
real 27-crashes-per-window incident. Both are pinned separately below,
and one test states the distinction directly so a failure says which.

The logger is asserted through `solver_writer._LOGGER`, not a logger of
this module's own, because that is a real property worth keeping: an
install with `custom_components.nimbus_load.solver_writer: debug` in
`configuration.yaml` still controls these lines after the move.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
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


class TestTheEnvelopeItself(_WarnStateReset):
    def test_percents_resolve_to_kwh(self):
        got = _resolve()
        self.assertEqual(got.max_soc_kwh, 36.0)
        self.assertEqual(got.initial_soc_kwh, 20.0)

    def test_min_goes_through_resolve_min_soc_kwh_not_a_bare_multiply(self):
        """`resolve_min_soc_kwh()` applies a strictly-below-max rule that a
        plain `capacity * min_pct / 100` would silently skip."""
        with patch.object(
            solver_writer, "resolve_min_soc_kwh", return_value=7.5
        ) as resolver:
            got = _resolve(min_pct=10.0)
        self.assertEqual(got.min_soc_kwh, 7.5)
        resolver.assert_called_once_with(10.0, CAPACITY, 36.0)

    def test_efficiency_is_the_square_root_of_the_round_trip(self):
        """Charge and discharge each carry half the round-trip loss, so
        the per-direction factor is the square root — not the configured
        percent itself."""
        got = _resolve(efficiency=90.0)
        self.assertAlmostEqual(got.charge_discharge_efficiency, 0.9**0.5)

    def test_efficiency_is_capped_below_one(self):
        """100% round-trip is a frictionless battery, which is the
        wash-trade degeneracy shape `BatteryConfig` refuses to construct
        at all."""
        got = _resolve(efficiency=100.0)
        self.assertLess(got.charge_discharge_efficiency, 1.0)
        self.assertAlmostEqual(got.charge_discharge_efficiency, 0.999**0.5)


class TestIssue328HonestPassThrough(_WarnStateReset):
    """The behaviour with no observable output of its own — a clamp here
    would look completely normal downstream and be wrong everywhere."""

    def test_a_below_floor_soc_reaches_the_lp_unchanged(self):
        got = _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(got.initial_soc_kwh, 2.0)

    def test_an_above_ceiling_soc_reaches_the_lp_unchanged(self):
        got = _resolve(max_pct=80.0, initial_pct=95.0)
        self.assertEqual(got.initial_soc_kwh, 38.0)

    def test_the_configured_bounds_are_still_reported_unchanged(self):
        """Passing the SoC through honestly must not also mean loosening
        the envelope — the LP needs the real floor to cost recovery
        toward."""
        got = _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(got.min_soc_kwh, 8.0)


class TestThePhysicalClampIsADifferentClamp(_WarnStateReset):
    def test_a_negative_reading_is_clamped_to_zero(self):
        got = _resolve(initial_pct=-5.0)
        self.assertEqual(got.initial_soc_kwh, 0.0)

    def test_a_reading_over_one_hundred_percent_is_clamped_to_capacity(self):
        got = _resolve(initial_pct=130.0)
        self.assertEqual(got.initial_soc_kwh, CAPACITY)

    def test_the_two_clamps_are_not_the_same_clamp(self):
        """Stated directly because collapsing them is the plausible
        tidy-up: the same reading is passed through against the
        configured floor and clamped against the physical one."""
        soft = _resolve(min_pct=20.0, initial_pct=5.0)
        hard = _resolve(min_pct=20.0, initial_pct=-5.0)
        self.assertEqual(soft.initial_soc_kwh, 2.0)
        self.assertEqual(hard.initial_soc_kwh, 0.0)

    def test_a_zero_capacity_battery_does_not_divide_by_zero(self):
        """The percent used in the log message divides by capacity. A
        misconfigured install reading 0 kWh must not crash the solve."""
        cfg = {"solver_efficiency_percent": 95.0}
        got = battery_soc.resolve_soc_envelope(
            cfg, capacity_kwh=0.0, min_pct=10.0, max_pct=90.0, initial_pct=50.0
        )
        self.assertEqual(got.initial_soc_kwh, 0.0)


class TestIssue601WarnOnceThenRecover(_WarnStateReset):
    def test_the_first_excursion_warns(self):
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(log.warning.call_count, 1)

    def test_the_second_excursion_is_debug_not_a_second_warning(self):
        """The 8 Sep day at 0% is the real case: ~800 cycles, one
        warning."""
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve(min_pct=20.0, initial_pct=5.0)
            _resolve(min_pct=20.0, initial_pct=5.0)
            _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(log.warning.call_count, 1)
        self.assertEqual(log.debug.call_count, 2)

    def test_recovery_logs_once_and_rearms(self):
        """Rearming is the half a naive "log once ever" would miss — a
        second, genuinely new excursion has to warn again."""
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve(min_pct=20.0, initial_pct=5.0)
            _resolve(min_pct=20.0, initial_pct=50.0)
            _resolve(min_pct=20.0, initial_pct=50.0)
            _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertEqual(log.info.call_count, 1)
        self.assertEqual(log.warning.call_count, 2)

    def test_a_healthy_soc_logs_nothing_at_all(self):
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve()
        self.assertEqual(log.warning.call_count, 0)
        self.assertEqual(log.info.call_count, 0)
        self.assertEqual(log.debug.call_count, 0)

    def test_the_physical_clamp_warns_every_time_deliberately(self):
        """Not covered by the #601 dedup, and correctly so: a sensor
        returning nonsense on every cycle is a different, louder problem
        than a battery sitting outside its configured floor."""
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve(initial_pct=130.0)
            _resolve(initial_pct=130.0)
        # One excursion warning (deduped) plus one physical-clamp warning
        # per call.
        self.assertEqual(log.warning.call_count, 3)


class TestItLogsThroughSolverWritersOwnLogger(_WarnStateReset):
    def test_patching_solver_writers_logger_captures_these_lines(self):
        """An install configuring `custom_components.nimbus_load.
        solver_writer: debug` still controls these lines after the move —
        a logger of this module's own would silently break that."""
        with patch.object(solver_writer, "_LOGGER") as log:
            _resolve(min_pct=20.0, initial_pct=5.0)
        self.assertTrue(log.warning.called)


class TestTheResultShape(_WarnStateReset):
    def test_all_four_values_are_carried(self):
        """Structural: a caller reconstructing `min_soc_kwh` from the
        percents would skip `resolve_min_soc_kwh()`'s own rule and get a
        subtly different number."""
        got = _resolve()
        for field in (
            "initial_soc_kwh",
            "min_soc_kwh",
            "max_soc_kwh",
            "charge_discharge_efficiency",
        ):
            self.assertTrue(hasattr(got, field), field)

    def test_the_result_is_frozen(self):
        import dataclasses

        got = _resolve()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            got.initial_soc_kwh = 1.0

    def test_the_percents_are_keyword_only(self):
        """Three bare floats of the same unit. A transposed min/initial
        pair would produce a plausible envelope and a wrong plan."""
        with self.assertRaises(TypeError):
            battery_soc.resolve_soc_envelope({}, CAPACITY, 10.0, 90.0, 50.0)


if __name__ == "__main__":
    unittest.main()
