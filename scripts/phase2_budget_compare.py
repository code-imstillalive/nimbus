"""Full phase 2 against the phase-2 budget, on the same synthetic sites (#2010).

For each site: solve with an unbounded phase 2 and with a budget, under
`CalibratedOptions` as production uses, and record the primary cost, the
secondary (tie-break) objective, the solve time and the largest change in
battery dispatch. The sites are synthetic, shaped like the reference test
install: a 202-period horizon (5, 15 and 30-minute periods over ~48 h), a
house battery, an EV battery and three switchable (adequacy) loads.

    python scripts/phase2_budget_compare.py --budget 5 --sites 10 --first 0 \\
        --out phase2.json

`tests/v2/solver/phase2_budget_measurement.json` holds the run behind #2010's
table. Solve times depend on the machine; the cost and tie-break columns do
not.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.append(
    str(Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load")
)
from solver import lp
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def horizon() -> np.ndarray:
    """202 periods over ~48 h: 5-min near term, then 15 and 30 min."""
    return np.array([5 / 60] * 72 + [0.25] * 82 + [0.5] * 48)


def site(seed: int) -> dict:
    rng = np.random.default_rng(seed)
    h = horizon()
    t = np.cumsum(h) - h  # hours from now
    clock = (t + 14.0) % 24  # start 2 pm
    solar = np.clip(np.sin((clock - 6) / 12 * np.pi), 0, None) * rng.uniform(6, 10)
    solar *= rng.uniform(0.7, 1.0, len(h))
    base = 0.6 + 0.8 * ((clock > 17) & (clock < 22)) + rng.uniform(0, 0.6, len(h))
    imp = 0.18 + 0.20 * ((clock > 16) & (clock < 21)) + rng.normal(0, 0.03, len(h))
    spikes = rng.random(len(h)) < 0.03
    imp = np.where(spikes, rng.uniform(1.0, 6.0, len(h)), imp)
    exp = imp - rng.uniform(0.02, 0.12, len(h))
    n = len(h)
    batteries = [
        BatteryConfig(
            name="house",
            capacity_kwh=13.5,
            initial_soc_kwh=rng.uniform(3, 10),
            min_soc_kwh=1.0,
            max_soc_kwh=13.5,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.05,
            salvage_value=0.10,
        ),
        BatteryConfig(
            name="ev",
            capacity_kwh=60.0,
            initial_soc_kwh=rng.uniform(5, 20),
            min_soc_kwh=3.0,
            max_soc_kwh=54.0,
            max_charge_kw=7.0,
            max_discharge_kw=0.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.15,
        ),
    ]
    adequacy = [
        AdequacyLoadConfig(
            name="hws",
            max_power_kw=3.6,
            target_kwh=rng.uniform(6, 12),
            deadline_period=n - 1,
            shortfall_price=0.5,
        ),
        AdequacyLoadConfig(
            name="pool",
            max_power_kw=1.2,
            target_kwh=rng.uniform(4, 8),
            deadline_period=n // 2,
            shortfall_price=0.3,
        ),
        AdequacyLoadConfig(
            name="spa",
            max_power_kw=2.4,
            target_kwh=rng.uniform(3, 6),
            deadline_period=n - 1,
            earliest_period=n // 4,
            shortfall_price=0.4,
        ),
    ]
    return {
        "periods": PeriodGrid(hours=h),
        "grid": GridConfig(
            import_price=imp,
            export_price=exp,
            import_limit_kw=20.0,
            export_limit_kw=10.0,
        ),
        "batteries": batteries,
        "solar": SolarConfig(forecast_kw=solar),
        "loads": [LoadConfig(name="house", forecast_kw=base)],
        "adequacy_loads": adequacy,
        "smoothness_weight": 0.001,
        "solve_options": lp.CalibratedOptions(),
    }


def run(seed: int, budget: float):
    """One solve with phase 2 bounded by `budget` seconds; the secondary
    objective it settled on, and whether the budget was spent."""
    lp.PHASE2_SECONDARY_BUDGET_SECONDS = budget
    lp._lex_calibration_failed_until = 0.0
    seen: dict[str, list[float]] = {}
    real = lp._ensure_optimal_value

    def spy(h, **kw):
        value = real(h, **kw)
        seen.setdefault(kw.get("phase", ""), []).append(value)
        return value

    lp._ensure_optimal_value = spy
    try:
        start = time.perf_counter()
        plan = build_plan(**site(seed))
        took = time.perf_counter() - start
    finally:
        lp._ensure_optimal_value = real
    spent = "phase2_secondary_pinned_lp" in seen and "phase2_secondary" not in seen
    values = (
        seen.get("phase2_secondary") or seen.get("phase2_secondary_pinned_lp") or []
    )
    return plan, took, (min(values) if values else None), spent


def compare(budget: float, sites: int, first: int) -> list[dict]:
    rows = []
    for seed in range(first, first + sites):
        full, t_full, s_full, _ = run(seed, 1e9)
        bud, t_bud, s_bud, spent = run(seed, budget)
        moved = float(
            np.max(
                np.abs(
                    (bud.battery_charge_kw - bud.battery_discharge_kw)
                    - (full.battery_charge_kw - full.battery_discharge_kw)
                )
            )
        )
        rows.append(
            {
                "budget_s": budget,
                "site_seed": seed,
                "full_solve_s": round(t_full, 1),
                "budget_solve_s": round(t_bud, 1),
                "budget_spent": spent,
                "cost_full": full.total_cost,
                "cost_budget": bud.total_cost,
                "cost_difference": (bud.total_cost or 0) - (full.total_cost or 0),
                "tie_break_full": s_full,
                "tie_break_budget": s_bud,
                "max_battery_move_kw": round(moved, 3),
            }
        )
        print(rows[-1], flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=float, default=5.0)
    parser.add_argument("--sites", type=int, default=10)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows = compare(args.budget, args.sites, args.first)
    if args.out:
        args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
