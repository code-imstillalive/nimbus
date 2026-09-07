#!/usr/bin/env python3
"""Static SVG diagrams for the deferrable/sheddable-load spec issue.

Plain-text SVG, same palette/typography as the dispatch-report skill's
make_svgs.py so the two read as one system. Writes into
docs/specs/deferrable-loads/ for raw.githubusercontent.com embedding.
"""
import os
import sys

W = 900
CSS = """
<style>
text{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;fill:#1b1f24}
.h{font-size:15px;font-weight:700}
.ttl{font-size:13px;font-weight:600}
.lbl{font-size:11px}
.sm{font-size:10px;fill:#57606a}
.tick{font-size:10px;fill:#57606a}
.gl{stroke:#e8ebef;stroke-width:1}
.box{fill:#f6f8fa;stroke:#8c959f;stroke-width:1;rx:6}
.box2{fill:#ffffff;stroke:#8c959f;stroke-width:1;rx:6}
.arr{stroke:#57606a;stroke-width:1.4;fill:none;marker-end:url(#m)}
.arr2{stroke:#d1226b;stroke-width:1.4;fill:none;marker-end:url(#m2);stroke-dasharray:4 3}
</style>
<defs>
<marker id="m" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#57606a"/></marker>
<marker id="m2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#d1226b"/></marker>
</defs>
"""

BLUE, ORANGE, GREEN, PINK, GREY, GOLD = "#2f6fdb", "#e8890c", "#2a9d5c", "#d1226b", "#8c959f", "#c9a400"


def wrap(body, h, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}">{CSS}'
            f'<rect x="0" y="0" width="{W}" height="{h}" fill="#ffffff"/>'
            f'<text x="16" y="22" class="h">{title}</text>{body}</svg>')


def box(x, y, w, h, title, lines, cls="box", tcol="#1b1f24"):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" class="{cls}"/>',
           f'<text x="{x+10}" y="{y+18}" class="ttl" fill="{tcol}">{title}</text>']
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x+10}" y="{y+34+i*13}" class="sm">{ln}</text>')
    return "\n".join(out)


def taxonomy(path):
    b = []
    # Load classes row
    cols = [
        ("Fixed load", ["forecast_kw (must serve)", "already: LoadConfig", "no decisions"], GREY),
        ("Sheddable", ["forecast_kw, shed_cost $/kWh", "min_fraction", "already: SheddableLoadConfig"], ORANGE),
        ("Deferrable / quota", ["target_kwh by deadline", "window, min_run, max_gap", "quota + rollover (pool pump)",
                                "already: AdequacyLoadConfig", "(hard today → soft shortfall)"], BLUE),
        ("Thermal-state", ["temp state + loss/gain model", "comfort windows (2pm swim)", "pre-cool on hot days",
                           "done-sensor early release", "new"], GREEN),
        ("Price-gated", ["value_per_kwh (fixed or entity)", "runs iff switchboard λ ≤ value", "miner: hashprice → $/kWh",
                         "new"], PINK),
    ]
    x = 16
    for t, lines, col in cols:
        b.append(box(x, 44, 166, 100, t, lines, tcol=col))
        b.append(f'<path d="M{x+83} 144 L{x+83} 178" class="arr"/>')
        x += 174
    # LP row
    b.append(box(16, 180, 868, 84, "Solver LP (network.py build_plan) — one power balance per period",
                 ["solar_used + Σ discharge + grid_import  =  load_fixed + Σ served_sheddable + Σ deferrable_kw + Σ thermal_kw + Σ gated_kw + Σ charge + grid_export",
                  "each load class adds its own variables + constraints; opt-in binaries (min-run / contiguity) via lp.py's existing MIP path, off by default",
                  "dual of power_balance_t  =  λ(t), the real switchboard marginal price  (already published: energy_shadow_price_now)"]))
    # Outputs row
    outs = [
        ("Per-device schedule", ["kW per period per load", "→ automation / switch"], BLUE),
        ("Shadow costing", ["cost(load) = Σ λ(t)·kW(t)·dt", "what THIS device cost, not", "the grid tariff"], PINK),
        ("Monitoring", ["actual kW vs scheduled kW", "tracking fidelity per device", "done / temp sensor feedback"], GREEN),
        ("Household mode", ["home / away / guests / economy", "swaps windows, quotas,", "comfort bands, values"], GOLD),
    ]
    x = 16
    for t, lines, col in outs:
        b.append(f'<path d="M{x+100} 264 L{x+100} 292" class="arr"/>')
        b.append(box(x, 294, 205, 76, t, lines, cls="box2", tcol=col))
        x += 221
    # feedback arrow: monitoring -> LP (re-solve)
    b.append(f'<path d="M 560 370 C 560 398, 300 398, 300 372" class="arr2"/>')
    b.append('<text x="430" y="414" text-anchor="middle" class="sm" fill="#d1226b">feedback: early completion / measured state → next solve (5-min cadence)</text>')
    open(path, "w").write(wrap("\n".join(b), 426, "Controllable loads in Nimbus — classes, the LP, and what comes back out"))


def hws(path):
    # 24h axis, hourly
    ML, MR = 60, 20
    hours = list(range(24))
    price = [17, 16, 15, 15, 16, 18, 19, 12, 9, 9, 8, 4, 3, 3, 4, 6, 25, 34, 34, 34, 34, 24, 19, 18]
    sched = [0]*24; sched[11] = 3.6; sched[12] = 3.6; sched[13] = 3.6      # 3h at 3.6kW, 11-14
    actual = [0]*24; actual[11] = 3.6; actual[12] = 3.6; actual[13] = 0.0  # setpoint hit at 13:00
    temp = [52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 50, 60, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51]

    def xat(i):
        return ML + (W - ML - MR) * i / 23

    def panel_lines(y0, h, vmin, vmax, unit, ticks):
        out = []
        for v in ticks:
            yy = y0 + h * (1 - (v - vmin) / (vmax - vmin))
            out.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="gl"/>')
            out.append(f'<text x="{ML-6}" y="{yy+4:.1f}" text-anchor="end" class="tick">{v}{unit}</text>')
        return out, (lambda v: y0 + h * (1 - (v - vmin) / (vmax - vmin)))

    b = []
    # Panel 1: price
    b.append(f'<text x="{ML}" y="44" class="ttl">Import price (¢/kWh) — the cheap window is 11:00–15:00</text>')
    g, y = panel_lines(52, 90, 0, 40, "¢", [0, 20, 40]); b += g
    d = "M " + " L ".join(f"{xat(i):.1f} {y(v):.1f}" for i, v in enumerate(price))
    b.append(f'<path d="{d}" fill="none" stroke="{ORANGE}" stroke-width="2"/>')
    # Panel 2: HWS power scheduled vs actual
    b.append(f'<text x="{ML}" y="172" class="ttl">Hot water element (kW): scheduled 3 h, needed 2 h</text>')
    g, y2 = panel_lines(180, 80, 0, 4, "", [0, 2, 4]); b += g
    bw = (W - ML - MR) / 23 * 0.7
    for i in hours:
        if sched[i] > 0:
            b.append(f'<rect x="{xat(i)-bw/2:.1f}" y="{y2(sched[i]):.1f}" width="{bw:.1f}" height="{y2(0)-y2(sched[i]):.1f}" fill="{BLUE}" opacity="0.25" stroke="{BLUE}" stroke-dasharray="3 2"/>')
        if actual[i] > 0:
            b.append(f'<rect x="{xat(i)-bw/2:.1f}" y="{y2(actual[i]):.1f}" width="{bw:.1f}" height="{y2(0)-y2(actual[i]):.1f}" fill="{BLUE}"/>')
    b.append(f'<rect x="{ML}" y="{178}" width="10" height="10" fill="{BLUE}"/><text x="{ML+14}" y="187" class="tick">actual</text>')
    b.append(f'<rect x="{ML+64}" y="{178}" width="10" height="10" fill="{BLUE}" opacity="0.25" stroke="{BLUE}"/><text x="{ML+78}" y="187" class="tick">scheduled</text>')
    # callout on released hour
    b.append(f'<path d="M {xat(14)+2:.1f} {y2(2.4):.1f} L {xat(13)+bw/2+3:.1f} {y2(2.4):.1f}" class="arr2"/>')
    b.append(f'<text x="{xat(14)+6:.1f}" y="{y2(3.2):.1f}" class="lbl" fill="{PINK}">13:00 slot released: setpoint hit at 13:00, the done-sensor</text>')
    b.append(f'<text x="{xat(14)+6:.1f}" y="{y2(3.2)+13:.1f}" class="lbl" fill="{PINK}">feeds the next 5-min solve, which drops the third hour</text>')
    b.append(f'<text x="{xat(14)+6:.1f}" y="{y2(3.2)+26:.1f}" class="lbl" fill="{PINK}">(3.6 kWh at 3¢ not bought; the 17:00 deadline is still met)</text>')
    # Panel 3: temperature
    b.append(f'<text x="{ML}" y="292" class="ttl">Tank temperature (°C) with setpoint 60 °C and a "ready by 17:00" deadline</text>')
    g, y3 = panel_lines(300, 90, 35, 65, "°", [40, 50, 60]); b += g
    d = "M " + " L ".join(f"{xat(i):.1f} {y3(v):.1f}" for i, v in enumerate(temp))
    b.append(f'<path d="{d}" fill="none" stroke="{GREEN}" stroke-width="2"/>')
    yy = y3(60); b.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{PINK}" stroke-width="1" stroke-dasharray="4 3"/>')
    b.append(f'<text x="{W-MR-4}" y="{yy-4:.1f}" text-anchor="end" class="sm" fill="{PINK}">setpoint</text>')
    xx = xat(17); b.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="300" y2="390" stroke="{GREY}" stroke-dasharray="4 3"/>')
    b.append(f'<text x="{xx+4:.1f}" y="312" class="sm">deadline 17:00</text>')
    for i in range(0, 24, 3):
        b.append(f'<text x="{xat(i):.1f}" y="406" text-anchor="middle" class="tick">{i:02d}:00</text>')
    open(path, "w").write(wrap("\n".join(b), 418, "Use case: hot water scheduled for 3 h, reaches temperature after 2 h"))


def shadow(path):
    ML, MR = 60, 20
    grid = [18, 17, 16, 16, 17, 19, 21, 14, 10, 9, 8, 6, 5, 5, 6, 8, 28, 36, 36, 35, 33, 26, 21, 19]
    # switchboard marginal price: ~0 when PV surplus is being curtailed / battery full, tracks battery discharge value at night
    lam = [8, 8, 8, 8, 8, 9, 10, 6, 2, 0.5, 0.2, 0.2, 0.2, 0.2, 0.3, 2, 9, 9, 9, 9, 9, 8, 8, 8]
    thr = 7.0  # miner value per kWh, 7 c/kWh (from hashprice entity)

    def xat(i):
        return ML + (W - ML - MR) * i / 23

    def yat(v, y0=52, h=150, vmin=0, vmax=40):
        return y0 + h * (1 - (v - vmin) / (vmax - vmin))

    b = [f'<text x="{ML}" y="44" class="ttl">Grid import price vs the switchboard shadow price λ(t) (¢/kWh)</text>']
    for v in (0, 10, 20, 30, 40):
        yy = yat(v); b.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="gl"/>')
        b.append(f'<text x="{ML-6}" y="{yy+4:.1f}" text-anchor="end" class="tick">{v}¢</text>')
    # run shading where lam <= thr
    bw = (W - ML - MR) / 23
    for i, v in enumerate(lam):
        if v <= thr:
            b.append(f'<rect x="{xat(i)-bw/2:.1f}" y="52" width="{bw:.1f}" height="150" fill="{PINK}" opacity="0.08"/>')
    d = "M " + " L ".join(f"{xat(i):.1f} {yat(v):.1f}" for i, v in enumerate(grid))
    b.append(f'<path d="{d}" fill="none" stroke="{ORANGE}" stroke-width="2"/>')
    d = "M " + " L ".join(f"{xat(i):.1f} {yat(v):.1f}" for i, v in enumerate(lam))
    b.append(f'<path d="{d}" fill="none" stroke="{BLUE}" stroke-width="2.4"/>')
    yy = yat(thr); b.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{PINK}" stroke-width="1.2" stroke-dasharray="5 3"/>')
    b.append(f'<text x="{ML+6}" y="{yy+13:.1f}" text-anchor="start" class="sm" fill="{PINK}">miner value_per_kwh = 7¢ (from a hashprice entity)</text>')
    # legend
    b.append(f'<rect x="{ML}" y="58" width="10" height="10" fill="{ORANGE}"/><text x="{ML+14}" y="67" class="tick">grid import price</text>')
    b.append(f'<rect x="{ML+120}" y="58" width="10" height="10" fill="{BLUE}"/><text x="{ML+134}" y="67" class="tick">switchboard λ(t) = dual of power_balance_t</text>')
    # annotations
    b.append(f'<text x="{xat(11):.1f}" y="{yat(30):.1f}" text-anchor="middle" class="lbl" fill="{BLUE}">λ≈0: PV surplus would be curtailed — free at the switchboard</text>')
    b.append(f'<text x="{xat(11):.1f}" y="{yat(30)+13:.1f}" text-anchor="middle" class="lbl" fill="{BLUE}">even though the grid says 5–8¢</text>')
    b.append(f'<path d="M {xat(11):.1f} {yat(30)+18:.1f} L {xat(11):.1f} {yat(0.2)-6:.1f}" class="arr"/>')
    b.append(f'<text x="{W-MR-4:.1f}" y="{yat(9)-22:.1f}" text-anchor="end" class="lbl" fill="{BLUE}">λ≈9¢ overnight: battery discharge value, not the 36¢ grid price</text>')
    # miner bars
    b.append(f'<text x="{ML}" y="226" class="ttl">Miner runs exactly where λ(t) ≤ value_per_kwh — an LP optimality condition, not a threshold rule</text>')
    for v in (0, 2):
        yy = 234 + 50 * (1 - v / 2); b.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="gl"/>')
        b.append(f'<text x="{ML-6}" y="{yy+4:.1f}" text-anchor="end" class="tick">{v} kW</text>')
    for i, v in enumerate(lam):
        if v <= thr:
            b.append(f'<rect x="{xat(i)-bw*0.35:.1f}" y="{234:.1f}" width="{bw*0.7:.1f}" height="50" fill="{PINK}"/>')
    for i in range(0, 24, 3):
        b.append(f'<text x="{xat(i):.1f}" y="302" text-anchor="middle" class="tick">{i:02d}:00</text>')
    b.append(f'<text x="{ML}" y="324" class="sm">A grid-price rule (run when import &lt; 7¢) would run 11:00–15:00 only. The shadow-price rule also runs 07:00–10:00, when PV would otherwise be curtailed —</text>')
    b.append(f'<text x="{ML}" y="337" class="sm">and it would run at night if a full battery had nothing better to do with its energy. Same knob, no rule to maintain: the LP decides from λ(t).</text>')
    open(path, "w").write(wrap("\n".join(b), 350, "Use case: run only when it is actually cheap at the switchboard (miner, pool pump, any price-gated load)"))


def rollover(path):
    ML, MR = 60, 20
    days = ["Mon", "Tue", "Wed", "Thu"]
    quota = 4.0
    delivered = [4.0, 2.5, 5.5, 4.0]   # Tue short by 1.5 (price spike / cap), Wed = 4 + 1.5 carried
    target = [4.0, 4.0, 5.5, 4.0]
    b = [f'<text x="{ML}" y="44" class="ttl">Pool pump: 4 h/day quota, missed hours roll into the next day (cap: +1 day, max 8 h/day)</text>']
    x0, colw = ML + 40, 190

    def yat(v):
        return 60 + 150 * (1 - v / 8)
    for v in (0, 2, 4, 6, 8):
        yy = yat(v); b.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="gl"/>')
        b.append(f'<text x="{ML-6}" y="{yy+4:.1f}" text-anchor="end" class="tick">{v} h</text>')
    for i, d in enumerate(days):
        x = x0 + i * colw
        b.append(f'<rect x="{x}" y="{yat(target[i]):.1f}" width="60" height="{yat(0)-yat(target[i]):.1f}" fill="{BLUE}" opacity="0.2" stroke="{BLUE}" stroke-dasharray="3 2"/>')
        b.append(f'<rect x="{x+70}" y="{yat(delivered[i]):.1f}" width="60" height="{yat(0)-yat(delivered[i]):.1f}" fill="{BLUE}"/>')
        b.append(f'<text x="{x+65}" y="232" text-anchor="middle" class="tick">{d}</text>')
        b.append(f'<text x="{x+30}" y="{yat(target[i])-4:.1f}" text-anchor="middle" class="sm">target {target[i]:g} h</text>')
        b.append(f'<text x="{x+100}" y="{yat(delivered[i])-4:.1f}" text-anchor="middle" class="sm">ran {delivered[i]:g} h</text>')
    b.append(f'<path d="M {x0+1*colw+100} {yat(2.5)-16:.1f} C {x0+1*colw+130} {yat(7.2):.1f}, {x0+2*colw+40} {yat(7.6):.1f}, {x0+2*colw+100} {yat(5.5)-16:.1f}" class="arr2"/>')
    b.append(f'<text x="{x0+1*colw+90}" y="{yat(7.6):.1f}" class="lbl" fill="{PINK}">1.5 h shortfall (evening price spike) carried into Wednesday</text>')
    b.append(f'<text x="{ML}" y="256" class="sm">quota(d) = base_hours + carry(d−1);   carry(d) = min(max(0, quota(d) − delivered(d)), carry_cap)</text>')
    b.append(f'<text x="{ML}" y="270" class="sm">Shortfall is priced, not forced: the LP can choose to fall short on a genuinely bad day and make it up on the next.</text>')
    open(path, "w").write(wrap("\n".join(b), 284, "Use case: fixed daily run-hours with rollover"))


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    taxonomy(f"{out}/load-taxonomy.svg")
    hws(f"{out}/hws-early-completion.svg")
    shadow(f"{out}/shadow-price-gating.svg")
    rollover(f"{out}/pool-pump-rollover.svg")
    for f in os.listdir(out):
        print(f, os.path.getsize(f"{out}/{f}"), "bytes")
