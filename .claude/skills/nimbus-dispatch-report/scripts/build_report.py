#!/usr/bin/env python3
"""Build the report page from the analysis outputs and the hand-written narrative.

  python3 build_report.py --analysis plan_analysis.json [--yesterday yesterday.json] \
      --narrative narrative.json --out report.html [--screenshot report.png]

The template (assets/report_template.html) renders everything from one JSON block, so this
script only merges the inputs. --screenshot renders the page with the pre-installed Chromium
for the single look before publishing.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "assets", "report_template.html")


def _default_yesterday_kpis(k: dict) -> list[list[str]]:
    # nimbus #1081 renamed score_day.py's raw j_star into `j_star_raw_lp_objective`
    # (which still carries soft-SoC/slack/bonus-as-variable search-machinery terms) plus a
    # new `j_star_evaluator` (the oracle's chosen trajectory repriced through the same real-
    # cost arithmetic j_ach uses -- what epr_pct/regret are actually derived from). This
    # function used to read the old `k["j_star"]` key directly, which score_day.py stopped
    # populating -- a KeyError on every call that didn't override kpis in narrative.json.
    # Found running the skill 2026-09-25. Prefer the evaluator figure (consistent with the
    # displayed epr_pct/regret, per the checklist), falling back to the raw objective only
    # if the evaluator key is absent (an older yesterday.json).
    j_star = k.get("j_star_evaluator", k.get("j_star_raw_lp_objective", k.get("j_star", 0.0))) or 0.0
    return [
        ["EPR", "%.1f %%" % (k.get("epr_pct") or 0), "of the $%.2f theoretical yield captured" % (k.get("theoretical_maximum_yield") or 0)],
        ["Idle-battery reference", "$%.2f" % k.get("j_ref", 0.0), "j_ref"],
        ["Achieved (as scored)", "$%.2f" % k.get("j_ach", 0.0), "j_ach"],
        ["Perfect-foresight oracle", ("−$%.2f" % -j_star) if j_star < 0 else "$%.2f" % j_star, "j_star_evaluator · no degradation cost"],
        ["Regret", "$%.2f" % k.get("regret", 0.0), "$%.2f of it in the top 4 hours" % (k.get("regret_top4_sum") or 0)],
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis", required=True)
    ap.add_argument("--yesterday")
    ap.add_argument("--narrative", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--screenshot")
    args = ap.parse_args()

    an = json.load(open(args.analysis))
    narr = json.load(open(args.narrative))
    report = {
        "title": narr.get("title") or "Nimbus day-ahead battery plan",
        "subtitle": narr.get("subtitle") or "",
        "stamp_extra": narr.get("stamp_extra") or "",
        "meta": an["meta"],
        "kpis": narr.get("kpis") or an["kpis"],
        "pts": an["pts"], "table": an["table"],
        "callouts": narr.get("callouts") or [],
        "strengths": narr.get("strengths") or [],
        "weaknesses": narr.get("weaknesses") or [],
        "footnote": narr.get("footnote") or "",
    }
    if args.yesterday and os.path.exists(args.yesterday):
        y = json.load(open(args.yesterday))
        yn = narr.get("yesterday") or {}
        k = y["kpis"]
        report["yesterday"] = {
            "rows": y["rows"], "kpis_raw": k,
            "kpis": yn.get("kpis") or _default_yesterday_kpis(k),
            "title": yn.get("title") or "Yesterday, %s: oracle vs achieved" % (k.get("latest_date") or ""),
            "subtitle": yn.get("subtitle") or "",
            "callouts": yn.get("callouts") or [],
            "lessons": yn.get("lessons") or [],
        }
    html = open(TEMPLATE, encoding="utf-8").read()
    assert html.count("__REPORT__") == 1
    html = html.replace("__REPORT__", json.dumps(report, separators=(",", ":")).replace("</", "<\\/"))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote", args.out, f"({len(html):,} bytes)")

    if args.screenshot:
        chrome = (glob.glob("/opt/pw-browsers/chromium*/chrome-linux*/chrome") or [None])[0]
        if not chrome:
            print("no chromium found; skipping screenshot")
            return 0
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
            tmp.write('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">' + html + "</body></html>")
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,3400",
                        "--virtual-time-budget=4000", f"--screenshot={args.screenshot}", "file://" + tmp.name],
                       check=False, capture_output=True, timeout=120)
        print("screenshot:", args.screenshot, os.path.exists(args.screenshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
