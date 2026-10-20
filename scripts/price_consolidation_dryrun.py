"""#1786 dry run: consolidation rules against made-up spike scenarios, in money.

Run: python scripts/price_consolidation_dryrun.py  (about 20 minutes).
Results of the 18 Oct 2026 run are on #1786.

Rolling solves every 5 minutes over 4 hours on a production-shaped battery
(119.7 kWh, 40 kW, Min 2%, house load ~1 kW, no solar). Each solve plans on a
rule's forecast; period 0 is always the settled price (Nimbus's settled
block); period 0 is executed and SoC carries forward. Money = export earned
minus import paid at the SETTLED price, plus leftover energy above Min valued
at a common $0.15/kWh, so no rule wins by draining or hoarding.

Sources (all $/kWh, 5-min truth):
  lv   LocalVolts: follows AEMO pre-dispatch's shape, 30-min averaged
  pd   AEMO pre-dispatch + markup, 30-min
  amb  Amber: damps pre-dispatch spikes (its own spike model), 5-min
  p5   P5MIN: 5-min, only 1 h ahead, accurate near term
Rules:
  R0       LocalVolts only (today)
  MEDIAN   consolidate() with no weights (the live rule until 50 samples)
  WEIGHTED consolidate() with fixed inverse-MAE-style weights
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path += [str(REPO / "custom_components" / "nimbus_load")]
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan
from solver_inputs import price_consolidation as pc

CAP = 119.7
H = 5 / 60
STEPS = 48  # 4 h of solves
N = 36  # 3 h horizon
BASE = 0.30
LEFTOVER = 0.15


def avg30(x):
    y = x.copy()
    for i in range(0, len(x), 6):
        y[i : i + 6] = x[i : i + 6].mean()
    return y


def scenario(name):
    """truth[t] and, per source, a function (issue_step) -> forecast array
    over absolute periods (NaN = not covered)."""
    T = STEPS + N + 6
    truth = np.full(T, BASE)
    spike = slice(24, 30)  # 2 h in, 30 min
    pd = np.full(T, BASE)
    amb = np.full(T, BASE)
    if name == "real, all see it":
        truth[spike] = 15.0
        pd[spike] = 15.0
        amb[spike] = 12.0
        lead_needed = 999
    elif name == "real, Amber damps":
        truth[spike] = 15.0
        pd[spike] = 15.0
        amb[spike] = 2.0
        lead_needed = 999
    elif name == "real, short notice":
        truth[spike] = 15.0
        lead_needed = 2  # only seen within 10 min
    elif name == "false, LV + PD":
        pd[spike] = 15.0
        amb[spike] = BASE
        lead_needed = 999
    elif name == "false, LV only":
        lead_needed = 999
    elif name == "real, LV only":
        truth[spike] = 15.0
        lead_needed = 999
    else:
        raise ValueError(name)

    def lv(step):
        f = avg30(pd * 1.0)
        if name in ("false, LV only", "real, LV only"):
            f[spike] = 15.0
        if name == "real, short notice":
            f = np.full(T, BASE)
        return f

    def pdf(step):
        return avg30(pd) if name != "real, short notice" else np.full(T, BASE)

    def ambf(step):
        f = amb.copy()
        if name == "real, short notice":
            f = np.full(T, BASE)
            if spike.start - step <= lead_needed:
                f[spike] = 10.0
        return f

    def p5(step):
        f = np.full(T, np.nan)
        hi = min(T, step + 12)
        f[step:hi] = truth[step:hi]
        if name == "real, short notice":
            f[step:hi] = BASE
            if spike.start - step <= lead_needed:
                f[spike.start : min(hi, spike.stop)] = 15.0
        if name in ("false, LV + PD", "real, LV only"):
            f[step:hi] = BASE
            if name == "real, LV only" and spike.start - step <= 0:
                f[step : min(hi, spike.stop)] = truth[step : min(hi, spike.stop)]
        return f

    return truth, {"lv": lv, "pd": pdf, "amb": ambf, "p5": p5}


WEIGHTS = {"lv": 0.2, "pd": 0.15, "amb": 0.35, "p5": 0.30}


def forecast(rule, sources, step):
    window = slice(step, step + N)
    series = {
        k: [None if np.isnan(v) else float(v) for v in fn(step)[window]]
        for k, fn in sources.items()
    }
    if rule == "R0":
        return np.array([v if v is not None else BASE for v in series["lv"]])
    w = None if rule in ("MEDIAN", "KEEP_SPIKES") else WEIGHTS
    threshold = 0.85 if rule == "KEEP_SPIKES" else None
    vals, _spread, _m = pc.consolidate(series, w, spike_threshold=threshold)
    return np.array([v if v is not None else BASE for v in vals])


def run(name, rule, start_pct):
    truth, sources = scenario(name)
    soc = CAP * start_pct / 100
    money = 0.0
    trace = []
    for step in range(STEPS):
        f = forecast(rule, sources, step)
        f[0] = truth[step]  # the settled current interval
        plan = build_plan(
            periods=PeriodGrid(np.full(N, H)),
            grid=GridConfig(
                import_price=f,
                export_price=f - 0.02,
                import_limit_kw=60.0,
                export_limit_kw=40.0,
            ),
            batteries=[
                BatteryConfig(
                    name="home",
                    capacity_kwh=CAP,
                    initial_soc_kwh=soc,
                    min_soc_kwh=0.02 * CAP,
                    max_soc_kwh=CAP,
                    max_charge_kw=40,
                    max_discharge_kw=40,
                    charge_efficiency=0.95,
                    discharge_efficiency=0.95,
                    charge_cost=0.01,
                    discharge_cost=0.05,
                    salvage_value=0.1,
                )
            ],
            solar=SolarConfig(forecast_kw=np.zeros(N)),
            loads=[LoadConfig(name="h", forecast_kw=np.full(N, 1.0))],
        )
        assert plan.status == "optimal", (name, rule, step, plan.status)
        imp, exp = plan.grid_import_kw[0], plan.grid_export_kw[0]
        money += (exp * (truth[step] - 0.02) - imp * truth[step]) * H
        soc = float(plan.battery_soc_kwh[0])
        if 18 <= step <= 30:
            trace.append(
                f"{plan.battery_discharge_kw[0] - plan.battery_charge_kw[0]:5.1f}"
            )
    money += max(soc - 0.02 * CAP, 0) * LEFTOVER
    return money, soc / CAP * 100, " ".join(trace)


if __name__ == "__main__":
    for start in (60, 25):
        print(f"\n=== start SoC {start}% ===")
        for name in (
            "real, all see it",
            "real, Amber damps",
            "real, short notice",
            "real, LV only",
            "false, LV + PD",
            "false, LV only",
        ):
            row = []
            for rule in ("R0", "MEDIAN", "WEIGHTED", "KEEP_SPIKES"):
                m, s, tr = run(name, rule, start)
                row.append((rule, m, s, tr))
            base = row[0][1]
            print(
                f"{name:20s} "
                + " | ".join(
                    f"{r}: ${m:7.2f} ({m - base:+6.2f}) end {s:4.1f}%"
                    for r, m, s, _ in row
                )
            )
