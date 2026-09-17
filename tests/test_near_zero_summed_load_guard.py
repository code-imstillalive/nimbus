"""nimbus issue #933: #118's near-zero load guard, for the SUMMED
multi-circuit path.

#118 established that a structurally-valid, near-all-zero load forecast
produces a confident `optimal` solve that told a household the battery
could export ~$46/day more than it really could, because the solver
believed nobody was consuming anything. That guard protects only the
single-sensor path. The summed path reaches the identical input state by
a different route: each per-entity fetch failure contributes 0.0 for
every period -- correct individually -- and enough simultaneous failures
accumulate silently into a near-zero total.

Not hypothetical. The reference household's daily statistics show the
summed sensor reaching **exactly 0.0** on five separate days, and HA's
statistics compiler skips non-numeric states, so `unavailable`/`unknown`
are excluded rather than stored as zero. A recorded 0.0 means the sensor
genuinely published 0.0, which on this path requires every contributing
circuit to have contributed 0.0.

The gate is the conjunction the thread converged on:

    failed_entities AND nonzero_fraction < 0.1

Each half alone is wrong in a way these tests pin:
  - the fraction alone refuses during legitimate onboarding
  - "all failed" alone misses a partial failure that still leaves the
    total near-zero

and the conjunction is correct whether or not failures cluster -- which
is what unblocked it, since the clustering measurement is currently
unreachable (#944 drops the attribute that would have answered it).

One scope correction to the issue's own phrasing, found while writing
these and pinned by `test_a_partial_failure_whose_survivors_carry_real_
load_is_ALLOWED`: "14 of 18 down still yields a badly wrong total" is
true about ACCURACY, but this gate's criterion is a **near-zero total**,
not **many entities failed**. A partial failure whose surviving circuits
carry real load leaves a total that is understated but not degenerate,
and Nimbus keeps planning. That is the right scope for a guard descended
from #118 -- which is about the confidently-wrong near-zero plan, not
about under-counting -- but it means that case remains unguarded, with
`failed_load_entities` still its only signal.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer

N = 96  # a real 24h forecast at 15-minute resolution
REAL_LOAD = [1.2] * N


def _err(total_kw, failed, self_consumption=0.0):
    return solver_writer.near_zero_summed_load_error(total_kw, failed, self_consumption)


class TestItRefusesTheStateNumber118WasFiledAbout(unittest.TestCase):
    def test_every_circuit_failed_is_refused(self):
        err = _err([0.0] * N, ["sensor.a", "sensor.b", "sensor.c"])
        self.assertIsNotNone(err)

    def test_a_partial_failure_that_took_the_load_with_it_is_refused(self):
        """The case 'refuse only when EVERY entity failed' would miss:
        14 of 18 down, the four survivors being small always-off
        circuits, so the total is still near-zero and the LP would plan
        against it.
        """
        total = [0.004] * N
        err = _err(total, [f"sensor.c{i}" for i in range(14)])
        self.assertIsNotNone(err)

    def test_the_message_names_the_failed_entities(self):
        """#118's own lesson was that a visible-but-unexplained refusal
        is barely better than a silent wrong answer. A household has to
        be able to act on this without reading the source."""
        err = _err([0.0] * N, ["sensor.pool_pump", "sensor.hws"])
        self.assertIn("sensor.pool_pump", err)
        self.assertIn("sensor.hws", err)
        self.assertIn("#933", err)


class TestItDoesNotRefuseLegitimateNearZero(unittest.TestCase):
    """The half that makes the conjunction necessary. Refusing to plan
    for a household whose data is fine is a regression, not a guard."""

    def test_a_genuinely_quiet_house_with_healthy_fetches_is_allowed(self):
        """An empty holiday house is a real, correct near-zero. Nothing
        is missing from its data, so there is nothing to refuse -- this
        is exactly the case the bare 10% test gets wrong."""
        self.assertIsNone(_err([0.0] * N, []))

    def test_onboarding_with_some_circuits_live_is_allowed(self):
        """The onboarding trap. A household adding circuits one at a
        time has failed fetches (a circuit that has never produced a
        forecast fails exactly like one transiently down) -- but the
        circuits that ARE reporting keep the total non-zero, so the
        fraction half of the gate does not trip and Nimbus keeps
        planning.
        """
        self.assertIsNone(_err(REAL_LOAD, ["sensor.not_trained_yet"]))

    def test_a_healthy_full_install_is_allowed(self):
        self.assertIsNone(_err(REAL_LOAD, []))

    def test_an_empty_forecast_is_not_refused_here(self):
        """Zero periods is a different failure with its own handling
        upstream; dividing by len() here would raise."""
        self.assertIsNone(_err([], ["sensor.a"]))

    def test_a_partial_failure_whose_survivors_carry_real_load_is_ALLOWED(self):
        """The guard's real limit, pinned deliberately rather than
        engineered around, because it surprised me while writing these.

        The issue's phrasing -- "14 of 18 down still yields a badly
        wrong total" -- is true about accuracy but this gate does not
        catch it in general. The criterion is **near-zero total**, not
        **many entities failed**: if the surviving circuits carry real
        load (0.08 kW in every period is already non-trivial by #118's
        own 0.01 threshold), the total is not near-zero and Nimbus keeps
        planning on an understated but non-degenerate forecast.

        That is the correct scope for THIS guard -- #118 is about the
        confidently-wrong near-zero plan, not about under-counting --
        but it means a partial failure with healthy survivors is still
        unguarded, and `failed_load_entities` remains the only signal
        for it. Worth knowing before anyone reads this as covering the
        whole partial case.
        """
        total = [0.08] * N
        self.assertIsNone(_err(total, [f"sensor.c{i}" for i in range(14)]))


class TestTheSelfConsumptionSubtlety(unittest.TestCase):
    """sum_load_forecasts() adds inverter_self_consumption_kw to every
    period AFTER summing, so a fully-failed total does not arrive as
    zeros -- it arrives as that constant, repeated. A naive `v > 0.01`
    test would see 100% non-zero periods on any install with a real
    value configured and never fire at all.
    """

    def test_a_failed_total_is_still_caught_when_self_consumption_is_set(self):
        # Every circuit failed; the only thing left is the flat bias.
        total = [0.25] * N
        self.assertIsNotNone(_err(total, ["sensor.a", "sensor.b"], 0.25))

    def test_that_same_total_would_pass_a_naive_check(self):
        """Pins WHY the parameter exists: without subtracting the bias
        the identical input reads as fully non-zero. If someone later
        drops the parameter, this test explains what breaks."""
        total = [0.25] * N
        naive_nonzero = sum(1 for v in total if v > 0.01)
        self.assertEqual(naive_nonzero, N)
        self.assertIsNotNone(_err(total, ["sensor.a"], 0.25))

    def test_real_load_on_top_of_self_consumption_is_still_allowed(self):
        total = [1.2 + 0.25] * N
        self.assertIsNone(_err(total, ["sensor.a"], 0.25))


class TestTheThresholdMatchesNumber118(unittest.TestCase):
    """Deliberately the same 10%-of-periods / 0.01 kW test #118 settled
    on, so the two paths cannot drift into disagreeing about what
    'near-zero' means."""

    def test_just_under_ten_percent_non_zero_is_refused(self):
        total = [1.2] * 9 + [0.0] * 91
        self.assertIsNotNone(_err(total, ["sensor.a"]))

    def test_just_over_ten_percent_non_zero_is_allowed(self):
        total = [1.2] * 11 + [0.0] * 89
        self.assertIsNone(_err(total, ["sensor.a"]))


if __name__ == "__main__":
    unittest.main()
