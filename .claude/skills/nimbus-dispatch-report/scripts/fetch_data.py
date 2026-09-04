#!/usr/bin/env python3
"""Fetch everything the report needs from a live Home Assistant via ha-mcp.

Writes into --out:
  diagnostics.json     nimbus_load config-entry diagnostics (the solver forecast lives here)
  quality_report.json  sensor.nimbus_solver_quality_report state + attributes
  cqr.json             compute_quality_report service response for --date (cross-check)
  recorder.json        hourly recorder statistics for the SoC / battery / grid / price sensors

Requires HA_MCP_URL in the environment (see hamcp.py). Every call is read-only except the
compute_quality_report service, which computes and returns a report without changing state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hamcp  # noqa: E402

QUALITY_SENSOR = "sensor.nimbus_solver_quality_report"


def _dump(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    print(f"wrote {path} ({os.path.getsize(path):,} bytes)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--entry-id", help="nimbus_load config entry id (discovered if omitted)")
    ap.add_argument("--date", help="local day to score (YYYY-MM-DD); default yesterday")
    ap.add_argument("--tz", help="IANA timezone; default from HA (fallback Australia/Brisbane)")
    ap.add_argument("--skip-cqr", action="store_true", help="skip the compute_quality_report service call")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # 1. config entry
    entry_id = args.entry_id
    if not entry_id:
        lst = hamcp.call_tool("ha_get_integration", {"domain": "nimbus_load"})
        entries = lst.get("entries") or []
        if not entries:
            sys.exit("no nimbus_load config entry found")
        entry_id = entries[0]["entry_id"]
        print("entry:", entry_id, entries[0].get("state"))

    # 2. diagnostics
    diag = hamcp.call_tool("ha_get_integration", {"entry_id": entry_id, "include_diagnostics": True})
    diagnostics = diag.get("diagnostics") or diag
    _dump(os.path.join(args.out, "diagnostics.json"), diagnostics)
    data = diagnostics.get("data", {}).get("data", {})
    options = (data.get("entry") or {}).get("options") or {}
    solver_cfg = data.get("solver_config") or {}

    # timezone + date
    tzname = args.tz or (diagnostics.get("data", {}).get("home_assistant", {}) or {}).get("time_zone") or "Australia/Brisbane"
    tz = zoneinfo.ZoneInfo(tzname)
    day = dt.date.fromisoformat(args.date) if args.date else (dt.datetime.now(tz).date() - dt.timedelta(days=1))
    start = dt.datetime.combine(day, dt.time.min, tz)
    end = start + dt.timedelta(days=1)
    print("scoring window:", start.isoformat(), "->", end.isoformat())

    # 3. quality report sensor
    st = hamcp.call_tool("ha_get_state", {"entity_id": [QUALITY_SENSOR]})
    _dump(os.path.join(args.out, "quality_report.json"), st)

    # 4. compute_quality_report cross-check
    if not args.skip_cqr:
        try:
            cqr = hamcp.call_tool(
                "ha_call_service",
                {"domain": "nimbus_load", "service": "compute_quality_report",
                 "data": {"start": start.isoformat(), "end": end.isoformat()},
                 "return_response": True, "wait": False},
            )
            _dump(os.path.join(args.out, "cqr.json"), cqr)
        except Exception as exc:  # noqa: BLE001 -- the cross-check is optional; the sensor alone still scores
            print("compute_quality_report failed (continuing without cross-check):", str(exc)[:300])

    # 5. recorder statistics for the sensors the entry uses
    def pick(*keys):
        for k in keys:
            v = options.get(k) or solver_cfg.get(k)
            if v:
                return v
        return None

    sensors = {
        "soc": pick("solver_battery_soc_sensor"),
        "battery": pick("solver_battery_power_sensor", "battery_sensor"),
        "grid": pick("grid_sensor", "solver_grid_power_sensor"),
        "import_price": pick("solver_import_price_sensor"),
        "export_price": pick("solver_export_price_sensor"),
    }
    ids = [v for v in sensors.values() if v]
    print("recorder sensors:", sensors)
    hist = hamcp.call_tool(
        "ha_get_history",
        {"entity_ids": ids, "source": "statistics", "period": "hour",
         "start_time": start.astimezone(dt.timezone.utc).isoformat(),
         "end_time": end.astimezone(dt.timezone.utc).isoformat()},
    )
    _dump(os.path.join(args.out, "recorder.json"), {"sensors": sensors, "tz": tzname, "day": day.isoformat(), "response": hist})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
