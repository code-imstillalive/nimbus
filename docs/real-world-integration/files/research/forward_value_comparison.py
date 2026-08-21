#!/usr/bin/env python3
"""Real-data comparison: today's flat terminal value (salvage_value/
headroom_value) vs a genuinely concave piecewise replacement (Mark
Purcell's Solver audit item #7), on real recent household load/solar/
price history. Research script, not a deployed writer.

Concave breakpoints calibrated to the SAME average $/kWh as today's real
flat rate over the P2P window (salvage_value=0.3), so any observed
difference is attributable to CURVATURE (front-loaded value, diminishing
near the top), not to accidentally injecting more or less total value:
  first 15% of the above-floor range: 2.2x flat rate (real reserve option value)
  next 55%:                            1.0x flat rate (roughly matches today)
  final 30% (near max_soc):            0.35x flat rate (diminished, headroom-like)
  blended average = 0.15*2.2 + 0.55*1.0 + 0.30*0.35 = 0.33+0.55+0.105 = 0.985x -- ~1x, by design

Requires the sibling nimbus repo's own PR #35 (terminal_value_breakpoints
on BatteryConfig) to run -- see that PR for the LP mechanism itself and
a controlled synthetic verification (a 10-segment concave sweep turning
the documented single 80kWh hard-corner jump into a genuine 6-step
staircase). This script is the companion real-data check.

## Real findings, run 2026-08-18

Both real days tested (2026-08-16, 2026-08-17):
  flat mechanism:    final_soc pinned at 100% (122.2kWh) -- the hard
                      corner, exactly as BatteryConfig's own docstring
                      already documented as a known limitation
  concave mechanism: final_soc lands at a moderate 71% (86.3kWh)
                      AND total_cost is ~$3.12-3.15/day MORE profitable
                      (more negative) than the flat version

Net finding: the flat mechanism wasn't just less smooth, it was
genuinely leaving real value on the table by driving the battery to a
hard extreme every night. Real dispatch difference: ~45kWh L1 norm
between the two plans, a meaningful behavioral change, not a rounding-
level tweak.

Honest caveat: this specific $3.12-3.15/day figure is naturally
sensitive to the specific breakpoint widths/rates chosen above (a
reasonable first design, not yet empirically calibrated against real
settled outcomes) -- the DIRECTION and rough MAGNITUDE of the finding
(meaningfully better, not pinned to an extreme) is the real result,
not this exact dollar figure to the cent.
"""
import json
import sys
import urllib.error
import urllib.request
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
FLAT_SALVAGE_VALUE = 0.3
P2P_BONUS_RATE = 0.50

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(entity_id: str) -> dict:
    req = urllib.request.Request(f"{HA_BASE}/api/states/{entity_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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


def build_and_solve(target_date, use_concave: bool):
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
        return None, None

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
    import_limit_kw = num("input_number.nimbus_solver_grid_import_limit_kw")
    export_limit_kw = num("input_number.nimbus_solver_grid_export_limit_kw")
    min_pct = num("input_number.nimbus_solver_battery_min_soc_pct")
    max_pct = num("input_number.nimbus_solver_battery_max_soc_pct")
    initial_pct = value_at_or_before(soc_hist, day_start, default=50.0)
    initial_soc_kwh = capacity_kwh * initial_pct / 100.0
    min_soc_kwh = capacity_kwh * min_pct / 100.0
    max_soc_kwh = capacity_kwh * max_pct / 100.0
    assumed_p2p_volume = 63.0

    if use_concave:
        above_floor = max_soc_kwh - min_soc_kwh
        breakpoints = [
            (above_floor * 0.15, FLAT_SALVAGE_VALUE * 2.2),
            (above_floor * 0.55, FLAT_SALVAGE_VALUE * 1.0),
            (above_floor * 0.30, FLAT_SALVAGE_VALUE * 0.35),
        ]
        battery_cfg = elements.BatteryConfig(
            capacity_kwh=capacity_kwh, initial_soc_kwh=initial_soc_kwh,
            min_soc_kwh=min_soc_kwh, max_soc_kwh=max_soc_kwh,
            max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
            charge_efficiency=0.999, discharge_efficiency=0.999,
            charge_cost=charge_cost, discharge_cost=discharge_cost_arr, salvage_value=0.0,
            terminal_value_breakpoints=breakpoints,
        )
    else:
        battery_cfg = elements.BatteryConfig(
            capacity_kwh=capacity_kwh, initial_soc_kwh=initial_soc_kwh,
            min_soc_kwh=min_soc_kwh, max_soc_kwh=max_soc_kwh,
            max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
            charge_efficiency=0.999, discharge_efficiency=0.999,
            charge_cost=charge_cost, discharge_cost=discharge_cost_arr, salvage_value=FLAT_SALVAGE_VALUE,
        )

    grid_cfg = elements.GridConfig(
        import_price=np.array(import_price), export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw, export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(bonus_price), export_bonus_volume_kwh=assumed_p2p_volume,
    )
    solar_cfg = elements.SolarConfig(forecast_kw=np.array(solar_kw))
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=np.array(load_kw))
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    plan = build_plan(periods=periods, grid=grid_cfg, battery=battery_cfg, solar=solar_cfg, loads=[load_cfg], adequacy_loads=[])
    return plan, (min_soc_kwh, max_soc_kwh, capacity_kwh)


def main() -> None:
    candidate_dates = ["2026-08-16", "2026-08-17"]
    for date_str in candidate_dates:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        print(f"\n=== {date_str} ===")
        flat_plan, bounds = build_and_solve(target, use_concave=False)
        concave_plan, _ = build_and_solve(target, use_concave=True)
        if flat_plan is None or concave_plan is None:
            print("  no real history available, skipping")
            continue
        if flat_plan.status != "optimal" or concave_plan.status != "optimal":
            print(f"  status flat={flat_plan.status} concave={concave_plan.status}, skipping")
            continue

        min_soc_kwh, max_soc_kwh, capacity_kwh = bounds
        flat_final = flat_plan.battery_soc_kwh[-1]
        concave_final = concave_plan.battery_soc_kwh[-1]
        dispatch_l1 = float(
            np.sum(np.abs(flat_plan.battery_charge_kw - concave_plan.battery_charge_kw)) * PERIOD_HOURS
            + np.sum(np.abs(flat_plan.battery_discharge_kw - concave_plan.battery_discharge_kw)) * PERIOD_HOURS
        )
        print(f"  battery range: [{min_soc_kwh:.1f}, {max_soc_kwh:.1f}] kWh (capacity {capacity_kwh:.1f})")
        print(f"  flat    total_cost=${flat_plan.total_cost:.2f}  final_soc={flat_final:.1f} kWh ({100*flat_final/capacity_kwh:.0f}%)")
        print(f"  concave total_cost=${concave_plan.total_cost:.2f}  final_soc={concave_final:.1f} kWh ({100*concave_final/capacity_kwh:.0f}%)")
        print(f"  dispatch difference (L1 norm): {dispatch_l1:.2f} kWh")


main()
