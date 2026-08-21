#!/usr/bin/env python3
"""Solver audit item #9 (forecasting) -- the real regret decomposition,
step two, following on from forecast_capture.py/forecast_accuracy_compare.py
(step one, which only measured raw forecast-vs-actual MAE, not economic
impact). Research script, run locally against one real captured snapshot
plus real matured actuals -- NOT a deployed writer yet.

Mark Purcell's own framing (see regret.py's own docstring for the full
quote): regret is the right metric, not MAE, because a low point-forecast
error says nothing about downstream $ impact once a real dispatch
decision has been made from it. This script answers the specific
follow-up question regret.py's own total-regret number can't answer on
its own: of that regret, how much is attributable to FORECAST ERROR
(unavoidable even for a perfect optimizer, given the same imperfect
inputs) versus OPTIMIZATION/CONTROL error (the real automation not
fully exploiting even the forecast it had)?

## Design

Three scenarios, evaluated on the SAME real ground truth via
evaluate_realized_cost() (regret.py), so they're directly comparable:

  A. Perfect foresight (J*)   -- oracle_dispatch() using REAL solar/load,
     the theoretical best possible outcome, already used by every prior
     regret/EPR script in this project.
  B. Perfect optimizer, imperfect forecast (new) -- build_plan() using
     the FORECASTED solar/load actually available at planning time
     yesterday (from a real captured snapshot, not synthetic), then
     evaluate THAT plan's resulting battery trajectory against REAL
     conditions -- answers "how well would even a flawless optimizer
     have done, working only from what was actually forecast?"
  C. Actual/realized -- the real, committed battery dispatch that
     genuinely happened, evaluated the same way.

  Total regret          = J(C) - J(A)   (matches regret.py's own number)
  Forecasting's share   = J(B) - J(A)   (unavoidable, even for a perfect
                                          optimizer, given the real
                                          forecast error that occurred)
  Optimisation's share  = J(C) - J(B)   (additional loss from the real
                                          automation not fully exploiting
                                          even the imperfect forecast)

## Deliberate scope limit -- price held REAL/constant in all 3 scenarios

The one real snapshot captured used nem_pd7day's own NEM *spot* price
forecast -- a genuinely different quantity from the real retail price
(costsflexup/earningsflexup, inclusive of network charges/margin) every
other regret/EPR script in this project prices against. Substituting one
for the other would introduce a real unit mismatch, not a clean forecast-
error test. Since forecast_accuracy_compare.py already measured price
forecast MAE separately (0.011 $/kWh -- genuinely small against real
price levels), and load (1.227kW MAE) / solar (0.581kW MAE) are the more
substantial, directly-comparable dimensions anyway, this first pass
isolates LOAD+SOLAR forecast error specifically, holding price at its
real, known value in every scenario. Price-forecast quality is reported
as a separate, honest side-note, not forced into the LP substitution.

## Window

14:20 Aug 19 (real snapshot capture time) through the run time -- NOT a
full calendar day. Solar forecast covers the whole day regardless of
capture time (Solcast), but load only forecasts forward from "now"
(Nimbus), so the honest overlap window starts at capture time. This
window happens to fully contain the P2P window (17:00-midnight), the
most economically significant part of the day.
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, r"C:\Users\Raf_local\nimbus\custom_components\nimbus_load")
from solver import elements  # noqa: E402
from solver.network import build_plan  # noqa: E402
from solver.regret import evaluate_realized_cost, hourly_regret_breakdown, oracle_dispatch  # noqa: E402
import numpy as np  # noqa: E402

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")
HA_BASE = "http://192.168.1.221:8123"
TOKEN_PATH = r"C:\Users\Raf_local\.ha_token"
SNAPSHOT_PATH = Path(__file__).parent / "forecast_snapshots" / "2026-08-19_1420.json"

PERIOD_HOURS = 0.25  # 15-min resolution, matching the household's other Solver research scripts

NETWORK_ENERGY_PEAK_RATE = 0.214863
NETWORK_ENERGY_OFFPEAK_RATE = 0.00476
NETWORK_ENERGY_SHOULDER_RATE = 0.066759
CERTIFICATES_RATE = 0.008246
BATTERY_DISCHARGE_COST_NIGHT = 0.01
BATTERY_DISCHARGE_COST_DAY = 0.09
BATTERY_SALVAGE_VALUE_NIGHT = 0.3
BATTERY_SALVAGE_VALUE_OTHER = 0.15
P2P_BONUS_RATE = 0.50

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(url_or_entity: str) -> dict:
    url = url_or_entity if url_or_entity.startswith("http") else f"{HA_BASE}/api/states/{url_or_entity}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def num(entity_id: str) -> float:
    return float(ha_get(entity_id)["state"])


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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
        f"?filter_entity_id={entity_id}&end_time={end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
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
    numeric = [(t, float(s)) for t, s in pts if _is_float(s)]
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


def _is_float(s) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def resample_forecast_points(points: list[dict], value_key: str, grid_times: list[datetime], default: float) -> list[float]:
    """Same nearest-at-or-before resampling as resample_nearest_float, but
    reading from the captured snapshot's own {time, <value_key>} point
    list instead of a live HA history fetch."""
    parsed = sorted((datetime.fromisoformat(p["time"]).astimezone(BRISBANE_TZ), p[value_key]) for p in points)
    out = []
    for gt in grid_times:
        val = parsed[0][1] if parsed else default
        for t, v in parsed:
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


def main() -> None:
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    window_start = datetime.fromisoformat(snap["captured_at_brisbane"]).astimezone(BRISBANE_TZ)
    # Round UP to the next 15-min boundary so the grid aligns cleanly.
    minute = ((window_start.minute // 15) + 1) * 15
    if minute >= 60:
        window_start = window_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        window_start = window_start.replace(minute=minute, second=0, microsecond=0)
    window_end = datetime.now(BRISBANE_TZ).replace(minute=0, second=0, microsecond=0)  # round down to the hour, matured data only

    n_periods = int((window_end - window_start).total_seconds() / 3600 / PERIOD_HOURS)
    grid_times = [window_start + timedelta(hours=i * PERIOD_HOURS) for i in range(n_periods)]
    period_hours_arr = [PERIOD_HOURS] * n_periods
    print(f"Analysis window: {window_start} -> {window_end} AEST  ({n_periods} periods, {n_periods * PERIOD_HOURS:.1f}h)")
    print(f"  (covers the full P2P window: {'YES' if any(17 <= t.hour < 24 for t in grid_times) else 'NO'})")
    print()

    # --- REAL ground truth (used for both scenario A's oracle solve AND evaluating every scenario's cost) ---
    solar_real_hist = fetch_history_range("sensor.combined_total_dc_power", window_start, window_end)
    load_real_hist = fetch_history_range("sensor.cb_total_combined_power_adjusted_kw", window_start, window_end)
    import_price_hist = fetch_history_range("sensor.costsflexup", window_start, window_end)
    export_price_hist = fetch_history_range("sensor.earningsflexup", window_start, window_end)
    soc_hist = fetch_history_range("sensor.logger_battery_level_soc", window_start - timedelta(hours=6), window_end)
    batt_power_hist = fetch_history_range("sensor.logger_battery_power", window_start, window_end)

    if not solar_real_hist or not load_real_hist:
        print("ERROR: no real history available for this window -- aborting")
        return

    solar_real_kw = np.array([max(0.0, v / 1000.0) for v in resample_nearest_float(solar_real_hist, grid_times)])
    load_real_kw = np.array([max(0.0, v) for v in resample_nearest_float(load_real_hist, grid_times)])
    spot_import_raw = resample_nearest_float(import_price_hist, grid_times, default=0.20)
    spot_export = np.array(resample_nearest_float(export_price_hist, grid_times, default=0.05))
    import_price_real = np.array([spot_import_raw[i] + network_energy_rate(grid_times[i].hour) + CERTIFICATES_RATE for i in range(n_periods)])
    bonus_price = np.array([P2P_BONUS_RATE if 17 <= grid_times[i].hour < 24 else 0.0 for i in range(n_periods)])

    # --- FORECASTED solar/load, from the real captured snapshot (used ONLY for scenario B's own plan) ---
    solar_fc_kw = np.array([max(0.0, v) for v in resample_forecast_points(snap["solar"]["points"], "pv_estimate_kw", grid_times, default=0.0)])
    load_fc_kw = np.array([max(0.0, v) for v in resample_forecast_points(snap["load"]["points"], "value_kw", grid_times, default=0.0)])

    # --- shared config (same for all 3 scenarios -- only solar/load differ between A and B; battery/grid params are real either way) ---
    capacity_kwh = num("input_number.nimbus_solver_battery_capacity_kwh")
    max_charge_kw = num("input_number.nimbus_solver_battery_max_charge_kw")
    max_discharge_kw = ha_get("number.logger_charging_discharging_power_kw")["attributes"]["max"]
    charge_cost = num("input_number.nimbus_solver_battery_charge_cost")
    discharge_cost_arr = np.array([battery_discharge_cost_rate(t.hour) for t in grid_times])
    salvage_value = BATTERY_SALVAGE_VALUE_NIGHT if grid_times[-1].hour >= 17 or grid_times[-1].hour < 7 else BATTERY_SALVAGE_VALUE_OTHER
    import_limit_kw = num("input_number.nimbus_solver_grid_import_limit_kw")
    export_limit_kw = num("input_number.nimbus_solver_grid_export_limit_kw")
    min_pct = num("input_number.nimbus_solver_battery_min_soc_pct")
    max_pct = num("input_number.nimbus_solver_battery_max_soc_pct")
    initial_pct = value_at_or_before(soc_hist, window_start, default=50.0)
    initial_soc_kwh = capacity_kwh * initial_pct / 100.0
    min_soc_kwh = capacity_kwh * min_pct / 100.0
    max_soc_kwh = capacity_kwh * max_pct / 100.0

    grid_cfg = elements.GridConfig(
        import_price=import_price_real, export_price=spot_export,
        import_limit_kw=import_limit_kw, export_limit_kw=export_limit_kw,
        export_bonus_price=bonus_price, export_bonus_volume_kwh=63.0,  # real recent trailing average, not the focus of this test
    )
    battery_cfg = elements.BatteryConfig(
        capacity_kwh=capacity_kwh, initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh, max_soc_kwh=max_soc_kwh,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.999, discharge_efficiency=0.999,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr, salvage_value=salvage_value,
    )
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    # === Scenario A: perfect foresight (J*) ===
    solar_cfg_real = elements.SolarConfig(forecast_kw=solar_real_kw)
    load_cfg_real = elements.LoadConfig(name="whole_house", forecast_kw=load_real_kw)
    a_charge_kw, a_discharge_kw, a_final_soc = oracle_dispatch(periods=periods, grid=grid_cfg, battery=battery_cfg, solar=solar_cfg_real, load=load_cfg_real)
    a_result = evaluate_realized_cost(
        hours=np.array(period_hours_arr), load_real_kw=load_real_kw, solar_real_kw=solar_real_kw,
        import_price_real=import_price_real, export_price_real=spot_export,
        charge_committed_kw=a_charge_kw, discharge_committed_kw=a_discharge_kw,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr,
        final_soc_kwh=a_final_soc, salvage_value=salvage_value,
        grid_import_limit_kw=import_limit_kw, grid_export_limit_kw=export_limit_kw,
    )

    # === Scenario B: perfect optimizer, imperfect (real, captured) forecast ===
    solar_cfg_fc = elements.SolarConfig(forecast_kw=solar_fc_kw)
    load_cfg_fc = elements.LoadConfig(name="whole_house", forecast_kw=load_fc_kw)
    plan_b = build_plan(periods=periods, grid=grid_cfg, battery=battery_cfg, solar=solar_cfg_fc, loads=[load_cfg_fc], adequacy_loads=[])
    if plan_b.status != "optimal":
        print(f"ERROR: scenario B plan status={plan_b.status} -- aborting")
        return
    b_result = evaluate_realized_cost(
        hours=np.array(period_hours_arr), load_real_kw=load_real_kw, solar_real_kw=solar_real_kw,
        import_price_real=import_price_real, export_price_real=spot_export,
        charge_committed_kw=plan_b.battery_charge_kw, discharge_committed_kw=plan_b.battery_discharge_kw,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr,
        final_soc_kwh=float(plan_b.battery_soc_kwh[-1]), salvage_value=salvage_value,
        grid_import_limit_kw=import_limit_kw, grid_export_limit_kw=export_limit_kw,
    )

    # === Scenario C: actual/realized (what really happened) ===
    batt_power_kw = np.array(resample_nearest_float(batt_power_hist, grid_times, default=0.0))
    c_charge_kw = np.maximum(0.0, -batt_power_kw)
    c_discharge_kw = np.maximum(0.0, batt_power_kw)
    c_final_pct = value_at_or_before(soc_hist, window_end, default=initial_pct)
    c_final_soc = capacity_kwh * c_final_pct / 100.0
    c_result = evaluate_realized_cost(
        hours=np.array(period_hours_arr), load_real_kw=load_real_kw, solar_real_kw=solar_real_kw,
        import_price_real=import_price_real, export_price_real=spot_export,
        charge_committed_kw=c_charge_kw, discharge_committed_kw=c_discharge_kw,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr,
        final_soc_kwh=c_final_soc, salvage_value=salvage_value,
        grid_import_limit_kw=import_limit_kw, grid_export_limit_kw=export_limit_kw,
    )

    print("=== Results (all in $, negative = profit) ===")
    print(f"  A. Perfect foresight (J*):              {a_result.total_cost:>10.2f}")
    print(f"  B. Perfect optimizer, imperfect forecast: {b_result.total_cost:>10.2f}")
    print(f"  C. Actual/realized:                      {c_result.total_cost:>10.2f}")
    print()
    total_regret = c_result.total_cost - a_result.total_cost
    forecasting_share = b_result.total_cost - a_result.total_cost
    control_share = c_result.total_cost - b_result.total_cost
    print(f"Total regret (C - A):              ${total_regret:>8.2f}")
    print(f"  Forecasting's share (B - A):     ${forecasting_share:>8.2f}  ({100*forecasting_share/total_regret if total_regret else 0:.0f}% of total)")
    print(f"  Optimisation's share (C - B):    ${control_share:>8.2f}  ({100*control_share/total_regret if total_regret else 0:.0f}% of total)")
    print()
    print("Real solar/load forecast MAE this window (for reference, price MAE deliberately excluded -- see module docstring):")
    solar_mae = float(np.mean(np.abs(solar_fc_kw - solar_real_kw)))
    load_mae = float(np.mean(np.abs(load_fc_kw - load_real_kw)))
    print(f"  solar MAE: {solar_mae:.3f} kW   load MAE: {load_mae:.3f} kW")

    print()
    print("=== Hourly breakdown: WHERE does each regret component actually accumulate? ===")
    print("(positive = rust, that hour lost money vs the comparison; negative = teal, that hour did BETTER)")
    print()
    control_hourly = hourly_regret_breakdown(timestamps=grid_times, actual_cost_per_period=c_result.cost_per_period, oracle_cost_per_period=b_result.cost_per_period)
    forecast_hourly = hourly_regret_breakdown(timestamps=grid_times, actual_cost_per_period=b_result.cost_per_period, oracle_cost_per_period=a_result.cost_per_period)
    all_hours = sorted(set(control_hourly) | set(forecast_hourly))
    print(f"{'Hour':>6}  {'Control (C-B)':>14}  {'Forecast (B-A)':>15}")
    for h in all_hours:
        print(f"{h:>6}  {control_hourly.get(h,0.0):>14.3f}  {forecast_hourly.get(h,0.0):>15.3f}")


main()
