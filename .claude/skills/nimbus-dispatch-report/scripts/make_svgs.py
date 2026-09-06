#!/usr/bin/env python3
"""Static SVG chart generator for the day-ahead + yesterday report.

Standalone, no libraries -- plain text SVG markup, safe to commit and
render inline in a GitHub issue via a raw.githubusercontent.com link.
"""
import json
import sys

W, H = 900, 260
ML, MR, MT, MB = 50, 20, 24, 46


def scale(vals, pad_frac=0.08):
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * pad_frac
    return lo - pad, hi + pad


def panel(title, unit, series, xs, xlabels, y0, y1, height=H, colors=None, area=None, hlines=None):
    """series: list of (name, values, style) ; style in {'line','bar'}"""
    colors = colors or {}
    n = len(xs)

    def xat(i):
        return ML + (W - ML - MR) * i / max(1, n - 1)

    def yat(v):
        return MT + (height - MT - MB) * (1 - (v - y0) / (y1 - y0))

    out = [f'<g transform="translate(0,0)">']
    out.append(f'<text x="{ML}" y="14" class="ttl">{title}</text>')
    # gridlines: 4 horizontal ticks
    for k in range(5):
        v = y0 + (y1 - y0) * k / 4
        yy = yat(v)
        out.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="gl"/>')
        out.append(f'<text x="{ML-6}" y="{yy+4:.1f}" text-anchor="end" class="tick">{v:.0f}{unit}</text>')
    if hlines:
        for v, label, dash in hlines:
            yy = yat(v)
            out.append(f'<line x1="{ML}" x2="{W-MR}" y1="{yy:.1f}" y2="{yy:.1f}" class="hl" stroke-dasharray="{dash}"/>')
    if area:
        name, vals, fill = area
        d = f"M {xat(0):.1f} {yat(0 if y0<=0<=y1 else y0):.1f} "
        d += " ".join(f"L {xat(i):.1f} {yat(v):.1f}" for i, v in enumerate(vals))
        d += f" L {xat(n-1):.1f} {yat(0 if y0<=0<=y1 else y0):.1f} Z"
        out.append(f'<path d="{d}" fill="{fill}" opacity="0.28"/>')
    for name, vals, style in series:
        col = colors.get(name, "#888")
        if style == "line":
            d = "M " + " L ".join(f"{xat(i):.1f} {yat(v):.1f}" for i, v in enumerate(vals))
            out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round"/>')
        elif style == "bar":
            bw = max(2.0, (W - ML - MR) / n * 0.6)
            zero_y = yat(0 if y0 <= 0 <= y1 else y0)
            for i, v in enumerate(vals):
                yy = yat(v)
                top = min(yy, zero_y)
                hgt = abs(zero_y - yy)
                bar_col = colors.get(name + ("_pos" if v >= 0 else "_neg"), col)
                out.append(f'<rect x="{xat(i)-bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(hgt,0.5):.1f}" fill="{bar_col}"/>')
    for i, lbl in xlabels:
        out.append(f'<text x="{xat(i):.1f}" y="{height-MB+14}" text-anchor="middle" class="tick">{lbl}</text>')
    # legend (bar series with pos/neg colors show the "pos" swatch)
    lx = ML
    for name, _, style in series:
        col = colors.get(name, colors.get(name + "_pos", "#888"))
        out.append(f'<rect x="{lx}" y="{height-16}" width="10" height="10" fill="{col}"/>')
        out.append(f'<text x="{lx+14}" y="{height-7}" class="tick">{name}</text>')
        lx += 14 + len(name) * 6.2 + 14
    out.append("</g>")
    return "\n".join(out), height


CSS = """
<style>
text{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;fill:#1b1f24}
.ttl{font-size:13px;font-weight:600}
.tick{font-size:10px;fill:#57606a}
.gl{stroke:#e8ebef;stroke-width:1}
.hl{stroke:#d1226b;stroke-width:1;opacity:.7}
</style>
"""


def svg_wrap(panels_svgs, total_h, title):
    body = f'<text x="{ML}" y="16" style="font-size:15px;font-weight:700">{title}</text>\n'
    y = 26
    for svg, h in panels_svgs:
        body += f'<g transform="translate(0,{y})">{svg}</g>\n'
        y += h + 14
    bg = f'<rect x="0" y="0" width="{W}" height="{y}" fill="#ffffff"/>\n'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {y}" width="{W}" height="{y}">{CSS}{bg}{body}</svg>'


def day_ahead(plan_path, out_path):
    d = json.load(open(plan_path))
    # Hourly "table" rows, not 5-min "pts" -- same visual result at 900px
    # wide, a fraction of the file size (24 points vs 288).
    pts = d["table"]
    xs = list(range(len(pts)))
    labels = [p[0] for p in pts]
    n = len(pts)
    xlabels = [(i, labels[i]) for i in range(0, n, max(1, n // 8))]

    imp = [p[1] for p in pts]
    exp = [p[2] for p in pts]
    lo, hi = scale(imp + exp)
    price_panel, ph = panel(
        "Price (c/kWh)", "", [("import", imp, "line"), ("export", exp, "line")], xs, xlabels, lo, hi,
        colors={"import": "#c0392b", "export": "#1a7f37"},
    )

    load = [p[3] for p in pts]
    solar = [p[4] for p in pts]
    batt = [p[7] for p in pts]
    gimp = [p[5] for p in pts]
    lo, hi = scale(load + solar + batt + gimp)
    power_panel, ph2 = panel(
        "Power (kW, +discharge/import)", "", [
            ("solar", solar, "line"), ("load", load, "line"),
            ("grid_import", gimp, "line"), ("battery", batt, "bar"),
        ], xs, xlabels, lo, hi,
        colors={"solar": "#d19a00", "load": "#57606a", "grid_import": "#8250df",
                "battery_pos": "#1a7f37", "battery_neg": "#c0392b"},
    )

    soc = [p[8] for p in pts]
    soc_panel, ph3 = panel(
        "SoC (%)", "", [("soc", soc, "line")], xs, xlabels, 0, 100,
        colors={"soc": "#0969da"}, area=("soc", soc, "#0969da"),
        hlines=[(5, "floor", "4,3")],
    )

    svg = svg_wrap([(price_panel, ph), (power_panel, ph2), (soc_panel, ph3)], 0, "Nimbus day-ahead plan (next 24h)")
    open(out_path, "w").write(svg)
    print("wrote", out_path, len(svg), "bytes")


def yesterday_chart(y_path, out_path):
    d = json.load(open(y_path))
    rows = d["rows"]
    xs = list(range(len(rows)))
    labels = [r[0] for r in rows]
    xlabels = [(i, labels[i]) for i in range(0, len(rows), 3)]

    real = [r[3] if r[3] is not None else 0 for r in rows]
    ach = [r[4] for r in rows]
    star = [r[5] for r in rows]
    soc_panel, h1 = panel(
        "SoC: real vs scored (achieved) vs oracle (%)", "", [
            ("real", real, "line"), ("achieved", ach, "line"), ("oracle", star, "line"),
        ], xs, xlabels, 0, 100,
        colors={"real": "#1b1f24", "achieved": "#0969da", "oracle": "#8250df"},
    )

    reg = [r[6] for r in rows]
    lo, hi = scale(reg)
    reg_panel, h2 = panel(
        "Hourly regret ($, achieved-oracle; negative = oracle spent to earn later)", "", [
            ("regret", reg, "bar"),
        ], xs, xlabels, lo, hi,
        colors={"regret_pos": "#c0392b", "regret_neg": "#1a7f37"},
        hlines=[(0, "zero", "2,2")],
    )

    svg = svg_wrap([(soc_panel, h1), (reg_panel, h2)], 0, "Yesterday's scorecard")
    open(out_path, "w").write(svg)
    print("wrote", out_path, len(svg), "bytes")


if __name__ == "__main__":
    out = sys.argv[1]
    day_ahead(f"{out}/plan_analysis.json", f"{out}/day_ahead_chart.svg")
    yesterday_chart(f"{out}/yesterday.json", f"{out}/yesterday_chart.svg")
