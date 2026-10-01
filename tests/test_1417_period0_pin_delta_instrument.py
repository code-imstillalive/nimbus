"""nimbus issue #1417: the period-0 pin instrument.

On a solve whose period[0] crosses the dispatch deadband relative to the
previous plan's period[0], re-solve once with period[0] pinned to the
previous value and log the objective difference. The comment block in
`solver_plan.py` says why; this file pins that it measures what it claims:

1. **The pin is honoured** by the real LP -- charge, discharge and zero.
2. **delta is 0 at the optimum and positive for a worse pin.** Pinning to the
   free solve's own period[0] costs nothing; pinning to the opposite extreme
   costs something. Without both, a delta of ~0 in the field could not be
   read as "a tie".
3. **Off by default**, including when the whole integration is at DEBUG --
   the re-solve is not free and must not ride along on unrelated debugging.
4. **It never changes the published plan**, on or off.
5. **It measures only crossings**, and survives a re-solve that raises.
"""

from __future__ import annotations

import dataclasses
import logging
import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_plan

from tests.test_1303_plan_assembly_extraction import CFG, _fingerprint, _kwargs

TOL = 1e-6


def _assemble(previous_plan=None):
    kwargs = _kwargs()
    kwargs["previous_plan"] = previous_plan
    return solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)


def _net0(plan) -> float:
    return float(plan.battery_discharge_kw[0]) - float(plan.battery_charge_kw[0])


def _with_period0(plan, net_kw):
    """A copy of `plan` whose period[0] net power is `net_kw`."""
    charge = np.array(plan.battery_charge_kw, dtype=float)
    discharge = np.array(plan.battery_discharge_kw, dtype=float)
    charge[0], discharge[0] = max(-net_kw, 0.0), max(net_kw, 0.0)
    return dataclasses.replace(
        plan, battery_charge_kw=charge, battery_discharge_kw=discharge
    )


class _InstrumentOn(unittest.TestCase):
    def setUp(self):
        logger = solver_plan.PERIOD0_PIN_LOGGER
        prior = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, prior)


class TestThePinIsHonoured(unittest.TestCase):
    def _pinned_plan(self, pin_kw):
        captured = {}

        def capture(plan, previous_plan, resolve_pinned, **_kw):
            captured["plan"] = resolve_pinned(pin_kw)

        orig = solver_plan.period0_crossing_delta
        solver_plan.period0_crossing_delta = capture
        try:
            _assemble()
        finally:
            solver_plan.period0_crossing_delta = orig
        return captured["plan"]

    def test_discharge_charge_and_zero_pins(self):
        for pin in (5.0, -4.0, 0.0):
            with self.subTest(pin=pin):
                plan = self._pinned_plan(pin)
                self.assertEqual(plan.status, "optimal")
                self.assertAlmostEqual(_net0(plan), pin, places=6)

    def test_the_pin_touches_period_0_only(self):
        """Later periods stay free to re-optimise around the pin."""
        free = _assemble().plan
        pin = -_net0(free) or 5.0
        pinned = self._pinned_plan(pin)
        self.assertNotEqual(_fingerprint_rest(free), _fingerprint_rest(pinned))


def _fingerprint_rest(plan):
    return np.round(
        np.asarray(plan.battery_discharge_kw[1:], dtype=float)
        - np.asarray(plan.battery_charge_kw[1:], dtype=float),
        6,
    ).tobytes()


class TestDeltaMeansWhatItSays(_InstrumentOn):
    def _delta_for(self, pin_kw):
        free = _assemble()
        # previous plan in the OPPOSITE deadband class, so this is a crossing
        # and the instrument re-solves pinned to exactly `pin_kw`.
        prev = _with_period0(free.plan, pin_kw)
        records = []
        orig = solver_plan.period0_crossing_delta

        def spy(*a, **k):
            records.append(orig(*a, **k))
            return records[-1]

        solver_plan.period0_crossing_delta = spy
        try:
            assembly = _assemble(previous_plan=prev)
        finally:
            solver_plan.period0_crossing_delta = orig
        return assembly, records[-1]

    def test_a_worse_pin_costs_something(self):
        free = _assemble().plan
        # the opposite extreme of whatever the free solve chose
        pin = -10.0 if _net0(free) > 0 else 25.0
        _, rec = self._delta_for(pin)
        self.assertIsNotNone(rec, "expected a crossing to be measured")
        self.assertEqual(rec["pinned_status"], "optimal")
        self.assertGreater(rec["delta_objective"], 0.01)

    def test_the_delta_is_never_negative(self):
        """The pin is a restriction of the same problem."""
        measured = 0
        for pin in (-10.0, -2.0, 0.0, 3.0, 25.0):
            with self.subTest(pin=pin):
                _, rec = self._delta_for(pin)
                if rec is not None and rec["delta_objective"] is not None:
                    measured += 1
                    self.assertGreaterEqual(rec["delta_objective"], -1e-4)
        self.assertGreaterEqual(measured, 3, "too few pins were crossings")

    def test_pinning_to_the_optimum_itself_costs_nothing(self):
        """Direct, without the crossing gate: resolve_pinned at the free
        solve's own period[0] reproduces the free objective."""
        free = _assemble().plan
        captured = {}

        def capture(plan, previous_plan, resolve_pinned, **_kw):
            captured["free"] = plan
            captured["pinned"] = resolve_pinned(_net0(plan))

        orig = solver_plan.period0_crossing_delta
        solver_plan.period0_crossing_delta = capture
        try:
            _assemble()
        finally:
            solver_plan.period0_crossing_delta = orig
        self.assertAlmostEqual(
            captured["pinned"].total_cost, captured["free"].total_cost, places=4
        )
        self.assertAlmostEqual(_net0(captured["free"]), _net0(free), places=6)

    def test_the_record_names_the_anchor_target(self):
        """The anchor ties new[0] to the old period CONTAINING its start. With
        the same start time (both solves in one minute) that is prev[0], and
        the record must report it -- the field that tells a scheduled change
        from a re-plan in the field."""
        free = _assemble()
        pin = -10.0 if _net0(free.plan) > 0 else 25.0
        _, rec = self._delta_for(pin)
        self.assertEqual(rec["anchor_prev_index"], 0)
        self.assertAlmostEqual(rec["anchor_target_kw"], pin, places=3)

    def test_the_log_line_carries_the_inputs(self):
        with self.assertLogs(solver_plan.PERIOD0_PIN_LOGGER, level="DEBUG") as cm:
            _, rec = self._delta_for(-10.0 if _net0(_assemble().plan) > 0 else 25.0)
        self.assertIn("#1417", cm.output[-1])
        self.assertEqual(
            set(rec["inputs"]), {"import_price", "export_price", "solar_kw", "load_kw"}
        )
        self.assertAlmostEqual(rec["inputs"]["import_price"], 0.10)


class TestOffByDefault(unittest.TestCase):
    def _crossing_call(self):
        calls = []
        plan = _assemble().plan
        prev = _with_period0(plan, -10.0 if _net0(plan) > 0 else 25.0)
        rec = solver_plan.period0_crossing_delta(
            plan, prev, lambda k: calls.append(k) or plan
        )
        return rec, calls

    def test_nothing_runs_at_the_default_level(self):
        rec, calls = self._crossing_call()
        self.assertIsNone(rec)
        self.assertEqual(calls, [])

    def test_integration_wide_debug_does_not_turn_it_on(self):
        parent = logging.getLogger(
            solver_plan.PERIOD0_PIN_LOGGER.name.rsplit(".", 1)[0]
        )
        prior = parent.level
        parent.setLevel(logging.DEBUG)
        self.addCleanup(parent.setLevel, prior)
        rec, calls = self._crossing_call()
        self.assertIsNone(rec)
        self.assertEqual(calls, [])


class TestItNeverChangesThePublishedPlan(_InstrumentOn):
    def test_on_and_off_publish_the_same_plan(self):
        free = _assemble()
        prev = _with_period0(free.plan, -10.0 if _net0(free.plan) > 0 else 25.0)
        on = _assemble(previous_plan=prev)
        solver_plan.PERIOD0_PIN_LOGGER.setLevel(logging.INFO)
        off = _assemble(previous_plan=prev)
        self.assertEqual(_fingerprint(on), _fingerprint(off))
        self.assertIsNone(on.all_batteries[0].period0_pin_net_kw)


class TestOnlyCrossingsAndNeverRaises(_InstrumentOn):
    def test_same_deadband_class_is_not_measured(self):
        plan = _assemble().plan
        calls = []
        rec = solver_plan.period0_crossing_delta(
            plan, plan, lambda k: calls.append(k) or plan
        )
        self.assertIsNone(rec)
        self.assertEqual(calls, [])

    def test_no_previous_plan_is_not_measured(self):
        plan = _assemble().plan
        self.assertIsNone(solver_plan.period0_crossing_delta(plan, None, None))

    def test_a_raising_re_solve_is_swallowed(self):
        plan = _assemble().plan
        prev = _with_period0(plan, -10.0 if _net0(plan) > 0 else 25.0)

        def boom(_k):
            raise RuntimeError("solver exploded")

        self.assertIsNone(solver_plan.period0_crossing_delta(plan, prev, boom))

    def test_the_deadband_matches_the_dispatch_automation(self):
        c = solver_plan._deadband_class
        self.assertEqual(
            [c(0.06), c(0.05), c(0.0), c(-0.05), c(-0.06)], [1, 0, 0, 0, -1]
        )


if __name__ == "__main__":
    unittest.main()
