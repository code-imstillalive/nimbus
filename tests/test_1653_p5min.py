"""nimbus #1653 / #1658: AEMO's 5-minute pre-dispatch, parsed and published.

The fixture is a real NEMWEB file (public AEMO market data, trimmed to the
REGIONSOLUTION table): run 2026-10-09 11:50, published 11:46:20.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import p5min
from custom_components.nimbus_load import sensor_p5min as sp

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "p5min"
    / "PUBLIC_P5MIN_202610091150_20261009114620.CSV"
)
TEXT = FIXTURE.read_text()


def test_one_region_twelve_five_minute_rows_in_dollars_per_kwh():
    run = p5min.parse_region_solution(TEXT, "QLD1")
    assert run["run_datetime"] == "2026-10-09T11:50:00+10:00"
    rows = run["forecast"]
    assert len(rows) == 12
    # INTERVAL_DATETIME is the interval END: the file published at 11:46
    # leads with the interval in progress, 11:45-11:50.
    assert rows[0]["start"] == "2026-10-09T11:45:00+10:00"
    assert rows[0]["end"] == "2026-10-09T11:50:00+10:00"
    assert rows[0]["value"] == -0.00544  # -5.44 $/MWh, negative kept
    assert rows[-1]["end"] == "2026-10-09T12:45:00+10:00"


def test_each_region_is_its_own_series():
    qld = p5min.parse_region_solution(TEXT, "qld1")["forecast"]
    nsw = p5min.parse_region_solution(TEXT, "NSW1")["forecast"]
    assert nsw[0]["value"] == 0.04672
    assert qld[0]["value"] != nsw[0]["value"]


def test_an_unknown_region_or_an_empty_file_is_none_not_an_empty_forecast():
    assert p5min.parse_region_solution(TEXT, "WA1") is None
    assert p5min.parse_region_solution("", "QLD1") is None


def test_the_intervention_run_is_ignored():
    """INTERVENTION 1 rows are a what-if, not the price the market settles."""
    lines = [
        ln.replace(",0,", ",1,", 1) if ",QLD1," in ln else ln
        for ln in TEXT.splitlines()
    ]
    assert p5min.parse_region_solution("\n".join(lines), "QLD1") is None


def test_the_9_oct_shape_a_spike_an_hour_out_is_visible():
    spiked = TEXT.replace(
        '"2026/10/09 12:25:00",QLD1,', '"2026/10/09 12:25:00",QLD1,500.0,DROP,'
    )
    # Rebuild only the RRP of that one row as $500/MWh.
    out = []
    for ln in spiked.splitlines():
        if "QLD1,500.0,DROP," in ln:
            head, _, tail = ln.partition("QLD1,500.0,DROP,")
            ln = head + "QLD1,500.0," + tail.split(",", 1)[1]
        out.append(ln)
    top = p5min.peak(p5min.parse_region_solution("\n".join(out), "QLD1")["forecast"])
    assert top == {
        "start": "2026-10-09T12:20:00+10:00",
        "end": "2026-10-09T12:25:00+10:00",
        "value": 0.5,
    }


def test_the_newest_file_is_picked_from_a_listing():
    listing = (
        '<a href="/Reports/CURRENT/P5_Reports/PUBLIC_P5MIN_202610091145_20261009114121.zip">'
        '<a href="/Reports/CURRENT/P5_Reports/PUBLIC_P5MIN_202610091150_20261009114620.zip">'
        '<a href="/Reports/CURRENT/P5_Reports/PUBLIC_P5MIN_202610091140_20261009113601.zip">'
    )
    assert p5min.latest_file_name(listing) == (
        "PUBLIC_P5MIN_202610091150_20261009114620.zip"
    )
    assert p5min.latest_file_name("<html></html>") is None


def test_region_comes_from_the_configured_aemo_sensor_ids():
    ids = [
        "sensor.nem_pd7day_qld1_nem_spot_price_forecast",
        "sensor.aemo_nem_qld1_current_5min_period_price",
    ]
    assert p5min.region_from_entity_ids(ids) == "QLD1"
    assert p5min.region_from_entity_ids(["sensor.aemo_nem_sa1_price"]) == "SA1"
    # Two regions named: refuse rather than pick one.
    assert p5min.region_from_entity_ids([ids[0], "sensor.aemo_nem_vic1_x"]) is None
    assert p5min.region_from_entity_ids([None, "sensor.localvolts_v2_buy"]) is None


def _entry(forecast_id, current_id):
    entry = MagicMock()
    entry.data = {
        sp.CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: forecast_id,
        sp.CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: current_id,
    }
    entry.options = {}
    return entry


def test_resolve_region_refuses_a_conflict_and_never_geocodes_over_it(monkeypatch):
    """#1668 review: two regions named must create no entity -- not fall
    through to the geocoded location as if nothing were configured."""
    geocode = MagicMock(return_value=("QLD1", "nem_pd7day_qld1"))
    monkeypatch.setattr(
        sp.sensor_discovery, "resolve_geocoded_region_and_prefix", geocode
    )
    hass = MagicMock()
    conflict = _entry("sensor.nem_pd7day_qld1_forecast", "sensor.aemo_nem_vic1_price")
    assert sp.resolve_region(hass, conflict) is None
    geocode.assert_not_called()


def test_resolve_region_geocodes_only_when_nothing_is_named(monkeypatch):
    geocode = MagicMock(return_value=("NSW1", "nem_pd7day_nsw1"))
    monkeypatch.setattr(
        sp.sensor_discovery, "resolve_geocoded_region_and_prefix", geocode
    )
    hass = MagicMock()
    assert sp.resolve_region(hass, _entry(None, "sensor.localvolts_v2_buy")) == "NSW1"
    geocode.assert_called_once()
    geocode.reset_mock()
    named = _entry("sensor.nem_pd7day_sa1_forecast", None)
    assert sp.resolve_region(hass, named) == "SA1"
    geocode.assert_not_called()


def _sensor():
    entry = MagicMock()
    entry.entry_id = "e1653"
    s = sp.NimbusP5MinForecastSensor(entry, "QLD1", "0.94.445")
    s.hass = None
    return s


def test_the_sensor_publishes_the_interval_in_progress_and_the_whole_run():
    s = _sensor()
    assert s.available is False
    s.apply("f1.zip", p5min.parse_region_solution(TEXT, "QLD1"))
    assert s.available is True
    assert s.native_value == -0.00544
    attrs = s.extra_state_attributes
    assert attrs["region"] == "QLD1" and len(attrs["forecast"]) == 12
    assert attrs["previous_forecast"] == [] and attrs["last_error"] is None


def test_a_new_run_keeps_the_previous_one_for_a_consecutive_check():
    s = _sensor()
    first = p5min.parse_region_solution(TEXT, "QLD1")
    s.apply("f1.zip", first)
    second = {
        "run_datetime": "2026-10-09T11:55:00+10:00",
        "forecast": [dict(first["forecast"][1], value=0.5)],
    }
    s.apply("f2.zip", second)
    attrs = s.extra_state_attributes
    assert attrs["previous_run_datetime"] == "2026-10-09T11:50:00+10:00"
    assert attrs["max_price"] == 0.5
    assert attrs["previous_max_price"] == p5min.peak(first["forecast"])["value"]


def test_a_file_without_the_region_keeps_the_last_good_run():
    s = _sensor()
    s.apply("f1.zip", p5min.parse_region_solution(TEXT, "QLD1"))
    s.apply("f2.zip", None)
    assert s.available is True and s.native_value == -0.00544
    assert "no QLD1 rows" in s.extra_state_attributes["last_error"]


def test_the_listing_is_not_read_before_the_next_run_can_exist():
    s = _sensor()
    assert s.due(datetime(2026, 10, 9, 1, 0, tzinfo=UTC))
    s.apply("f1.zip", p5min.parse_region_solution(TEXT, "QLD1"))  # run 11:50
    assert not s.due(datetime(2026, 10, 9, 1, 50, 30, tzinfo=UTC))  # 11:50:30
    assert s.due(datetime(2026, 10, 9, 1, 51, 20, tzinfo=UTC))  # 11:51:20


def test_series_are_excluded_from_recorder():
    assert sp.NimbusP5MinForecastSensor._unrecorded_attributes == frozenset(
        {"forecast", "previous_forecast"}
    )


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


def test_the_fixture_is_the_real_event_mark_measured():
    assert len(OCT9) == 20
    peaks = {
        p.stem[13:25]: p5min.future_peak(
            p5min.parse_region_solution(p.read_text(), "QLD1")
        )["value"]
        for p in OCT9
    }
    # #1660: ~$500 at 04:20:52, $502 at 04:25:49, $528 at 04:30:54.
    assert peaks["202610090425"] == 0.500019
    assert peaks["202610090430"] == 0.502179
    assert peaks["202610090435"] == 0.527514
    assert peaks["202610090505"] == 22.563343  # the false extreme


def test_the_signal_confirms_on_the_second_run_and_not_before():
    seen = _replay()
    first_on = next(i for i, (_, on, _a) in enumerate(seen) if on)
    run, _on, attrs = seen[first_on]
    assert run == "04:30"
    assert attrs["readiness_signal_since"] == "2026-10-09T04:25:49+10:00"
    assert attrs["readiness_signal_peak"]["value"] == 0.502179
    assert attrs["readiness_signal_peak"]["start"] == "2026-10-09T05:20:00+10:00"
    assert attrs["readiness_signal_mode"] == "shadow"
    assert not any(on for _, on, _a in seen[:first_on])


def test_the_signal_is_held_at_most_sixty_minutes_and_not_extended():
    seen = _replay()
    on = [run for run, active, _a in seen if active]
    assert on[0] == "04:30" and on[-1] == "05:30"
    # Repeat confirmations (04:55, 05:05 ...) do not move the start.
    assert {a["readiness_signal_since"] for _, active, a in seen if active} == {
        "2026-10-09T04:25:49+10:00"
    }
    assert seen[-1][0] == "05:35" and seen[-1][1] is False


def test_a_missed_run_breaks_the_pair():
    s = _sensor()
    s.apply(
        OCT9[5].stem + ".zip", p5min.parse_region_solution(OCT9[5].read_text(), "QLD1")
    )
    s.apply(
        OCT9[7].stem + ".zip", p5min.parse_region_solution(OCT9[7].read_text(), "QLD1")
    )
    assert s.extra_state_attributes["readiness_signal"] is False
