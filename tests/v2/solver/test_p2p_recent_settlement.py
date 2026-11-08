"""What tonight's P2P is expected to be, from the recent settled days (#2393: solver_inputs/p2p.py's
coverage, one behaviour per test): only the newest days count, a day with no export is no evidence, and
anything unreadable falls back to the defaults rather than stopping the plan.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest
import solver_shared
from solver_inputs import p2p

SENSOR = "sensor.lv_p2p_confirmed_history"


def _history(days: dict[str, tuple[float, float]]):
    """{date: (P2P kWh, spot kWh)} as the settlement sensor's history attribute."""
    return {
        "attributes": {
            "history": {
                d: {"export_volume": a, "spot_export_volume": b}
                for d, (a, b) in days.items()
            }
        }
    }


DAYS = {
    "2026-11-01": (90.0, 10.0),  # older than the recent five: ignored
    "2026-11-03": (60.0, 40.0),
    "2026-11-04": (0.0, 0.0),  # no export that night: no evidence
    "2026-11-05": (30.0, 70.0),
    "2026-11-06": (45.0, 55.0),
    "2026-11-07": (75.0, 25.0),
}


def test_only_the_recent_settled_nights_with_export_count():
    # test_only_the_recent_settled_nights_with_export_count
    with patch.object(solver_shared, "ha_get", lambda eid: _history(DAYS)):
        assert p2p.p2p_match_fraction(5, SENSOR) == pytest.approx(
            (0.6 + 0.3 + 0.45 + 0.75) / 4
        )
        assert p2p.p2p_recent_avg_volume_kwh(5, SENSOR) == pytest.approx(
            (60 + 30 + 45 + 75) / 4
        )


@pytest.mark.parametrize(
    "read",
    [
        None,  # no sensor configured
        urllib.error.HTTPError(SENSOR, 404, "not found", None, None),
        KeyError("history"),
        json.JSONDecodeError("bad", "", 0),
        {
            "attributes": {
                "history": {
                    "2026-11-07": {"export_volume": 0.0, "spot_export_volume": 0.0}
                }
            }
        },
    ],
)
def test_unreadable_or_empty_history_falls_back_to_the_defaults(read):
    # test_unreadable_or_empty_history_falls_back_to_the_defaults
    def ha_get(eid):
        if isinstance(read, Exception):
            raise read
        return read

    sensor = None if read is None else SENSOR
    with patch.object(solver_shared, "ha_get", ha_get):
        assert p2p.p2p_match_fraction(5, sensor) == p2p.P2P_MATCH_FRACTION_FALLBACK
        assert (
            p2p.p2p_recent_avg_volume_kwh(5, sensor)
            == p2p.P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
        )
