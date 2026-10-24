"""nimbus #1259: the nowcast check measures and never corrects.

Blending the live anchor forward would have made the one measured event
2.8x-6.9x worse, so it was deliberately not built: the solar input path
anchors index 0 and only index 0, and the check's attribute is published
only when the check ran. The measurement itself is tested in
`tests/v2/forecasting/test_solar_measurements.py` (#1837).
"""

from __future__ import annotations

from pathlib import Path

from _writer_source import writer_source

SOLAR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "solar.py"
).read_text(encoding="utf-8")


def test_the_solar_input_path_has_no_nowcast_blend():
    assert "solar_kw[0] = max(0.0, live_solar_kw)" in SOLAR
    for banned in ("solar_kw[1] =", "solar_kw[2] =", "nowcast_weight", "decay_w"):
        assert banned not in SOLAR, (
            f"{banned!r} is in the solar input path: #1259's blend was deliberately "
            "not built. Reopen #1259 with a new measurement first."
        )


def test_the_check_is_published_only_when_it_ran():
    text = writer_source()
    assert '"solar_nowcast_check": (solar_delivery or {})' in text
    assert '"solar_nowcast_check": nowcast,' not in text
