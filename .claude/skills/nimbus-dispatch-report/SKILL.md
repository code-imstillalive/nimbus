---
name: nimbus-dispatch-report
description: Produce a Nimbus battery dispatch report from a live Home Assistant install - pull the nimbus_load diagnostics dump, analyse the day-ahead plan (charge/discharge windows, SoC path, price blend, cost split), score yesterday's oracle-vs-achieved quality report against the recorder, and publish an interactive chart page with numbered callouts plus strengths, weaknesses and lessons. Use this whenever someone asks to check, review, analyse, chart or explain what Nimbus (or its solver / battery forecast / quality report / EPR / regret) is planning or did, wants a "day-ahead analysis", "how did the battery go yesterday", "oracle vs achieved", "download the diagnostics", or a dispatch chart with callouts - even if they do not say "report".
---

# Nimbus dispatch report

One method, three deliverables: a **day-ahead read** of the plan Nimbus just published, a
**yesterday scorecard** (Nimbus's own quality report checked against what the battery really
did), and a **published page** with charts, callouts and a strengths / weaknesses / lessons
write-up. The scripts do the fetching and arithmetic so the session's effort goes into
reading the plan, not parsing it.

## Before you start

- You need a Home Assistant endpoint. Either the `ha-mcp` connector is attached (tools named
  `ha_get_integration`, `ha_get_state`, `ha_get_history`, `ha_call_service`) or the household
  has given you its ha-mcp webhook URL. In the second case `export HA_MCP_URL='<url>'` and use
  `scripts/hamcp.py` - the URL carries a secret, so it goes in the environment only, never in
  a file, commit, issue or artifact.
- `python3` with the stdlib is enough for the scripts. Chromium (`/opt/pw-browsers/...`) is
  optional, for the one screenshot before publishing.
- Read `references/analysis-checklist.md` before writing any narrative. It lists what to look
  for in a plan and the known ways the quality report can mislead you.
- Load the `dataviz` and `artifact-design` skills before touching the page; the template
  already follows them, but callouts and any new panel must too.

## Workflow

Work in a scratch directory (`OUT=/path/to/scratch`).

### 1. Fetch (one command)

```bash
python3 scripts/fetch_data.py --out $OUT [--date YYYY-MM-DD] [--entry-id <id>]
```

Writes `diagnostics.json` (the `nimbus_load` config-entry diagnostics), `quality_report.json`
(`sensor.nimbus_solver_quality_report`), `cqr.json` (the `compute_quality_report` service run
for the same local day, an independent cross-check), and `recorder.json` (hourly recorder
statistics for the SoC, battery-power, grid and price sensors the entry is configured with).
`--date` defaults to yesterday in the HA timezone. If only the connector is available, call the
same four tools yourself and save the JSON under those names; the later scripts only read files.

### 2. Analyse the day-ahead plan

```bash
python3 scripts/analyze_plan.py --diag $OUT/diagnostics.json --out $OUT [--hours 24]
```

Prints the hourly table, charge / discharge / export runs with their price ranges, SoC
extremes and floor time, where the blended price departs from the raw feed, cap-pinned
periods, and the cost split. Writes `plan_analysis.json` (everything the page needs) and a
starter `narrative.json` with empty callout / strengths / weaknesses arrays and the numbers
the script measured, so the write-up starts from facts already on disk.

### 3. Score yesterday

```bash
python3 scripts/score_day.py --quality $OUT/quality_report.json --recorder $OUT/recorder.json \
    --cqr $OUT/cqr.json --diag $OUT/diagnostics.json --out $OUT
```

Prints reference / achieved / oracle by hour with the hourly regret, the regret's top hours,
throughput of each trajectory, the recorder-vs-scored SoC discrepancy, and a degradation
re-pricing estimate (see the checklist for why that matters). Writes `yesterday.json`.

### 4. Write the narrative

Edit `$OUT/narrative.json`. It is the only hand-written input: title, subtitle, KPI overrides,
callouts for both charts, strengths, weaknesses, lessons. Each callout anchors to a panel key
(`price`, `power`, `soc` for the plan; `yprice`, `ysoc`, `yreg` for yesterday), a time label
exactly as it appears in the data (`"13:30"`, `"+02:00"` for tomorrow, `"20:00"` for
yesterday's hourly rows) and a value on that panel's axis; `dx`/`dy` nudge the marker so it
does not sit on a label. Five to seven callouts per chart is the useful range - past that
they stop being read.

Write callouts as a claim plus the number that proves it ("74 kWh bought 11:56-16:00 at
2.7-4.9 ¢"), never a restatement of the axis. Strengths and weaknesses are judgements about
the *plan*, not about Nimbus in general; each bullet names the mechanism and the money or
energy at stake.

### 5. Build, look once, publish

```bash
python3 scripts/build_report.py --analysis $OUT/plan_analysis.json --yesterday $OUT/yesterday.json \
    --narrative $OUT/narrative.json --out $OUT/report.html --screenshot $OUT/report.png
```

Look at the screenshot once, fix any marker sitting on a label, then publish `report.html`
with the Artifact tool (title like "Nimbus Day-Ahead Plan", favicon 🔋). The page is
self-contained: no libraries, both themes, crosshair tooltip, keyboard navigation, and an
hourly table view so every value is reachable without colour.

## Report structure

The page renders, in this order, from the JSON it is given:

1. Header: title, subtitle (what the reader is looking at and when it was solved), a stamp with
   version, solve time and status.
2. KPI row: grid cost for the window, load, import / export, battery throughput with cycles and
   degradation cost, expected cost with its band.
3. Day-ahead chart: three stacked panels on one time axis - prices (import, export, shaded
   fallback region), power flows (solar, grid import, battery diverging around zero, load
   line), state of charge with the floor - then the numbered callout list.
4. Strengths and weaknesses, side by side.
5. Yesterday: KPI row (EPR, reference, achieved, oracle, regret), three panels (prices, real vs
   scored vs oracle SoC, hourly regret bars), callouts, then the lessons list.
6. Footnote naming the sources and the sign / price conventions.

Sections whose data is absent are simply not rendered, so a day-ahead-only report is fine.

## Reading the numbers (short version; the checklist has the full list)

- Sign convention comes from the diagnostics (`battery_kw_sign_convention`); the scripts
  normalise to positive = discharge. The quality report uses the opposite sign; `score_day.py`
  handles it, do not "correct" it again by hand.
- Prices in the plan are the *blended* series. Where `export_price != export_price_raw` the
  fallback source is in play; say so, because decisions in that region rest on a forecast, not
  the retailer's feed.
- `bonus_price` is the incremental P2P premium, not a rate. The absolute P2P rate is
  `export_price + bonus_price`.
- The load forecast can disagree with the whole-house meter; both are in the diagnostics
  (`load_summed_18_now_kw` vs `load_whole_house_cross_check_now_kw`). A large gap is a
  weakness of the plan on its own.
- The quality report's oracle and reference LPs carry no degradation cost, and its "achieved"
  trajectory is integrated from the battery-power sensor rather than read from the SoC sensor.
  Both can make the regret look larger, or land in the wrong hours. Check the recorder before
  attributing regret to a decision.
