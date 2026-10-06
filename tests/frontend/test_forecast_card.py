"""nimbus #1529: the Forecaster charts, auto-detected.

`nimbus-forecast-card.js` builds the reference household's two
apexcharts-card charts ("Nimbus Power Signals", "Nimbus Load Forecasts")
from whatever Nimbus entities the install has, and hands the config to
apexcharts-card. On the reference install those charts were produced by a
Python generator script against its own entity ids.

## How this was verified, and what this file can and cannot check

Run headlessly (Playwright, Chromium) against a fixture of the reference
household's real production state, read-only, 5 Oct 2026, and diffed
series by series against that install's live charts:

* Power Signals: 20 series against the live 20. Every colour, axis, fill
  and dash identical. By design the weather overlay comes from Nimbus's
  own temperature/humidity forecasts rather than a household's weather
  integration.
* Load Forecasts: 40 against the live 38. The two extra are a Load the
  hand-maintained chart had never been given (the EV charger). Every
  circuit colour matched the generator's hash except the five the
  household had hand-picked.

CI has no Node (same posture as every test in this directory), so what is
pinned here is structure that would silently break the card.
"""

from __future__ import annotations

import colorsys
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "custom_components" / "nimbus_load"
CARD_JS = PKG / "frontend" / "nimbus-forecast-card.js"


def _card() -> str:
    return CARD_JS.read_text(encoding="utf-8")


def test_card_is_registered_with_a_matching_tag() -> None:
    frontend_py = (PKG / "frontend.py").read_text(encoding="utf-8")
    assert (
        '_CardAsset("nimbus-forecast-card.js", "nimbus-forecast-card")' in frontend_py
    )
    src = _card()
    assert 'customElements.define("nimbus-forecast-card"' in src
    assert 'type: "nimbus-forecast-card"' in src


def test_renders_through_apexcharts_and_says_so_when_missing() -> None:
    src = _card()
    assert 'document.createElement("apexcharts-card")' in src
    assert 'customElements.get("apexcharts-card")' in src
    assert "Nimbus Forecaster needs ApexCharts Card" in src


def test_every_nimbus_attribute_the_card_reads_is_published() -> None:
    src = _card()
    read = set(re.findall(r"\battributes\.([a-z_]+)", src))
    read -= {"friendly_name", "unit_of_measurement", "forecast"}
    assert read, "found no attribute reads -- the pattern above is stale"
    publishers = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (PKG / "const.py", PKG / "sensor.py", PKG / "solver_publish.py")
    )
    missing = sorted(a for a in read if f'"{a}"' not in publishers)
    assert not missing, f"card reads attributes nothing publishes: {missing}"


def test_discovers_rather_than_lists() -> None:
    src = _card()
    for needle in (
        'id.startsWith("sensor.nimbus_")',
        'a.subentry_type === "load"',
        'a.subentry_type === "power_signal"',
        "solver_load_forecast_sensor",
        'st["sensor.nimbus_solver_battery_forecast"]',
        "s.attributes.source_sensor",
    ):
        assert needle in src, needle
    # No household entity id may appear in the card.
    assert not re.search(r"sensor\.(cb_|logger_|archerfield|pirateweather)", src)


def test_w_sources_are_scaled_to_kw() -> None:
    assert (
        'if (unit === "w" && fu === "kw") series.transform = "return x / 1000;";'
        in _card()
    )


def _py_hash_color(entity_id: str) -> str:
    """The generator script's own _hash_color_for(), verbatim in logic."""
    h = 0
    for ch in entity_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.55, 0.55)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


def test_python_reference_hash_matches_live_colours() -> None:
    """The live chart's circuit colours, as rendered on the reference
    install. The card's JS port was diffed against these by headless run;
    this pins the reference algorithm the port must equal."""
    live = {
        "sensor.nimbus_cb_lt_l1_power_forecast": "#CB4D9A",
        "sensor.nimbus_cb_pw_ac_b1_power_forecast": "#4DCB79",
        "sensor.nimbus_cb_pw_comms_power_forecast": "#514DCB",
        "sensor.nimbus_cb_pw_oven_power_forecast": "#92CB4D",
    }
    for eid, colour in live.items():
        assert _py_hash_color(eid) == colour, eid


def test_js_hash_port_has_the_same_constants() -> None:
    src = _card()
    body = src[src.index("function nimbusFcHashColor") :]
    body = body[: body.index("\n}\n")]
    assert "Math.imul(h, 31) + entityId.charCodeAt(i)) >>> 0" in body
    assert "(h % 360) / 360.0" in body
    assert "const l = 0.55;" in body and "const s = 0.55;" in body
    assert "Math.floor(x * 255)" in body


def test_soc_series_is_labelled_percent_not_the_plan_sensors_kw() -> None:
    """The Solver plan sensor's unit is kW, so without an explicit unit the
    SoC series inherited it and its legend read "70.6 kW"."""
    src = _card()
    soc = src[src.index('name: "Solver SoC % (proposed)"') :]
    soc = soc[: soc.index("}")]
    assert 'unit: "%"' in soc


def test_sizes_itself_full_width_in_a_sections_view() -> None:
    """Without getGridOptions() a sections view gives the card a small
    default tile and the wide charts are squeezed into a narrow column
    (seen live on the reference household, 6 Oct 2026)."""
    src = _card()
    body = src[src.index("getGridOptions()") :]
    body = body[: body.index("}") + 1]
    assert 'columns: "full"' in body
    assert 'rows: "auto"' in body
    assert ":host{display:block;width:100%}" in src
