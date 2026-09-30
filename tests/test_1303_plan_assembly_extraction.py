"""Phase 4 of #1298 (#1303, spec 004): plan assembly moved to `solver_plan.py`.

## Why this file exists at all, and why it is different from every other phase's

Phases 1-3 relocated existing NAMED functions and could therefore be *proved*:
strip the inserted prefixes, diff the bytes; after `ruff format`, compare
`ast.dump()`. Identity.

Phase 4 has no pre-existing function to compare against. It lifts a contiguous
244-line span of `main()`'s own body into a new function, and **the
parameterisation is the change** -- names that were `main()` locals became 27
keyword-only arguments. No "strip the prefixes and diff" applies.

#1380's review made that point and asked for a specific replacement: a deliberate
**mutation check**, because keyword-only arguments stop the call site's *order*
from drifting but do nothing about a *transposition* between two arguments of the
same shape. Counted from spec 004's own list, 18 of the inputs sit in such a
group -- 8 per-period price arrays, 3 load, 3 solar, 4 scalar kW -- where a swap
is type-correct, shape-correct, and invisible to both the type checker and the
signature.

`test_a_transposition_between_same_shaped_arguments_is_detected` is that check.
It swaps a pair within each group, one pair at a time, and requires the solve to
notice. Every swap is preceded by an assertion that the two values actually
DIFFER in the fixture, because a swap of two equal values proves nothing and is
the way this kind of test passes vacuously.

## What the mutation check found, which is better than the review assumed

All 12 transpositions probed are caught, but not all by the same mechanism, and
the difference is worth pinning:

* The six **load and solar** swaps cannot produce a wrong plan at all. They raise
  `ValueError` from `solver/elements.py`'s shared confidence-band validation
  (`lower_kw <= forecast_kw <= upper_kw at every period`), so the transposition is
  structurally impossible rather than merely detectable. That is a pre-existing
  invariant this phase inherits, not something it added.
* The **price** and **scalar kW** swaps produce a genuinely different plan --
  different battery dispatch, different total cost -- so they are caught by
  behaviour, which is what the golden master is for.

Both are asserted below, separately, so that if either mechanism ever weakens the
test says which one.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

import _solver_path  # noqa: F401
import numpy as np
import solver_plan
import solver_shared
import solver_writer
from _writer_source import writer_source_paths

AEST = timezone(timedelta(hours=10))
N = 8
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=AEST)
GRID_TIMES = [NOW + timedelta(minutes=30 * i) for i in range(N)]

# risk aversion ON, so the *_upper / *_lower confidence bands genuinely enter the
# objective -- with both dials at 0.0 an import_price/import_price_upper swap
# would be a true no-op and the check would pass vacuously.
CFG = {
    "solver_risk_aversion": 0.4,
    "solver_import_price_risk_aversion": 0.5,
    "solver_export_price_risk_aversion": 0.5,
}


class _Env:
    """Stands in for `solver_inputs.battery_soc.SocEnvelope`. Only the four
    fields `assemble_and_solve_plan()` reads are needed, and reading exactly
    those is itself part of what this pins."""

    initial_soc_kwh = 20.0
    min_soc_kwh = 4.0
    max_soc_kwh = 38.0
    charge_discharge_efficiency = 0.95


def _kwargs() -> dict:
    """A real, solvable scenario in which every same-shaped argument holds a
    DISTINCT value, so a transposition is in principle observable."""
    return {
        "now": NOW,
        "grid_times": list(GRID_TIMES),
        "period_hours_arr": [0.5] * N,
        "n_periods": N,
        "soc_envelope": _Env(),
        "capacity_kwh": 40.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 25.0,
        "charge_cost": 0.005,
        "discharge_cost_arr": [0.01] * N,
        "salvage_value": 0.06,
        "spike_override_kw": None,
        "import_price": [0.10, 0.60, 0.12, 0.62, 0.14, 0.64, 0.16, 0.66],
        "export_price": [0.03, 0.40, 0.04, 0.42, 0.05, 0.44, 0.06, 0.46],
        "import_price_upper": [0.30, 0.90, 0.32, 0.92, 0.34, 0.94, 0.36, 0.96],
        "export_price_lower": [0.01, 0.10, 0.01, 0.12, 0.01, 0.14, 0.01, 0.16],
        "export_bonus_price": [0.00, 0.20, 0.00, 0.22, 0.00, 0.24, 0.00, 0.26],
        "import_limit_kw": 15.0,
        "export_limit_kw": 30.0,
        "p2p_recent_volume_kwh": 0.0,
        "solar_kw": [0.0, 2.0, 5.0, 8.0, 6.0, 3.0, 1.0, 0.0],
        "solar_lower_kw": [0.0, 1.0, 3.0, 5.0, 4.0, 2.0, 0.5, 0.0],
        "solar_upper_kw": [0.0, 3.0, 7.0, 11.0, 9.0, 5.0, 2.0, 0.0],
        "load_kw": [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5],
        "load_lower_kw": [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
        "load_upper_kw": [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0],
        "previous_plan": None,
    }


def _fingerprint(assembly) -> tuple:
    plan = assembly.plan
    parts = []
    for attr in (
        "battery_charge_kw",
        "battery_discharge_kw",
        "grid_import_kw",
        "grid_export_kw",
        "battery_soc_kwh",
    ):
        value = getattr(plan, attr, None)
        if value is not None:
            parts.append(np.round(np.asarray(value, dtype=float), 6).tobytes())
    total = getattr(plan, "total_cost", None)
    return (
        b"|".join(parts),
        None if total is None else round(float(total), 8),
        plan.status,
    )


# (group, arg_a, arg_b) -- one or more pairs per same-shaped group
_BAND_SWAPS = [
    ("load arrays", "load_kw", "load_lower_kw"),
    ("load arrays", "load_kw", "load_upper_kw"),
    ("load arrays", "load_lower_kw", "load_upper_kw"),
    ("solar arrays", "solar_kw", "solar_upper_kw"),
    ("solar arrays", "solar_kw", "solar_lower_kw"),
    ("solar arrays", "solar_lower_kw", "solar_upper_kw"),
]
_BEHAVIOURAL_SWAPS = [
    ("price arrays", "import_price", "export_price"),
    ("price arrays", "import_price", "import_price_upper"),
    ("price arrays", "export_price", "export_price_lower"),
    ("price arrays", "export_bonus_price", "export_price_lower"),
    ("scalar kW", "max_charge_kw", "max_discharge_kw"),
    ("scalar kW", "import_limit_kw", "export_limit_kw"),
    ("scalars", "charge_cost", "salvage_value"),
]


class TestPlanAssemblyMutationCheck(unittest.TestCase):
    """#1380's review: "Swap one pair within each of the four groups, one at a
    time, and confirm the golden master fails each time. If any swap passes, the
    golden scenarios do not discriminate that input and the extraction is not
    actually covered where it matters."

    Implemented against `assemble_and_solve_plan()` directly rather than by
    mutating the golden master's own inputs: it tests the same property -- are
    these inputs discriminated -- in seconds instead of four full golden runs,
    and it names the argument pair in the failure message.
    """

    def test_the_baseline_scenario_actually_solves(self):
        """Non-vacuity. Every swap below is compared against this; if the
        baseline were infeasible, "the plan changed" would be meaningless."""
        assembly = solver_plan.assemble_and_solve_plan(dict(CFG), **_kwargs())
        self.assertEqual(assembly.plan.status, "optimal")
        self.assertIsNotNone(getattr(assembly.plan, "total_cost", None))
        # the battery must actually MOVE, or a dispatch difference cannot show up
        self.assertGreater(float(np.max(assembly.plan.battery_discharge_kw)), 0.0)
        self.assertGreater(float(np.max(assembly.plan.battery_charge_kw)), 0.0)

    def test_a_transposition_between_same_shaped_arguments_is_detected(self):
        baseline = _fingerprint(
            solver_plan.assemble_and_solve_plan(dict(CFG), **_kwargs())
        )
        for group, a, b in _BEHAVIOURAL_SWAPS:
            with self.subTest(group=group, swap=f"{a}<->{b}"):
                kwargs = _kwargs()
                self.assertNotEqual(
                    kwargs[a],
                    kwargs[b],
                    f"VACUOUS: the fixture gives {a} and {b} equal values, so "
                    "swapping them proves nothing",
                )
                kwargs[a], kwargs[b] = kwargs[b], kwargs[a]
                mutated = _fingerprint(
                    solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)
                )
                self.assertNotEqual(
                    baseline,
                    mutated,
                    f"swapping {a} and {b} produced an IDENTICAL plan -- these two "
                    "inputs are not discriminated, so a transposition between "
                    "them at the call site would be silent (#1380 review)",
                )

    def test_a_confidence_band_transposition_cannot_even_build(self):
        """The six load/solar swaps are stronger than "detected": they are
        impossible. `solver/elements.py`'s shared band validation rejects
        `lower_kw > forecast_kw` or `upper_kw < forecast_kw` at construction, so
        such a transposition raises before any solve happens.

        Asserted separately from the behavioural swaps so that if this invariant
        ever weakens, the failure says which mechanism was lost."""
        for group, a, b in _BAND_SWAPS:
            with self.subTest(group=group, swap=f"{a}<->{b}"):
                kwargs = _kwargs()
                self.assertNotEqual(kwargs[a], kwargs[b], f"VACUOUS: {a} == {b}")
                kwargs[a], kwargs[b] = kwargs[b], kwargs[a]
                with self.assertRaises(ValueError) as caught:
                    solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)
                self.assertIn("confidence band", str(caught.exception))


class TestPlanAssemblyStructure(unittest.TestCase):
    def test_the_facade_the_one_external_caller_needs_is_identity(self):
        """`solver_reports/counterfactual.py` reads
        `sw.terminal_value_breakpoints_for`. Spec 004 keeps a re-export for it;
        this is the same facade-identity convention every prior phase pinned."""
        self.assertIs(
            solver_writer.terminal_value_breakpoints_for,
            solver_plan.terminal_value_breakpoints_for,
        )

    def test_solver_plan_has_no_upward_dependency_on_solver_writer(self):
        """The property that makes this phase unusual: unlike every other module
        #1298 extracted, `solver_plan.py` needs NO deferred `_solver_writer()`
        seam. `previous_plan` is a parameter precisely so that
        `load_previous_plan()` -- which stays in `solver_writer.py` -- is called
        by `main()` instead of from here.

        If this ever fails, `nimbus-layers` needs an `ignore_imports` exception
        and the module docstring's claim is stale."""
        tree = ast.parse(pathlib.Path(solver_plan.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    imported.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        offenders = sorted(n for n in imported if "solver_writer" in n)
        self.assertEqual(
            offenders, [], f"solver_plan.py imports solver_writer: {offenders!r}"
        )
        # AST, not a text search. This module's own docstring DISCUSSES the
        # `_solver_writer()` seam and the `sw.` prefix in order to say it has
        # neither, so `assertNotIn("_solver_writer", source)` fails on prose --
        # the same docstring-is-not-code trap that nearly corrupted five
        # docstrings in Phase 5 (#1304).
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("_solver_writer", defined)
        sw_reads = sorted(
            {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "sw"
            }
        )
        self.assertEqual(sw_reads, [], f"solver_plan.py reads sw.{sw_reads}")

    def test_the_logger_is_reached_through_solver_shared_not_the_sw_seam(self):
        """#1380's review, repeating the point it made on #1370: qualify the
        moved span's `_LOGGER` as `sw._LOGGER` and
        `test_callers_mode_counts_only_real_references.py:178` fails, because it
        asserts ZERO real `sw._LOGGER` references outside `solver_writer.py`.
        `solver_shared._LOGGER` is the form that satisfies both."""
        source = pathlib.Path(solver_plan.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "debug",
                "info",
                "warning",
                "error",
            }:
                target = node.value
                if isinstance(target, ast.Attribute) and target.attr == "_LOGGER":
                    self.assertIsInstance(target.value, ast.Name)
                    self.assertEqual(target.value.id, "solver_shared")
                    calls += 1
                elif isinstance(target, ast.Name) and target.id == "_LOGGER":
                    self.fail(
                        "bare _LOGGER in solver_plan.py -- must be solver_shared._LOGGER"
                    )
        self.assertEqual(
            calls,
            3,
            "expected the span's two #757 diagnostics plus #489's "
            "deferred-ranging DEBUG line",
        )
        self.assertIsNotNone(solver_shared._LOGGER)

    def test_build_plan_is_called_with_exactly_the_same_keywords_as_before(self):
        """Spec 004's own Invariants: "no argument reordered, no default changed,
        no field renamed". This pins the keyword SET of the moved
        `network.build_plan()` call, so a dropped or renamed argument fails here
        rather than showing up as a quietly different plan."""
        tree = ast.parse(pathlib.Path(solver_plan.__file__).read_text(encoding="utf-8"))
        found = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "build_plan"
            ):
                found = node
        self.assertIsNotNone(found, "no network.build_plan() call in solver_plan.py")
        self.assertEqual(found.args, [], "build_plan must be called keyword-only")
        self.assertEqual(
            sorted(k.arg for k in found.keywords),
            [
                "adequacy_loads",
                "batteries",
                "battery_charge_earliness_budget_kw",
                "compute_offer_curve",
                "compute_signals",
                "export_price_risk_aversion",
                "grid",
                "import_price_risk_aversion",
                "loads",
                "periods",
                "previous_plan",
                "proximal_weight",
                "risk_aversion",
                "sheddable_loads",
                "smoothness_weight",
                "solar",
                "solve_options",
                "thermal_loads",
            ],
        )

    def test_the_result_object_carries_every_output_main_still_reads(self):
        """Spec 004 declares `-> network.Plan`. Measured against `main()`, the
        rest of the function reads EIGHT names the span binds, not one, and all
        but `plan` feed `publish_plan()`'s argument list. This pins the eight, so
        a future edit that drops one fails here instead of raising `NameError`
        deep inside a solve cycle."""
        fields = sorted(solver_plan.PlanAssembly.__dataclass_fields__)
        self.assertEqual(
            fields,
            [
                "all_batteries",
                "export_price_risk_aversion",
                "fleet_capacity_kwh",
                # nimbus issue #489: the ninth -- main() passes it to both flex
                # publishers so a deferred-ranging cycle holds their payload.
                "flex_ranging_deferred",
                "grid",
                "import_price_risk_aversion",
                "plan",
                "risk_aversion",
                "solve_started",
            ],
        )

    def test_the_moved_names_left_solver_writer(self):
        """`RISK_AVERSION` and `midnight_boundary_period_indices` move with the
        span and need no facade -- zero code references either one outside
        `solver_plan.py`. `terminal_value_breakpoints_for` does need one (above),
        so it is deliberately still reachable on `solver_writer`."""
        writer = pathlib.Path(solver_writer.__file__).read_text(encoding="utf-8")
        tree = ast.parse(writer)
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("midnight_boundary_period_indices",):
            self.assertNotIn(
                name, defined, f"{name} should have moved to solver_plan.py"
            )
        assigned = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertNotIn("RISK_AVERSION", assigned)
        # and the span itself is gone from solver_writer.py
        self.assertNotIn("elements.BatteryConfig(", writer)

    def test_solver_plan_is_part_of_the_writer_source_union(self):
        """`tests/_writer_source.py` is what keeps "which file is this code in"
        assertions working across #1298's phases. A module extracted without
        being added to it silently shrinks what those tests see."""
        names = {pathlib.Path(p).name for p in writer_source_paths()}
        self.assertIn("solver_plan.py", names)


if __name__ == "__main__":
    unittest.main()
