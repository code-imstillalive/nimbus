"""nimbus issue #1012: does each battery's measured energy reconcile
with its measured SoC swing?

Every kWh-based reconstruction in the scorer rests on one unstated
assumption -- that the configured efficiency, applied to the power
sensor's readings, converts to the same energy the SoC sensor reports.
Nothing had ever checked it. #1012 is what happens when it is wrong: a
~37 point SoC-trajectory divergence that survived #1008's energy-total
fix, tracing to either a wrong `solver_efficiency_percent` or an AC/DC
reference-plane mismatch in how the counters are read.

    modelled delta = in * charge_eff - out / discharge_eff
    measured delta = final_soc - initial_soc
    residual       = modelled - measured

The headline output is the **implied** charge efficiency rather than the
residual, because a residual says "something is off" and the implied
efficiency says which thing -- and it is the specific number #1012's
thread has been arguing about from two directions (85.8% configured
against ~95% implied) without either side being able to measure it.

Per battery and never blended: #949 established that fleet-blending
produces a false signal from perfect data once more than one battery is
scored, so this is immune to that by construction, and it names WHICH
battery fails to reconcile.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer


def _bal(**over):
    kw = {
        "name": "home",
        "in_kwh": 100.0,
        "out_kwh": 50.0,
        "initial_soc_kwh": 10.0,
        "final_soc_kwh": 10.0,
        "capacity_kwh": 120.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
    }
    kw.update(over)
    return solver_writer.battery_energy_balance(**kw)


class TestAConsistentBatteryReconciles(unittest.TestCase):
    def test_perfectly_consistent_data_has_no_residual(self):
        """Construct the SoC swing the model itself predicts, then check
        the balance closes. If this fails, the arithmetic is wrong and
        every other test here is meaningless."""
        in_kwh, out_kwh, eff = 100.0, 50.0, 0.95
        delta = in_kwh * eff - out_kwh / eff
        r = _bal(
            in_kwh=in_kwh,
            out_kwh=out_kwh,
            initial_soc_kwh=10.0,
            final_soc_kwh=10.0 + delta,
        )
        self.assertAlmostEqual(r["residual_kwh"], 0.0, places=3)

    def test_the_implied_efficiency_recovers_the_configured_one(self):
        """The property that makes this a diagnosis rather than an
        alarm: on consistent data the implied value lands back on the
        configured one, so a DIFFERENCE is genuinely informative."""
        in_kwh, out_kwh, eff = 100.0, 50.0, 0.95
        delta = in_kwh * eff - out_kwh / eff
        r = _bal(
            in_kwh=in_kwh,
            out_kwh=out_kwh,
            initial_soc_kwh=10.0,
            final_soc_kwh=10.0 + delta,
        )
        self.assertAlmostEqual(r["implied_charge_efficiency"], eff, places=4)
        self.assertIsNone(r["implied_efficiency_reason"])


class TestItReproducesNumber1012sOwnShape(unittest.TestCase):
    """The case this was built for. If the power sensor reads PACK-side
    while the scorer treats it as AC-side, the conversion loss is
    counted twice -- the SoC rises more than the model predicts, and the
    efficiency that would close the balance comes out HIGHER than the
    configured one. That is exactly the 85.8%-vs-~95% gap #1012
    reports."""

    def test_a_pack_side_sensor_implies_a_higher_efficiency_than_configured(self):
        # Configured 0.858, but the readings already have the loss in
        # them, so the real stored energy is the full in_kwh.
        in_kwh = 100.0
        r = _bal(
            in_kwh=in_kwh,
            out_kwh=0.0,
            initial_soc_kwh=10.0,
            final_soc_kwh=10.0 + in_kwh * 0.95,
            charge_efficiency=0.858,
            discharge_efficiency=0.858,
        )
        self.assertGreater(r["implied_charge_efficiency"], 0.858)
        self.assertAlmostEqual(r["implied_charge_efficiency"], 0.95, places=4)
        # And the residual is negative: the model under-predicted the
        # rise, which is the signature of double-counting the loss.
        self.assertLess(r["residual_kwh"], 0.0)

    def test_an_impossible_implied_efficiency_is_named_not_just_reported(self):
        """Above 1.0 is not a mis-tuned dial -- no battery stores more
        than it is given. It is proof of a sign or plane error, and must
        never read as 'very efficient'."""
        r = _bal(
            in_kwh=100.0,
            out_kwh=0.0,
            initial_soc_kwh=10.0,
            final_soc_kwh=10.0 + 115.0,
        )
        self.assertGreater(r["implied_charge_efficiency"], 1.0)
        self.assertEqual(
            r["implied_efficiency_reason"], "implied_efficiency_above_unity"
        )

    def test_an_overstated_efficiency_implies_a_lower_one(self):
        """The mirror direction, so the metric is not one-sided: if the
        configured efficiency is too generous, the implied value comes
        out below it and the residual is positive."""
        r = _bal(
            in_kwh=100.0,
            out_kwh=0.0,
            initial_soc_kwh=10.0,
            final_soc_kwh=10.0 + 80.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
        )
        self.assertLess(r["implied_charge_efficiency"], 0.95)
        self.assertGreater(r["residual_kwh"], 0.0)


class TestTheDischargeSideMirror(unittest.TestCase):
    """nimbus issue #1012: the discharge half, and why it earns its own
    field rather than being inferable from the charge one.

    Let `k` be the ratio of the capacity the scorer assumes to the pack's
    true usable capacity, and `e` the true one-way efficiency. For an
    AC-side power sensor:

        charge:     measured / in    ==  e * k
        discharge:  |measured| / out ==  k / e

    A charge-only window yields the single product `e*k` — and every
    (capacity, efficiency) pair on that hyperbola fits it equally well.
    **That degeneracy is why #1012 went back and forth between "the
    efficiency is wrong" and "the plane is wrong" without either being
    settleable**: with one direction they are the same measurement. Two
    one-directional windows separate them.
    """

    def test_a_pure_discharge_window_implies_a_discharge_efficiency(self):
        """The real reference-household window that made the joint solve
        possible: 79.468 kWh out against a measured 84.069 kWh drop."""
        r = _bal(
            in_kwh=0.0,
            out_kwh=79.468,
            initial_soc_kwh=100.0,
            final_soc_kwh=100.0 - 84.069,
            capacity_kwh=119.756,
            charge_efficiency=0.9263,
            discharge_efficiency=0.9263,
        )
        self.assertAlmostEqual(
            r["implied_discharge_efficiency"], 79.468 / 84.069, places=4
        )
        # The charge side genuinely cannot be implied from this window.
        self.assertIsNone(r["implied_charge_efficiency"])
        self.assertEqual(r["implied_efficiency_reason"], "no_charge_throughput")

    def test_consistent_data_recovers_the_configured_discharge_efficiency(self):
        """Same property the charge side has: on self-consistent data the
        implied value lands back on the configured one, so a difference
        is genuinely informative rather than an artefact."""
        out_kwh, eff = 50.0, 0.95
        drop = out_kwh / eff
        r = _bal(
            in_kwh=0.0,
            out_kwh=out_kwh,
            initial_soc_kwh=100.0,
            final_soc_kwh=100.0 - drop,
            charge_efficiency=eff,
            discharge_efficiency=eff,
        )
        self.assertAlmostEqual(r["implied_discharge_efficiency"], eff, places=4)

    def test_a_pure_charge_window_has_no_discharge_efficiency_to_imply(self):
        r = _bal(in_kwh=100.0, out_kwh=0.0, final_soc_kwh=10.0 + 95.0)
        self.assertIsNone(r["implied_discharge_efficiency"])

    def test_both_configured_efficiencies_are_reported(self):
        """So a reader comparing implied against configured never has to
        go and find what was configured."""
        r = _bal(charge_efficiency=0.91, discharge_efficiency=0.93)
        self.assertAlmostEqual(r["configured_charge_efficiency"], 0.91)
        self.assertAlmostEqual(r["configured_discharge_efficiency"], 0.93)

    def test_the_two_directions_jointly_separate_capacity_from_efficiency(self):
        """The whole point, worked on the real measured numbers.

        This is arithmetic over two windows rather than a code path —
        deliberately, because within ONE window `measured` is a NET swing
        and cannot be attributed to either direction. Pinned here so the
        derivation that answered #1012 is reproducible rather than living
        only in an issue comment.
        """
        rc = 99.637 / 93.396  # charge window: e*k
        rd = 84.069 / 79.468  # discharge window: k/e
        k = (rc * rd) ** 0.5
        e = (rc / rd) ** 0.5
        assumed_capacity = 119.756
        self.assertAlmostEqual(assumed_capacity / k, 112.73, places=1)
        self.assertAlmostEqual(e, 1.0042, places=3)
        # Both configured values are wrong, in different directions --
        # which no single-direction window could have shown.
        self.assertGreater(assumed_capacity / k + 1.0, 112.0)
        self.assertGreater(e, 0.9263)


class TestSoCMovingWithoutThroughput(unittest.TestCase):
    """nimbus issue #1012, observed on a real participant 2026-09-17:
    `Test EV`'s SoC fell 6.096 kWh — 10% of its capacity — across a
    window where its power sensor recorded NO throughput either way.

    Energy cannot appear or leave without flowing, so that is the
    participant's own power sensor failing to see its dispatch: a
    reconstruction blind spot, not a quiet battery. It needs its own
    label, because `no_charge_throughput` is the ordinary
    discharge-only case and sharing it makes a real instrumentation gap
    read as "nothing to report".
    """

    def test_the_real_observed_case_is_named(self):
        r = _bal(
            name="Test EV",
            in_kwh=0.0,
            out_kwh=0.0,
            initial_soc_kwh=30.0,
            final_soc_kwh=30.0 - 6.096,
            capacity_kwh=60.0,
        )
        self.assertEqual(r["implied_efficiency_reason"], "soc_moved_without_throughput")
        self.assertAlmostEqual(r["residual_kwh"], 6.096, places=3)

    def test_a_genuinely_idle_battery_is_not_flagged(self):
        """The distinction that makes the label worth having: zero
        throughput AND no real SoC movement is an ordinary idle window."""
        r = _bal(in_kwh=0.0, out_kwh=0.0, initial_soc_kwh=30.0, final_soc_kwh=30.0)
        self.assertEqual(r["implied_efficiency_reason"], "no_charge_throughput")

    def test_sensor_noise_does_not_trip_it(self):
        """A few tenths of a kWh is quantisation or self-discharge, not
        a missing dispatch — the floor exists so this stays a real
        signal rather than a permanent warning on every idle battery."""
        r = _bal(in_kwh=0.0, out_kwh=0.0, initial_soc_kwh=30.0, final_soc_kwh=29.7)
        self.assertEqual(r["implied_efficiency_reason"], "no_charge_throughput")

    def test_an_ordinary_discharge_window_keeps_its_own_label(self):
        """Real throughput with real movement is the normal case and
        must not be swept into the new label."""
        r = _bal(in_kwh=0.0, out_kwh=50.0, initial_soc_kwh=100.0, final_soc_kwh=46.0)
        self.assertEqual(r["implied_efficiency_reason"], "no_charge_throughput")


class TestHonestAbsence(unittest.TestCase):
    def test_no_charge_throughput_reports_none_with_a_reason(self):
        """A day the battery only discharged cannot imply a CHARGE
        efficiency -- that term is not in the balance. None plus a named
        reason, never a fabricated number."""
        r = _bal(in_kwh=0.0, out_kwh=50.0, final_soc_kwh=-42.0)
        self.assertIsNone(r["implied_charge_efficiency"])
        self.assertEqual(r["implied_efficiency_reason"], "no_charge_throughput")

    def test_the_residual_is_still_reported_without_an_implied_value(self):
        """The weaker signal still has to survive -- an absent diagnosis
        must not suppress the discrepancy itself."""
        r = _bal(in_kwh=0.0, out_kwh=50.0, final_soc_kwh=-42.0)
        self.assertIsInstance(r["residual_kwh"], float)
        self.assertIsInstance(r["measured_soc_delta_kwh"], float)

    def test_zero_capacity_does_not_divide_by_zero(self):
        r = _bal(capacity_kwh=0.0)
        self.assertIsNone(r["residual_pct_of_capacity"])


class TestItIsScaleFreeAndNamed(unittest.TestCase):
    def test_the_same_residual_reads_differently_on_different_packs(self):
        """Why the percentage exists: 5 kWh is a rounding error on a
        120 kWh fleet and a third of a 15 kWh home pack, so one absolute
        threshold cannot serve both."""
        big = _bal(capacity_kwh=120.0, final_soc_kwh=10.0)
        small = _bal(capacity_kwh=15.0, final_soc_kwh=10.0)
        self.assertEqual(big["residual_kwh"], small["residual_kwh"])
        self.assertGreater(
            abs(small["residual_pct_of_capacity"]),
            abs(big["residual_pct_of_capacity"]),
        )

    def test_the_battery_is_named_so_a_fleet_result_is_attributable(self):
        """#949's lesson: a blended figure cannot say which battery is
        at fault, and on a fleet install that is the whole question."""
        self.assertEqual(_bal(name="Test EV")["name"], "Test EV")


if __name__ == "__main__":
    unittest.main()
