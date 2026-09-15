"""nimbus issue #921: proof that the P2P commitment actually REACHES
`compute_binding_constraint_label()`, driven through real `main()`.

This test exists because of a specific, embarrassing gap. #921's fix was
shipped twice — v0.94.324 and again v0.94.326 — and judged both times
against a live install's published attribute. Every unit test on that
path asserts what `compute_binding_constraint_label()` does *when handed
a pin*. Not one asserted that `publish_plan()` hands it one. So the
whole wiring — `fetch_p2p_fixed_export_kw()` -> `GridConfig.
fixed_export_kw` -> `_fixed_export_now` -> the label — was untested, and
the only thing standing in for it was a devhub reading that turned out
to be published by roughly thirty-releases-old code.

A function-boundary test cannot catch a wiring bug. This one drives the
real `main()` the way `test_main_golden_output_guardrail.py` does and
asserts on the attributes actually pushed.

The frozen instant is deliberate and load-bearing: `10:00:00+00:00` is
**20:00 Brisbane**, which sits inside the 17:00-24:00 commitment this
fixture configures. Move it outside that window and the assertion below
stops testing anything, which is why the window membership is asserted
explicitly rather than assumed.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer
from test_main_golden_output_guardrail import (
    _HEALTHY_LOAD_STATE,
    _LOAD_SENSOR,
    _SOLVER_CONFIG_ATTRS,
)

# A rate the fixture's own plant can actually deliver: 5 kW battery
# discharge against a 1.5 kW load, well inside the 15 kW export limit.
# A pin near the export limit would make "pinned" and "at the ceiling"
# indistinguishable, which is the one thing this test must not do.
_PIN_KW = 3.0

_P2P_CONFIG_ATTRS = {
    **_SOLVER_CONFIG_ATTRS,
    "solver_p2p_block_1_rate_kw": _PIN_KW,
    "solver_p2p_block_1_start_hour": 17,
    "solver_p2p_block_1_end_hour": 24,
    "solver_p2p_block_lead_time_minutes": 0,
}


def _make_ha_get():
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _P2P_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: _HEALTHY_LOAD_STATE,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


def _run_main_and_capture():
    posted = {}

    def _capture(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    with (
        patch.object(solver_writer, "ha_get", side_effect=_make_ha_get()),
        patch.object(solver_writer, "ha_post_state", side_effect=_capture),
        patch.object(solver_writer, "acquire_lock", return_value=True),
        patch.object(solver_writer, "release_lock"),
        patch.object(
            solver_writer,
            "PLAN_STATE_PATH",
            "/tmp/nonexistent_plan_state_p2p_pin_test.json",
        ),
    ):
        solver_writer.main()
    return posted


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestThePinReachesTheBindingLabel:
    def test_the_frozen_instant_is_inside_the_configured_window(self, freezer):
        """Guards the test's own premise. If the frozen time drifts
        outside 17:00-24:00 local, every assertion below would pass
        vacuously for the wrong reason."""
        from datetime import datetime

        from solver_writer import LOCAL_TZ

        local_hour = datetime.now(LOCAL_TZ).hour
        assert 17 <= local_hour < 24, (
            f"frozen instant is {local_hour}:00 local, outside this "
            "fixture's own 17:00-24:00 P2P block -- the pin would be NaN "
            "and this file would stop testing what it claims to"
        )

    def test_the_solve_is_real_and_the_export_is_actually_pinned(self, freezer):
        """The label is only meaningful if the LP genuinely honoured the
        commitment -- otherwise a correct label would be describing
        something that did not happen."""
        posted = _run_main_and_capture()
        assert solver_writer.ENTITY_ID in posted
        _state, attrs = posted[solver_writer.ENTITY_ID]

        assert attrs["status"] == "optimal"
        assert attrs["forecast"][0]["grid_export_kw"] == pytest.approx(
            _PIN_KW, abs=1e-3
        )

    def test_binding_constraint_now_names_the_commitment(self, freezer):
        """#921 itself, end to end. Before the fix this read

            Grid export at 3.00 kW (unexpected -- neither its 0 nor
            15.00 kW bound)

        and no unit test could have caught it, because the unit tests
        never asked whether the pin arrives."""
        posted = _run_main_and_capture()
        _state, attrs = posted[solver_writer.ENTITY_ID]

        assert attrs["binding_constraint_now"] == (
            f"Grid export pinned at {_PIN_KW:.2f} kW by P2P export commitment"
        ), (
            "the P2P commitment did not reach compute_binding_constraint_label() "
            "through publish_plan() -- this is the wiring #921 was about, and "
            f"the published label was: {attrs['binding_constraint_now']!r}"
        )

    def test_it_is_not_merely_the_ceiling_label(self, freezer):
        """A pin equal to the export limit would make this test pass for
        the wrong reason. The fixture keeps them well apart (3 kW vs
        15 kW), and this asserts that separation rather than trusting it."""
        posted = _run_main_and_capture()
        _state, attrs = posted[solver_writer.ENTITY_ID]

        assert attrs["envelope_export_limit_kw"] != _PIN_KW
        assert "Grid export limit" != attrs["binding_constraint_now"]
