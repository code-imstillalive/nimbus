"""nimbus #1657 (Mark Purcell, 9 Oct 2026): the import/export price entities
carry the full resolved price series live, as `detailedForecast`, excluded
from Recorder (the Amber Express #82 pattern). The real-recorder half of the
proof is tests/hass_integration/test_1657_detailed_forecast_unrecorded.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor_flattened as sf


def _entities():
    entry = MagicMock()
    entry.entry_id = "e1657"
    ents = sf.create_flattened_entities_current(entry, sw_version="0.94.445")
    for e in ents:
        e.hass = None
    return {e.entity_id: e for e in ents}


ROWS = [
    {
        "time": "2026-10-09T04:25:00+10:00",
        "hours": 1 / 60,
        "import_price": 0.39,
        "import_price_raw": 0.35,
        "import_price_source": "primary",
        "export_price": 0.2861,
        "export_price_raw": 0.2861,
        "export_price_source": "primary",
    },
    {
        "time": "2026-10-09T04:26:00+10:00",
        "hours": 5 / 60,
        "import_price": None,  # a gap: kept as None, never zero
        "export_price": -0.031,  # negative prices survive
        "export_price_source": "fallback",
    },
    {
        "time": "2026-10-09T05:20:00+10:00",
        "hours": 0.5,
        "import_price": 1.3195,
        "export_price": 1.042,
    },
]


def _publish(ents, rows=ROWS):
    sf.dispatch_to_flattened(
        list(ents.values()),
        {
            "forecast": rows,
            "status": "optimal",
            "import_price": 0.39,
            "export_price": 0.2861,
        },
    )


def test_each_price_entity_publishes_its_own_direction():
    ents = _entities()
    _publish(ents)
    imp = ents["sensor.nimbus_solver_current_import_price"].extra_state_attributes
    exp = ents["sensor.nimbus_solver_current_export_price"].extra_state_attributes
    assert [r["value"] for r in imp["detailedForecast"]] == [0.39, None, 1.3195]
    assert [r["value"] for r in exp["detailedForecast"]] == [0.2861, -0.031, 1.042]
    assert imp["detailed_forecast_meta"]["direction"] == "import"
    assert exp["detailed_forecast_meta"]["direction"] == "export"


def test_rows_are_half_open_intervals_at_their_own_targets():
    """#1657 acceptance: a 05:20 source peak stays at 05:20 and cannot become
    period 0 through start/end conversion or indexing."""
    ents = _entities()
    _publish(ents)
    rows = ents["sensor.nimbus_solver_current_import_price"].extra_state_attributes[
        "detailedForecast"
    ]
    assert rows[0]["start"] == "2026-10-09T04:25:00+10:00"
    assert rows[0]["end"] == "2026-10-09T04:26:00+10:00"
    assert rows[1]["end"] == "2026-10-09T04:31:00+10:00"
    peak = max((r for r in rows if r["value"] is not None), key=lambda r: r["value"])
    assert peak["start"] == "2026-10-09T05:20:00+10:00"
    assert peak["end"] == "2026-10-09T05:50:00+10:00"
    assert rows[0]["value"] == 0.39


def test_raw_and_source_are_kept_beside_the_resolved_value():
    ents = _entities()
    _publish(ents)
    rows = ents["sensor.nimbus_solver_current_import_price"].extra_state_attributes[
        "detailedForecast"
    ]
    assert rows[0]["value_raw"] == 0.35 and rows[0]["source"] == "primary"
    assert rows[2]["value_raw"] is None  # not recorded by the plan: absent, not zero


def test_the_series_is_excluded_from_recorder_and_the_metadata_is_not():
    unrecorded = sf._FlattenedAttributeSensor._unrecorded_attributes
    assert "detailedForecast" in unrecorded
    assert "detailed_forecast_meta" not in unrecorded


def test_other_entities_get_no_detailed_forecast():
    ents = _entities()
    _publish(ents)
    others = [
        e
        for eid, e in ents.items()
        if eid
        not in (
            "sensor.nimbus_solver_current_import_price",
            "sensor.nimbus_solver_current_export_price",
        )
    ]
    assert others
    for e in others:
        attrs = e.extra_state_attributes or {}
        assert "detailedForecast" not in attrs, e.entity_id


def test_state_and_rows_come_from_the_same_publish():
    """#1657 acceptance: the numeric state and the detailed rows cannot be
    published from different solves. A second publish replaces both."""
    ents = _entities()
    _publish(ents)
    later = [dict(ROWS[0], time="2026-10-09T04:27:00+10:00", import_price=0.41)]
    sf.dispatch_to_flattened(
        list(ents.values()),
        {
            "forecast": later,
            "status": "optimal",
            "import_price": 0.41,
            "export_price": 0.29,
        },
    )
    imp = ents["sensor.nimbus_solver_current_import_price"]
    rows = imp.extra_state_attributes["detailedForecast"]
    assert len(rows) == 1 and rows[0]["value"] == 0.41
    assert imp.extra_state_attributes["detailed_forecast_meta"]["period_0_start"] == (
        "2026-10-09T04:27:00+10:00"
    )


def test_a_publish_without_rows_keeps_the_previous_series():
    ents = _entities()
    _publish(ents)
    sf.dispatch_to_flattened(list(ents.values()), {"status": "optimal"})
    rows = ents["sensor.nimbus_solver_current_import_price"].extra_state_attributes[
        "detailedForecast"
    ]
    assert len(rows) == 3
