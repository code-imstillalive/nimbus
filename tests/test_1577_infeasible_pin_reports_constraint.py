"""nimbus issue #1577: an infeasible period-0 pin names the constraint it hit.

#1417's instrument logs `delta_objective=None (pinned status=infeasible)` when
the previous plan's period[0] setpoint is infeasible under the current model.
#1577 adds `blocked_by(<battery>)=`, computed by `solver/pin_blockers.py` from
the configs the pinned re-solve was built from. This file pins that:

1. **Each blocker is real.** For every constraint it can name, a pin is built
   that the REAL LP (`network.build_plan`, the same kwargs the free solve used)
   rejects as infeasible, and the explainer names exactly that constraint:
   power bound, SoC headroom, discharge reserve, grid export limit, the P2P
   charge gate and fixed-export floor, a power curve.
2. **It never accuses a feasible pin.** Every pin the real LP accepts comes
   back with an empty list -- each check is a sufficient condition.
3. **It is diagnostic only.** The explainer is consulted only on an infeasible
   pin, a feasible pin's record and log line are unchanged, a raising
   explainer is swallowed, and the published plan is identical on or off.
4. **It is scoped to batteries[0], and says so.** On a two-battery site the
   record and log line name the participant; the second battery's own limits
   are never reported as the pinned one's; a shared-charger group is checked.
5. **Nothing consumes it.** No module but `solver_plan` imports the helper or
   reads its record fields -- in particular not the Repairs path
   (`setup_health.py`) -- so insufficient or unexplained evidence cannot
   become a Repair, a limit change or a dispatch input.
"""

from __future__ import annotations

import dataclasses
import logging
import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_plan
from solver import network, pin_blockers

from tests.test_1303_plan_assembly_extraction import CFG, _Env, _fingerprint, _kwargs


class _LowSoc(_Env):
    initial_soc_kwh = 4.5  # 0.5 kWh above min_soc_kwh=4.0


class _HighSoc(_Env):
    initial_soc_kwh = 39.5  # 0.5 kWh below capacity_kwh=40.0


def _scenario(env=_Env, **overrides) -> dict:
    kwargs = _kwargs()
    kwargs["soc_envelope"] = env()
    kwargs.update(overrides)
    return kwargs


def _build_kwargs(kwargs) -> dict:
    """The exact `network.build_plan()` kwargs the free solve used."""
    seen = {}
    orig = network.build_plan

    def spy(**kw):
        seen.setdefault("kw", kw)
        return orig(**kw)

    solver_plan.network.build_plan = spy
    try:
        solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)
    finally:
        solver_plan.network.build_plan = orig
    return seen["kw"]


def _pinned_status(kw, pin, *, battery0=None, grid=None) -> str:
    b0 = battery0 or kw["batteries"][0]
    pinned = dict(kw)
    pinned.update(
        batteries=[
            dataclasses.replace(b0, period0_pin_net_kw=pin),
            *kw["batteries"][1:],
        ],
        grid=grid or kw["grid"],
        compute_offer_curve=False,
        compute_signals=False,
    )
    return network.build_plan(**pinned).status


def _explain(kw, pin, *, battery0=None, grid=None, exact=True) -> list[str]:
    return pin_blockers.period0_pin_blockers(
        pin,
        batteries=[battery0 or kw["batteries"][0], *kw["batteries"][1:]],
        grid=grid or kw["grid"],
        hours0=float(kw["periods"].hours[0]),
        solar=kw["solar"],
        loads=kw["loads"],
        sheddable_loads=kw["sheddable_loads"],
        adequacy_loads=kw["adequacy_loads"],
        thermal_loads=kw["thermal_loads"],
        risk_aversion=kw["risk_aversion"] if exact else None,
    )


def _names(blockers) -> list[str]:
    return [b.split(":", 1)[0] for b in blockers]


class TestEachBlockerIsReal(unittest.TestCase):
    def _assert_blocked(self, kw, pin, expected, **mod):
        self.assertEqual(_pinned_status(kw, pin, **mod), "infeasible")
        self.assertEqual(_names(_explain(kw, pin, **mod)), expected)

    def test_pin_outside_the_power_bound(self):
        kw = _build_kwargs(_scenario())  # max_charge_kw=10, max_discharge_kw=25
        self._assert_blocked(kw, -15.0, ["battery_max_charge"])
        self._assert_blocked(kw, 30.0, ["battery_max_discharge"])

    def test_pin_outside_soc_headroom(self):
        """0.5 kWh below full: a 4 kW charge for half an hour overflows."""
        kw = _build_kwargs(_scenario(_HighSoc))
        self._assert_blocked(kw, -4.0, ["soc_capacity_headroom"])

    def test_pin_outside_the_discharge_reserve(self):
        """0.5 kWh above min_soc: #328's reserve row forbids a 5 kW draw --
        the shape of #1417's 06:1x devhub cases (a small discharge pin at
        dawn)."""
        kw = _build_kwargs(_scenario(_LowSoc))
        self._assert_blocked(kw, 5.0, ["discharge_reserve"])

    def test_pin_outside_the_grid_export_limit(self):
        """Export capped at 2 kW, load at most 4.5 kW: 20 kW has nowhere to go."""
        kw = _build_kwargs(_scenario(export_limit_kw=[2.0] * 8))
        self._assert_blocked(kw, 20.0, ["grid_export_limit"])

    def test_p2p_fixed_window_charge_gate_and_export_floor(self):
        kw = _build_kwargs(_scenario())
        fixed = np.full(len(kw["periods"].hours), np.nan)
        fixed[0] = 5.0  # a 5 kW commitment in period 0, solar there is 0
        grid = dataclasses.replace(kw["grid"], fixed_export_kw=fixed)
        self._assert_blocked(
            kw,
            -3.0,
            ["p2p_fixed_window_charge_gate", "p2p_fixed_export_commitment"],
            grid=grid,
        )
        self._assert_blocked(kw, 2.0, ["p2p_fixed_export_commitment"], grid=grid)

    def test_pin_above_the_charge_power_curve(self):
        """A CV taper: at 20 kWh the curve allows 10 - (20-4)*0.25 = 6 kW."""
        kw = _build_kwargs(_scenario())
        b0 = dataclasses.replace(
            kw["batteries"][0], charge_power_curve=[(4.0, 10.0), (38.0, 1.5)]
        )
        self.assertEqual(_pinned_status(kw, -6.0, battery0=b0), "optimal")
        self._assert_blocked(kw, -9.0, ["charge_power_curve"], battery0=b0)


class TestNeverAccusesAFeasiblePin(unittest.TestCase):
    def test_every_feasible_pin_explains_to_nothing(self):
        checked = 0
        for env, extra in (
            (_Env, {}),
            (_LowSoc, {}),
            (_HighSoc, {}),
            (_Env, {"export_limit_kw": [2.0] * 8}),
        ):
            kw = _build_kwargs(_scenario(env, **extra))
            for pin in (-10.0, -4.0, -1.0, 0.0, 0.5, 3.0, 6.0, 15.0, 25.0):
                status = _pinned_status(kw, pin)
                blockers = _explain(kw, pin)
                with self.subTest(env=env.__name__, extra=extra, pin=pin):
                    if status == "optimal":
                        checked += 1
                        self.assertEqual(blockers, [])
                        # the loose fallback (no risk_aversion) is sound too
                        self.assertEqual(_explain(kw, pin, exact=False), [])
                    else:
                        self.assertEqual(status, "infeasible")
                        self.assertTrue(blockers, "infeasible pin left unexplained")
        self.assertGreaterEqual(checked, 15, "too few feasible pins exercised")

    def test_a_pin_that_is_not_applied_is_never_blamed(self):
        kw = _build_kwargs(_scenario(_LowSoc))
        spiked = dataclasses.replace(
            kw["batteries"][0], spike_override_discharge_kw=5.0
        )
        self.assertEqual(_explain(kw, 20.0, battery0=spiked), [])


def _two_batteries(kw, *, group_kw=None, ev_limits_kw=None) -> dict:
    """`kw` with a second battery ("ev") alongside batteries[0] ("home")."""
    home = kw["batteries"][0]
    ev = dataclasses.replace(home, name="ev")
    if ev_limits_kw is not None:
        ev = dataclasses.replace(
            ev, max_charge_kw=ev_limits_kw, max_discharge_kw=ev_limits_kw
        )
    if group_kw is not None:
        home = dataclasses.replace(
            home, shared_charger_group="g", shared_charger_max_kw=group_kw
        )
        ev = dataclasses.replace(
            ev, shared_charger_group="g", shared_charger_max_kw=group_kw
        )
    return dict(kw, batteries=[home, ev])


class TestMultiBatteryScope(unittest.TestCase):
    def test_a_shared_charger_group_is_checked(self):
        """home and ev share a 6 kW charger: an 8 kW charge pin on home is
        within its own 10 kW bound but not the group's."""
        kw = _two_batteries(_build_kwargs(_scenario()), group_kw=6.0)
        self.assertEqual(_pinned_status(kw, -8.0), "infeasible")
        self.assertEqual(_names(_explain(kw, -8.0)), ["shared_charger_g"])
        self.assertEqual(_pinned_status(kw, -5.0), "optimal")
        self.assertEqual(_explain(kw, -5.0), [])

    def test_the_other_batterys_limits_are_never_reported(self):
        """ev is limited to 1 kW; a 20 kW pin on home is feasible and blames
        nothing, and a 30 kW pin blames home's own max_discharge_kw (25)."""
        kw = _two_batteries(_build_kwargs(_scenario()), ev_limits_kw=1.0)
        self.assertEqual(_pinned_status(kw, 20.0), "optimal")
        self.assertEqual(_explain(kw, 20.0), [])
        self.assertEqual(_pinned_status(kw, 30.0), "infeasible")
        blockers = _explain(kw, 30.0)
        self.assertEqual(_names(blockers), ["battery_max_discharge"])
        self.assertIn("max_discharge_kw 25.000", blockers[0])

    def test_no_feasible_pin_is_accused_on_a_two_battery_site(self):
        for extra in ({"group_kw": 6.0}, {"ev_limits_kw": 1.0}):
            kw = _two_batteries(_build_kwargs(_scenario()), **extra)
            for pin in (-10.0, -6.0, -2.0, 0.0, 2.0, 8.0, 20.0, 25.0):
                status = _pinned_status(kw, pin)
                with self.subTest(extra=extra, pin=pin):
                    if status == "optimal":
                        self.assertEqual(_explain(kw, pin), [])
                    else:
                        self.assertTrue(_explain(kw, pin))


class TestNothingConsumesTheExplanation(unittest.TestCase):
    """Insufficient or unexplained evidence must never become an action. The
    structural guarantee: the helper and its record fields are reachable only
    from the instrument's own log-line code."""

    def test_only_solver_plan_uses_the_helper_or_its_fields(self):
        import pathlib

        root = pathlib.Path(solver_plan.__file__).resolve().parent
        allowed = {
            root / "solver_plan.py",
            root / "solver" / "pin_blockers.py",
        }
        users = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(
                token in text
                for token in ("pin_blockers", "pinned_blockers", "pinned_participant")
            ):
                users.append(path)
        self.assertTrue(users, "scan found nothing -- wrong root?")
        self.assertEqual(sorted(set(users) - allowed), [])

    def test_the_repairs_path_does_not_read_the_instrument(self):
        import pathlib

        root = pathlib.Path(solver_plan.__file__).resolve().parent
        text = (root / "setup_health.py").read_text(encoding="utf-8")
        for token in ("period0", "pin_blockers", "pinned_blockers", "#1577"):
            self.assertNotIn(token, text)


class _InstrumentOn(unittest.TestCase):
    def setUp(self):
        logger = solver_plan.PERIOD0_PIN_LOGGER
        prior = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, prior)


def _net0(plan) -> float:
    return float(plan.battery_discharge_kw[0]) - float(plan.battery_charge_kw[0])


def _with_period0(plan, net_kw):
    charge = np.array(plan.battery_charge_kw, dtype=float)
    discharge = np.array(plan.battery_discharge_kw, dtype=float)
    charge[0], discharge[0] = max(-net_kw, 0.0), max(net_kw, 0.0)
    return dataclasses.replace(
        plan, battery_charge_kw=charge, battery_discharge_kw=discharge
    )


def _assemble(kwargs, previous_plan=None):
    kwargs = dict(kwargs, previous_plan=previous_plan)
    return solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)


class TestTheInstrumentReportsIt(_InstrumentOn):
    def test_end_to_end_log_line_names_the_constraint(self):
        """Low SoC, cheap import now: the free solve charges, the previous
        plan said discharge 5 kW -- a crossing whose pin #328's reserve row
        forbids."""
        kwargs = _scenario(_LowSoc)
        free = _assemble(kwargs)
        self.assertLess(_net0(free.plan), -0.05, "scenario must charge at t0")
        prev = _with_period0(free.plan, 5.0)
        with self.assertLogs(solver_plan.PERIOD0_PIN_LOGGER, level="DEBUG") as cm:
            records = []
            orig = solver_plan.period0_crossing_delta

            def spy(*a, **k):
                records.append(orig(*a, **k))
                return records[-1]

            solver_plan.period0_crossing_delta = spy
            try:
                _assemble(kwargs, previous_plan=prev)
            finally:
                solver_plan.period0_crossing_delta = orig
        rec = records[-1]
        self.assertEqual(rec["pinned_status"], "infeasible")
        self.assertIsNone(rec["delta_objective"])
        self.assertEqual(_names(rec["pinned_blockers"]), ["discharge_reserve"])
        self.assertIn("pinned status=infeasible", cm.output[-1])
        self.assertEqual(rec["pinned_participant"], "home")
        self.assertIn("blocked_by(home)=discharge_reserve:", cm.output[-1])

    def test_published_plan_is_identical_on_and_off(self):
        kwargs = _scenario(_LowSoc)
        prev = _with_period0(_assemble(kwargs).plan, 5.0)
        on = _assemble(kwargs, previous_plan=prev)
        solver_plan.PERIOD0_PIN_LOGGER.setLevel(logging.INFO)
        off = _assemble(kwargs, previous_plan=prev)
        self.assertEqual(_fingerprint(on), _fingerprint(off))


class _Plan:
    def __init__(self, net, status="optimal", total_cost=1.0):
        self.battery_discharge_kw = [max(net, 0.0)]
        self.battery_charge_kw = [max(-net, 0.0)]
        self.status = status
        self.total_cost = total_cost
        self.periods = None


class TestOnlyOnTheInfeasibleBranch(_InstrumentOn):
    def _record(self, pinned_status, explain):
        return solver_plan.period0_crossing_delta(
            _Plan(-3.0),
            _Plan(+3.0),
            lambda _k: _Plan(+3.0, status=pinned_status, total_cost=1.5),
            explain_infeasible=explain,
        )

    def test_a_feasible_pin_never_consults_the_explainer(self):
        calls = []
        with self.assertLogs(solver_plan.PERIOD0_PIN_LOGGER, level="DEBUG") as cm:
            rec = self._record("optimal", lambda k: calls.append(k) or ["x: y"])
        self.assertEqual(calls, [])
        self.assertIsNone(rec["pinned_blockers"])
        self.assertEqual(rec["delta_objective"], 0.5)
        self.assertNotIn("blocked_by", cm.output[-1])
        self.assertTrue(cm.output[-1].endswith("inputs={}"))

    def test_a_solver_error_is_not_explained_as_a_constraint(self):
        calls = []
        rec = self._record("error", lambda k: calls.append(k) or ["x: y"])
        self.assertEqual(calls, [])
        self.assertIsNone(rec["pinned_blockers"])

    def test_nothing_found_says_so(self):
        rec = self._record("infeasible", lambda _k: [])
        self.assertEqual(rec["pinned_blockers"], [pin_blockers.UNEXPLAINED])

    def test_the_explainer_receives_the_pin(self):
        seen = []
        self._record("infeasible", lambda k: seen.append(k) or ["a: b"])
        self.assertEqual(seen, [3.0])

    def test_a_raising_explainer_is_swallowed(self):
        def boom(_k):
            raise RuntimeError("explainer exploded")

        rec = self._record("infeasible", boom)
        self.assertEqual(rec["pinned_status"], "infeasible")
        self.assertIsNone(rec["pinned_blockers"])


if __name__ == "__main__":
    unittest.main()
