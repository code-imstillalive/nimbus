"""A broken load forecast's zero fallback reaches the flex telemetry
record as `no_measurement`, never as an ordinary-looking 0 kW; a readable
live whole-house reading makes period 0 a real measurement again (#1665)
(#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import solver_writer

ERROR = "load forecast sensor sensor.x has no forecast attribute"


def test_the_zero_fallback_is_no_measurement():
    assert solver_writer._measured_house_load_kw([0.0, 0.0], ERROR, None) is None
