"""Retrospective parameter-sensitivity backtesting -- "if you'd used a
different setting, knowing what actually happened that day, would you
have come out ahead or behind?" (2026-08-25, direct household ask for
an "outstanding... truly special unique" idea, narrowed to: an offline,
continuous backtesting engine that proves Nimbus's own decisions
against reality, rather than a bigger LP or a fancier model).

## What this genuinely can and cannot test (read before adding a
## candidate parameter)

This module re-solves a real, already-elapsed day's REAL, KNOWN
load/solar/price (the same "perfect foresight" convention regret.py's
own oracle_dispatch() already uses for EPR's own j_star) under an
ALTERNATIVE BatteryConfig/GridConfig, and scores the result the same
way. This is honest and cheap for any parameter that changes the LP's
own economic tradeoff even under PERFECT knowledge -- efficiency,
charge/discharge friction cost, capacity, SoC limits, power limits,
salvage value. These all directly appear in J (see regret.py's own
module docstring) or the feasible region, so a different value
genuinely produces a different re-solved plan and a different score.

It CANNOT meaningfully test `risk_aversion` / `import_price_risk_
aversion` / `export_price_risk_aversion`, or "what if a different
forecast source had been used". Both of those only have any effect
when a genuine forecast CONFIDENCE BAND is present (see network.py's
own `_risk_adjusted()`/`_risk_adjusted_bound()` -- risk_aversion is a
mathematically exact no-op whenever `lower_kw`/`upper_kw` are None) --
and this module's whole "score against real, already-known ground
truth" approach has, by construction, zero uncertainty to hedge
against. Testing risk_aversion this way would silently produce the
EXACT SAME score for every candidate, every single day, which looks
like "risk_aversion doesn't matter" when it actually means "this
measurement method structurally cannot see it." Do not add it here.
A genuine risk_aversion/forecast-source backtest needs Nimbus to
ARCHIVE each day's real forecast + confidence band at the time it was
published -- a real, disclosed, NOT-YET-BUILT prerequisite, not
something to fake by silently reusing this module's own machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .regret import evaluate_realized_cost, oracle_dispatch


@dataclass(frozen=True)
class CandidateResult:
    """One candidate parameter value's real economic outcome for ONE
    real, already-elapsed day -- re-solved and scored against that same
    day's real known load/solar/price.

    `undeliverable_kwh` (nimbus issue #1232): discharge this candidate's
    own plan committed to that the SCORING battery could not physically
    have delivered -- the plan asked for energy below the real pack's own
    floor. Zero on any candidate whose plan is deliverable as-is, and
    always zero on the legacy same-config path (a plan is always
    deliverable under the very assumptions it was built on). Surfaced
    rather than absorbed, because a candidate that only looks good by
    promising energy the pack does not have is a candidate whose score
    should be read with that caveat attached.
    """

    label: str
    total_cost: float
    undeliverable_kwh: float = 0.0


@dataclass(frozen=True)
class CandidateScore:
    """`score_candidate_day_detail()`'s full return -- the cost plus what
    it took to get there (nimbus issue #1232)."""

    total_cost: float
    final_soc_kwh: float
    undeliverable_kwh: float


def _final_soc_under(
    *,
    battery: BatteryConfig,
    charge_kw,
    discharge_kw,
    hours,
) -> tuple[float, float]:
    """Replay a committed charge/discharge schedule under `battery`'s OWN
    efficiencies and SoC bounds (nimbus issue #1232).

    Returns `(final_soc_kwh, undeliverable_kwh)`.

    **Why this exists.** `oracle_dispatch()` returns the SoC trajectory
    implied by whatever battery it planned with. Scoring a candidate's
    plan against a DIFFERENT battery therefore cannot reuse that
    trajectory -- the candidate's own optimistic efficiency would be
    smuggled back in through `final_soc_kwh`, which is credited at
    `salvage_value`. This recomputes the trajectory from the committed
    power under the scoring battery's real physics instead.

    **Clamping is the physical truth, and is counted.** A real pack stops
    at its own floor and ceiling; it does not go negative because a plan
    asked it to. So the SoC is clamped to `[min_soc_kwh, max_soc_kwh]`,
    and any discharge the floor refused is accumulated into
    `undeliverable_kwh`. That number is the honest measure of how much of
    the candidate's plan was fiction.
    """
    eta_c = float(battery.charge_efficiency)
    eta_d = float(battery.discharge_efficiency)
    lo = float(battery.min_soc_kwh)
    hi = float(battery.max_soc_kwh)
    soc = float(battery.initial_soc_kwh)
    undeliverable = 0.0
    for c, d, h in zip(
        np.asarray(charge_kw, dtype=np.float64),
        np.asarray(discharge_kw, dtype=np.float64),
        np.asarray(hours, dtype=np.float64),
        strict=True,
    ):
        want = float(c) * eta_c * float(h) - float(d) * float(h) / eta_d
        raw = soc + want
        soc = min(hi, max(lo, raw))
        if raw < lo:
            # The floor refused this much of the committed discharge.
            # Converted back to delivered-energy terms (the plan promised
            # AC-side export, the shortfall is what never left the pack).
            undeliverable += (lo - raw) * eta_d
    return soc, undeliverable


def score_candidate_day(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
    scoring_battery: BatteryConfig | None = None,
) -> float:
    """`score_candidate_day_detail()`'s total_cost alone -- see that
    function for the real documentation, including what `scoring_battery`
    is for (nimbus issue #1232).
    """
    return score_candidate_day_detail(
        periods=periods,
        grid=grid,
        battery=battery,
        solar=solar,
        load=load,
        scoring_battery=scoring_battery,
    ).total_cost


def score_candidate_day_detail(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
    scoring_battery: BatteryConfig | None = None,
) -> CandidateScore:
    """Re-solves ONE real, already-elapsed day's real known load/solar/
    price under `battery`, and scores the resulting dispatch under
    `scoring_battery`.

    **`battery` is what the LP PLANS with. `scoring_battery` is the
    physics the resulting plan is JUDGED by** (nimbus issue #1232). They
    are different questions and conflating them is what made this
    module's own efficiency sweep meaningless:

    Before #1232 every candidate was scored under itself. A candidate
    assuming 99% round-trip efficiency planned more aggressive cycling,
    was then charged only 1% loss per leg, AND reached a higher final SoC
    which is credited at `salvage_value` -- every term moving the same
    way. The sweep was therefore **strictly monotonic in the candidate**
    on every install and every day, measured live on the reference
    household 2026-09-24 as 85% -> -33.32, 90% -> -34.99, 95% -> -36.53,
    99% -> -37.74. `best_candidate` was always simply the largest number
    in `EFFICIENCY_CANDIDATES_PERCENT`, carrying no information about
    what the setting should be. It is not a comparison of settings; it is
    a comparison of universes, and the lossless universe always wins.

    Worse than useless: the sensor published `best_candidate: 99%`
    against a configured 85.8%, which reads as advice -- while
    `BatteryConfig.__post_init__` rejects exactly 100% as a degeneracy
    guard and the Solver-settings wizard explicitly warns against setting
    this near 100% (the wash-trade degeneracy this project diagnosed once
    for HAEO already).

    **With a fixed `scoring_battery` the comparison becomes meaningful**:
    every candidate's plan is judged by one physics, so a candidate that
    over-cycles on an efficiency it does not really have is penalised by
    the losses it assumed away, and a candidate can genuinely score
    worse than the configured value. That is the question a household is
    actually asking -- "would configuring X have served MY battery
    better" -- not "would I rather own a different battery".

    `scoring_battery=None` keeps the pre-#1232 same-config behaviour, and
    is still the right choice for a caller that genuinely wants "how
    would a day look if this were true all along" rather than a setting
    comparison. It is NOT the right choice for a sensitivity sweep.

    Raises whatever oracle_dispatch() raises (a genuinely infeasible
    scenario) -- callers running a sweep across many days/candidates
    must catch this per-candidate, not let one bad combination abort
    the whole sweep (see run_efficiency_sensitivity_sweep()'s own
    docstring).

    nimbus issue #768: oracle_dispatch() now takes/returns the same
    real multi-asset shape build_plan() does (a list of batteries, the
    full Plan back) -- this function still only ever exercises ONE
    battery/load (that's this module's own single-asset parameter-
    sensitivity scope, see the module docstring), so it wraps/unwraps
    the single-element list here. `plan.battery_charge_kw`/
    `battery_discharge_kw`/`battery_soc_kwh[-1]` are the SAME aggregate
    the old tuple return carried for exactly this one-battery case --
    zero behaviour change.
    """
    plan = oracle_dispatch(
        periods=periods, grid=grid, batteries=[battery], solar=solar, loads=[load]
    )
    judge = battery if scoring_battery is None else scoring_battery
    if scoring_battery is None:
        # Legacy path: the plan was built under `judge` itself, so its own
        # trajectory already IS the trajectory under the scoring physics.
        final_soc = float(plan.battery_soc_kwh[-1])
        undeliverable = 0.0
    else:
        final_soc, undeliverable = _final_soc_under(
            battery=judge,
            charge_kw=plan.battery_charge_kw,
            discharge_kw=plan.battery_discharge_kw,
            hours=periods.hours,
        )
    result = evaluate_realized_cost(
        hours=periods.hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid.import_price,
        export_price_real=grid.export_price,
        charge_committed_kw=plan.battery_charge_kw,
        discharge_committed_kw=plan.battery_discharge_kw,
        charge_cost=judge.charge_cost,
        discharge_cost=judge.discharge_cost,
        final_soc_kwh=final_soc,
        salvage_value=judge.salvage_value,
        grid_import_limit_kw=grid.import_limit_kw,
        grid_export_limit_kw=grid.export_limit_kw,
    )
    return CandidateScore(
        total_cost=result.total_cost,
        final_soc_kwh=final_soc,
        undeliverable_kwh=undeliverable,
    )


# Bounded, curated candidate set (2026-08-25) -- deliberately NOT a
# combinatorial grid across every economic parameter at once. Each
# sweep varies ONE parameter, holding everything else at the
# household's real configured value, so a result is directly
# interpretable ("here's how sensitive your economics are to
# round-trip efficiency specifically") rather than an opaque
# many-dimensional search. Revisit if/when a real household asks for
# a specific OTHER parameter's own sensitivity -- adding one is a
# small, mechanical extension of the same pattern, not a redesign.
def efficiency_label(pct: float) -> str:
    """The one place a candidate efficiency becomes a label.

    `%g` so an integer candidate stays "90%" (byte-identical to the old
    `f"{pct:.0f}%"` for the fixed set) while a real install's own
    configured value survives intact -- nimbus issue #1232 sweeps the
    configured value too, and `.0f` would render 85.8 as "86%",
    indistinguishable from a genuine 86% candidate.

    Exported so `solver_writer.py` can match a result to the configured
    value by label without reimplementing the format. Two
    implementations of one string is exactly how this project's own
    entity_id/unique_id mismatches keep happening.
    """
    return f"{pct:g}%"


EFFICIENCY_CANDIDATES_PERCENT: tuple[float, ...] = (85.0, 90.0, 95.0, 99.0)
"""99.0, not 100.0 -- BatteryConfig's own __post_init__ deliberately
REJECTS exactly 100% efficiency as a real degeneracy guard (see
elements.py's own DegenerateConfigError), independent of this module.
99% is close enough to "near-perfect" for a genuinely useful sensitivity
read while staying strictly inside the valid (0, 1) range."""


def run_efficiency_sensitivity_sweep(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    base_battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
    candidates_percent: tuple[float, ...] = EFFICIENCY_CANDIDATES_PERCENT,
) -> list[CandidateResult]:
    """Re-solves the SAME real day under each candidate round-trip
    efficiency, holding every other battery/grid setting at the
    household's real configured value. Mirrors solver_writer.py's own
    sqrt-split efficiency convention: a candidate expressed as a single
    round-trip percentage is applied as charge_efficiency =
    discharge_efficiency = sqrt(candidate / 100).

    Each candidate is solved independently and defensively -- a single
    candidate's own infeasibility (e.g. an absurdly low efficiency
    making the real day's real load unservable within real power
    limits) is skipped, not allowed to abort the whole sweep, since the
    genuinely useful output is "here's what DID solve," not an
    all-or-nothing batch.
    """
    results: list[CandidateResult] = []
    for pct in candidates_percent:
        efficiency = (pct / 100.0) ** 0.5
        candidate_battery = replace(
            base_battery,
            charge_efficiency=efficiency,
            discharge_efficiency=efficiency,
        )
        try:
            # nimbus issue #1232: PLAN with the candidate, SCORE with the
            # configured battery. Scoring each candidate under itself made
            # this sweep strictly monotonic in the candidate and therefore
            # informationless -- see score_candidate_day_detail()'s own
            # docstring for the measured numbers.
            score = score_candidate_day_detail(
                periods=periods,
                grid=grid,
                battery=candidate_battery,
                solar=solar,
                load=load,
                scoring_battery=base_battery,
            )
        except RuntimeError:
            continue
        results.append(
            CandidateResult(
                label=efficiency_label(pct),
                total_cost=score.total_cost,
                undeliverable_kwh=score.undeliverable_kwh,
            )
        )
    return results
