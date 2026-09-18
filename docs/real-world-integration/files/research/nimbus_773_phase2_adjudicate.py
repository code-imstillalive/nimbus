"""nimbus #773: adjudicate a `phase2_secondary` answer independently of
HiGHS's own status reporting.

## Why this exists

On 2026-09-18 the shipped `CalibratedOptions` path was measured returning a
`phase2_secondary` solution **1.89% worse on the secondary objective** than a
different integer assignment that is feasible for the same tie row -- while
reporting `status=Optimal`.

**CORRECTED the same day, and the correction is the point of reading this
docstring rather than only running the script.** The first framing was "the
dual bound is not valid, the optimality claim is wrong." Two follow-up
experiments do not support that:

1. **Take the warm start out entirely.** Dump the phase-2 model, reload it in a
   FRESH Highs instance with `mip_rel_gap = mip_abs_gap = 0`, solve from
   scratch. **Five of six scenarios agree with the in-process run**, including
   the one used as the headline example. An independent method reaches the same
   answer, so "the proof is invalid" is not supported.
2. **Measure where the better point actually sits.** Against the tie row's real
   bound, with the LP's primal tolerance tightened to 1e-9:

       tie row:  primary_expr <= 38.388375263643
       in-process assignment   activity 38.388375163643   headroom 1.0e-07
       the better assignment   activity 38.388375263643   headroom 1.4e-14

So the better solution is feasible and sits **exactly on the tie-row boundary**,
at machine precision. Branch-and-bound works inward from the relaxation and
prunes on bounds computed to ~1e-7; a point lying ON a constraint the algorithm
derived itself is below the resolution the search operates at. Handing it over
works; finding it does not.

**The supportable claim:** phase 2's true optimum can lie exactly on the
tie-row boundary, where the search cannot reach it. The reported answer is
optimal to the resolution the search uses; the better point exists below that
resolution. That makes `_LEX_PRIMARY_TIE_ABS_SLACK = 1e-7` structural rather
than incidental -- it defines a band whose optimum is at its own edge.

**One scenario points the other way and is deliberately not explained away.**
16x24 s2's in-process run reports a value BETTER than both the fresh re-solve
and the warm run, and its own assignment returns `Solve error` when pinned and
re-solved at a 1e-9 primal tolerance. There the production answer is the one
that fails scrutiny. One case in six is not a distribution; it is recorded
because it is the half most easily lost.

This script settles the comparison from outside the solve that produced it:

1. dump the phase-2 model to `.mps` at the instant phase 2 is about to run --
   the tie row and the secondary cost vector are both already in place by then
   (see `lp._lex_or_calibrated_solve()`: `addConstr` then `_set_cost_vector`
   then `_ensure_optimal_value(phase="phase2_secondary")`);
2. capture the binary assignment each run actually chose;
3. reload the model **fresh**, pin the binaries to each assignment in turn, and
   solve the resulting pure LP.

A pinned LP that returns `Optimal` proves its assignment is feasible for the tie
row. Whichever feasible assignment has the lower secondary objective is the
better solution -- and any run that reported a higher value as optimal was
wrong, whatever its own gap said.

## What is NOT established

The mechanism, still. The boundary measurement above says WHERE the unreachable
optimum sits, not why the band is posed at a width whose edge matters:
`mip_feasibility_tolerance` is 1e-6, ten times wider than the 1e-7 band, and
`primal_feasibility_tolerance` is 1e-7, the same order as the band itself. Same
tolerance-mismatch family as the one #979 fixed at `phase2_pin_resolve` -- and
still a coincidence of scale until something discriminates it.

**The obvious remedy is confounded.** Widening `_LEX_PRIMARY_TIE_ABS_SLACK`
lowers the secondary objective at every step, but a wider band is a strictly
larger feasible set, so that is guaranteed regardless of mechanism -- and by 1e-5
it gives away real primary cost, which is what the tie row exists to prevent. Do
not read a widening sweep as evidence.

## Usage

Run from a checkout with the dev extra installed. No HA, no install, no network:

    python docs/real-world-integration/files/research/nimbus_773_phase2_adjudicate.py

Known branching scenarios (phase 2 resolves at the root on most sizes, which is
why two earlier attempts to measure this had no power -- they swept upward in
size, and the branching points are at *smaller* horizons with more loads):

    24x24 seed 0/1/2, 32x48 seed 0, 16x24 seed 2, 10x96 seed 3
"""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO / "custom_components" / "nimbus_load"))

import highspy
import numpy as np
from solver import lp
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import CalibratedOptions
from solver.network import build_plan

_REAL_ENSURE = lp._ensure_optimal_value

# Deliberately the same shape as
# tests/test_773_tolerance_and_tie_slack_end_to_end.py's own
# _many_binaries_tied_price_scenario(), so results here are comparable with
# that file's sweeps rather than being a third independent approximation.
BRANCHING_SCENARIOS = [(24, 24, 1), (24, 24, 0), (32, 48, 0), (16, 24, 2)]


def scenario(*, n_loads: int, n_periods: int, seed: int, price_noise: float = 1e-5):
    """Many semi-continuous adequacy loads over near-flat prices, so the LP has
    little genuine economic preference between periods and the binaries carry
    all the flexibility. The battery is given zero charge/discharge power
    deliberately, to maximise binary columns for a given size."""
    rng = np.random.default_rng(seed)
    periods = PeriodGrid(
        hours=np.full(n_periods, 1.0),
        start=datetime(2026, 9, 16, 0, 0, 0, tzinfo=UTC),
    )
    grid = GridConfig(
        import_price=0.20 + rng.normal(0, price_noise, n_periods),
        export_price=np.zeros(n_periods),
        import_limit_kw=100.0,
        export_limit_kw=100.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n_periods))
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=20.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
    adequacy = [
        AdequacyLoadConfig(
            name=f"load{i}",
            max_power_kw=float(rng.uniform(1, 5)),
            target_kwh=float(rng.uniform(2, 10)),
            deadline_period=n_periods - 1,
            earliest_period=0,
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
        )
        for i in range(n_loads)
    ]
    return periods, grid, battery, solar, adequacy


def solve_capturing_phase2(
    n_loads: int, n_periods: int, seed: int, *, warm: bool, dump: Path | None
) -> dict:
    """One full `build_plan()`, capturing what `phase2_secondary` reported and
    which binary assignment it chose.

    `warm=True` hands phase 1's own column solution to phase 2 via the SPARSE
    `setSolution(n, index, value)` overload. That is NOT a proposed fix -- it
    changes the answer and must not ship (see #999) -- it is the discriminator
    that exposes the disagreement.
    """
    captured: dict = {}
    result: dict = {}

    def spy(h, *, phase="", problem=None, binary_cols=None):
        if phase == "phase2_secondary":
            if dump is not None and "dumped" not in result:
                h.writeModel(str(dump))
                result["dumped"] = True
            if warm and "cols" in captured:
                cols = captured["cols"]
                idx = np.arange(len(cols), dtype=np.int32)
                h.setSolution(len(cols), idx, np.asarray(cols, dtype=np.float64))
        value = _REAL_ENSURE(h, phase=phase, problem=problem, binary_cols=binary_cols)
        if phase == "phase1_primary":
            captured["cols"] = list(h.getSolution().col_value)
        if phase == "phase2_secondary" and "value" not in result:
            cols = list(h.getSolution().col_value)
            info = h.getInfo()
            result.update(
                value=value,
                bins=[round(cols[i]) for i in (binary_cols or [])],
                binary_cols=list(binary_cols or []),
                gap=info.mip_gap,
                dual=info.mip_dual_bound,
                nodes=info.mip_node_count,
            )
        return value

    lp._ensure_optimal_value = spy
    # Clear the #773 cooldown, or an earlier failure in this same process
    # silently skips the phased path and the run measures nothing.
    lp._lex_calibration_failed_until = 0.0
    try:
        periods, grid, battery, solar, adequacy = scenario(
            n_loads=n_loads, n_periods=n_periods, seed=seed
        )
        build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            adequacy_semi_continuous=True,
            solve_options=CalibratedOptions(),
        )
    finally:
        lp._ensure_optimal_value = _REAL_ENSURE
    return result


def evaluate_pinned(
    mps: Path, binary_cols, assignment
) -> tuple[str, float | None, float | None]:
    """Reload the dumped phase-2 model, pin every binary to `assignment`, and
    solve the resulting pure LP. Returns (status, objective, tie-row activity).

    The tie row is the last row in the model, since it is the last thing
    `_lex_or_calibrated_solve()` adds before phase 2 runs.
    """
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.readModel(str(mps))
    n_rows = h.getNumRow()
    for col, val in zip(binary_cols, assignment, strict=True):
        h.changeColBounds(col, float(val), float(val))
    h.run()
    status = h.modelStatusToString(h.getModelStatus())
    if status != "Optimal":
        return status, None, None
    return (
        status,
        float(h.getObjectiveValue()),
        list(h.getSolution().row_value)[n_rows - 1],
    )


def adjudicate(n_loads: int, n_periods: int, seed: int, out_dir: Path) -> None:
    print(f"\n{'=' * 74}")
    print(f"{n_loads} loads x {n_periods} periods, seed {seed}")
    print("=" * 74)
    mps = out_dir / f"nimbus773_phase2_{n_loads}x{n_periods}s{seed}.mps"
    cold = solve_capturing_phase2(n_loads, n_periods, seed, warm=False, dump=mps)
    warm = solve_capturing_phase2(n_loads, n_periods, seed, warm=True, dump=None)

    for name, run in (("cold", cold), ("warm", warm)):
        print(
            f"  {name}: reported {run['value']:.10f}  gap={run['gap']:.3e}  "
            f"dual={run['dual']:.10f}  nodes={run['nodes']}"
        )

    if cold["bins"] == warm["bins"]:
        print("  -> identical assignments; nothing to adjudicate here")
        return

    differing = sum(
        1 for a, b in zip(cold["bins"], warm["bins"], strict=True) if a != b
    )
    print(f"  assignments differ in {differing} of {len(cold['bins'])} binaries")
    print("\n  Independent re-evaluation (fresh model, binaries pinned, pure LP):")

    verdicts: dict[str, tuple[str, float | None, float | None]] = {}
    for name, run in (("cold", cold), ("warm", warm)):
        status, obj, activity = evaluate_pinned(mps, cold["binary_cols"], run["bins"])
        verdicts[name] = (status, obj, activity)
        shown = f"{obj:.10f}" if obj is not None else "n/a"
        act = f"{activity:.12f}" if activity is not None else "n/a"
        print(f"    {name}: {status:<12} secondary {shown}  tie-row activity {act}")

    feasible = {k: v for k, v in verdicts.items() if v[0] == "Optimal"}
    if len(feasible) == 2:
        best = min(feasible, key=lambda k: feasible[k][1])
        worst = "warm" if best == "cold" else "cold"
        delta = feasible[worst][1] - feasible[best][1]
        act_delta = feasible[worst][2] - feasible[best][2]
        print(
            f"\n  BOTH feasible for the same tie row. {best} is better by "
            f"{delta:.10f} ({100 * delta / abs(feasible[best][1]):.2f}%).\n"
            f"  => the {worst} run reported a strictly worse solution as optimal; "
            f"its dual bound is not a valid lower bound.\n"
            f"  tie-row activity difference: {act_delta:.3e} "
            f"(_LEX_PRIMARY_TIE_ABS_SLACK = {lp._LEX_PRIMARY_TIE_ABS_SLACK:g})"
        )
    elif len(feasible) == 1:
        ok = next(iter(feasible))
        bad = "warm" if ok == "cold" else "cold"
        print(
            f"\n  Only {ok}'s assignment solved; {bad}'s returned "
            f"{verdicts[bad][0]} when pinned.\n"
            f"  NOTE: a non-Optimal pinned solve is not by itself proof of "
            f"infeasibility -- it can be a numerical failure of this probe. "
            f"Treat as inconclusive rather than as evidence."
        )
    else:
        print("\n  Neither assignment solved when pinned -- inconclusive.")


def main() -> None:
    logging.disable(logging.CRITICAL)
    # Deliberately a temp dir, not this file's own directory: each dump is a
    # multi-megabyte .mps of a ~12k-column model, and writing them next to the
    # source is how they end up accidentally committed.
    with tempfile.TemporaryDirectory(prefix="nimbus773_") as tmp:
        out_dir = Path(tmp)
        print(f"model dumps -> {out_dir}")
        for n_loads, n_periods, seed in BRANCHING_SCENARIOS:
            adjudicate(n_loads, n_periods, seed, out_dir)


if __name__ == "__main__":
    main()
