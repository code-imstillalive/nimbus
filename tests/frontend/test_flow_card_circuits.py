"""The Power Flow card shows every powered circuit, under the household's name
for it (5 Nov 2026).

A tester's card showed two circuit bubbles named after the raw sensors
("Smappee MQTT Circuit 1 Realtime W") while their Topology card showed the
same circuits as "Circuit - Power 1" and "Circuit - Power 2", and a third,
"Circuit - Lights 1" at 23 W, was missing. Two causes:

- The card listed circuits from the Nimbus forecast sensors first, named from
  the source sensor, and only then the household's own loads, which it
  skipped as duplicates. The loads now come first, so their names win.
- The scan then named a circuit after its source sensor, not after the
  forecast signal the household named; the name now comes from the signal.
- A circuit showed only above 50 W. The household: whatever is powered should
  show, as on the Topology card, which counts anything above 5 W as active.
"""

from __future__ import annotations

import pathlib
import re

FLOW = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-flow-card.js"
)


def test_powered_circuits_show_under_the_households_names():
    src = FLOW.read_text(encoding="utf-8")
    cutoff = float(re.search(r"const FLOW_CIRCUIT_MIN_KW = ([0-9.]+);", src).group(1))
    assert cutoff <= 0.005, f"a {cutoff * 1000:.0f} W cutoff hides a lit circuit"

    body = src[src.index("_circuits(site, drawn) {") :]
    body = body[: body.index("\n  }\n")]
    loads = body.index("for (const l of site.loads)")
    scan = body.index("Object.entries(this._hass.states)")
    assert loads < scan, "a sensor's own name wins over the household's load name"

    # A tester's circuits are forecast power signals, not controllable loads,
    # so they came through the scan under the raw sensor's name ("Smappee MQTT
    # Circuit 1 Realtime W") rather than theirs ("Circuit - Lights 1"). The
    # name comes from the Nimbus forecast entity, which carries the signal's
    # own title.
    assert "String(a.friendly_name || this._attrs(src).friendly_name || src)" in body
    assert r'.replace(/\s*forecast$/i, "")' in body

    # #2299 (Mark Purcell, Cottonwood): a second whole-house meter
    # ("Sigen Plant Total Load") was drawn as a circuit equal to House. Every
    # whole-house meter the household named is excluded, not only the load
    # forecast's own source.
    site = src[src.index("_site() {") :]
    site = site[: site.index("\n  }\n")]
    assert "cfg.solver_whole_house_cross_check_sensor" in site
    assert "cfg.solver_load_forecast_entities" in site
    assert "...site.houseAlso" in src[src.index("const drawn = new Set(") :][:200]

    # #2293: a sub-device ("part of" another circuit) is listed under its
    # circuit with the rest as "other" on Power Flow, and indented under its
    # circuit's tile on Topology, not drawn again beside it.
    assert "a.part_of_source_sensor" in body and "c.other = c.kw -" in body
    topo = (FLOW.parent / "nimbus-topology-card.js").read_text(encoding="utf-8")
    assert "attrs.part_of_source_sensor" in topo and "load.sub" in topo

    # test_the_card_says_when_there_is_no_plan (7 Nov 2026): for about a
    # minute after Nimbus reloads, the plan sensor has no forecast. A tester's
    # card then showed no Buy/Sell price and kept the last plan's "On plan" and
    # "Solar xx% of forecast": `.pill` sets `display`, which overrode their
    # `hidden`. The card hides what it hides, and says it is waiting.
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", src)
    render = src[src.index("  _render() {") :]
    render = render[: render.index("\n  }\n")]
    assert '"Waiting for the plan"' in render
    assert "planPill.hidden = !m.p0" not in render
