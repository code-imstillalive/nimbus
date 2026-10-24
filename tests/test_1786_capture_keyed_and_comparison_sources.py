"""nimbus #1786: `solver_price_comparison_sensors` records price sources for
comparison only; the solve never reads it.

The capture behaviour this file once also covered (the array kept per key,
each comparison source kept with the horizons) lives in
`tests/v2/reports/test_decision_inputs.py` (#1837).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_the_solve_never_reads_the_comparison_list():
    """No Solver module names the field. The bridge sensor exposes it like
    every wizard key, so it reaches the solve's config, unread."""
    pkg = REPO / "custom_components" / "nimbus_load"
    readers = sorted(
        p.relative_to(pkg).as_posix()
        for p in pkg.rglob("*.py")
        if re.search(
            r"CONF_SOLVER_PRICE_COMPARISON_SENSORS|solver_price_comparison_sensors",
            p.read_text(encoding="utf-8"),
        )
    )
    assert readers == [
        "const.py",
        "decision_inputs.py",
        "decision_inputs_store.py",
        "flows/hub_options.py",
        "sensor.py",
    ]
    assert not [r for r in readers if r.startswith("solver")]
