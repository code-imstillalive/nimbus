"""Buy and sell prices for the plan's whole horizon, and where the price
provider's own forecast ends (#1753) (#1837).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import solver_tab
from custom_components.nimbus_load.solver_writer import provider_forecast_until

T0 = datetime(2026, 10, 13, 9, 10, tzinfo=UTC)
GRID = [T0 + timedelta(minutes=30 * i) for i in range(6)]


def test_a_mask_that_is_not_a_sequence_is_none():
    assert provider_forecast_until(None, GRID) is None


def _prices_section():
    sections = solver_tab.template()
    titles = [s["cards"][0].get("header", {}).get("title") for s in sections]
    i = titles.index("Prices ahead")
    return sections, i
