"""nimbus #1529: the Forecaster ships a chart of its own.

`nimbus-forecast-card.js` is the fourth card frontend.py registers. It needs
no configuration: it discovers every Nimbus forecast sensor from live state,
draws the measured source's recorder history, the forecast and its range,
and explains which model is in use.

## How this was verified, and what this file can and cannot check

Rendered headlessly (Playwright, Chromium) against a fixture built from the
reference household's real production state, read-only, on 2026-10-05:
26 forecast sensors, 24 h of history for all 26 source sensors, and
`sensor.nimbus_solver_config`. Five views (whole house at desktop and phone
width, a circuit, temperature, the signed battery signal) rendered with no
console errors and no horizontal scroll at 390 px.

CI has no Node (same posture as every test in this directory), so what is
pinned here is structure that would silently break the card:

* the card is registered and its custom-element tag matches the
  registration;
* every attribute the card reads is one Nimbus actually publishes, so a
  rename on the Python side fails here rather than blanking the card;
* the model panel follows ml/model.py's own selection rule, including
  `naive` being a real winner;
* the card opens on the forecast the Solver plans with, and never treats
  the Solver's own plan as a forecast.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "custom_components" / "nimbus_load"
CARD_JS = PKG / "frontend" / "nimbus-forecast-card.js"


def _card() -> str:
    return CARD_JS.read_text(encoding="utf-8")


def test_card_is_registered_with_a_matching_tag() -> None:
    # Read from source: importing frontend.py pulls in homeassistant.* (see
    # that module's own note on its deferred imports). The real-HA harness
    # in tests/hass_integration/test_frontend_cards_registered.py loops
    # over _CARDS and proves every entry is served and registered.
    frontend_py = (PKG / "frontend.py").read_text(encoding="utf-8")
    assert (
        '_CardAsset("nimbus-forecast-card.js", "nimbus-forecast-card")' in frontend_py
    )
    src = _card()
    assert 'customElements.define("nimbus-forecast-card"' in src
    assert 'type: "nimbus-forecast-card"' in src


def test_every_attribute_the_card_reads_is_published() -> None:
    """A rename on the Python side must fail here, not blank the card."""
    src = _card()
    read = set(re.findall(r"\battr(?:s|ibutes)\.([a-z_]+)", src))
    # Home Assistant's own attributes, not Nimbus's.
    read -= {"friendly_name", "unit_of_measurement"}
    assert read, "found no attribute reads -- the pattern above is stale"
    publishers = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (PKG / "const.py", PKG / "sensor.py", PKG / "solver_publish.py")
    )
    missing = sorted(a for a in read if f'"{a}"' not in publishers)
    assert not missing, f"card reads attributes nothing publishes: {missing}"


def test_model_panel_follows_the_forecasters_own_selection_rule() -> None:
    model_py = (PKG / "ml" / "model.py").read_text(encoding="utf-8")
    # The rule the card mirrors. If it changes there, revisit _modelInfo.
    assert "model_type = min(recursive_mae, key=recursive_mae.__getitem__)" in model_py
    assert "model_type = min(candidate_mae, key=candidate_mae.__getitem__)" in model_py
    assert 'model_type = (\n        "knn"' in model_py

    src = _card()
    body = src[src.index("_modelInfo(attrs) {") :]
    body = body[: body.index("\n  }\n")]
    # recursive when every candidate has one, else one-step, else k-NN
    assert "recKeys.length === oneKeys.length" in body
    assert 'basis = "recursive"' in body and 'basis = "one-step"' in body
    assert 'chosen: "knn"' in body
    # naive can win and the card must say so rather than hide it
    assert 'naiveWins: chosen === "naive"' in body
    assert "naiveWins" in src[src.index("_render(entities)") :]


def test_opens_on_the_solvers_load_forecast_and_skips_the_plan() -> None:
    src = _card()
    assert 'states["sensor.nimbus_solver_config"]' in src
    assert "solver_load_forecast_sensor" in src
    assert "if (solverLoad && s.entity_id === solverLoad) return 0;" in src
    # The Solver's published plan is dispatch, not a Forecaster output.
    assert 'if (id.startsWith("sensor.nimbus_solver_")) continue;' in src
    const_py = (PKG / "const.py").read_text(encoding="utf-8")
    assert (
        'CONF_SOLVER_LOAD_FORECAST_SENSOR: Final = "solver_load_forecast_sensor"'
        in const_py
    )


def test_range_is_not_drawn_below_zero_for_a_non_negative_signal() -> None:
    src = _card()
    assert (
        "const nonNeg = histIn.every((p) => p[1] >= 0) && fc.every((p) => p.v >= 0);"
        in src
    )
    assert "p.lo < 0 ? { ...p, lo: 0 } : p" in src


def test_whole_house_rollup_draws_the_sum_of_its_circuits() -> None:
    src = _card()
    assert "Array.isArray(a.source_entities)" in src
    assert "_sumSeries(lists" in src


def test_table_and_chart_draw_the_same_clipped_series() -> None:
    """The hourly table and the chart must never disagree: the clip is
    applied once in _render, before either is built from `fc`."""
    src = _card()
    render = src[
        src.index("_render(entities) {") : src.index("_table(fc, now, unit) {")
    ]
    clip_at = render.index("const nonNeg = histIn.every")
    assert clip_at < render.index("this._table(fc, now, unit)")
    assert clip_at < render.index("this._chart(histIn, fc, t0, t1, now, unit)")
    chart = src[src.index("_chart(hist, fc, t0, t1, now, unit) {") :]
    assert "nonNeg" not in chart[: chart.index("_esc(s) {")]
