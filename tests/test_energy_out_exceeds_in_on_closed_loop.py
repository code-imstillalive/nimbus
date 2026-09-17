"""nimbus #1086: a day that returns to its starting SoC cannot deliver more
energy than it received.

Observed on a real scored day, 13 Sep 2026, on the reference install:

    energy in                108.136 kWh
    energy out               113.328 kWh
    measured SoC delta        -0.240 kWh

The pack finished within **0.11% of throughput** of where it started and
delivered **5.2 kWh more than it took in**. That violates conservation of
energy at any efficiency, so it is not a statement about the battery — it
is proof that one of the three inputs is incomplete.

On that day it was the recorder. `load_nowcast_skill_coverage` read 0.958
(46 of 48 periods), and the achieved discharge came back 113.328 against
inverter counters of ~119.3 — a gap #1012's own body already records as
environmental.

**Why it needs its own reason rather than riding an existing one.** The
report did flag that day, as `mixed_window_not_decisive_implied_above_unity`.
That is true and it is the wrong signal: it says "this window cannot
separate the two directions", where the actual finding is "this day's data
is missing periods". A reader acting on the first would look at the
battery. The second sends them to the recorder.

Every other reason in this diagnostic describes reduced *confidence* in a
number that is otherwise sound. This one says the inputs do not add up,
which is a different claim and deserves a different word.

**Why it is gated on the loop closing.** A window that legitimately starts
full and ends empty has `out >> in` by design — the reference install's own
evening discharge runs 79 kWh out against 0 in. That is not remarkable. It
is only impossible when the pack came back to where it began, which is
exactly what the gate tests.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer


def _bal(**over):
    kw = {
        "name": "home",
        "in_kwh": 100.0,
        "out_kwh": 100.0,
        "initial_soc_kwh": 50.0,
        "final_soc_kwh": 50.0,
        "capacity_kwh": 119.72,
        "charge_efficiency": 0.9263,
        "discharge_efficiency": 0.9263,
    }
    kw.update(over)
    return solver_writer.battery_energy_balance(**kw)


class TestTheGateItself(unittest.TestCase):
    """`_is_closed_soc_loop()` in isolation."""

    def test_a_day_returning_to_its_start_is_a_closed_loop(self):
        self.assertTrue(solver_writer._is_closed_soc_loop(108.136, 113.328, -0.240))

    def test_a_pure_discharge_window_is_not(self):
        """The reference install's own evening block: 79 kWh out, nothing
        in, the pack demonstrably lower than it started. `out > in` there
        is the normal shape of a discharge and must never be flagged."""
        self.assertFalse(solver_writer._is_closed_soc_loop(0.0, 79.044, -84.667))

    def test_a_pure_charge_window_is_not(self):
        self.assertFalse(solver_writer._is_closed_soc_loop(92.971, 0.0, 100.954))

    def test_an_idle_window_is_not_a_loop(self):
        """No throughput at all: there is nothing to compare, and dividing
        by it would raise. `soc_moved_without_throughput` owns that case."""
        self.assertFalse(solver_writer._is_closed_soc_loop(0.0, 0.0, 6.096))


class TestTheRealDay(unittest.TestCase):
    def test_13_sep_is_flagged(self):
        r = _bal(
            in_kwh=108.136,
            out_kwh=113.328,
            initial_soc_kwh=50.0,
            final_soc_kwh=50.0 - 0.240,
        )
        self.assertEqual(
            r["implied_efficiency_reason"],
            "energy_out_exceeds_in_on_closed_loop",
            "the day that delivered 5.2 kWh more than it received, while "
            "returning to within 0.11% of throughput of its starting SoC, "
            "was not identified as having incomplete inputs",
        )

    def test_a_normal_mixed_day_is_not_flagged(self):
        """14 Sep's real figures — in exceeds out, as it must on a day that
        ends slightly fuller. This is the common case and it must keep its
        existing reason, or the new check has swallowed the ordinary."""
        r = _bal(
            in_kwh=107.364,
            out_kwh=103.682,
            initial_soc_kwh=50.0,
            final_soc_kwh=50.0 + 0.719,
        )
        self.assertNotEqual(
            r["implied_efficiency_reason"], "energy_out_exceeds_in_on_closed_loop"
        )

    def test_a_pure_discharge_day_is_not_flagged(self):
        """`out > in` trivially, but the loop does not close. The reference
        install's own evening window."""
        r = _bal(
            in_kwh=0.0,
            out_kwh=79.044,
            initial_soc_kwh=100.0,
            final_soc_kwh=100.0 - 84.667,
        )
        self.assertNotEqual(
            r["implied_efficiency_reason"], "energy_out_exceeds_in_on_closed_loop"
        )


class TestItDoesNotStealOtherReasons(unittest.TestCase):
    """Precedence matters: this sits between `soc_moved_without_throughput`
    and the confidence caveats, so both neighbours need pinning."""

    def test_soc_moved_without_throughput_still_wins(self):
        """Zero throughput both ways with a real SoC move is Mark's #768
        finding and a different defect. It must not be re-labelled — and
        it cannot be, because an idle window is not a closed loop."""
        r = _bal(in_kwh=0.0, out_kwh=0.0, initial_soc_kwh=50.0, final_soc_kwh=43.904)
        self.assertEqual(r["implied_efficiency_reason"], "soc_moved_without_throughput")

    def test_a_mixed_window_with_in_above_out_keeps_its_own_reason(self):
        r = _bal(in_kwh=100.0, out_kwh=50.0, initial_soc_kwh=50.0, final_soc_kwh=90.0)
        self.assertIn("mixed_window_not_decisive", r["implied_efficiency_reason"] or "")


if __name__ == "__main__":
    unittest.main()
