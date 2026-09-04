#!/usr/bin/env python3
"""Score yesterday: Nimbus's quality report (reference / achieved / oracle) against the recorder.

  python3 score_day.py --quality quality_report.json --recorder recorder.json \
      [--cqr cqr.json] [--diag diagnostics.json] --out DIR

Prints the hourly table with regret, the regret's top hours, per-trajectory throughput, the
recorder-vs-scored SoC discrepancy and a degradation re-pricing estimate. Writes DIR/yesterday.json.
Sign note: the quality report's battery_kw is negative = discharge (SoC falls); grid_kw negative = export.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import zoneinfo


def _attrs(qr):
    d = qr.get("data", qr)
    states = d.get("states") or {}
    if states:
        st = next(iter(states.values()))
        return st["attributes"], st.get("state")
    return qr.get("attributes", qr), qr.get("state")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quality", required=True)
    ap.add_argument("--recorder", required=True)
    ap.add_argument("--cqr")
    ap.add_argument("--diag")
    ap.add_argument("--degradation", type=float, help="$/kWh; default from diagnostics, else 0.03")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    a, state = _attrs(json.load(open(args.quality)))
    ref, ach, star, reg = a["j_ref_hourly"], a["j_ach_hourly"], a["j_star_hourly"], a["hourly_regret"]
    rec = json.load(open(args.recorder))
    tz = zoneinfo.ZoneInfo(rec.get("tz", "Australia/Brisbane"))
    sensors = rec.get("sensors", {})
    resp = rec.get("response", rec)
    ents = (resp.get("data") or resp).get("entities") or []
    by_hour = {}
    for e in ents:
        for r in e.get("statistics") or []:
            t = r["start"]
            t = t / 1000 if isinstance(t, (int, float)) and t > 1e12 else t
            h = (dt.datetime.fromtimestamp(t, tz) if isinstance(t, (int, float)) else dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(tz)).strftime("%H")
            by_hour.setdefault(h, {})[e["entity_id"]] = r.get("mean")

    deg = args.degradation
    if deg is None and args.diag:
        sol = json.load(open(args.diag)).get("data", {}).get("data", {}).get("solver", {})
        deg = float(sol.get("degradation_cost_per_kwh") or 0.0)
    if deg is None:
        deg = 0.03

    rows = []
    keys = sorted(ref)
    for k in keys:
        h = k[11:13]
        r = by_hour.get(h, {})
        real = r.get(sensors.get("soc"))
        rows.append([f"{h}:00", round(ref[k]["import_price_aud_per_kwh"] * 100, 1), round(ref[k]["export_price_aud_per_kwh"] * 100, 1),
                     round(real, 1) if isinstance(real, (int, float)) else None, round(ach[k]["soc_pct"], 1), round(star[k]["soc_pct"], 1),
                     round(reg[str(int(h))], 3), round(ach[k]["battery_kw"], 2), round(star[k]["battery_kw"], 2),
                     round(ref[k]["load_kw"], 1), round(ref[k]["solar_kw"], 1), round(ach[k]["grid_kw"], 2), round(star[k]["grid_kw"], 2),
                     r.get(sensors.get("grid")), r.get(sensors.get("battery"))])

    def thr(rows_):
        return sum(abs(rows_[k]["battery_kw"]) for k in keys)

    thr_ach, thr_star = thr(ach), thr(star)
    disc = [abs(r[3] - r[4]) for r in rows if r[3] is not None]
    kpis = {
        "epr_pct": a.get("epr_pct"), "j_ref": a.get("j_ref"), "j_ach": a.get("j_ach"), "j_star": a.get("j_star"),
        "regret": a.get("regret_dollars"), "theoretical_maximum_yield": a.get("theoretical_maximum_yield"),
        "value_captured": a.get("value_captured"), "tracking_fidelity": a.get("tracking_fidelity"), "tracking_cost": a.get("tracking_cost"),
        "latest_date": a.get("latest_date"), "generated_at": a.get("generated_at"),
        "throughput_ach_kwh": round(thr_ach, 1), "throughput_star_kwh": round(thr_star, 1), "degradation_rate": deg,
        "degradation_ach": round(deg * thr_ach, 2), "degradation_star": round(deg * thr_star, 2),
        "j_ach_repriced": round(a["j_ach"] + deg * thr_ach, 2), "j_star_repriced_upper_bound": round(a["j_star"] + deg * thr_star, 2),
        "soc_discrepancy_max": round(max(disc), 1) if disc else None, "soc_discrepancy_mean": round(sum(disc) / len(disc), 1) if disc else None,
        "real_close": rows[-1][3], "scored_close": rows[-1][4], "oracle_close": rows[-1][5],
    }
    top = sorted(((v, h) for h, v in reg.items()), reverse=True)
    kpis["regret_top_hours"] = [(int(h), round(v, 2)) for v, h in top[:5]]
    kpis["regret_top4_sum"] = round(sum(v for v, _ in top[:4]), 2)
    cross = None
    if args.cqr and os.path.exists(args.cqr):
        c = json.load(open(args.cqr))
        c = c.get("service_response") or c
        cross = {k: c.get(k) for k in ("epr_pct", "j_ref", "j_ach", "j_star", "regret_dollars", "window_start", "window_end")}
        cross["matches_sensor"] = all(abs(float(c.get(k, 0)) - float(a.get(k, 0))) < 1e-3 for k in ("j_ref", "j_ach", "j_star"))
    json.dump({"rows": rows, "kpis": kpis, "cross_check": cross, "sensors": sensors}, open(os.path.join(args.out, "yesterday.json"), "w"), separators=(",", ":"))

    print(f'{"hr":>5s} {"imp¢":>5s} {"exp¢":>5s} {"load":>5s} {"pv":>5s} | {"real":>5s} {"ach":>5s} {"orc":>5s} | {"achBat":>7s} {"orcBat":>7s} | {"achGrd":>7s} {"mtrGrd":>7s} | {"regret":>7s}')
    for r in rows:
        mg = r[13] if isinstance(r[13], (int, float)) else float("nan")
        print(f'{r[0]:>5s} {r[1]:5.1f} {r[2]:5.1f} {r[9]:5.1f} {r[10]:5.1f} | {(r[3] if r[3] is not None else float("nan")):5.1f} {r[4]:5.1f} {r[5]:5.1f} | {r[7]:7.2f} {r[8]:7.2f} | {r[11]:7.2f} {mg:7.2f} | {r[6]:7.3f}')
    print("state:", state, "| kpis:", json.dumps(kpis))
    print("cross-check:", cross)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
