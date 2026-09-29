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

# nimbus issue #1451 (IV&V pass, following #1422): `real_p2p_settlement_status`
# is not a binary applied/not-applied -- solver_writer.py's own
# `_PROVISIONAL_SETTLEMENT_STATUSES` docstring distinguishes 5 real values into
# 3 classes: "applied" (final), two genuinely time-bound statuses that will
# resolve on their own ("no_settlement_entry_for_this_date",
# "settlement_sensor_unreadable"), and two PERMANENT non-applicable statuses
# that will never become "applied" because there is no settlement to wait for
# ("no_sensor_configured", "window_is_not_one_local_calendar_day"). #1422's own
# fix (score_day.py, a950f81) collapsed all four non-"applied" values into one
# "withhold pending settlement" bucket, so an install with no P2P sensor
# configured at all would have its scorecard withheld forever, every day, even
# though those figures are already final (correctly priced at zero P2P
# credit). Kept as a local copy rather than an import -- these scripts read a
# live HA install over ha-mcp, not the integration's own Python package.
_SETTLEMENT_STATUSES_NEVER_APPLY = frozenset(
    {"no_sensor_configured", "window_is_not_one_local_calendar_day"}
)


def _settlement_is_final(status) -> bool:
    return status == "applied" or status in _SETTLEMENT_STATUSES_NEVER_APPLY


def _real_soc_at_boundary(h, *, by_hour_last, by_hour, soc_entity):
    """The real SoC at hour `h`'s own boundary: the preceding hour's "last"
    recorder reading (the true instant-sample value), falling back to `h`'s
    own hourly mean when there's no preceding "last" bucket (h==0, or a data
    gap -- nimbus #1423). Returns `(value, fell_back)`; `fell_back` is True
    only when the mean fallback was actually used for this specific hour."""
    prev_h = f"{(int(h) - 1) % 24:02d}"
    prev = by_hour_last.get(prev_h, {}).get(soc_entity)
    if prev is not None:
        return prev, False
    return by_hour.get(h, {}).get(soc_entity), True


def _attrs(qr):
    d = qr.get("data", qr)
    states = d.get("states") or {}
    if states:
        st = next(iter(states.values()))
        return st["attributes"], st.get("state")
    return qr.get("attributes", qr), qr.get("state")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--quality", required=True)
    ap.add_argument("--recorder", required=True)
    ap.add_argument("--cqr")
    ap.add_argument("--diag")
    ap.add_argument(
        "--degradation", type=float, help="$/kWh; default from diagnostics, else 0.03"
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    a, state = _attrs(json.load(open(args.quality)))
    ref, ach, star, reg = (
        a["j_ref_hourly"],
        a["j_ach_hourly"],
        a["j_star_hourly"],
        a["hourly_regret"],
    )
    rec = json.load(open(args.recorder))
    tz = zoneinfo.ZoneInfo(rec.get("tz", "Australia/Brisbane"))
    sensors = rec.get("sensors", {})
    resp = rec.get("response", rec)
    ents = (resp.get("data") or resp).get("entities") or []
    by_hour = {}
    by_hour_last = {}
    for e in ents:
        for r in e.get("statistics") or []:
            t = r["start"]
            t = t / 1000 if isinstance(t, (int, float)) and t > 1e12 else t
            h = (
                dt.datetime.fromtimestamp(t, tz)
                if isinstance(t, (int, float))
                else dt.datetime.fromisoformat(
                    str(t).replace("Z", "+00:00")
                ).astimezone(tz)
            ).strftime("%H")
            by_hour.setdefault(h, {})[e["entity_id"]] = r.get("mean")
            by_hour_last.setdefault(h, {})[e["entity_id"]] = r.get("last")

    # nimbus issue #681 (Mark Purcell): this script's own soc_discrepancy
    # reconstruction disagreed 3x with sensor.nimbus_solver_quality_report's
    # own soc_discrepancy_max/mean_pct (10.4/3.4pt here vs the sensor's
    # 32.13/6.41pt) on the same day's data. Root cause found by reading
    # solver_writer.py's own _soc_discrepancy_stats() side by side with this
    # script: that function compares the achieved trajectory's INSTANT value
    # at each hour boundary (j_ach_hourly's own keys are "day_start + h
    # hours") against the real SoC sensor's nearest single sample to that
    # SAME instant (resample_history_nearest). This script instead compared
    # against an HOURLY MEAN of the real SoC sensor's statistics bucket that
    # STARTS at that same hour -- averaging over the hour AFTER the boundary,
    # not sampling AT it. On a day where the real pack fell to and sat at
    # 0% overnight (this issue's own real case), a mean-over-the-hour smooths
    # over exactly the kind of sharp transition the sensor's instant-sample
    # approach is designed to catch -- explaining both the direction (this
    # script under-reports) and rough size of the disagreement.
    # Fix, SoC only (grid/battery power comparisons a few lines down are a
    # genuinely different question -- interval-average power vs interval-
    # average power -- and correctly keep using `by_hour`'s own mean): for
    # hour h's own boundary instant, use the PRECEDING hour's bucket "last"
    # value (the real SoC sensor's last known reading before that boundary)
    # as the instant proxy, falling back to h's own mean when there's no
    # preceding bucket (h==0, or a data gap).
    # nimbus issue #1423 (Mark Purcell): the fix below never activated. Every
    # hourly bucket came back with "last": null, because fetch_data.py's own
    # ha_get_history call did not request that statistic -- so `prev` was
    # ALWAYS None and this ALWAYS took its fallback, silently reproducing the
    # hourly-mean behaviour #681 was written to replace. Measured the same day:
    # this script printed soc_discrepancy max/mean 30.8/12.3pt against the
    # sensor's own 5.56/2.03pt, a >5x disagreement in the opposite direction to
    # #681's original report.
    #
    # fetch_data.py now asks for `last`. It may still not arrive, because
    # whether the MCP tool forwards a `types` parameter is not something either
    # script can guarantee. So the fallback stays -- but it is no longer
    # silent, and anything derived from it is labelled.
    #
    # nimbus issue #1450 (IV&V pass): this used to be a blanket `any(...)`
    # over every hour `by_hour_last` happens to carry -- True the moment a
    # SINGLE hour anywhere had a "last" value, even if every hour actually
    # used to build a row below fell back to the mean estimate. A day with
    # exactly one instant sample and 23 fallbacks read as fully "instant".
    # Tracked per row instead: `soc_boundary_is_instant` is true only when
    # EVERY boundary this script actually computed used the real "last"
    # reading, not the smoothed fallback.
    _soc_boundary_fell_back_hours = []

    def real_soc_at_boundary(h):
        value, fell_back = _real_soc_at_boundary(
            h,
            by_hour_last=by_hour_last,
            by_hour=by_hour,
            soc_entity=sensors.get("soc"),
        )
        if fell_back:
            _soc_boundary_fell_back_hours.append(h)
        return value

    deg = args.degradation
    if deg is None and args.diag:
        sol = (
            json.load(open(args.diag)).get("data", {}).get("data", {}).get("solver", {})
        )
        deg = float(sol.get("degradation_cost_per_kwh") or 0.0)
    if deg is None:
        deg = 0.03

    rows = []
    keys = sorted(ref)
    for k in keys:
        # nimbus-dispatch-report skill bug (found 2026-09-24): `k` is a UTC
        # ISO timestamp (j_ref_hourly/hourly_regret's own key shape), so
        # `k[11:13]` is the UTC hour. `by_hour`/`by_hour_last` above are
        # keyed by LOCAL hour (tz applied at their own construction). Using
        # the UTC hour to index them silently pulled the recorder cross-
        # check (`real`/`mtrGrd`) for the wrong hour of the day -- up to a
        # 10h (Brisbane UTC+10) misalignment against every other column in
        # the same row, which are all genuinely UTC-consistent with each
        # other. `reg` (hourly_regret) is itself UTC-keyed, so that lookup
        # was already correct and must keep using the UTC hour.
        h_utc = k[11:13]
        h_local = dt.datetime.fromisoformat(k).astimezone(tz).strftime("%H")
        r = by_hour.get(h_local, {})
        real = real_soc_at_boundary(h_local)
        rows.append(
            [
                f"{h_local}:00",
                round(ref[k]["import_price_aud_per_kwh"] * 100, 1),
                round(ref[k]["export_price_aud_per_kwh"] * 100, 1),
                round(real, 1) if isinstance(real, (int, float)) else None,
                round(ach[k]["soc_pct"], 1),
                round(star[k]["soc_pct"], 1),
                round(reg[str(int(h_utc))], 3),
                round(ach[k]["battery_kw"], 2),
                round(star[k]["battery_kw"], 2),
                round(ref[k]["load_kw"], 1),
                round(ref[k]["solar_kw"], 1),
                round(ach[k]["grid_kw"], 2),
                round(star[k]["grid_kw"], 2),
                r.get(sensors.get("grid")),
                r.get(sensors.get("battery")),
            ]
        )

    # nimbus #1450: computed after the loop above, from what actually
    # happened for the rows this script built -- see real_soc_at_boundary().
    soc_boundary_is_instant = not _soc_boundary_fell_back_hours

    def thr(rows_):
        return sum(abs(rows_[k]["battery_kw"]) for k in keys)

    thr_ach, thr_star = thr(ach), thr(star)
    disc = [abs(r[3] - r[4]) for r in rows if r[3] is not None]
    # nimbus #1081: epr_pct/regret_dollars/theoretical_maximum_yield/value_captured are all
    # computed server-side from j_star_evaluator (the oracle's chosen trajectory repriced
    # through the same real-cost arithmetic j_ach uses), NOT from the raw LP objective
    # j_star (which still carries soft-SoC/slack/bonus-as-variable search-machinery terms).
    # Displaying raw j_star next to those derived figures is internally inconsistent -- a
    # reader doing (j_ref-j_ach)/(j_ref-j_star) by hand with the displayed j_star gets a
    # different number than the displayed epr_pct. Show both, clearly separated, and base
    # any further derived math (the repricing upper bound) on the evaluator figure.
    j_star_eval = a.get("j_star_evaluator", a.get("j_star"))
    kpis = {
        "epr_pct": a.get("epr_pct"),
        "j_ref": a.get("j_ref"),
        "j_ach": a.get("j_ach"),
        "j_star_raw_lp_objective": a.get("j_star"),
        "j_star_evaluator": j_star_eval,
        "j_star_path_delta": a.get("j_star_path_delta"),
        "regret": a.get("regret_dollars"),
        "theoretical_maximum_yield": a.get("theoretical_maximum_yield"),
        "value_captured": a.get("value_captured"),
        "tracking_fidelity": a.get("tracking_fidelity"),
        "tracking_cost": a.get("tracking_cost"),
        "latest_date": a.get("latest_date"),
        "generated_at": a.get("generated_at"),
        "throughput_ach_kwh": round(thr_ach, 1),
        "throughput_star_kwh": round(thr_star, 1),
        "degradation_rate": deg,
        "degradation_ach": round(deg * thr_ach, 2),
        "degradation_star": round(deg * thr_star, 2),
        "j_ach_repriced": round(a["j_ach"] + deg * thr_ach, 2),
        "j_star_repriced_upper_bound": round(j_star_eval + deg * thr_star, 2),
        "soc_discrepancy_max": round(max(disc), 1) if disc else None,
        "soc_discrepancy_mean": round(sum(disc) / len(disc), 1) if disc else None,
        # nimbus #1428-adjacent, and the reason #1422's own scorecard was wrong:
        # the previous day is scored PROVISIONALLY until its P2P settlement lands.
        # Measured on the reference household 2026-09-25 by 10-minute polling,
        # settlement arrives between 02:47 and 06:00 AEST and
        # repair_provisional_quality_history() rescores on the 06:00 cycle -- so a
        # report generated at ~06:05 races that rescore and can publish a figure
        # that is about to change by a lot. On 2026-09-28 the same day read
        # EPR 25.32% before and 73.0% after, with j_ach changing SIGN
        # (+$1.09 -> -$3.87).
        #
        # `matches_sensor` above structurally cannot catch it: cqr.json and the
        # sensor are computed from the same pre-settlement inputs, so they agree
        # with each other while both are provisional. It detects a stale SENSOR
        # READ, never a stale SETTLEMENT.
        "real_p2p_settlement_status": a.get("real_p2p_settlement_status"),
        "scorecard_is_final": _settlement_is_final(a.get("real_p2p_settlement_status")),
        # nimbus #1423: says which of the two this script actually computed.
        # "instant" means the `last` statistic was present and the #681 fix ran;
        # "hourly_mean_estimate" means it did not, the figures above are
        # smoothed, and the sensor's own soc_discrepancy_*_pct is authoritative.
        "soc_discrepancy_basis": "instant"
        if soc_boundary_is_instant
        else "hourly_mean_estimate",
        "soc_discrepancy_authoritative": {
            "max_pct": a.get("soc_discrepancy_max_pct"),
            "mean_pct": a.get("soc_discrepancy_mean_pct"),
            "source": "sensor.nimbus_solver_quality_report",
        },
        "real_close": rows[-1][3],
        "scored_close": rows[-1][4],
        "oracle_close": rows[-1][5],
    }
    # Found 2026-09-25, same area as the #1081-adjacent UTC/local fix above (2026-09-24):
    # `reg` (hourly_regret) is UTC-keyed (see this file's own docstring, "Key labels"), so
    # sorting `reg.items()` directly and reporting `int(h)` displays the *UTC* hour of day
    # while every other hour-labeled figure in this script (`rows[*][0]`) is local. Brisbane
    # is UTC+10 with no DST, so the mislabeling is a full 10-hour shift, not a rounding
    # error -- a genuinely different hour of the day. Confirmed against a real day's data:
    # the raw dict reported "19, 17, 20" as the top regret hours (which read as a plausible
    # evening-price-peak story) while the true local hours, read off `rows`, are 05:00,
    # 03:00, 06:00 -- overnight, not the evening peak. Fixed by deriving top hours from
    # `rows` (already correctly local-labeled) instead of `reg` directly.
    top = sorted(((r[6], r[0]) for r in rows), reverse=True)
    kpis["regret_top_hours"] = [(h, round(v, 2)) for v, h in top[:5]]
    kpis["regret_top4_sum"] = round(sum(v for v, _ in top[:4]), 2)
    # nimbus #1416: the DENOMINATOR `regret_top4_sum` is actually a share of.
    # `kpis["regret"]` is the full total_cost difference and INCLUDES the
    # one-time salvage_value adjustment; these hourly buckets come from
    # hourly_regret_breakdown(), which deliberately excludes it -- stated
    # outright in solver/regret.py's own docstring. So the two have different
    # bases and the hourly sum can exceed the day regret (measured on
    # 2026-09-27: $7.36 of hours against a $7.04 day). Emitting the hourly
    # total lets a consumer quote a share of something the numerator is really
    # part of, instead of "of it" being false.
    kpis["regret_hourly_sum"] = round(sum(v for v, _ in top), 2)
    cross = None
    if args.cqr and os.path.exists(args.cqr):
        c = json.load(open(args.cqr))
        c = c.get("service_response") or c
        cross = {
            k: c.get(k)
            for k in (
                "epr_pct",
                "j_ref",
                "j_ach",
                "j_star",
                "j_star_evaluator",
                "regret_dollars",
                "window_start",
                "window_end",
            )
        }
        cross["matches_sensor"] = all(
            abs(float(c.get(k, 0)) - float(a.get(k, 0))) < 1e-3
            for k in ("j_ref", "j_ach", "j_star", "j_star_evaluator")
        )
    json.dump(
        {"rows": rows, "kpis": kpis, "cross_check": cross, "sensors": sensors},
        open(os.path.join(args.out, "yesterday.json"), "w"),
        separators=(",", ":"),
    )

    print(
        f"{'hr':>5s} {'imp¢':>5s} {'exp¢':>5s} {'load':>5s} {'pv':>5s} | {'real':>5s} {'ach':>5s} {'orc':>5s} | {'achBat':>7s} {'orcBat':>7s} | {'achGrd':>7s} {'mtrGrd':>7s} | {'regret':>7s}"
    )
    for r in rows:
        mg = r[13] if isinstance(r[13], (int, float)) else float("nan")
        print(
            f"{r[0]:>5s} {r[1]:5.1f} {r[2]:5.1f} {r[9]:5.1f} {r[10]:5.1f} | {(r[3] if r[3] is not None else float('nan')):5.1f} {r[4]:5.1f} {r[5]:5.1f} | {r[7]:7.2f} {r[8]:7.2f} | {r[11]:7.2f} {mg:7.2f} | {r[6]:7.3f}"
        )
    _settle = a.get("real_p2p_settlement_status")
    if not _settlement_is_final(_settle):
        print(
            "WARNING (nimbus #1422): this day's P2P settlement is "
            f"{_settle!r}, not 'applied' -- every figure below derived from "
            "j_ach (EPR, regret and its hourly concentration, the degradation "
            "re-pricing) is PROVISIONAL and can move by a lot, including "
            "changing sign. Settlement lands between 02:47 and 06:00 AEST and "
            "repair_provisional_quality_history() rescores on the 06:00 cycle. "
            "Do not publish a scorecard from this run: publish the day-ahead "
            "section only and say the scorecard is withheld pending "
            "settlement. `cross-check: matches_sensor` does NOT cover this -- "
            "both sides are computed from the same provisional inputs."
        )
    if not soc_boundary_is_instant:
        print(
            "NOTE (nimbus #1423): no `last` statistic in the fetched recorder data, so "
            "soc_discrepancy_max/mean above are an HOURLY-MEAN ESTIMATE, not the "
            "instant-sample comparison the sensor makes. The sensor's own figures are "
            f"authoritative: max {a.get('soc_discrepancy_max_pct')}pt, "
            f"mean {a.get('soc_discrepancy_mean_pct')}pt. Every other figure here "
            "(j_ref/j_ach/j_star/regret/EPR) is unaffected."
        )
    print("state:", state, "| kpis:", json.dumps(kpis))
    print("cross-check:", cross)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
