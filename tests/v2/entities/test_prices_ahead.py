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


def test_a_gap_inside_the_coverage_does_not_end_it_early():
    mask = [True, False, True, True, False, False]
    assert provider_forecast_until(mask, GRID) == GRID[3].isoformat()


def test_a_mask_that_is_not_a_sequence_is_none():
    assert provider_forecast_until(None, GRID) is None


def _prices_section():
    sections = solver_tab.template()
    titles = [s["cards"][0].get("header", {}).get("title") for s in sections]
    i = titles.index("Prices ahead")
    return sections, i


def test_the_chart_sits_full_width_under_the_plan_chart():
    sections, i = _prices_section()
    assert sections[i - 1]["cards"][0]["header"]["title"] == (
        "Battery: plan against measured"
    )
    assert sections[i]["column_span"] == 4


def test_buy_and_sell_each_have_a_solid_provider_and_a_dashed_estimate_line():
    sections, i = _prices_section()
    chart = sections[i]["cards"][0]
    assert chart["graph_span"] == "4d"
    by_name = {s["name"]: s for s in chart["series"]}
    assert set(by_name) == {
        "Buy, forecast",
        "Buy, Nimbus estimate",
        "Sell, forecast",
        "Sell, Nimbus estimate",
        "First-load width",
    }
    for side, field, until in (
        ("Buy", "import_price", "import_price_provider_until"),
        ("Sell", "export_price", "export_price_provider_until"),
    ):
        solid = by_name[f"{side}, forecast"]
        dashed = by_name[f"{side}, Nimbus estimate"]
        assert solid["stroke_dash"] == 0 and dashed["stroke_dash"] > 0
        assert solid["color"] == dashed["color"]
        for s in (solid, dashed):
            assert s["entity"] == "@nimbus_solver_battery_forecast"
            assert f"p.{field} * 100" in s["data_generator"]
            assert f"entity.attributes.{until}" in s["data_generator"]
        assert "rows.slice(0, i + 2)" in solid["data_generator"]
        assert "rows.slice(i + 1)" in dashed["data_generator"]


def test_four_equal_day_cards_follow_the_chart():
    sections, i = _prices_section()
    days = sections[i + 1 : i + 5]
    assert [s["column_span"] for s in days] == [1, 1, 1, 1]
    assert {s["cards"][0]["grid_options"]["rows"] for s in days} == {5}
    for k, s in enumerate(days):
        content = s["cards"][0]["content"]
        assert f"timedelta(days={k})" in content
        assert "__K__" not in content
        assert "import_price_provider_until" in content


def test_one_hidden_history_series_makes_the_chart_wait_for_its_width():
    """Household, 21 Oct 2026: drawn from data_generators alone, the chart
    drew before its section had a width and stayed a third wide until a
    reload. A series read from history makes it wait, as the battery chart
    does; it draws nothing and shows nowhere."""
    sections, i = _prices_section()
    chart = sections[i]["cards"][0]
    from_history = [s for s in chart["series"] if "data_generator" not in s]
    assert len(from_history) == 1
    (gate,) = from_history
    assert gate["entity"] == "@nimbus_solver_battery_forecast"
    assert gate["show"] == {"in_chart": False, "in_header": False, "in_legend": False}
