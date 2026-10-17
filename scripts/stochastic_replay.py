#!/usr/bin/env python3
"""Closed-loop paired replay: does stochastic dispatch earn more? (nimbus #1797, PR C)

Shadow mode reports what the stochastic plan WOULD do. Whether that is worth
switching on is a question about money on real days, and an expected cost
inside one solve cannot answer it: every scenario in it is a forecast. This
replays both policies over the same captured days and settles each against
what actually happened.

    python scripts/stochastic_replay.py EXPORT.json [EXPORT2.json ...] \\
        [--arms main,stochastic,p50] [--json OUT.json]

The input is what `nimbus_load.export_decision_inputs` returns (#1657 gate 2),
in any shape `scripts/forecast_bias.py` reads: JSON, `.json.gz`, the REST
wrapper, or YAML. Several files may be passed to cover more than one call's
24 hours; a record repeated across files is used once. Read-only: nothing is
sent anywhere.

## What one tick does

Every full-horizon record (one per 30 minutes, more around a change of
direction) is one decision. For each arm:

1. Build the solve from the record: its periods, prices, envelope, load and
   solar forecast, and (when the record carries the calibrated band, #1807)
   `solar_p10_kw` / `solar_p90_kw`; the battery from the record's resolved
   config, starting at the SoC THIS ARM's replay has reached, not the SoC
   the live install had.
2. Solve it, each arm on the inputs its live counterpart is given:
   - `main`: the solar and prices the live LP counted on after the
     risk-aversion dials (`solar_effective_kw`, `import_price_effective`,
     `export_price_effective`), with the dials at 0 so nothing is applied
     twice. That is the plan that ran.
   - `stochastic`: `build_stochastic_plan()` on the raw solar forecast and
     its band (solar risk aversion is a no-op inside the scenarios) and the
     same effective prices, under drive's own refusal rules
     (`solver_plan._refusal_reason(drive=True)`), falling back to `main`'s
     plan exactly where drive would.
   - `p50`: the raw solar forecast and the effective prices. `main` minus
     `p50` is the solar risk-aversion term's own value, so the hedge is not
     credited with what dropping that term alone would do.

   A record from before the effective fields existed has only the raw
   inputs; `main` then equals `p50` and the report counts such records
   (`raw_input_records`). A record captured while drive was publishing the
   stochastic plan carries that plan's effective solar (the raw forecast);
   the report counts those too (`drive_records`).
3. Follow that plan's battery power until the next decision, interval by
   interval, against the REALISED solar, load and prices.

## Realised

Each record's row 0 is the live reading at that solve: solar and load are
anchored to the measured value for the current period (`solver_inputs/
solar.py`, `load.py`), and the price rows in row 0's 5-minute block are the
price sensor's current state (#220). Every record in the export (head and
horizon) is one such reading, held until the next. A gap longer than
`--max-gap-minutes` is not settled for any arm, so no arm is charged for a
stretch nobody observed.

## Settlement, the same for every arm

Battery power is clipped to the battery's power and SoC limits (round-trip
efficiency split evenly across the legs, as `battery_soc.py` does). The grid
takes the rest: `load - solar + charge - discharge`. Export above the
envelope's export limit is curtailed; import above its import limit first
cuts charging. Money is `grid_money()`'s: imports at the import price minus
exports at the export price. The remaining SoC is valued at the config's
salvage value, the same for every arm, so an arm cannot look better by
ending emptier.

## What this model leaves out, on purpose

The record is the household's aggregate: one battery (the configured home
battery, its capacity scaled by State of Health), one load, one solar
array. EVs, thermal loads, controllable loads and P2P blocks are not in it,
and the P2P bonus is not settled. Every arm is missing the same things, so
the DIFFERENCE between arms is the measurement; the absolute dollars are not
the household's bill. Between decisions an arm follows its last plan open
loop, where the live install re-solves every few minutes; again the same for
every arm.

## Statistics

The paired difference per settled interval is summed per local day and in
total. The 95% interval is a moving-block bootstrap over intervals (blocks of
`--block-hours`), because an interval's money depends on the SoC the
previous ones left; with only a few days it is wide, and it says so.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import _solver_path  # noqa: F401
from solver import elements, network, stochastic_dispatch

sys.path.insert(0, str(REPO / "scripts"))
import forecast_bias

ARMS = ("main", "stochastic", "p50")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_TZ = "Australia/Brisbane"
#: Config defaults where the record's config lacks a key: const.py's own.
DEFAULT_EFFICIENCY_PERCENT = 95.0
DEFAULT_CHARGE_COST = 0.01
DEFAULT_DISCHARGE_COST = 0.01
DEFAULT_SALVAGE_VALUE = 0.15


# ---- reading ----------------------------------------------------------------


def _time(value: Any) -> datetime | None:
    return forecast_bias.parse_time(value)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _published(record: dict[str, Any]) -> datetime | None:
    return _time(record.get("published_at")) or _time(record.get("captured_at"))


def merge_exports(
    exports: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every record once (by kind and publish time), oldest first, and every
    config body any export carried."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    configs: dict[str, Any] = {}
    for export in exports:
        configs.update(export.get("configs") or {})
        for record in export.get("records") or []:
            when = _published(record)
            if when is None:
                continue
            seen.setdefault((str(record.get("kind")), when.isoformat()), record)
    records = sorted(seen.values(), key=lambda r: (_published(r) or _EPOCH).timestamp())
    return records, configs


@dataclass(frozen=True)
class Reading:
    """What was live at one solve: row 0 of that solve's record."""

    at: datetime
    solar_kw: float
    load_kw: float
    import_price: float
    export_price: float


def readings(records: Sequence[dict[str, Any]]) -> list[Reading]:
    """One reading per publish time, oldest first. A record whose row 0 lacks
    any of the four values is skipped, never filled."""
    out: dict[datetime, Reading] = {}
    for record in records:
        when = _published(record)
        rows = record.get("rows") or {}
        values = [
            _num((rows.get(k) or [None])[0])
            for k in ("solar_kw", "load_kw", "import_price", "export_price")
        ]
        if when is None or any(v is None for v in values):
            continue
        solar, load, buy, sell = (float(v) for v in values)  # type: ignore[arg-type]
        out.setdefault(when, Reading(when, max(solar, 0.0), max(load, 0.0), buy, sell))
    return [out[k] for k in sorted(out)]


@dataclass(frozen=True)
class Interval:
    """A settled stretch: one reading held until the next."""

    start: datetime
    hours: float
    reading: Reading


def intervals(
    found: Sequence[Reading], max_gap: timedelta
) -> tuple[list[Interval], int]:
    """Each reading held until the next. A gap above `max_gap` is not settled
    (counted, returned second); the last reading has no end and is dropped."""
    out: list[Interval] = []
    skipped = 0
    for here, nxt in pairwise(found):
        span = nxt.at - here.at
        if span <= timedelta(0):
            continue
        if span > max_gap:
            skipped += 1
            continue
        out.append(Interval(here.at, span.total_seconds() / 3600.0, here))
    return out, skipped


# ---- the solve, built from a record -----------------------------------------


@dataclass(frozen=True)
class BatterySpec:
    capacity_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    leg_efficiency: float
    charge_cost: float
    discharge_cost: float
    salvage_value: float


def _cfg(config: dict[str, Any], key: str, default: float) -> float:
    value = _num(config.get(key))
    return default if value is None else value


def battery_spec(config: dict[str, Any]) -> BatterySpec | None:
    """The home battery from the resolved config, or None without a
    capacity or power limits (nothing to dispatch)."""
    capacity = _num(config.get("solver_battery_capacity_kwh"))
    charge = _num(config.get("solver_max_charge_kw"))
    discharge = _num(config.get("solver_max_discharge_kw"))
    if not capacity or capacity <= 0 or charge is None or discharge is None:
        return None
    capacity *= (
        min(max(_cfg(config, "solver_battery_soh_percent", 100.0), 1.0), 100.0) / 100.0
    )
    lo = min(max(_cfg(config, "solver_battery_min_soc_percent", 0.0), 0.0), 100.0)
    hi = min(max(_cfg(config, "solver_battery_max_soc_percent", 100.0), lo), 100.0)
    rt = min(
        _cfg(config, "solver_efficiency_percent", DEFAULT_EFFICIENCY_PERCENT) / 100.0,
        0.999,
    )
    return BatterySpec(
        capacity_kwh=capacity,
        min_soc_kwh=capacity * lo / 100.0,
        max_soc_kwh=capacity * hi / 100.0,
        max_charge_kw=max(charge, 0.0),
        max_discharge_kw=max(discharge, 0.0),
        leg_efficiency=max(rt, 0.01) ** 0.5,
        charge_cost=_cfg(config, "solver_charge_cost", DEFAULT_CHARGE_COST),
        discharge_cost=_cfg(config, "solver_discharge_cost", DEFAULT_DISCHARGE_COST),
        salvage_value=_cfg(config, "solver_salvage_value", DEFAULT_SALVAGE_VALUE),
    )


def _column(rows: dict[str, Any], key: str, n: int) -> np.ndarray | None:
    values = rows.get(key)
    if not isinstance(values, list) or len(values) < n:
        return None
    nums = [_num(v) for v in values[:n]]
    if any(v is None for v in nums):
        return None
    return np.array(nums, dtype=np.float64)


@dataclass(frozen=True)
class Decision:
    """One full-horizon record turned into a solve, minus the SoC and the
    solar each arm counts on."""

    at: datetime
    start: datetime
    kwargs: dict[str, Any]  # periods, grid (effective prices), loads
    battery: BatterySpec
    solar_kw: np.ndarray  # the raw forecast
    effective_solar_kw: np.ndarray | None  # what the live LP counted on
    p10_kw: np.ndarray | None
    p90_kw: np.ndarray | None
    drive: bool  # captured while drive published the stochastic plan

    @property
    def calibrated_band(self) -> bool:
        return self.p10_kw is not None and self.p90_kw is not None

    def solar(self, arm: str) -> elements.SolarConfig:
        if arm == "main" and self.effective_solar_kw is not None:
            return elements.SolarConfig(forecast_kw=self.effective_solar_kw)
        if arm == "stochastic" and self.calibrated_band:
            return elements.SolarConfig(
                forecast_kw=self.solar_kw, p10_kw=self.p10_kw, p90_kw=self.p90_kw
            )
        return elements.SolarConfig(forecast_kw=self.solar_kw)


def decision(record: dict[str, Any], config: dict[str, Any]) -> Decision | None:
    """The record's solve, or None when the record cannot be one (no rows,
    a missing column, no battery)."""
    rows = record.get("rows") or {}
    times = rows.get("time")
    when = _published(record)
    spec = battery_spec(config)
    if not isinstance(times, list) or not times or when is None or spec is None:
        return None
    start = _time(times[0])
    n = len(times)
    hours = _column(rows, "hours", n)
    cols = {
        k: _column(rows, k, n)
        for k in (
            "import_price",
            "export_price",
            "load_kw",
            "solar_kw",
            "envelope_import_limit_kw",
            "envelope_export_limit_kw",
        )
    }
    if start is None or hours is None or any(v is None for v in cols.values()):
        return None
    p10 = _column(rows, "solar_p10_kw", n)
    p90 = _column(rows, "solar_p90_kw", n)
    banded = p10 is not None and p90 is not None
    effective_solar = _column(rows, "solar_effective_kw", n)
    buy = _column(rows, "import_price_effective", n)
    sell = _column(rows, "export_price_effective", n)
    kwargs: dict[str, Any] = {
        "periods": elements.PeriodGrid(hours=hours, start=start),
        "grid": elements.GridConfig(
            import_price=buy if buy is not None else cols["import_price"],
            export_price=sell if sell is not None else cols["export_price"],
            import_limit_kw=np.maximum(cols["envelope_import_limit_kw"], 0.0),  # type: ignore[arg-type]
            export_limit_kw=np.maximum(cols["envelope_export_limit_kw"], 0.0),  # type: ignore[arg-type]
        ),
        "loads": [
            elements.LoadConfig(
                name="household",
                forecast_kw=np.maximum(cols["load_kw"], 0.0),  # type: ignore[arg-type]
            )
        ],
    }
    stochastic = record.get("stochastic")
    return Decision(
        at=when,
        start=start,
        kwargs=kwargs,
        battery=spec,
        solar_kw=np.maximum(cols["solar_kw"], 0.0),  # type: ignore[arg-type]
        effective_solar_kw=None
        if effective_solar is None
        else np.maximum(effective_solar, 0.0),
        p10_kw=np.maximum(p10, 0.0) if banded else None,  # type: ignore[arg-type]
        p90_kw=np.maximum(p90, 0.0) if banded else None,  # type: ignore[arg-type]
        drive=isinstance(stochastic, dict) and bool(stochastic.get("drives_dispatch")),
    )


def _battery(spec: BatterySpec, soc_kwh: float) -> elements.BatteryConfig:
    return elements.BatteryConfig(
        name="home",
        capacity_kwh=spec.capacity_kwh,
        initial_soc_kwh=min(max(soc_kwh, spec.min_soc_kwh), spec.max_soc_kwh),
        min_soc_kwh=spec.min_soc_kwh,
        max_soc_kwh=spec.max_soc_kwh,
        max_charge_kw=spec.max_charge_kw,
        max_discharge_kw=spec.max_discharge_kw,
        charge_efficiency=spec.leg_efficiency,
        discharge_efficiency=spec.leg_efficiency,
        charge_cost=spec.charge_cost,
        discharge_cost=spec.discharge_cost,
        salvage_value=spec.salvage_value,
    )


# ---- policies ---------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """A plan's net battery power (+ discharge, - charge) per period."""

    start: datetime
    ends_h: np.ndarray  # cumulative hours at each period's end
    battery_kw: np.ndarray

    def at(self, when: datetime) -> float:
        h = (when - self.start).total_seconds() / 3600.0
        if h < 0 or not len(self.ends_h):
            return 0.0
        i = int(np.searchsorted(self.ends_h, h, side="right"))
        return float(self.battery_kw[min(i, len(self.battery_kw) - 1)])


def _schedule(plan: network.Plan, d: Decision) -> Schedule:
    hours = np.asarray(d.kwargs["periods"].hours, dtype=np.float64)
    net = np.asarray(plan.battery_discharge_kw, dtype=np.float64) - np.asarray(
        plan.battery_charge_kw, dtype=np.float64
    )
    return Schedule(d.start, np.cumsum(hours), net)


@dataclass
class Outcome:
    """What one arm decided at one tick."""

    schedule: Schedule | None
    used: str  # "main", "stochastic", "p50" or "none"
    reason: str | None = None
    seconds: float = 0.0


Policy = Callable[[Decision, float], Outcome]


def _kwargs(d: Decision, soc: float, arm: str) -> dict[str, Any]:
    """The arm's solve. Every risk-aversion dial is 0: `main`'s inputs are
    already the adjusted ones, and the other arms are defined without."""
    return {
        **d.kwargs,
        "solar": d.solar(arm),
        "batteries": [_battery(d.battery, soc)],
        "risk_aversion": 0.0,
    }


def _solve(d: Decision, soc: float, arm: str) -> tuple[network.Plan, float]:
    started = time.monotonic()
    plan = network.build_plan(**_kwargs(d, soc, arm))
    return plan, time.monotonic() - started


def _deterministic(arm: str) -> Policy:
    def policy(d: Decision, soc: float) -> Outcome:
        plan, secs = _solve(d, soc, arm)
        if plan.status != "optimal":
            return Outcome(None, "none", f"{arm}_{plan.status}", secs)
        return Outcome(_schedule(plan, d), arm, None, secs)

    return policy


main_policy = _deterministic("main")
p50_policy = _deterministic("p50")


def stochastic_policy(
    weights: tuple[float, float, float] = stochastic_dispatch.SCENARIO_WEIGHTS,
    stage1_minutes: float = stochastic_dispatch.STAGE1_MINUTES,
) -> Policy:
    """Drive's own behaviour: the stochastic plan when drive would publish
    it, else the main plan, with drive's reason."""
    solver_plan = importlib.import_module("solver_plan")

    def policy(d: Decision, soc: float) -> Outcome:
        det, det_secs = _solve(d, soc, "main")
        if det.status != "optimal":
            return Outcome(None, "none", f"main_{det.status}", det_secs)
        result = stochastic_dispatch.build_stochastic_plan(
            _kwargs(d, soc, "stochastic"),
            weights=weights,
            stage1_minutes=stage1_minutes,
            blend_weight=det.calibration_weight_used,
        )
        secs = det_secs + result.solve_seconds
        reason = solver_plan._refusal_reason(result, det, drive=True)
        if reason is not None or result.plan is None:
            return Outcome(_schedule(det, d), "main", reason, secs)
        return Outcome(_schedule(result.plan, d), "stochastic", None, secs)

    return policy


# ---- settlement -------------------------------------------------------------


@dataclass(frozen=True)
class Settled:
    soc_kwh: float
    import_kw: float
    export_kw: float
    battery_kw: float
    curtailed_kw: float
    money: float  # $ paid, negative = earned


def settle(
    planned_kw: float,
    spec: BatterySpec,
    soc_kwh: float,
    iv: Interval,
    import_limit_kw: float,
    export_limit_kw: float,
) -> Settled:
    """One interval of a planned battery power against what happened."""
    h = iv.hours
    r = iv.reading
    eff = spec.leg_efficiency
    charge = min(max(-planned_kw, 0.0), spec.max_charge_kw)
    discharge = min(max(planned_kw, 0.0), spec.max_discharge_kw)
    charge = min(charge, max(spec.max_soc_kwh - soc_kwh, 0.0) / (eff * h))
    discharge = min(discharge, max(soc_kwh - spec.min_soc_kwh, 0.0) * eff / h)
    net = r.load_kw - r.solar_kw + charge - discharge
    if net > import_limit_kw and charge > 0.0:
        charge = max(charge - (net - import_limit_kw), 0.0)
        net = r.load_kw - r.solar_kw + charge - discharge
    imp = max(net, 0.0)
    exp = max(-net, 0.0)
    curtailed = max(exp - export_limit_kw, 0.0)
    exp -= curtailed
    soc = soc_kwh + (charge * eff - discharge / eff) * h
    return Settled(
        soc_kwh=min(max(soc, 0.0), spec.capacity_kwh),
        import_kw=imp,
        export_kw=exp,
        battery_kw=discharge - charge,
        curtailed_kw=curtailed,
        money=h * (imp * r.import_price - exp * r.export_price),
    )


def _limit(d: Decision, key: str, when: datetime) -> float:
    grid = d.kwargs["grid"]
    arr = np.atleast_1d(np.asarray(getattr(grid, key), dtype=np.float64))
    h = (when - d.start).total_seconds() / 3600.0
    ends = np.cumsum(np.asarray(d.kwargs["periods"].hours, dtype=np.float64))
    i = int(np.searchsorted(ends, max(h, 0.0), side="right"))
    return float(arr[min(i, len(arr) - 1)])


@dataclass
class ArmRun:
    name: str
    money: list[float] = field(default_factory=list)  # per interval, $
    soc_end_kwh: float = 0.0
    used: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seconds: float = 0.0
    curtailed_kwh: float = 0.0
    import_kwh: float = 0.0
    export_kwh: float = 0.0


def run_arm(
    name: str,
    policy: Policy,
    decisions: Sequence[Decision],
    settled: Sequence[Interval],
    soc0_kwh: float,
) -> ArmRun:
    """The closed loop for one arm. Every arm sees the same decisions and
    intervals; an interval is settled under the latest decision at or before
    its start, and an arm with no usable plan yet holds the battery idle."""
    run = ArmRun(name)
    soc = soc0_kwh
    schedule: Schedule | None = None
    current: Decision | None = None
    k = 0
    for iv in settled:
        while k < len(decisions) and decisions[k].at <= iv.start:
            current = decisions[k]
            out = policy(current, soc)
            run.seconds += out.seconds
            run.used[out.used] += 1
            if out.reason:
                run.reasons[out.reason] += 1
            if out.schedule is not None:
                schedule = out.schedule
            k += 1
        if current is None:
            run.money.append(math.nan)
            continue
        planned = schedule.at(iv.start) if schedule is not None else 0.0
        s = settle(
            planned,
            current.battery,
            soc,
            iv,
            _limit(current, "import_limit_kw", iv.start),
            _limit(current, "export_limit_kw", iv.start),
        )
        soc = s.soc_kwh
        run.money.append(s.money)
        run.curtailed_kwh += s.curtailed_kw * iv.hours
        run.import_kwh += s.import_kw * iv.hours
        run.export_kwh += s.export_kw * iv.hours
    run.soc_end_kwh = soc
    return run


# ---- statistics -------------------------------------------------------------


def block_bootstrap(
    deltas: Sequence[float], block: int, n_boot: int = 2000, seed: int = 1797
) -> tuple[float, float] | None:
    """95% interval of the summed `deltas` by moving-block bootstrap, or None
    with fewer than two blocks of data."""
    x = np.asarray(deltas, dtype=np.float64)
    n = len(x)
    block = max(int(block), 1)
    if n < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    starts = np.arange(n - block + 1)
    n_blocks = math.ceil(n / block)
    sums = np.empty(n_boot)
    for b in range(n_boot):
        picks = rng.choice(starts, size=n_blocks)
        sample = np.concatenate([x[s : s + block] for s in picks])[:n]
        sums[b] = sample.sum()
    lo, hi = np.percentile(sums, [2.5, 97.5])
    return float(lo), float(hi)


def compare(
    a: ArmRun,
    b: ArmRun,
    settled: Sequence[Interval],
    tz: tzinfo,
    salvage: float,
    block_hours: float,
) -> dict[str, Any]:
    """`b` minus `a`: negative means `b` paid less (better)."""
    pairs = [
        (iv, mb - ma)
        for iv, ma, mb in zip(settled, a.money, b.money, strict=True)
        if math.isfinite(ma) and math.isfinite(mb)
    ]
    deltas = [d for _iv, d in pairs]
    per_day: dict[str, float] = defaultdict(float)
    for iv, d in pairs:
        per_day[iv.start.astimezone(tz).date().isoformat()] += d
    step_h = float(np.median([iv.hours for iv, _d in pairs])) if pairs else 0.0
    block = round(block_hours / step_h) if step_h > 0 else 1
    terminal = -(b.soc_end_kwh - a.soc_end_kwh) * salvage
    total = float(sum(deltas))
    return {
        "arms": f"{b.name} - {a.name}",
        "grid_money_delta": round(total, 4),
        "terminal_soc_delta_kwh": round(b.soc_end_kwh - a.soc_end_kwh, 3),
        "terminal_value_delta": round(terminal, 4),
        "total_delta": round(total + terminal, 4),
        "per_day": {k: round(v, 4) for k, v in sorted(per_day.items())},
        "ci95_grid_money_delta": block_bootstrap(deltas, block),
        "intervals": len(deltas),
        "block_intervals": block,
    }


# ---- driver -----------------------------------------------------------------


def replay(
    exports: Sequence[dict[str, Any]],
    *,
    arms: Sequence[str] = ARMS,
    max_gap: timedelta = timedelta(minutes=15),
    tz: tzinfo | None = None,
    block_hours: float = 6.0,
    soc0_pct: float | None = None,
    weights: tuple[float, float, float] = stochastic_dispatch.SCENARIO_WEIGHTS,
    stage1_minutes: float = stochastic_dispatch.STAGE1_MINUTES,
) -> dict[str, Any]:
    tz = tz or ZoneInfo(DEFAULT_TZ)
    records, configs = merge_exports(exports)
    found = readings(records)
    settled, gaps = intervals(found, max_gap)
    decisions: list[Decision] = []
    skipped_records = 0
    for record in records:
        if record.get("kind") != "horizon":
            continue
        d = decision(record, configs.get(str(record.get("config"))) or {})
        if d is None:
            skipped_records += 1
        else:
            decisions.append(d)
    decisions.sort(key=lambda d: d.at)
    if not decisions or not settled:
        return {
            "error": "nothing to replay",
            "horizon_records": len(decisions),
            "skipped_records": skipped_records,
            "intervals": len(settled),
        }
    first = decisions[0]
    spec = first.battery
    rows0 = next(
        (r.get("rows") or {} for r in records if _published(r) == first.at), {}
    )
    pct = (
        soc0_pct if soc0_pct is not None else _num((rows0.get("soc_pct") or [None])[0])
    )
    soc0 = spec.capacity_kwh * (pct if pct is not None else 50.0) / 100.0
    policies: dict[str, Policy] = {
        "main": main_policy,
        "p50": p50_policy,
        "stochastic": stochastic_policy(weights, stage1_minutes),
    }
    runs = {
        name: run_arm(name, policies[name], decisions, settled, soc0) for name in arms
    }
    out: dict[str, Any] = {
        "window": [settled[0].start.isoformat(), settled[-1].start.isoformat()],
        "horizon_records": len(decisions),
        "calibrated_band_records": sum(d.calibrated_band for d in decisions),
        "raw_input_records": sum(d.effective_solar_kw is None for d in decisions),
        "drive_records": sum(d.drive for d in decisions),
        "skipped_records": skipped_records,
        "intervals": len(settled),
        "unsettled_gaps": gaps,
        "soc0_kwh": round(soc0, 3),
        "arms": {
            name: {
                "grid_money": round(float(np.nansum(r.money)), 4),
                "soc_end_kwh": round(r.soc_end_kwh, 3),
                "import_kwh": round(r.import_kwh, 3),
                "export_kwh": round(r.export_kwh, 3),
                "curtailed_kwh": round(r.curtailed_kwh, 3),
                "plans_used": dict(r.used),
                "fallback_reasons": dict(r.reasons),
                "solve_seconds": round(r.seconds, 1),
            }
            for name, r in runs.items()
        },
        "comparisons": [],
    }
    salvage = spec.salvage_value
    for other in ("main", "p50"):
        if "stochastic" in runs and other in runs:
            out["comparisons"].append(
                compare(
                    runs[other], runs["stochastic"], settled, tz, salvage, block_hours
                )
            )
    if "main" in runs and "p50" in runs:
        out["comparisons"].append(
            compare(runs["main"], runs["p50"], settled, tz, salvage, block_hours)
        )
    return out


def _print(result: dict[str, Any]) -> None:
    if "error" in result:
        print(result)
        return
    print(f"window {result['window'][0]} .. {result['window'][1]}")
    print(
        f"{result['horizon_records']} decisions ({result['calibrated_band_records']} with a "
        f"calibrated band, {result['raw_input_records']} without the effective inputs, "
        f"{result['drive_records']} captured in drive), {result['intervals']} settled intervals, "
        f"{result['unsettled_gaps']} gaps not settled, start SoC {result['soc0_kwh']} kWh"
    )
    print()
    print(
        f"{'arm':<11}{'grid $':>10}{'end kWh':>9}{'import':>9}{'export':>9}  plans / fallbacks"
    )
    for name, a in result["arms"].items():
        print(
            f"{name:<11}{a['grid_money']:>10.2f}{a['soc_end_kwh']:>9.1f}"
            f"{a['import_kwh']:>9.1f}{a['export_kwh']:>9.1f}  {a['plans_used']} {a['fallback_reasons']}"
        )
    print()
    for c in result["comparisons"]:
        ci = c["ci95_grid_money_delta"]
        ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a (too little data)"
        print(
            f"{c['arms']}: grid {c['grid_money_delta']:+.2f}, end SoC {c['terminal_soc_delta_kwh']:+.1f} kWh "
            f"({c['terminal_value_delta']:+.2f}), total {c['total_delta']:+.2f}; 95% {ci_s}"
        )
        for day, v in c["per_day"].items():
            print(f"    {day}  {v:+.2f}")
    print(
        "\nNegative = the second arm paid less. Aggregate model: see the module docstring."
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("exports", nargs="+", type=Path)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--max-gap-minutes", type=float, default=15.0)
    ap.add_argument("--block-hours", type=float, default=6.0)
    ap.add_argument("--tz", default=DEFAULT_TZ)
    ap.add_argument("--soc0-pct", type=float, default=None)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = sorted(set(arms) - set(ARMS))
    if unknown:
        ap.error(f"unknown arms {unknown}; choose from {ARMS}")
    result = replay(
        forecast_bias.load_exports(args.exports),
        arms=arms,
        max_gap=timedelta(minutes=args.max_gap_minutes),
        tz=ZoneInfo(args.tz),
        block_hours=args.block_hours,
        soc0_pct=args.soc0_pct,
    )
    _print(result)
    if args.json:
        args.json.write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
