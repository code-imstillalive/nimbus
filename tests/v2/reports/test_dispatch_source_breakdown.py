"""Each period's sources and destinations in merit order: where the energy came
from and went to (#1837).
"""

from __future__ import annotations

import solver_publish


def test_discharging_entirely_to_grid_when_solar_already_covers_load():
    # Solar alone already covers the whole load -- every bit of
    # discharge is a real export, none of it serves local load.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_publish._dispatch_source_breakdown(
            battery_kw=6.0, solar_kw_i=5.0, load_kw_i=3.0
        )
    )
    assert direction == "discharge"
    assert a_label == "Load"
    assert a_pct == 0.0
    assert b_label == "Grid"
    assert b_pct == 100.0
