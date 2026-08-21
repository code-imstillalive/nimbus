#!/usr/bin/env python3
"""Forecast capture step for Mark Purcell's Solver audit item #9
(forecasting) -- the missing per-layer regret decomposition (topology /
forecasting / optimisation / control, per Mark's own closing point,
quoted directly in solver/epr.py) is "not built yet." This is step one
of building it, run locally, no HA state changes.

Real, hard data constraint found and confirmed live before writing this
(not assumed): NONE of the day-ahead forecasts this project has -- solar
(Solcast) or price (nem_pd7day) -- have ANY recorder history at all (0
history points over a 2-day check). Both are excluded from HA's
recorder, same deliberate pattern this project already uses everywhere
else to avoid database bloat. Nimbus's own load forecast
(sensor.nimbus_household_load_total_forecast) DOES have real historical
STATE points, but its `forecast` array attribute (where the actual
prediction lives) is not preserved -- only the bare summary state is
kept. Conclusion: there is no way to reconstruct what any of these
forecasts said for a PAST day. This isn't a data-quality gap, it's a
data-existence gap -- the only honest fix is to start capturing forward
from today, same pattern as every other daily writer in this project
(accumulate real data over time, don't try to retroactively invent
missing history).

This script captures TODAY's live forecast snapshot -- for solar and
price, both the remainder of today AND all of tomorrow (Solcast/
nem_pd7day both already forecast that far); for load, Nimbus's own
forecast horizon (~90h) -- to a durable, git-tracked JSON file, keyed by
the real generation timestamp. `forecast_accuracy_compare.py` (the
companion script, run LATER once enough real time has passed for a
captured window to have real settled actuals) reads this back and
computes real forecast-vs-actual error.

2026-08-21: converted to a real, unattended NUC cron job (same pattern
as every other writer script in this project) -- a single local run
was fine to unblock the first comparison, but real accumulation needs
this running every day regardless of whether anyone remembers to run
it. Snapshots persist NUC-locally (not git-tracked -- same convention
as nimbus_counterfactual_history.json, a growing dataset that doesn't
belong in the repo itself).

Deploy (host cron, NOT inside the HA container -- same pattern as every
other writer script):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/research/forecast_capture.py > /opt/forecast_capture.py
  mkdir -p /home/homehub/forecast_snapshots
  sudo touch /opt/forecast_capture.log && sudo chown homehub:homehub /opt/forecast_capture.log
  python3 /opt/forecast_capture.py   # one-off test run first
  (crontab -l 2>/dev/null; echo "0 */6 * * * python3 /opt/forecast_capture.py >> /opt/forecast_capture.log 2>&1") | crontab -
  # every 6h -- solar/price/load forecasts don't meaningfully change
  # faster than that, and this keeps the snapshot count/disk growth
  # modest over weeks of accumulation.
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")
HA_BASE = "http://192.168.1.221:8123"
TOKEN_PATH = "/home/homehub/.ha_token"
SNAPSHOT_DIR = Path("/home/homehub/forecast_snapshots")

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(entity_id: str) -> dict:
    req = urllib.request.Request(f"{HA_BASE}/api/states/{entity_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def capture_solar() -> dict:
    """Solcast's own detailedForecast array -- period_start (ISO,
    already tz-aware), pv_estimate (kW) -- for the remainder of today
    plus all of tomorrow. pv_estimate10/90 kept too (Solcast's own
    P10/P90 confidence band) for a future, richer comparison, not just
    the point estimate.
    """
    out = []
    for ent in ("sensor.solcast_pv_forecast_forecast_today", "sensor.solcast_pv_forecast_forecast_tomorrow"):
        d = ha_get(ent)
        for p in d["attributes"]["detailedForecast"]:
            out.append({
                "time": p["period_start"],
                "pv_estimate_kw": p["pv_estimate"],
                "pv_estimate10_kw": p["pv_estimate10"],
                "pv_estimate90_kw": p["pv_estimate90"],
            })
    # dedupe by time (today/tomorrow endpoints can overlap at the boundary)
    seen = {}
    for p in out:
        seen[p["time"]] = p
    return {"source": "solcast_pv_forecast", "points": sorted(seen.values(), key=lambda p: p["time"])}


def capture_price() -> dict:
    """nem_pd7day's own real forecast array -- use `calibrated`, not
    `raw_value` (this project's own already-confirmed finding: raw AEMO
    forecasts can predict false spikes the isotonic-calibrated figure
    correctly suppresses -- see 116KAT-HA-AI CLAUDE.md, the LV price
    forecast writer's own AEMO-anchoring work)."""
    d = ha_get("sensor.nem_pd7day_qld1_nem_spot_price_forecast")
    points = [
        {"time": p["time"], "calibrated_price": p["calibrated"], "raw_price": p["raw_value"]}
        for p in d["attributes"]["forecast"]
    ]
    return {"source": "nem_pd7day_calibrated", "points": points}


def capture_lv_retail_price() -> dict:
    """The REAL retail price forecast the Solver actually dispatches
    against (sensor.localvolts_price_forecast -- costsflexup/
    earningsflexup), NOT the AEMO wholesale forecast capture_price()
    already captures above. Real, important distinction (2026-08-21,
    Mark Purcell's own feedback on price_risk_aversion, relayed by the
    household): "'more expensive in the afternoons' is a bias claim, not
    a variance one, and if the forecast is systematically low at 17:00
    the fix is to debias the forecaster rather than hand the user a
    slider." compare_price_bias() below only becomes able to test that
    claim once THIS specific forecast (the one actually feeding
    dispatch) has real captured-vs-actual pairs -- capture_price()'s own
    AEMO wholesale comparison is a genuinely different quantity (no
    network/margin/retail markup) and can't answer this question no
    matter how much data accumulates there.

    Real coverage limit (confirmed live, session history): LV's own
    forecast only extends ~14-36h ahead depending on direction, unlike
    AEMO's genuine 7-day reach -- captures whatever's currently there,
    same "start accumulating from today, don't invent missing history"
    principle as every other capture in this file.
    """
    d = ha_get("sensor.localvolts_price_forecast")
    points = [
        {"time": p["time"], "costs_flex_up": p.get("costsflexup"), "earnings_flex_up": p.get("earningsflexup")}
        for p in d["attributes"]["forecast"]
    ]
    return {"source": "localvolts_price_forecast", "points": points}


def capture_load() -> dict:
    """Nimbus's own whole-house load forecast -- real point estimate
    plus its own lower/upper band."""
    d = ha_get("sensor.nimbus_household_load_total_forecast")
    points = [
        {"time": p["time"], "value_kw": p["value"], "lower_kw": p["lower"], "upper_kw": p["upper"]}
        for p in d["attributes"]["forecast"]
    ]
    return {"source": "nimbus_household_load_forecast", "points": points}


def main() -> None:
    now = datetime.now(timezone.utc)
    now_bris = now.astimezone(BRISBANE_TZ)

    snapshot = {
        "captured_at_utc": now.isoformat(),
        "captured_at_brisbane": now_bris.isoformat(),
        "solar": capture_solar(),
        "price": capture_price(),
        "lv_retail_price": capture_lv_retail_price(),
        "load": capture_load(),
    }

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    out_path = SNAPSHOT_DIR / f"{now_bris.strftime('%Y-%m-%d_%H%M')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Captured snapshot at {now_bris.strftime('%Y-%m-%d %H:%M')} AEST -> {out_path}")
    print(f"  solar:           {len(snapshot['solar']['points'])} points, "
          f"{snapshot['solar']['points'][0]['time']} -> {snapshot['solar']['points'][-1]['time']}")
    print(f"  price (AEMO):    {len(snapshot['price']['points'])} points, "
          f"{snapshot['price']['points'][0]['time']} -> {snapshot['price']['points'][-1]['time']}")
    if snapshot["lv_retail_price"]["points"]:
        print(f"  price (LV retail): {len(snapshot['lv_retail_price']['points'])} points, "
              f"{snapshot['lv_retail_price']['points'][0]['time']} -> {snapshot['lv_retail_price']['points'][-1]['time']}")
    else:
        print("  price (LV retail): 0 points -- sensor.localvolts_price_forecast unavailable this run")
    print(f"  load:            {len(snapshot['load']['points'])} points, "
          f"{snapshot['load']['points'][0]['time']} -> {snapshot['load']['points'][-1]['time']}")


main()
