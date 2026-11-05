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
