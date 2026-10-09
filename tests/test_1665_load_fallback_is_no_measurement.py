"""nimbus #1665 (Mark Purcell's IV&V, part of #1663): a broken load forecast's
zero-fallback must reach the flex telemetry record as `no_measurement`, not as
an ordinary-looking `house_load_kw: 0.0`.

`solver_inputs/load.py` keeps `load_kw` at 0.0 on a non-transient load
forecast error, by design (#370/#416), and returns the error beside it. A live
whole-house reading, when configured and readable, replaces period 0 and makes
it a real measurement again. No history is read (#1634).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

ERROR = "load forecast sensor sensor.x has no forecast attribute"


def test_a_healthy_forecast_publishes_period_0():
    assert solver_writer._measured_house_load_kw([1.4, 2.0], None, None) == 1.4


def test_the_zero_fallback_is_no_measurement():
    assert solver_writer._measured_house_load_kw([0.0, 0.0], ERROR, None) is None


def test_a_live_whole_house_reading_is_a_measurement_even_under_fallback():
    # load.py writes the live reading into load_kw[0] itself.
    assert solver_writer._measured_house_load_kw([2.3, 0.0], ERROR, 2.3) == 2.3


def test_main_passes_the_record_through_the_helper():
    """The record's call site, not a copy of the rule, decides this."""
    tree = ast.parse(Path(solver_writer.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish_flex_telemetry_record"
    ]
    assert calls, "publish_flex_telemetry_record is no longer called"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        value = kw.get("house_load_kw")
        assert isinstance(value, ast.Call), ast.dump(value) if value else None
        assert getattr(value.func, "id", None) == "_measured_house_load_kw"
