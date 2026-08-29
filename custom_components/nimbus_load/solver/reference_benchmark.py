"""A standardized, synthetic reference-household benchmark scenario for
comparing Nimbus's own forecast-regret decomposition across releases.

Nimbus issue #273 (Mark Purcell), item #3: *"a standardized reference-
household scenario (synthetic, not any one real install) to be a fair,
comparable benchmark across releases."* Items #1 (compute_forecast_
regret()) and #2 (the design pushback on feeding HAEO) are both already
resolved on that issue -- this module is #3.

## Why this needs to be SYNTHETIC, not a captured real day

Every other regret/EPR tool in this package (quality_report.py,
forecast_regret.py itself, the various scripts/research/*.py one-off
investigations in the sibling 116KAT-HA-AI repo) deliberately runs
against REAL, captured household data -- that's the right choice for
"how did we actually do last night," but it's the WRONG choice for "did
this release make the Solver's own regret/EPR math genuinely better or
worse," because a real day's own weather, price shocks, and one-off
incidents (a NUC failover, a noisy sensor) dominate any two real days'
difference far more than a code change ever could. Comparing v0.94.20's
real Tuesday against v0.94.25's real Thursday tells you almost nothing
about the code. A FIXED, deterministic, versioned synthetic scenario
holds every real-world variable constant across releases, so a change
in the benchmark's own reported numbers can only come from a genuine
change in the Solver/regret logic itself.

## Design principles, in order of priority

1. **Fully deterministic.** No unseeded randomness anywhere -- the
   forecast/persistence "error" mechanisms below are either purely
   analytic (a fixed lag + fixed scale factor) or use `np.random.
   default_rng` with a hardcoded seed. Running this twice, on the same
   Nimbus version, must produce byte-identical output.
2. **Generic, not household-specific.** Every number here (battery
   capacity, solar size, load shape, price levels) is a round,
   documented, plausible residential figure -- deliberately NOT this
   project's own real reference household's actual config (122.2kWh
   battery, 11.5kW P2P block, etc.), per this project's own standing
   "should work for anyone" principle (see BatteryConfig's own
   docstring on Mark's audit item #8 for the same reasoning applied to
   a different question). Anyone running Nimbus on their own hardware
   should be able to run THIS SAME scenario and get a comparable number
   to quote against this project's own published benchmark history.
3. **Exercises real production code, not a re-implementation.**
   `run_reference_benchmark()` calls `compute_forecast_regret()`
   directly -- the exact function `nimbus_solver_quality_writer.py`
   wires into production (issue #273 item #1, 116KAT-HA-AI PR #787).
   If that function's own LP/regret math changes, this benchmark's
   score moves; if this module's own scenario data changes, it doesn't
   test anything real. Never duplicate build_plan()/evaluate_realized_
   cost() logic here.
4. **A number to WATCH, not a pass/fail gate.** This is deliberately
   NOT wired into CI as a hard-fail assertion on an exact value --
   per issue #217's own conclusion (a soak-window/RC-process decision
   is the project owner's call, not something to impose unilaterally),
   the right use of this tool is: run it before and after a Solver-
   affecting change, look at whether nimbus_value_add_dollars moved and
   in which direction, and record that in the CHANGELOG entry for the
   change -- the same way a performance benchmark gets quoted, not
   gated on. tests/test_reference_benchmark.py checks the SCENARIO's
   own structural invariants (determinism, oracle-never-beaten) and
   loose sanity bounds, deliberately not an exact-value regression test
   that would need updating every time the Solver legitimately improves.

## What this deliberately does NOT cover

This benchmarks the SOLVER package only (build_plan, regret, EPR) via a
synthetic solar/load/price scenario -- it does NOT exercise the ML
Forecaster (coordinator.py, ml/model.py). The real k-NN/GBRT forecaster
needs genuine HA recorder history to train against; there is no
portable, zero-HA-dependency way to run it standalone the way every
other module in solver/ is designed to be run (see this package's own
README for that constraint). A synthetic "Forecaster accuracy"
benchmark would need its own, separately-scoped design -- not attempted
here. What "solar_forecast_kw"/"load_forecast_kw" mean in this module
is a synthetic, fixed-error PROXY for "whatever a forecaster produced,"
not a claim about how well Nimbus's own Forecaster performs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, PeriodGrid
from .forecast_regret import ForecastRegretResult, compute_forecast_regret

# Version of the SCENARIO ITSELF (battery/grid/solar/load shape, error
# mechanism), independent of Nimbus's own manifest.json version. Bump
# this if the scenario's own inputs ever change -- a benchmark result
# tagged with a different REFERENCE_HOUSEHOLD_VERSION is NOT comparable
# to an earlier one, since the thing being measured changed, not just
# the code measuring it. Keep this at "1.0" for any change that doesn't
# alter the scenario's own numbers (docs, refactors, new metadata
# fields on the result).
REFERENCE_HOUSEHOLD_VERSION = "1.0"

_N_PERIODS = 24  # one real day, hourly periods -- coarse enough to run
# in well under a second (no need for this horizon's own finer tiers;
# see nimbus_solver_forecast_writer.py's own multi-tier grid for what
# production actually solves against -- this benchmark deliberately
# trades that resolution away for a fast, simple, exactly-reproducible
# scenario).
_HOUR = np.arange(_N_PERIODS)

# A fixed seed, used ONLY for the forecast-error mechanism below --
# never for the scenario's own real/persistence curves, which are pure
# analytic functions of hour-of-day.
_RNG_SEED = 273  # nimbus issue #273, for traceability -- not a magic number


@dataclass(frozen=True)
class ReferenceBenchmarkResult:
    """One run's full result, with enough metadata that a saved JSON
    dump is self-describing months later without needing this module's
    own source to interpret it."""

    scenario_version: str
    regret: ForecastRegretResult

    @property
    def nimbus_value_add_dollars(self) -> float:
        return self.regret.nimbus_value_add_dollars


def _reference_grid() -> GridConfig:
    """A generic residential time-of-use price shape -- flat off-peak,
    a real evening peak window, flat (lower) export. No P2P bonus
    mechanics (export_bonus_price/volume_kwh, fixed_export_kw) --
    deliberately, since a P2P-style export program is a specific
    retailer/region arrangement, not a universal feature of "a
    household with solar and a battery." Including it here would make
    this benchmark implicitly test P2P-specific code paths that most
    Nimbus installs never exercise at all.
    """
    import_price = np.where(
        (_HOUR >= 16) & (_HOUR < 21),
        0.35,  # real evening peak, a plausible AU residential TOU rate
        np.where((_HOUR >= 7) & (_HOUR < 16), 0.22, 0.18),  # shoulder / off-peak
    )
    export_price = np.full(_N_PERIODS, 0.07)  # flat, generic feed-in tariff
    return GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=15.0,
        export_limit_kw=10.0,
    )


def _reference_battery() -> BatteryConfig:
    """A generic Powerwall-class residential battery -- 15kWh usable,
    10kW inverter-limited charge/discharge, 90% round-trip (split evenly
    across the charge/discharge legs via sqrt, matching this project's
    own established convention for a blended round-trip figure -- see
    BatteryConfig's own docstring on charge_efficiency/discharge_
    efficiency meaning the SYSTEM's blended figure, not a raw cell spec).
    """
    round_trip = 0.90
    leg_efficiency = float(np.sqrt(round_trip))
    return BatteryConfig(
        capacity_kwh=15.0,
        initial_soc_kwh=7.5,  # start at 50% -- no free head start either direction
        min_soc_kwh=1.5,  # 10% floor
        max_soc_kwh=15.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=leg_efficiency,
        discharge_efficiency=leg_efficiency,
        # Both real, above elements.py's own MIN_CHARGE_DISCHARGE_COST_
        # SPREAD floor -- a generic, small, non-wash-trade-degenerate
        # cost pair, not tuned to any real household's own live values.
        charge_cost=0.005,
        discharge_cost=0.015,
        salvage_value=0.10,
    )


def _reference_real_curves() -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The scenario's own "ground truth" solar and load for the day
    being scored -- pure analytic functions of hour-of-day, no
    randomness. Same shape family as the existing test_solver_forecast_
    regret.py fixture (a clipped-sine solar hump, a load curve with
    morning + evening peaks), generalized to round, documented generic
    numbers rather than that test's own arbitrary constants.
    """
    solar_kw = np.clip(6.0 * np.sin((_HOUR - 6.0) / 12.0 * np.pi), 0.0, None)
    load_kw = (
        0.6  # overnight base load
        + 0.6 * np.clip(np.sin((_HOUR - 6.0) / 8.0 * np.pi), 0.0, None)  # morning bump
        + 2.2 * ((_HOUR >= 17) & (_HOUR < 21))  # real evening peak
    )
    return solar_kw, load_kw


def _reference_forecast_curves(
    solar_real_kw: NDArray[np.float64], load_real_kw: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """A synthetic, fixed-seed, BOUNDED forecast error -- a proxy for
    "whatever a real forecaster produced," not a claim about Nimbus's
    own Forecaster's real accuracy (see module docstring's own scope
    note). Genuinely better than raw persistence (smaller, unbiased
    noise around the true curve) so a healthy Solver/regret pipeline
    should show a positive nimbus_value_add_dollars on this scenario --
    if a future change made that go negative, that's exactly the kind
    of real signal this benchmark exists to surface.
    """
    rng = np.random.default_rng(_RNG_SEED)
    solar_noise = rng.normal(0.0, 0.4, _N_PERIODS)
    load_noise = rng.normal(0.0, 0.15, _N_PERIODS)
    solar_forecast_kw = np.clip(solar_real_kw + solar_noise, 0.0, None)
    load_forecast_kw = np.clip(load_real_kw + load_noise, 0.0, None)
    return solar_forecast_kw, load_forecast_kw


def _reference_persistence_curves(
    solar_real_kw: NDArray[np.float64], load_real_kw: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """A naive "same hour, a fixed lag back" baseline -- deliberately
    NOT identical to solar_real_kw/load_real_kw (a genuine persistence
    baseline never sees today's real curve, only an earlier one), and
    deliberately NOT random -- a fixed phase shift plus a fixed scale
    factor, same analytic-not-random discipline as the real curves
    above. The shift/scale values themselves are arbitrary but FIXED,
    matching the existing test_solver_forecast_regret.py fixture's own
    "yesterday, flat-shifted" reasoning.
    """
    solar_persistence_kw = np.roll(solar_real_kw, 2) * 0.85
    load_persistence_kw = np.roll(load_real_kw, 1) * 1.10
    return solar_persistence_kw, load_persistence_kw


def build_reference_scenario() -> dict:
    """Assemble every input compute_forecast_regret() needs for the
    fixed reference-household scenario. Returned as a plain dict (not a
    dataclass) so a caller can pass it straight through as **kwargs,
    and so a future scenario field addition doesn't need a matching
    dataclass field everywhere this is used.
    """
    solar_real_kw, load_real_kw = _reference_real_curves()
    solar_forecast_kw, load_forecast_kw = _reference_forecast_curves(
        solar_real_kw, load_real_kw
    )
    solar_persistence_kw, load_persistence_kw = _reference_persistence_curves(
        solar_real_kw, load_real_kw
    )
    return {
        "periods": PeriodGrid(hours=np.array([1.0] * _N_PERIODS)),
        "grid": _reference_grid(),
        "battery": _reference_battery(),
        "solar_real_kw": solar_real_kw,
        "load_real_kw": load_real_kw,
        "solar_forecast_kw": solar_forecast_kw,
        "load_forecast_kw": load_forecast_kw,
        "solar_persistence_kw": solar_persistence_kw,
        "load_persistence_kw": load_persistence_kw,
    }


def run_reference_benchmark() -> ReferenceBenchmarkResult:
    """Run the fixed reference-household scenario through the real,
    production compute_forecast_regret() and return a self-describing
    result. Pure function -- no I/O, no HA dependency, safe to call
    from a plain local script or a test, same as every other module in
    this package.

    Deliberately no CLI/`__main__` entry point in this file -- this
    module uses relative imports (`.elements`, `.forecast_regret`) like
    every other file in this package, so it can't be run directly as
    `python reference_benchmark.py` (breaks the same way `python
    network.py` would). See tests/run_reference_benchmark.py for the
    actual standalone runner, which reuses tests/_solver_path.py's
    already-proven sys.path setup instead of duplicating it here.
    """
    scenario = build_reference_scenario()
    regret = compute_forecast_regret(**scenario)
    return ReferenceBenchmarkResult(
        scenario_version=REFERENCE_HOUSEHOLD_VERSION, regret=regret
    )
