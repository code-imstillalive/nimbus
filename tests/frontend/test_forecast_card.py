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


def test_card_is_registered_with_a_matching_tag():
    # test_card_is_registered_with_a_matching_tag
    # test_card_is_registered_with_a_matching_tag
    frontend_py = (PKG / "frontend.py").read_text(encoding="utf-8")
    assert (
        '_CardAsset("nimbus-forecast-card.js", "nimbus-forecast-card")' in frontend_py
    )
    src = _card()
    assert (
        'nimbusRegisterCard("nimbus-forecast-card", NimbusForecastCard)' in src
    )  # nimbus #1601
    assert 'type: "nimbus-forecast-card"' in src

    # test_renders_through_apexcharts_and_says_so_when_missing
    src = _card()
    assert 'document.createElement("apexcharts-card")' in src
    assert 'customElements.get("apexcharts-card")' in src
    assert "Nimbus Forecaster needs ApexCharts Card" in src

    # test_every_nimbus_attribute_the_card_reads_is_published
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

    # test_discovers_rather_than_lists
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

    # test_w_sources_are_scaled_to_kw
    # nimbus #1570: any power unit through the shared table, not only W
    # behind a kW forecast; test_1570_every_reader_and_card_converts.py checks the table.
    src = _card()
    assert (
        "NimbusForecastCard.powerScaleToKw(unit) / NimbusForecastCard.powerScaleToKw(fu)"
        in src
    )
    assert 'if (k !== 1) series.transform = "return x * " + k + ";";' in src

    # test_python_reference_hash_matches_live_colours: The live chart's circuit colours, as rendered on the reference
    live = {
        "sensor.nimbus_cb_lt_l1_power_forecast": "#CB4D9A",
        "sensor.nimbus_cb_pw_ac_b1_power_forecast": "#4DCB79",
        "sensor.nimbus_cb_pw_comms_power_forecast": "#514DCB",
        "sensor.nimbus_cb_pw_oven_power_forecast": "#92CB4D",
    }
    for eid, colour in live.items():
        assert _py_hash_color(eid) == colour, eid

    # test_js_hash_port_has_the_same_constants
    src = _card()
    body = src[src.index("function nimbusFcHashColor") :]
    body = body[: body.index("\n}\n")]
    assert "Math.imul(h, 31) + entityId.charCodeAt(i)) >>> 0" in body
    assert "(h % 360) / 360.0" in body
    assert "const l = 0.55;" in body and "const s = 0.55;" in body
    assert "Math.floor(x * 255)" in body

    # test_soc_series_is_labelled_percent_not_the_plan_sensors_kw: The Solver plan sensor's unit is kW, so without an explicit unit the
    src = _card()
    soc = src[src.index('name: "Solver SoC % (proposed)"') :]
    soc = soc[: soc.index("}")]
    assert 'unit: "%"' in soc

    # test_sizes_itself_full_width_in_a_sections_view
    # test_sizes_itself_full_width_in_a_sections_view: Without getGridOptions() a sections view gives the card a small
    src = _card()
    body = src[src.index("getGridOptions()") :]
    body = body[: body.index("}") + 1]
    assert 'columns: "full"' in body
    assert 'rows: "auto"' in body
    assert ":host{display:block;width:100%}" in src

    # test_charts_refresh_once_a_minute_not_on_every_state_change: nimbus #1602: with no update_interval, apexcharts-card refreshes on
    src = _card()
    base = src[src.index("_base(title, height) {") :]
    base = base[: base.index("\n  }\n")]
    assert 'update_interval: "1min"' in base
    # Both charts are built from _base(): nothing builds a chart config
    # that could skip it.
    assert src.count('type: "custom:apexcharts-card"') == 1
    assert src.count("this._base(") >= 2

    # test_no_loading_spinner_on_each_refresh: The household still saw the spinner on every one-minute refresh with
    src = _card()
    base = src[src.index("_base(title, height) {") :]
    base = base[: base.index("\n  }\n")]
    assert "show: { loading: false }" in base

    # test_history_lines_are_steps_not_smoothed: Household, 9 Oct 2026: hot water history was drawn as a smooth ramp
    src = _card()
    hist = src[src.index("  _history(s, name, color, yaxis, width) {") :]
    hist = hist[: hist.index("\n  }\n")]
    assert 'curve: "stepline"' in hist
    script = (
        pathlib.Path(__file__).resolve().parents[2]
        / "docs/real-world-integration/files/lovelace_build_merged_forecast_chart.py"
    ).read_text(encoding="utf-8")
    assert script.count('"curve": "stepline"') == script.count('(history)",')


def _py_hash_color(entity_id: str) -> str:
    """The generator script's own _hash_color_for(), verbatim in logic."""
    h = 0
    for ch in entity_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.55, 0.55)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"
