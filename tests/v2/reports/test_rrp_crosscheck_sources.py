"""Which sources the RRP cross-check reads, and how it reads them (#2190, #2393).

`prices.rrp_crosscheck_report` gathers the regional AEMO sensors and the
household's comparison list, reads each once, and hands them to the report.
Main's CI never reached it. A report only: no price, plan or dispatch reads
it. Synthetic states only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from solver_inputs import prices
from solver_inputs import rrp_crosscheck as rc

NOW = datetime(2026, 10, 31, 0, 2, tzinfo=UTC)  # 10:02 Brisbane
STATES = {
    "sensor.rrp_now": {"state": "0.12", "attributes": {}},
    "sensor.rrp_forecast": {
        "state": "0.10",
        "attributes": {},
        "last_updated": "2026-10-31T00:01:00+00:00",
    },
}


@pytest.fixture
def reads(monkeypatch):
    seen = []

    def ha_get(entity_id):
        seen.append(entity_id)
        if entity_id not in STATES:
            raise KeyError(entity_id)
        return STATES[entity_id]

    monkeypatch.setattr(prices._solver_writer(), "ha_get", ha_get)
    monkeypatch.setattr(prices, "_RRP_STALE_WATCH", rc.StaleWatch())
    return seen


def test_each_source_is_read_once_and_an_unreadable_one_is_reported(reads):
    cfg = {
        "solver_regional_spot_current_price_sensor": "sensor.rrp_now",
        "solver_regional_spot_forecast_sensor": "sensor.rrp_forecast",
        "solver_price_comparison_sensors": ["sensor.rrp_now", "sensor.gone"],
    }
    out = prices.rrp_crosscheck_report(cfg, NOW)

    assert reads == ["sensor.rrp_now", "sensor.rrp_forecast", "sensor.gone"]
    assert [(s["source"], s["intervals"], s["reason"]) for s in out["sources"]] == [
        ("sensor.rrp_now", 1, None),
        ("sensor.rrp_forecast", 1, None),
        ("sensor.gone", 0, "no_value"),
    ]
    # No TNI and DLF configured: no site loss factor, never an assumed one.
    assert out["site_loss_factor"] is None
    assert out["sources"][1]["as_of"] == "2026-10-31T00:01:00+00:00"


def test_fewer_than_two_distinct_sources_is_no_report(reads):
    assert prices.rrp_crosscheck_report({}, NOW) is None
    one_twice = {
        "solver_regional_spot_current_price_sensor": "sensor.rrp_now",
        "solver_price_comparison_sensors": ["sensor.rrp_now", ""],
    }
    assert prices.rrp_crosscheck_report(one_twice, NOW) is None
    assert reads == []
