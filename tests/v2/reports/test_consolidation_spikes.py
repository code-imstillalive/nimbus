"""The consolidated price keeps a spike that any source forecasts (#1786)
(#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver_inputs import price_consolidation as pc

BASE = 0.30
THRESHOLD = 0.85


def test_no_threshold_is_the_previous_rule():
    series = {"lv": [BASE, 15.0], "pd": [BASE, BASE], "amb": [BASE, BASE]}
    for off in (None, 0.0, -1.0):
        assert pc.consolidate(series, spike_threshold=off) == pc.consolidate(series)
