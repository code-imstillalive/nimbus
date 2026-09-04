#!/usr/bin/env python3
"""Contract-risk / match-failure injection test (Mark Purcell's Solver
audit item #4) -- research script, run locally against real settled
data, NOT a deployed writer yet.

The question: the Solver currently plans tonight's dispatch assuming
export_bonus_volume_kwh = a plain 5-day trailing average of real settled
P2P match volume (p2p_recent_avg_volume_kwh() in
nimbus_solver_forecast_writer.py). That's a point estimate with real
variance around it -- LocalVolts' own matching is not a guaranteed
contract, it's subject to real counterparty/demand variability night to
night. This script quantifies: if the REAL match volume that actually
materializes on a given night comes in at some fraction of what was
assumed at planning time, how much real revenue is at risk?

Methodology, for each of several recent REAL settled days D:
  1. Reconstruct what p2p_recent_avg_volume_kwh() would have returned
     AT PLANNING TIME for D -- the average of the 5 real settled days
     immediately BEFORE D (not today's own trailing average, which
     would be a different, later value).
  2. Build the Solver's real plan for day D using REAL (not forecast)
     load/solar/price history, with export_bonus_volume_kwh set to that
     reconstructed assumed value -- same real-data-reconstruction
     pattern as nimbus_solver_quality_writer.py's own EPR/regret
     analysis (reused directly, not re-derived).
  3. Extract the plan's own real grid_export_kw trajectory during the
     P2P window (17:00-24:00) -- this is the PHYSICAL dispatch the
     plan produces, which happens regardless of how much of it actually
     gets matched (the battery discharges to serve the plan; matching
     is a downstream, separate, real-world process).
  4. For match-failure fractions f in [1.0, 0.75, 0.5, 0.25, 0.0]
     (the real match volume that materializes, as a fraction of what
     was assumed): re-price the SAME total P2P-window export volume,
     capping bonus-eligible volume at assumed_cap * f (can't exceed
     what was physically exported either), pricing the remainder at
     plain spot. Report the real $ shortfall vs f=1.0 (full match, as
     originally planned).

Deliberately aggregate, not period-by-period: this project's own
extensive P2P investigation history (116KAT-HA-AI's own CLAUDE.md) has
repeatedly found LocalVolts' API exposes no real per-period match-status
detail -- an aggregate "X kWh of tonight's total export got matched"
re-pricing is the honest, defensible level of precision available, not
a false-precision per-period allocation.

## Real findings, run 2026-08-18

Only 2 of 5 candidate recent days had usable real history --
sensor.cb_total_combined_power_adjusted_kw (the real load input this
script needs) only exists since 2026-08-16, genuinely too new to reach
further back. A real, honest limitation of this specific run, not a
bug -- revisit once more real days accumulate.

A real, non-obvious finding confirmed by this run, not assumed: the
Solver's own LP-optimal plan reliably exports almost EXACTLY the
assumed cap during the P2P window (63.5kWh assumed vs 63.5kWh actually
planned on 2026-08-16; 62.8 vs 62.8 on 2026-08-17) -- not a bug in this
script, a real economic property of the model: the $0.50 bonus rate is
so far above plain spot that the LP has strong incentive to maximise
bonus-eligible export right up to whatever cap it's given, and real
battery capacity comfortably supports it. This means the Solver's plan
is essentially FULLY exposed on the entire assumed volume, not
partially hedged by some natural slack.

Theoretical stress-test exposure (averaged across the 2 usable days):
  - Real match at 50% of assumed:  -$13.39/night
  - Real match completely fails:   -$26.77/night (essentially the
    whole night's P2P premium, though the underlying spot-rate revenue
    is still earned -- not a zero-revenue night, just a lost premium)

REALISTIC exposure, grounded in real observed variability (not a
stress test): the real settled export_volume across all 15 real
settled days (2026-08-03 through 2026-08-17) has mean=63.1kWh,
stdev=3.2kWh, min=55.9kWh -- a tight 5% coefficient of variation.
Nothing close to a 50% or 100% match failure has actually happened in
this account's real history so far. Using the real historical WORST
night observed (55.9kWh, an 11.5% shortfall vs the mean) as the
realistic "how bad has it actually gotten" scenario: -$3.08/night.

Net read: the theoretical worst-case exposure is real and worth
knowing (a genuine, if unlikely, ~$27/night tail risk), but the
empirical track record so far has been remarkably stable -- this is
reassuring context, not a reason to ignore the tail risk, which is
exactly the kind of two-part honest answer this project's own
docstring discipline (regret.py/tracking.py/epr.py) calls for.
"""
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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
MATCH_FAILURE_FRACTIONS = [1.0, 0.75, 0.5, 0.25, 0.0]

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


def assumed_p2p_volume_at_planning_time(confirmed_hist: dict, target_date_str: str, recent_days: int = 5) -> float:
    """Reconstructs exactly what p2p_recent_avg_volume_kwh() would have
    returned on the MORNING of target_date_str (planning time for that
    evening) -- the average of the `recent_days` real settled days
    STRICTLY BEFORE target_date_str, matching the live writer's own
    "sorted(hist.keys())[-recent_days:]" logic but anchored to a
    historical date instead of always "today."
    """
    prior_dates = sorted(d for d in confirmed_hist if d < target_date_str)[-recent_days:]
    volumes = [confirmed_hist[d].get("export_volume", 0.0) for d in prior_dates if confirmed_hist[d].get("export_volume", 0.0) > 0]
    if not volumes:
        return 60.0  # same fallback magnitude as the live writer's own constant
    return sum(volumes) / len(volumes)


@dataclass
class DayResult:
    date_str: str
    assumed_volume_kwh: float
    total_p2p_window_export_kwh: float
    scenario_revenue: dict[float, float]  # fraction -> $


def run_for_day(target_date, confirmed_hist: dict) -> DayResult | None:
    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=BRISBANE_TZ)
    day_end = day_start + timedelta(days=1)
    date_str = target_date.isoformat()

    assumed_volume = assumed_p2p_volume_at_planning_time(confirmed_hist, date_str)

    grid_times = [day_start + timedelta(hours=i * PERIOD_HOURS) for i in range(N_PERIODS)]
    period_hours_arr = [PERIOD_HOURS] * N_PERIODS

    solar_hist = fetch_history_range("sensor.combined_total_dc_power", day_start, day_end)
    load_hist = fetch_history_range("sensor.cb_total_combined_power_adjusted_kw", day_start, day_end)
    import_price_hist = fetch_history_range("sensor.costsflexup", day_start, day_end)
    export_price_hist = fetch_history_range("sensor.earningsflexup", day_start, day_end)
    soc_hist = fetch_history_range("sensor.logger_battery_level_soc", day_start - timedelta(hours=6), day_end)

    if not solar_hist or not load_hist:
        print(f"  {date_str}: no real history available, skipping")
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

    battery_cfg = elements.BatteryConfig(
        capacity_kwh=capacity_kwh, initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * min_pct / 100.0, max_soc_kwh=capacity_kwh * max_pct / 100.0,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.999, discharge_efficiency=0.999,
        charge_cost=charge_cost, discharge_cost=discharge_cost_arr, salvage_value=salvage_value,
    )
    grid_cfg = elements.GridConfig(
        import_price=np.array(import_price), export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw, export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(bonus_price), export_bonus_volume_kwh=assumed_volume,
    )
    solar_cfg = elements.SolarConfig(forecast_kw=np.array(solar_kw))
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=np.array(load_kw))
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    plan = build_plan(periods=periods, grid=grid_cfg, battery=battery_cfg, solar=solar_cfg, loads=[load_cfg], adequacy_loads=[])
    if plan.status != "optimal":
        print(f"  {date_str}: plan status={plan.status}, skipping")
        return None

    p2p_window_mask = [17 <= t.hour < 24 for t in grid_times]
    total_p2p_export_kwh = float(sum(
        plan.grid_export_kw[i] * period_hours_arr[i] for i in range(N_PERIODS) if p2p_window_mask[i]
    ))
    avg_spot_export_rate = float(np.mean([spot_export[i] for i in range(N_PERIODS) if p2p_window_mask[i]]))

    scenario_revenue = {}
    for f in MATCH_FAILURE_FRACTIONS:
        real_match_kwh = min(assumed_volume * f, total_p2p_export_kwh)
        remaining_kwh = total_p2p_export_kwh - real_match_kwh
        revenue = real_match_kwh * P2P_BONUS_RATE + remaining_kwh * avg_spot_export_rate
        scenario_revenue[f] = revenue

    return DayResult(
        date_str=date_str, assumed_volume_kwh=assumed_volume,
        total_p2p_window_export_kwh=total_p2p_export_kwh, scenario_revenue=scenario_revenue,
    )


def main() -> None:
    confirmed_hist = ha_get("sensor.lv_v2_p2p_confirmed_history")["attributes"]["history"]
    all_dates = sorted(confirmed_hist.keys())
    # Test the most recent 5 real settled days with enough real prior
    # history (at least 5 earlier days) to compute a genuine assumed
    # volume, not the fallback constant.
    candidate_dates = [d for d in all_dates if len([x for x in all_dates if x < d]) >= 5][-5:]

    results = []
    for d in candidate_dates:
        target = datetime.strptime(d, "%Y-%m-%d").date()
        print(f"Running {d}...")
        r = run_for_day(target, confirmed_hist)
        if r:
            results.append(r)

    print()
    print("=" * 100)
    print(f"{'Date':<12} {'Assumed kWh':>12} {'Real Export kWh':>16} " + "".join(f"{'f='+str(f):>12}" for f in MATCH_FAILURE_FRACTIONS))
    print("=" * 100)
    for r in results:
        row = f"{r.date_str:<12} {r.assumed_volume_kwh:>12.1f} {r.total_p2p_window_export_kwh:>16.1f} "
        row += "".join(f"{r.scenario_revenue[f]:>12.2f}" for f in MATCH_FAILURE_FRACTIONS)
        print(row)

    print()
    print("Real $ shortfall vs full-match (f=1.0) baseline:")
    for r in results:
        baseline = r.scenario_revenue[1.0]
        shortfalls = {f: baseline - r.scenario_revenue[f] for f in MATCH_FAILURE_FRACTIONS}
        print(f"  {r.date_str}: " + ", ".join(f"f={f}: -${shortfalls[f]:.2f}" for f in MATCH_FAILURE_FRACTIONS if f != 1.0))

    if results:
        avg_shortfall_50 = sum(r.scenario_revenue[1.0] - r.scenario_revenue[0.5] for r in results) / len(results)
        avg_shortfall_0 = sum(r.scenario_revenue[1.0] - r.scenario_revenue[0.0] for r in results) / len(results)
        print()
        print(f"AVERAGE real $ exposure if match comes in at 50% of assumed: ${avg_shortfall_50:.2f}/night")
        print(f"AVERAGE real $ exposure if match completely fails (0%): ${avg_shortfall_0:.2f}/night")

        # Realistic (empirically-grounded) exposure, using the REAL
        # historical minimum/stdev of settled export_volume (all 15 real
        # days, not just the 2 usable for a fresh LP solve) -- the
        # 50%/0% scenarios above are legitimate stress tests, but nothing
        # close to that has actually happened yet. This answers "what's
        # genuinely at risk given real observed variability," not "what's
        # theoretically possible."
        all_vols = confirmed_hist_export_volumes(confirmed_hist)
        if len(all_vols) >= 2:
            import statistics
            real_mean = statistics.mean(all_vols)
            real_min = min(all_vols)
            realistic_fraction = real_min / real_mean if real_mean > 0 else 1.0

            # Direct linear interpolation between the tested fractions
            # (revenue is very close to linear in f, since bonus_rate >>
            # spot_rate dominates the formula) rather than re-running the
            # LP for one more exact fraction.
            def interp_revenue(r: DayResult, f: float) -> float:
                fs = sorted(MATCH_FAILURE_FRACTIONS)
                for lo, hi in zip(fs, fs[1:]):
                    if lo <= f <= hi:
                        t = (f - lo) / (hi - lo)
                        return r.scenario_revenue[lo] + t * (r.scenario_revenue[hi] - r.scenario_revenue[lo])
                return r.scenario_revenue[1.0]

            avg_shortfall_realistic = sum(
                r.scenario_revenue[1.0] - interp_revenue(r, realistic_fraction) for r in results
            ) / len(results)
            print()
            print(f"Real historical export_volume: mean={real_mean:.1f}kWh, min={real_min:.1f}kWh (15 real settled days)")
            print(f"REALISTIC exposure (assumed=mean, real=historical min, f={realistic_fraction:.3f}): ${avg_shortfall_realistic:.2f}/night")


def confirmed_hist_export_volumes(confirmed_hist: dict) -> list[float]:
    return [confirmed_hist[d].get("export_volume", 0.0) for d in confirmed_hist if confirmed_hist[d].get("export_volume", 0.0) > 0]


main()
