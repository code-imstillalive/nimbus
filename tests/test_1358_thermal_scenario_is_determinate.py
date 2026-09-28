"""The `kind=thermal` golden scenarios stay determinate, and the pair keeps
discriminating (nimbus issue #1358).

`golden/scenarios_thermal.py`'s docstring argues three things. All three are
claims about numbers, so they are checked here rather than trusted:

1. the hard temperature constraint demands 0.90 kWh of a 3.0 kWh window, so the
   scenario has real slack and sits nowhere near the infeasibility-relaxation
   path a snapshot must not be balanced on;
2. the prices the LP sees are all distinct, so cheapest-first is a total order
   and the placement is unique rather than a tie HiGHS happens to break one way;
3. the two ordinary scenarios differ **only** in the price forecast, and their
   recorded dispatch decisions differ because of it.

(3) is the one worth a test rather than a comment. Either scenario alone proves
little -- "did not dispatch" is also what a load that was never built produces,
and "dispatched immediately" is also what a solver ignoring price would do. If a
change ever makes the pair agree, both snapshots still regenerate happily and
the coverage numbers stay put, while the thing they were recorded to show has
quietly stopped being shown. That is the failure this file exists to catch.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from golden import scenarios_thermal as st

SNAPSHOTS = pathlib.Path(__file__).parent / "golden" / "snapshots"


def _snapshot(name: str) -> dict:
    return json.loads((SNAPSHOTS / f"{name}.json").read_bytes())


def _dispatch_calls(name: str) -> list[dict]:
    """Every service call the scenario's first cycle made on the tank."""
    return [
        c
        for c in _snapshot(name)["cycles"][0]["service_calls"]
        if c.get("data", {}).get("entity_id") == st.TANK
    ]


class TestTheDerivation(unittest.TestCase):
    def test_the_constraint_demands_the_energy_the_docstring_says(self):
        self.assertAlmostEqual(
            st.energy_the_constraint_demands(st.THERMAL_TARGET_C), 0.90, places=9
        )
        self.assertAlmostEqual(st.window_capacity_kwh(), 3.0, places=9)

    def test_there_is_real_slack_rather_than_a_knife_edge(self):
        """The whole reason thermal was left out of `scenarios_native`: a
        configuration that only just meets its target sits a rounding error from
        the relaxation path. This one uses under a third of the window."""
        need = st.energy_the_constraint_demands(st.THERMAL_TARGET_C)
        capacity = st.window_capacity_kwh()
        self.assertLess(
            need / capacity,
            0.5,
            f"the thermal scenario now needs {need:.3f} kWh of a {capacity:.1f} kWh "
            f"window ({need / capacity:.0%}). Above half, it is drifting back "
            f"towards the knife edge #1358 exists to avoid -- lower the target or "
            f"widen the window rather than accepting it.",
        )

    def test_the_relaxed_scenario_is_unreachable_by_a_margin(self):
        need = st.energy_the_constraint_demands(st.THERMAL_UNREACHABLE_TARGET_C)
        capacity = st.window_capacity_kwh()
        self.assertGreater(
            need,
            capacity * 1.25,
            f"the relaxation scenario needs {need:.3f} kWh of {capacity:.1f} kWh. "
            f"It must be unreachable by a WIDE margin, not a near miss -- a near "
            f"miss is the knife edge, just approached from the other side.",
        )


class TestThePricesAdmitNoTie(unittest.TestCase):
    def test_every_price_the_lp_sees_is_distinct(self):
        for ladder_name in ("DEFER_LADDER", "HEAT_NOW_LADDER"):
            ladder = getattr(st, ladder_name)
            seen = st.prices_the_lp_sees(ladder)
            with self.subTest(ladder=ladder_name):
                dupes = sorted({p for p in seen if seen.count(p) > 1})
                self.assertEqual(
                    dupes,
                    [],
                    f"{ladder_name} makes the LP see {dupes} more than once "
                    f"(period 0 is the LIVE price, not ladder[0] -- that is how "
                    f"the first draft accidentally tied 0.30 with itself). A tie "
                    f"means the placement is HiGHS's choice, not the price's.",
                )

    def test_defer_ladder_puts_its_cheapest_energy_in_the_future(self):
        seen = st.prices_the_lp_sees(st.DEFER_LADDER)
        self.assertLess(
            min(seen[1:]),
            seen[0],
            "the defer scenario needs cheaper energy later than now, or waiting "
            "is not the optimum and the pair stops discriminating.",
        )

    def test_heat_now_ladder_puts_its_cheapest_energy_now(self):
        seen = st.prices_the_lp_sees(st.HEAT_NOW_LADDER)
        self.assertEqual(
            seen[0],
            min(seen),
            "the heats-now scenario needs NOW to be the cheapest period in the "
            "window, which means every array value must sit above the live "
            f"price {st.LIVE_IMPORT_PRICE}.",
        )


class TestThePairStillDiscriminates(unittest.TestCase):
    """Read from the committed snapshots, so a regeneration that collapses the
    pair fails here instead of passing quietly."""

    def test_the_defer_scenario_records_no_dispatch(self):
        calls = _dispatch_calls("native_thermal_load_defers")
        self.assertEqual(
            calls,
            [],
            f"native_thermal_load_defers dispatched {calls}. Its whole point is "
            f"that the cheapest energy is later, so the optimum is to wait.",
        )

    def test_the_heats_now_scenario_records_a_dispatch(self):
        calls = _dispatch_calls("native_thermal_load_heats_now")
        self.assertEqual(
            [(c["domain"], c["service"]) for c in calls],
            [("water_heater", "set_operation_mode")],
            f"native_thermal_load_heats_now recorded {calls}. Its whole point is "
            f"that now IS the cheapest period, so the optimum is to start.",
        )

    def test_the_two_scenarios_differ_only_in_price(self):
        """Guards the guard. If the two fixtures ever diverge in some other way,
        the pair stops isolating price as the cause of the difference in
        dispatch, and the argument in the module docstring stops holding even
        while both assertions above still pass."""
        defer = st._states(st.DEFER_LADDER)
        heat = st._states(st.HEAT_NOW_LADDER)
        self.assertEqual(
            sorted(defer),
            sorted(heat),
            "the two thermal fixtures no longer publish the same set of entities.",
        )
        differing = sorted(k for k in defer if defer[k] != heat[k])
        self.assertEqual(
            differing,
            [st.PRICE_ARRAY],
            f"the two thermal fixtures differ in {differing}, not only in the "
            f"price forecast. The pair's argument is that price is the ONLY "
            f"difference, so anything else here has to be removed or the "
            f"docstring's claim has to be weakened to match.",
        )


class TestTheRelaxationIsProvable(unittest.TestCase):
    """The relaxation scenario must actually relax, which the first version did
    not (nimbus #1358 follow-up).

    `THERMAL_UNREACHABLE_TARGET_C` was 54.0, derived from an assumed 1-hour /
    3.0 kWh window. The window the scenario really resolves to is periods 0..12
    of the 202-period grid -- 1.5 h and ~4.5 kWh, because the grid coarsens --
    and 54.0 needs ~4.75 kWh, a 6% overshoot on paper that landed on the
    FEASIBLE side in practice. So a scenario named `native_thermal_relaxed`
    solved optimal and never relaxed, sitting on exactly the knife edge the
    module docstring warns against.

    Verified against the real path rather than re-derived: 54.0 does not relax,
    65.0 and 80.0 both do.

    `solver/network.py` logs a WARNING naming each relaxed load, and the harness
    records WARNING and above, so the record CAN show this -- which also
    corrects the first version's claim that the relaxation is "not logged".
    """

    RELAXED = "hard-constrained thermal LP came back infeasible"

    def _warnings(self, name: str) -> list[str]:
        return [
            str(w.get("message", "")) for w in _snapshot(name)["cycles"][0]["warnings"]
        ]

    def test_the_relaxed_scenario_records_the_relaxation(self):
        hits = [
            w for w in self._warnings("native_thermal_relaxed") if self.RELAXED in w
        ]
        self.assertEqual(
            len(hits),
            1,
            "native_thermal_relaxed does not record the relaxation warning, so it "
            "is not exercising the path it is named for -- which is exactly how "
            "the 54.0 target went unnoticed. Raise the target until it does, and "
            "check against the real path rather than the arithmetic.",
        )
        self.assertIn("Tank", hits[0])

    def test_the_two_ordinary_scenarios_do_not_relax(self):
        """The other half: if these ever relax, their own derivation has drifted
        towards infeasible and their placements stop meaning anything."""
        for name in ("native_thermal_load_defers", "native_thermal_load_heats_now"):
            with self.subTest(scenario=name):
                hits = [w for w in self._warnings(name) if self.RELAXED in w]
                self.assertEqual(
                    hits,
                    [],
                    f"{name} relaxed its thermal guarantee. It is supposed to sit "
                    f"well inside feasible -- about 1.15 kWh of a 4.5 kWh window.",
                )

    def test_no_snapshot_records_a_machine_specific_path(self):
        """A relaxation scenario reaches #773's failing-model dump every time,
        which names a temp file. Recording it verbatim would make the snapshot
        pass only on the machine that wrote it -- the once-per-machine failure
        #1330/#1331 found for /tmp state paths. `canonical()` normalises the
        temp directory to `<TMP>/`."""
        for path in sorted(SNAPSHOTS.glob("*.json")):
            with self.subTest(snapshot=path.name):
                raw = path.read_text(encoding="utf-8")
                for marker in ("AppData", "/tmp/", "C:" + chr(92) + chr(92) + "Users"):
                    self.assertNotIn(
                        marker,
                        raw,
                        f"{path.name} records {marker!r}, which is specific to the "
                        f"machine that wrote it.",
                    )


if __name__ == "__main__":
    unittest.main()
