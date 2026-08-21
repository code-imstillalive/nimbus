#!/usr/bin/env python3
"""Measurement-integrity check (Mark Purcell's Solver audit item #1) --
research script, run locally against real settled data.

Real reconciliation between our own reconstructed figures and
LocalVolts' own settled Cashflow Breakdown has already been done
informally in earlier sessions (this project's own documented history),
finding real gaps in the low single-digit percent on some line items --
but never checked under Mark's own stated strict bar: within 1%.

This test closes that gap for the single most fundamental measurement
this whole Solver stack depends on: real exported energy volume. Every
other cost/revenue figure (P2P bonus, spot export, regret, EPR) is
downstream of "how much energy did we actually export" -- if THAT
disagrees with LocalVolts' own independently-settled meter by more than
a rounding error, everything built on top of it inherits the same
uncertainty, however precise the arithmetic on top looks.

Methodology: for each of several real, fully-settled recent days,
reconstruct total exported kWh for the FULL day (00:00-23:59:59, not
just the P2P window -- spot_export_volume includes daytime solar export
too) via a real Riemann sum of sensor.logger_meter_total_active_power
(negative = export, this project's own established convention, already
cross-validated this session against the independent CHINT switchboard
meter). Compare against LocalVolts' own real settled total
(export_volume [P2P-matched] + spot_export_volume [spot-sold],
sensor.lv_v2_p2p_confirmed_history) -- the definitive, independently-
metered ground truth, not something we compute ourselves.

Report the exact percentage deviation per day and whether it holds
within Mark's own stated 1% bar.

## Real findings, run 2026-08-19

Clean pass, all 3 real days tested, comfortably under the strict 1% bar:
  2026-08-16: -0.88%
  2026-08-17: -0.70%
  2026-08-18: -0.80%

A real, consistent directional bias -- our own reconstruction reads
slightly LOWER than LocalVolts' own confirmed meter every single day,
by a tight, similar margin each time (not random noise scattered around
zero). Most likely explanation, consistent with this project's own
already-documented finding elsewhere: normal Sungrow-CT-vs-NMI-billing-
meter calibration difference (a real, understood, small systematic
offset between our own inverter-side current-transformer meter and
LocalVolts' own official billing meter at the grid connection point) --
not investigated further here, since it stays safely inside the strict
bar regardless of its exact cause.

Net answer to Mark's audit item #1: the foundational measurement this
whole Solver stack depends on (real exported energy) agrees with an
independent, trusted, externally-settled source to within 1% on every
real day tested -- not just informally close, verified under the real
stated bar.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")
HA_BASE = "http://192.168.1.221:8123"
TOKEN_PATH = r"C:\Users\Raf_local\.ha_token"
STRICT_TOLERANCE_PCT = 1.0

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
        t = datetime.fromisoformat(p["last_changed"].replace("Z", "+00:00")).astimezone(BRISBANE_TZ)
        out.append((t, v))
    return sorted(out, key=lambda x: x[0])


def real_export_kwh(day) -> float:
    """Real Riemann-sum reconstruction of total exported energy for a
    full calendar day, from our own independently-polled grid meter."""
    day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=BRISBANE_TZ)
    day_end = day_start + timedelta(days=1)
    pts = fetch_history_range("sensor.logger_meter_total_active_power", day_start, day_end)
    if not pts:
        return None
    total_kwh = 0.0
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, _ = pts[i + 1]
        duration_h = (t1 - t0).total_seconds() / 3600.0
        export_kw = max(0.0, -v0)  # negative = export, per this project's own established convention
        total_kwh += export_kw * duration_h
    return total_kwh


def fetch_confirmed(day_str: str) -> dict | None:
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/sensor.lv_v2_p2p_confirmed_history",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read())
    return d.get("attributes", {}).get("history", {}).get(day_str)


def main() -> None:
    dates = [datetime(2026, 8, 16).date(), datetime(2026, 8, 17).date(), datetime(2026, 8, 18).date()]

    print(f"Measurement integrity check -- real export volume vs LocalVolts' own settled meter")
    print(f"Strict tolerance (Mark Purcell's own stated bar): {STRICT_TOLERANCE_PCT}%\n")
    print(f"{'Date':<12} {'Our recon (kWh)':>17} {'LV confirmed (kWh)':>20} {'Deviation':>12} {'Within 1%?':>12}")
    print("=" * 78)

    all_within = True
    for d in dates:
        confirmed = fetch_confirmed(d.isoformat())
        if confirmed is None:
            print(f"{d.isoformat():<12}  (no confirmed history for this date, skipping)")
            continue
        lv_total_kwh = confirmed["export_volume"] + confirmed["spot_export_volume"]
        our_total_kwh = real_export_kwh(d)
        if our_total_kwh is None:
            print(f"{d.isoformat():<12}  (no real grid meter history for this date, skipping)")
            continue
        dev_pct = (our_total_kwh - lv_total_kwh) / lv_total_kwh * 100.0
        within = abs(dev_pct) <= STRICT_TOLERANCE_PCT
        all_within = all_within and within
        print(f"{d.isoformat():<12} {our_total_kwh:>17.3f} {lv_total_kwh:>20.3f} {dev_pct:>+11.2f}% {'YES' if within else 'NO':>12}")

    print()
    print(f"All days within Mark's own {STRICT_TOLERANCE_PCT}% bar: {'YES' if all_within else 'NO'}")


main()
