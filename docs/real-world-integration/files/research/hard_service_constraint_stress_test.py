#!/usr/bin/env python3
"""Hard-service-constraint stress test (Mark Purcell's Solver audit item
#5) -- research script, run locally against real recent household data.

The question: `AdequacyLoadConfig` (elements.py) enforces a real,
physical deadline constraint -- cumulative energy delivered by
`deadline_period` must reach `target_kwh`, modeled as a hard LP
constraint, not a soft cost term. Structurally this SHOULD hold
regardless of price (a correctly-formulated LP never trades away a
hard constraint for cost savings -- it just pays whatever it costs), but
"structurally correct" and "actually verified under real economic
pressure, at real scale, against a real solver" are different claims.
This test closes that gap: does the constraint genuinely survive extreme
price stress on real data, does cost scale sensibly as price scales, and
does a genuinely-infeasible target fail HONESTLY (status="infeasible")
rather than silently or wrongly?

No adequacy load is configured in the real production plan today
(nimbus_solver_forecast_writer.py passes adequacy_loads=[] -- confirmed
directly, same finding already noted in the objective-completeness
ablation test's own docstring) -- this test builds a realistic
HYPOTHETICAL one, shaped exactly like a real HWS heating window from
this project's own documented HWS redesign (session, 2026-07-16):
HWS L1's real fixed slot is 11:00-12:30 (1.5h), real element power
3700W. max_power_kw=3.7, target_kwh=5.55 (3.7kW held for the full 1.5h
-- the real worst-case heating requirement if the water started cold),
earliest_period/deadline_period converted from those real clock times
at PERIOD_HOURS=0.25 (period 44 = 11:00, period 49 = 12:15-12:30
inclusive).

Methodology, per real day D:
  1. Baseline: real import prices, unmodified.
  2. Stress: import prices scaled 2x, 5x, 10x for the ENTIRE day (not
     just the adequacy window) -- the harder, more realistic test: if
     electricity is expensive everywhere, does the LP still find SOME
     way to meet the deadline (there's no cheap escape), and does it
     correctly pay more to do so rather than silently under-delivering.
  3. Genuinely infeasible case: target_kwh set to 1.5x what
     max_power_kw*window can physically deliver (a hard, unsatisfiable
     target regardless of price) -- confirms the solver reports
     status="infeasible" honestly rather than a wrong/partial answer.

Report per scenario: status, delivered_by_deadline_kwh vs target_kwh,
total_cost, and whether cost increased monotonically with the price
multiplier (a real, checkable sanity property -- more expensive
electricity should never make the SAME physical delivery cheaper).

## Real findings, run 2026-08-19

Clean pass across both real days tested (2026-08-16, 2026-08-17), all
four price multipliers (1x/2x/5x/10x):
  - The real 5.55kWh target was delivered EXACTLY (5.550kWh, to the
    displayed precision) in every single scenario -- the hard deadline
    constraint held regardless of price, exactly as the LP formulation
    promises structurally. No degradation, no partial delivery, no
    silent under-shoot under stress.
  - total_cost scaled monotonically more expensive as the price
    multiplier increased, on both days -- a real, checkable economic
    sanity property, confirmed, not assumed.
  - The genuinely-infeasible case (target_kwh set to 1.5x what the
    window can physically deliver, at baseline 1x price) correctly
    returned status="infeasible" with an empty adequacy_loads list on
    both days -- honest failure, not a wrong or silently-adjusted
    answer.

No adequacy load is configured in the real production plan today (see
this test's own earlier note) -- this was a hypothetical, realistically-
shaped scenario, not a live production regression test. But the
mechanism itself, under real data and real economic stress up to 10x,
holds up exactly as designed. Net answer to Mark's audit item #5: the
hard service constraint is genuinely robust at this scale, not just
structurally plausible.
"""
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, r"C:\Users\Raf_local\nimbus\custom_components\nimbus_load")
from solver import elements  # noqa: E402
from solver.network import build_plan  # noqa: E402
import numpy as np  # noqa: E402

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")
HA_BASE = "http://192.168.1.221:8123"
TOKEN_PATH = r"C:\Users\Raf_local\.ha_token"

PERIOD_HOURS = 0.25
N_PERIODS = 96

NETWORK_ENERGY_PEAK_RATE = 0.214863
NETWORK_ENERGY_OFFPEAK_RATE = 0.00476
NETWORK_ENERGY_SHOULDER_RATE = 0.066759
CERTIFICATES_RATE = 0.008246
BATTERY_DISCHARGE_COST_NIGHT = 0.01
BATTERY_DISCHARGE_COST_DAY = 0.09
BATTERY_SALVAGE_VALUE_NIGHT = 0.3
BATTERY_SALVAGE_VALUE_OTHER = 0.15
P2P_BONUS_RATE = 0.50

# Real HWS L1 window, per this project's own HWS redesign session:
# 11:00-12:30, 3.7kW element.
ADEQUACY_MAX_POWER_KW = 3.7
ADEQUACY_EARLIEST_PERIOD = 44  # 11:00
ADEQUACY_DEADLINE_PERIOD = 49  # 12:15-12:30 inclusive, 6 periods = 1.5h
ADEQUACY_WINDOW_HOURS = (ADEQUACY_DEADLINE_PERIOD - ADEQUACY_EARLIEST_PERIOD + 1) * PERIOD_HOURS
ADEQUACY_TARGET_KWH = ADEQUACY_MAX_POWER_KW * ADEQUACY_WINDOW_HOURS  # 5.55 kWh -- real worst-case cold-water heat requirement
ADEQUACY_INFEASIBLE_TARGET_KWH = ADEQUACY_TARGET_KWH * 1.5  # genuinely impossible regardless of price

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(entity_id: str) -> dict:
    req = urllib.request.Request(f"{HA_BASE}/api/states/{entity_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def num(entity_id: str) -> float:
    return float(ha_get(entity_id)["state"])


def network_energy_rate(hour: int) -> float:
    if 16 <= hour < 21:
        return NETWORK_ENERGY_PEAK_RATE
    if 11 <= hour < 16:
        return NETWORK_ENERGY_OFFPEAK_RATE
    return NETWORK_ENERGY_SHOULDER_RATE


def battery_discharge_cost_rate(hour: int) -> float:
    return BATTERY_DISCHARGE_COST_NIGHT if (hour >= 17 or hour < 7) else BATTERY_DISCHARGE_COST_DAY


def fetch_history_range(entity_id: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out = []
    for p in data[0]:
        state = p.get("state")
        if state in (None, "unknown", "unavailable"):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(BRISBANE_TZ), state))
    return sorted(out, key=lambda x: x[0])


def resample_nearest_float(pts, grid_times, default=0.0):
    numeric = []
    for t, s in pts:
        try:
            numeric.append((t, float(s)))
        except ValueError:
            continue
    out = []
    for gt in grid_times:
        val = numeric[0][1] if numeric else default
        for t, v in numeric:
            if t <= gt:
                val = v
            else:
                break
        out.append(val)
    return out


def value_at_or_before(pts, t, default):
    val = default
    for pt_t, pt_v in pts:
        if pt_t <= t:
            try:
                val = float(pt_v)
            except ValueError:
                continue
        else:
            break
    return val


@dataclass
class BuiltInputs:
    periods: object
    grid: object
    battery: object
    solar: object
    load: object


def build_real_inputs(target_date):
    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=BRISBANE_TZ)
    day_end = day_start + timedelta(days=1)

    grid_times = [day_start + timedelta(hours=i * PERIOD_HOURS) for i in range(N_PERIODS)]
    period_hours_arr = [PERIOD_HOURS] * N_PERIODS

    solar_hist = fetch_history_range("sensor.combined_total_dc_power", day_start, day_end)
    load_hist = fetch_history_range("sensor.cb_total_combined_power_adjusted_kw", day_start, day_end)
    import_price_hist = fetch_history_range("sensor.costsflexup", day_start, day_end)
    export_price_hist = fetch_history_range("sensor.earningsflexup", day_start, day_end)
    soc_hist = fetch_history_range("sensor.logger_battery_level_soc", day_start - timedelta(hours=6), day_end)

    if not solar_hist or not load_hist:
        return None

    solar_kw = [max(0.0, v / 1000.0) for v in resample_nearest_float(solar_hist, grid_times)]
    load_kw = [max(0.0, v) for v in resample_nearest_float(load_hist, grid_times)]

    spot_import_raw = resample_nearest_float(import_price_hist, grid_times, default=0.20)
    spot_export = resample_nearest_float(export_price_hist, grid_times, default=0.05)
    import_price = [spot_import_raw[i] + network_energy_rate(grid_times[i].hour) + CERTIFICATES_RATE for i in range(N_PERIODS)]
    bonus_price = [P2P_BONUS_RATE if 17 <= grid_times[i].hour < 24 else 0.0 for i in range(N_PERIODS)]

    capacity_kwh = num("input_number.nimbus_solver_battery_capacity_kwh")
    max_charge_kw = num("input_number.nimbus_solver_battery_max_charge_kw")
    max_discharge_kw = ha_get("number.logger_charging_discharging_power_kw")["attributes"]["max"]
    charge_cost = num("input_number.nimbus_solver_battery_charge_cost")
    discharge_cost_arr = np.array([battery_discharge_cost_rate(t.hour) for t in grid_times])
    salvage_value = BATTERY_SALVAGE_VALUE_NIGHT if grid_times[-1].hour >= 17 else BATTERY_SALVAGE_VALUE_OTHER
    import_limit_kw = num("input_number.nimbus_solver_grid_import_limit_kw")
    export_limit_kw = num("input_number.nimbus_solver_grid_export_limit_kw")
    min_pct = num("input_number.nimbus_solver_battery_min_soc_pct")
    max_pct = num("input_number.nimbus_solver_battery_max_soc_pct")
    initial_pct = value_at_or_before(soc_hist, day_start, default=50.0)
    initial_soc_kwh = capacity_kwh * initial_pct / 100.0
    assumed_p2p_volume = 63.0  # representative real recent figure, not the focus of this test

    battery_cfg = elements.BatteryConfig(
        capacity_kwh=capacity_kwh, initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * min_pct / 100.0, max_soc_kwh=capacity_kwh * max_pct / 100.0,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.999, discharge_efficiency=0.999,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr, salvage_value=salvage_value,
        headroom_value=0.0,
    )
    grid_cfg = elements.GridConfig(
        import_price=np.array(import_price), export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw, export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(bonus_price), export_bonus_volume_kwh=assumed_p2p_volume,
    )
    solar_cfg = elements.SolarConfig(forecast_kw=np.array(solar_kw))
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=np.array(load_kw))
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    return BuiltInputs(periods=periods, grid=grid_cfg, battery=battery_cfg, solar=solar_cfg, load=load_cfg)


def scale_import_price(inputs: BuiltInputs, multiplier: float) -> BuiltInputs:
    return replace(inputs, grid=replace(inputs.grid, import_price=inputs.grid.import_price * multiplier))


def solve_with_adequacy(inputs: BuiltInputs, target_kwh: float):
    adequacy = elements.AdequacyLoadConfig(
        name="hws_l1_stress_test",
        max_power_kw=ADEQUACY_MAX_POWER_KW,
        target_kwh=target_kwh,
        deadline_period=ADEQUACY_DEADLINE_PERIOD,
        earliest_period=ADEQUACY_EARLIEST_PERIOD,
    )
    return build_plan(
        periods=inputs.periods, grid=inputs.grid, battery=inputs.battery,
        solar=inputs.solar, loads=[inputs.load], adequacy_loads=[adequacy],
    )


def main() -> None:
    dates = [datetime(2026, 8, 16).date(), datetime(2026, 8, 17).date()]
    multipliers = [1, 2, 5, 10]

    print(f"Adequacy load: max_power_kw={ADEQUACY_MAX_POWER_KW}, target_kwh={ADEQUACY_TARGET_KWH:.2f}, "
          f"window=period {ADEQUACY_EARLIEST_PERIOD}-{ADEQUACY_DEADLINE_PERIOD} "
          f"({ADEQUACY_WINDOW_HOURS:.2f}h, 11:00-12:30)")
    print(f"Infeasible-target stress case: target_kwh={ADEQUACY_INFEASIBLE_TARGET_KWH:.2f} "
          f"(1.5x what the window can physically deliver)")
    print()

    for d in dates:
        print(f"{'=' * 78}\n{d} -- price-stress sweep, real target ({ADEQUACY_TARGET_KWH:.2f} kWh)\n{'=' * 78}")
        inputs = build_real_inputs(d)
        if inputs is None:
            print(f"  (no usable real history for {d}, skipping)")
            continue

        prev_cost = None
        costs_monotonic = True
        for mult in multipliers:
            stressed = scale_import_price(inputs, mult)
            plan = solve_with_adequacy(stressed, ADEQUACY_TARGET_KWH)
            if plan.status != "optimal":
                print(f"  {mult:>3}x price: status={plan.status} (UNEXPECTED -- should always be feasible at this real target)")
                continue
            adq = plan.adequacy_loads[0]
            met = adq.delivered_by_deadline_kwh >= ADEQUACY_TARGET_KWH - 1e-6
            cost_note = ""
            if prev_cost is not None and plan.total_cost is not None:
                if plan.total_cost < prev_cost - 1e-6:
                    costs_monotonic = False
                    cost_note = "  <== COST DECREASED despite higher price multiplier (unexpected)"
            prev_cost = plan.total_cost if plan.total_cost is not None else prev_cost
            print(f"  {mult:>3}x price: status={plan.status}  delivered={adq.delivered_by_deadline_kwh:.3f}kWh "
                  f"(target {ADEQUACY_TARGET_KWH:.2f}, {'MET' if met else 'MISSED -- REAL PROBLEM'})  "
                  f"total_cost=${plan.total_cost:.2f}{cost_note}")

        print(f"  -> cost scaled monotonically with price: {'YES' if costs_monotonic else 'NO -- worth a closer look'}")
        print()

        print(f"  Genuinely infeasible target ({ADEQUACY_INFEASIBLE_TARGET_KWH:.2f} kWh, 1x price):")
        plan = solve_with_adequacy(inputs, ADEQUACY_INFEASIBLE_TARGET_KWH)
        expected = "infeasible"
        result_ok = plan.status == expected
        print(f"    status={plan.status}  (expected '{expected}': {'CORRECT' if result_ok else 'WRONG -- real bug'})")
        print(f"    adequacy_loads present on infeasible result: {len(plan.adequacy_loads)} (expected 0, per Plan's own honesty discipline)")
        print()


main()
