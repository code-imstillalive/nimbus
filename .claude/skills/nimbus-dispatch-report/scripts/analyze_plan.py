#!/usr/bin/env python3
"""Analyse the day-ahead window of the solver forecast inside a nimbus_load diagnostics dump.

  python3 analyze_plan.py --diag diagnostics.json --out DIR [--hours 24]

Prints an hourly table, charge/discharge/export runs, SoC extremes, price-blend fallback
periods, cap-pinned periods and the cost split. Writes:
  DIR/plan_analysis.json   points, hourly table, events, totals, meta, prefilled KPIs
  DIR/narrative.json       starter narrative (only created if absent) for the page build
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os

FIELDS = ("import_price", "export_price", "export_price_raw", "load_kw", "solar_kw", "grid_import_kw",
          "grid_export_kw", "battery_kw", "soc_pct", "hours")


def _t(p):
    return dt.datetime.fromisoformat(p["time"])


def _runs(win, pred, kw_field, hours="hours"):
    out, cur = [], None
    for p in win:
        if pred(p):
            if cur is None:
                cur = {"start": _t(p), "end": _t(p), "kwh": 0.0, "prices": []}
            cur["end"] = _t(p) + dt.timedelta(hours=p[hours])
            cur["kwh"] += abs(p[kw_field]) * p[hours]
            cur["prices"].append(p["import_price"])
        elif cur:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return [
        {"start": r["start"].strftime("%a %H:%M"), "end": r["end"].strftime("%H:%M"), "kwh": round(r["kwh"], 1),
         "import_price_min": round(min(r["prices"]) * 100, 1), "import_price_max": round(max(r["prices"]) * 100, 1)}
        for r in out if r["kwh"] > 0.5
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    diag = json.load(open(args.diag))
    data = diag.get("data", {}).get("data", diag)
    sol = data["solver"]
    fc = sol["forecast"]
    if sol.get("battery_kw_sign_convention", "positive_discharge_negative_charge") != "positive_discharge_negative_charge":
        for p in fc:
            p["battery_kw"] = -p["battery_kw"]
    t0 = _t(fc[0])
    t_end = t0 + dt.timedelta(hours=args.hours)
    win = [p for p in fc if _t(p) < t_end]
    day0 = t0.date()

    def label(p):
        t = _t(p)
        return ("" if t.date() == day0 else "+") + t.strftime("%H:%M")

    pts = [[label(p), round(t.timestamp()), round(p["import_price"] * 100, 2), round(p["export_price"] * 100, 2),
            round(p.get("export_price_raw", p["export_price"]) * 100, 2), round(p["load_kw"], 2), round(p["solar_kw"], 2),
            round(p["grid_import_kw"], 2), round(p["battery_kw"], 2), round(p["soc_pct"], 1), round(p["hours"], 4),
            1 if abs(p["export_price"] - p.get("export_price_raw", p["export_price"])) > 1e-4
            or abs(p["import_price"] - p.get("import_price_raw", p["import_price"])) > 1e-4 else 0]
           for p in win for t in [_t(p)]]

    # hourly table (energy-weighted)
    H = collections.OrderedDict()
    for p in win:
        k = _t(p).strftime("%a %H:00")
        h = p["hours"]
        a = H.setdefault(k, collections.defaultdict(float))
        a["h"] += h
        for f in ("grid_import_kw", "grid_export_kw", "load_kw", "solar_kw", "battery_kw"):
            a[f] += p[f] * h
        a["net_cost"] += p.get("net_cost", 0.0)
        a["imp"] += p["import_price"] * h
        a["exp"] += p["export_price"] * h
        a["soc"] = p["soc_pct"]
    table = [[k, round(a["imp"] / a["h"] * 100, 1), round(a["exp"] / a["h"] * 100, 1), round(a["load_kw"], 1),
              round(a["solar_kw"], 1), round(a["grid_import_kw"], 1), round(a["grid_export_kw"], 1), round(a["battery_kw"], 1),
              round(a["soc"], 1), round(a["net_cost"], 2)] for k, a in H.items()]

    tot = {f: round(sum(p[f] * p["hours"] for p in win), 1) for f in ("load_kw", "solar_kw", "grid_import_kw", "grid_export_kw")}
    tot["net_cost"] = round(sum(p.get("net_cost", 0.0) for p in win), 2)
    chg = sum(-p["battery_kw"] * p["hours"] for p in win if p["battery_kw"] < 0)
    dis = sum(p["battery_kw"] * p["hours"] for p in win if p["battery_kw"] > 0)
    cap = float(sol.get("battery_capacity_kwh") or (data.get("solver_config") or {}).get("solver_battery_capacity_kwh") or 0) or None
    flows = {f: round(sum(p.get(f, 0.0) * p["hours"] for p in win), 1) for f in
             ("flow_grid_to_battery_kw", "flow_pv_to_battery_kw", "flow_battery_to_grid_kw", "flow_battery_to_load_kw")}
    soc = [(_t(p), p["soc_pct"]) for p in win]
    smin, smax = min(soc, key=lambda x: x[1]), max(soc, key=lambda x: x[1])
    min_soc_pct = float((data.get("solver_config") or {}).get("solver_battery_min_soc_percent") or 0)
    imp_limit = float((data.get("solver_config") or {}).get("solver_grid_max_import_kw") or 0)
    max_chg = float((data.get("solver_config") or {}).get("solver_max_charge_kw") or 0)
    div = [p for p in pts if p[11]]
    events = {
        "charge_runs": _runs(win, lambda p: p["battery_kw"] < -0.5, "battery_kw"),
        "discharge_runs": _runs(win, lambda p: p["battery_kw"] > 0.5, "battery_kw"),
        "export_runs": _runs(win, lambda p: p["grid_export_kw"] > 0.2, "grid_export_kw"),
        "soc_start": round(soc[0][1], 1), "soc_end": round(soc[-1][1], 1),
        "soc_min": round(smin[1], 1), "soc_min_at": smin[0].strftime("%a %H:%M"),
        "soc_max": round(smax[1], 1), "soc_max_at": smax[0].strftime("%a %H:%M"),
        "hours_at_floor": round(sum(p["hours"] for p in win if p["soc_pct"] <= min_soc_pct + 0.01), 2),
        "hours_at_full": round(sum(p["hours"] for p in win if p["soc_pct"] >= 99.99), 2),
        "periods_import_capped": sum(1 for p in win if imp_limit and p["grid_import_kw"] >= imp_limit - 0.01),
        "periods_charge_capped": sum(1 for p in win if max_chg and p["battery_kw"] <= -(max_chg - 0.01)),
        "fallback_periods": len(div), "periods": len(win), "fallback_first": div[0][0] if div else None,
        "top_import_prices": [(pp[0], pp[2]) for pp in sorted(pts, key=lambda r: -r[2])[:3]],
        "lowest_import_prices": [(pp[0], pp[2]) for pp in sorted(pts, key=lambda r: r[2])[:3]],
    }
    deg_rate = float(sol.get("degradation_cost_per_kwh") or 0.0)
    meta = {k: v for k, v in sol.items() if k != "forecast" and not isinstance(v, (list, dict))}
    meta["cost_breakdown"] = sol.get("cost_breakdown")
    meta["cost_band"] = sol.get("cost_band")
    meta["window_hours"] = round(sum(p["hours"] for p in win), 2)
    meta["window_start"] = fc[0]["time"]
    meta["capacity_kwh"] = cap
    meta["min_soc_pct"] = min_soc_pct
    meta["version"] = (diag.get("data", {}).get("integration_manifest") or {}).get("version")
    kpis = [
        ["Grid cost, next %d h" % round(args.hours), "$%.2f" % tot["net_cost"],
         "%.1f ¢ per kWh of load" % (tot["net_cost"] / tot["load_kw"] * 100 if tot["load_kw"] else 0)],
        ["House load", "%.1f kWh" % tot["load_kw"], "%.1f kWh solar available" % tot["solar_kw"]],
        ["Grid import", "%.1f kWh" % tot["grid_import_kw"], "%.1f kWh exported" % tot["grid_export_kw"]],
        ["Battery throughput", "%.0f kWh" % (chg + dis),
         ("%.2f full cycles" % ((chg + dis) / (2 * cap)) if cap else "") + (" · ≈$%.2f degradation" % (deg_rate * (chg + dis)) if deg_rate else "")],
        ["%.0f h expected cost" % float(sol.get("horizon_hours", 0)), "$%.2f" % float(sol.get("total_cost", 0)),
         ("band $%.1f – $%.1f" % (sol["cost_band"]["lower"], sol["cost_band"]["upper"]) if sol.get("cost_band") else "")
         + (" (risk aversion %s)" % sol.get("risk_aversion") if sol.get("risk_aversion") is not None else "")],
    ]
    analysis = {"meta": meta, "kpis": kpis, "pts": pts, "table": table, "totals": tot,
                "battery": {"charge_kwh": round(chg, 1), "discharge_kwh": round(dis, 1), "throughput_kwh": round(chg + dis, 1),
                            "degradation_cost": round(deg_rate * (chg + dis), 2), **flows},
                "events": events}
    json.dump(analysis, open(os.path.join(args.out, "plan_analysis.json"), "w"), separators=(",", ":"))

    # ---- print
    print(f'{"hour":10s}{"imp¢":>6s}{"exp¢":>6s}{"load":>6s}{"pv":>6s}{"imp":>6s}{"exp":>6s}{"batt":>7s}{"soc":>6s}{"cost$":>7s}')
    for r in table:
        print(f'{r[0]:10s}{r[1]:6.1f}{r[2]:6.1f}{r[3]:6.1f}{r[4]:6.1f}{r[5]:6.1f}{r[6]:6.1f}{r[7]:7.1f}{r[8]:6.1f}{r[9]:7.2f}')
    print("totals:", tot, "| battery:", analysis["battery"])
    for k in ("charge_runs", "discharge_runs", "export_runs"):
        for r in events[k]:
            print(f"  {k[:-5]:9s} {r['start']}→{r['end']} {r['kwh']:6.1f} kWh at import {r['import_price_min']}–{r['import_price_max']} ¢")
    print({k: v for k, v in events.items() if not k.endswith("_runs")})
    print("cost_breakdown:", meta["cost_breakdown"], "cost_band:", meta["cost_band"], "total_cost:", meta.get("total_cost"))
    print("load now (forecast vs meter):", meta.get("load_summed_18_now_kw"), "vs", meta.get("load_whole_house_cross_check_now_kw"),
          "| solar_delivery_ratio:", meta.get("solar_delivery_ratio"), "| binding now:", meta.get("binding_constraint_now"))

    narr = os.path.join(args.out, "narrative.json")
    if not os.path.exists(narr):
        json.dump({
            "title": "Nimbus day-ahead battery plan",
            "subtitle": "The next %d hours as the solver published them at %s." % (round(args.hours), meta.get("generated_at")),
            "stamp_extra": "",
            "kpis": None,
            "callouts": [{"panel": "power", "t": "12:00", "v": 0, "dx": 0, "dy": -24, "text": "<b>Claim.</b> Number that proves it."}],
            "strengths": [], "weaknesses": [],
            "yesterday": {"kpis": None, "callouts": [], "lessons": []},
            "footnote": "",
            "_measured": {"events": events, "totals": tot, "battery": analysis["battery"]},
        }, open(narr, "w"), indent=1)
        print("starter narrative written:", narr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
