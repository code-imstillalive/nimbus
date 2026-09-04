#!/usr/bin/env python3
"""Forecast accuracy comparison, step two of Mark Purcell's Solver audit
item #9 (forecasting) -- companion to forecast_capture.py.

Reads back every snapshot in forecast_snapshots/, and for each captured
point whose forecasted time has ALREADY PASSED (so a real, settled
actual now exists), fetches the real measured value from HA's recorder
and computes forecast error. Safe to re-run at any time -- it only ever
reports on whatever has genuinely "matured" so far (load points mature
within hours; solar/price points need a full day). Re-running later
just reports on more matured points, nothing needs to be re-captured.

Real actual sources, matched to what each forecast is actually
predicting (not just "the closest-sounding sensor"):
  - solar:  sensor.combined_total_dc_power (W -> kW) -- same real solar
    measurement this project's own Solver test scripts already use.
  - price:  sensor.aemo_nem_qld1_current_5min_period_price -- the real
    AEMO NEM spot price, the correct like-for-like comparison against
    nem_pd7day's own `calibrated` NEM spot forecast (NOT costsflexup,
    which is a retail price including network/margin, a different real
    quantity entirely).
  - load:   sensor.cb_total_combined_power_adjusted_kw -- the clean,
    already-validated real load sensor this project's own P2P
    automation and every recent Solver test script already use (session
    2026-08-16 root-caused and replaced the noisier
    sensor.logger_load_power for exactly this reason).

Reports per source: n points compared, MAE, and (once the mechanism
proves out) is the natural next step toward the real Mark Purcell
decomposition -- forecasting layer's share of regret vs optimisation/
control's share -- described in forecast_capture.py's own docstring.
This first pass reports raw forecast accuracy only; the full regret
decomposition is a real, separate follow-up once there's enough matured
data to make it meaningful (a single day's worth of solar/price points
is a reasonable first read, but not yet a statistically solid one).
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")
# 2026-08-21: NUC-local paths, matching forecast_capture.py's own real
# cron deployment -- run on demand (not cron-scheduled itself) via:
#   git show origin/main:scripts/research/forecast_accuracy_compare.py > /opt/forecast_accuracy_compare.py
#   HA_TOKEN_PATH=/home/homehub/.ha_token SNAPSHOT_DIR=/home/homehub/forecast_snapshots python3 /opt/forecast_accuracy_compare.py
#
# nimbus issue #364 finding 4 (Mark Purcell, codebase review): these used
# to be hardcoded to one specific household's own real IP/paths -- see
# forecast_capture.py's own identical comment for the full reasoning.
HA_BASE = os.environ.get("HA_BASE", "http://homeassistant.local:8123")
TOKEN_PATH = os.environ["HA_TOKEN_PATH"]
SNAPSHOT_DIR = Path(os.environ["SNAPSHOT_DIR"])

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = f.read().strip()


def fetch_history_range(entity_id: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
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
        try:
            v = float(p["state"])
        except (ValueError, KeyError):
            continue
        t = datetime.fromisoformat(p["last_changed"])
        out.append((t, v))
    return sorted(out, key=lambda x: x[0])


def nearest_actual(pts: list[tuple[datetime, float]], target: datetime, max_gap_minutes: float = 30.0) -> float | None:
    best = None
    best_gap = None
    for t, v in pts:
        gap = abs((t - target).total_seconds()) / 60.0
        if best_gap is None or gap < best_gap:
            best, best_gap = v, gap
    if best_gap is not None and best_gap <= max_gap_minutes:
        return best
    return None


def load_all_snapshots() -> list[dict]:
    snapshots = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            snapshots.append(json.load(f))
    return snapshots


def compare_source(source_key: str, value_field: str, actual_entity: str, actual_unit_scale: float, snapshots: list[dict], hourly_breakdown: bool = False) -> None:
    """MAE alone (the original metric here) cannot tell a systematic BIAS
    (forecast consistently too high/low) apart from genuine random
    VARIANCE (sometimes high, sometimes low, cancelling out on average) --
    a real, direct Mark Purcell finding (2026-08-21, relayed by the
    household): "'more expensive in the afternoons' is a bias claim, not
    a variance one... if the forecast is systematically low the fix is to
    debias the forecaster rather than hand the user a slider." Now also
    reports SIGNED mean error (forecast - actual, not abs()) alongside
    MAE -- a mean error near zero with a real MAE means genuine variance
    (a risk-aversion slider is the right tool); a mean error that's a
    real fraction of the MAE means a genuine, fixable bias instead.

    hourly_breakdown=True additionally buckets by real hour-of-day (AEST)
    so a specific claim like "the afternoons" is actually testable, not
    just an aggregate across the whole day -- exactly what's needed to
    check the 17:00 window specifically once enough data has matured.
    """
    now = datetime.now(timezone.utc)

    all_points = []
    for snap in snapshots:
        for p in snap[source_key]["points"]:
            t = datetime.fromisoformat(p["time"])
            if t.astimezone(timezone.utc) < now:
                val = p[value_field]
                if val is not None:
                    all_points.append((t, val))

    if not all_points:
        print(f"  {source_key}: 0 matured points yet (nothing forecasted has happened yet -- check back later)")
        return

    earliest_t = min(t for t, _ in all_points)
    actual_pts_raw = fetch_history_range(actual_entity, earliest_t - timedelta(minutes=30), now)
    actual_pts = [(t, v * actual_unit_scale) for t, v in actual_pts_raw]

    if not actual_pts:
        print(f"  {source_key}: {len(all_points)} matured forecast points, but no real actual history found for {actual_entity} -- check entity name")
        return

    signed_errors = []  # (local_hour, forecast - actual)
    for t, forecast_v in all_points:
        actual_v = nearest_actual(actual_pts, t)
        if actual_v is None:
            continue
        local_hour = t.astimezone(BRISBANE_TZ).hour
        signed_errors.append((local_hour, forecast_v - actual_v))

    if not signed_errors:
        print(f"  {source_key}: {len(all_points)} matured forecast points, but no matching real actual sample within 30min of any of them")
        return

    errs = [e for _, e in signed_errors]
    mae = sum(abs(e) for e in errs) / len(errs)
    mean_error = sum(errs) / len(errs)
    bias_share = abs(mean_error) / mae if mae > 1e-9 else 0.0
    print(f"  {source_key}: {len(errs)}/{len(all_points)} points matched to real actuals")
    print(f"    MAE = {mae:.4f}, mean error (signed, forecast-actual) = {mean_error:+.4f} "
          f"({bias_share*100:.0f}% of MAE is bias, not variance)")

    if hourly_breakdown:
        by_hour: dict[int, list[float]] = {}
        for h, e in signed_errors:
            by_hour.setdefault(h, []).append(e)
        for h in sorted(by_hour):
            vals = by_hour[h]
            h_mean = sum(vals) / len(vals)
            h_mae = sum(abs(v) for v in vals) / len(vals)
            flag = " <-- afternoon window" if 14 <= h <= 18 else ""
            print(f"      {h:02d}:00 AEST  n={len(vals):3d}  mean_error={h_mean:+.4f}  MAE={h_mae:.4f}{flag}")


def main() -> None:
    snapshots = load_all_snapshots()
    if not snapshots:
        print("No snapshots captured yet -- run forecast_capture.py first.")
        return

    print(f"Loaded {len(snapshots)} snapshot(s): {[Path(s['captured_at_brisbane']).name if False else s['captured_at_brisbane'] for s in snapshots]}")
    print()
    print("=== Forecast accuracy, matured points only (forecasted time already passed) ===")
    compare_source("solar", "pv_estimate_kw", "sensor.combined_total_dc_power", 1.0 / 1000.0, snapshots)
    compare_source("price", "calibrated_price", "sensor.aemo_nem_qld1_current_5min_period_price", 1.0, snapshots)
    compare_source("load", "value_kw", "sensor.cb_total_combined_power_adjusted_kw", 1.0, snapshots)
    print()
    print("=== LV retail price (the forecast the Solver actually dispatches against) ===")
    print("--- import (costs_flex_up) ---")
    compare_source("lv_retail_price", "costs_flex_up", "sensor.localvolts_costs_flex_up", 1.0, snapshots, hourly_breakdown=True)
    print("--- export (earnings_flex_up) ---")
    compare_source("lv_retail_price", "earnings_flex_up", "sensor.localvolts_earnings_flex_up", 1.0, snapshots, hourly_breakdown=True)
    print()
    print("Note: this is raw forecast-vs-actual error only. The full Mark Purcell")
    print("regret decomposition (forecasting layer's share vs optimisation/control's")
    print("share) is the real next step once there's enough matured data to be")
    print("meaningful -- see forecast_capture.py's own docstring for the design.")


main()
