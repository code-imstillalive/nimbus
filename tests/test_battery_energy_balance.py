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
