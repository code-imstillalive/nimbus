"""AEMO's 5-minute pre-dispatch run, parsed for one region and published,
and which region an install is in (#1653, #1658) (#1837).

The fixture is a public NEMWEB file trimmed to the REGIONSOLUTION table:
run 2026-10-09 11:50, published 11:46:20.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import p5min
from custom_components.nimbus_load import sensor_p5min as sp

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "p5min"
    / "PUBLIC_P5MIN_202610091150_20261009114620.CSV"
)
TEXT = FIXTURE.read_text()


def test_an_unknown_region_or_an_empty_file_is_none_not_an_empty_forecast():
    # test_an_unknown_region_or_an_empty_file_is_none_not_an_empty_forecast
    assert p5min.parse_region_solution(TEXT, "WA1") is None
    assert p5min.parse_region_solution("", "QLD1") is None

    # test_region_comes_from_the_configured_aemo_sensor_ids
    ids = [
        "sensor.nem_pd7day_qld1_nem_spot_price_forecast",
        "sensor.aemo_nem_qld1_current_5min_period_price",
    ]
    assert p5min.region_from_entity_ids(ids) == "QLD1"
    assert p5min.region_from_entity_ids(["sensor.aemo_nem_sa1_price"]) == "SA1"
    # Two regions named: refuse rather than pick one.
    assert p5min.region_from_entity_ids([ids[0], "sensor.aemo_nem_vic1_x"]) is None
    assert p5min.region_from_entity_ids([None, "sensor.localvolts_v2_buy"]) is None

    # test_a_file_without_the_region_keeps_the_last_good_run
    s = _sensor()
    s.apply("f1.zip", p5min.parse_region_solution(TEXT, "QLD1"))
    s.apply("f2.zip", None)
    assert s.available is True and s.native_value == -0.00544
    assert "no QLD1 rows" in s.extra_state_attributes["last_error"]

    # test_series_are_excluded_from_recorder
    # #1778 added two lists that can grow: the first interval's violated
    # constraints and the forecast-vs-dispatch record.
    assert sp.NimbusP5MinForecastSensor._unrecorded_attributes == frozenset(
        {
            "forecast",
            "previous_forecast",
            "violated_constraints",
            "forecast_at_dispatch",
        }
    )


def _entry(forecast_id, current_id):
    entry = MagicMock()
    entry.data = {
        sp.CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: forecast_id,
        sp.CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: current_id,
    }
    entry.options = {}
    return entry


def _sensor():
    entry = MagicMock()
    entry.entry_id = "e1653"
    s = sp.NimbusP5MinForecastSensor(entry, "QLD1", "0.94.445")
    s.hass = None
    return s


# ---- #1660's readiness rule, shadow only, on the real 9 Oct runs ----------
OCT9 = sorted((FIXTURE.parent / "2026-10-09").glob("*.CSV"))


def _replay():
    s = _sensor()
    seen = []
    for path in OCT9:
        s.apply(
            path.stem + ".zip", p5min.parse_region_solution(path.read_text(), "QLD1")
        )
        a = s.extra_state_attributes
        seen.append((a["run_datetime"][11:16], a["readiness_signal"], a))
    return seen
