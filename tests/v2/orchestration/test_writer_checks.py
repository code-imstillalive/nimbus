"""solver_writer's checks on what it reads and what it scores (#1837).

The load-forecast attribute parser (every shape a household's sensor can
take), the once-only misconfiguration notification, the EPR reliability
verdicts and codes, and the provisional-day repair. Covers what lived in
#1200, #1465, #1570, #1643, #1825 and the load-forecast validation files.
"""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
import solver_writer as sw
from golden.fake_ha import BASE, FakeHA

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


@pytest.fixture
def ha(monkeypatch):
    fake = FakeHA(states={})
    monkeypatch.setattr(sw, "HA_BASE", BASE)
    monkeypatch.setattr(sw, "_load_token", lambda: "test")
    monkeypatch.setattr(sw.NATIVE, "hass", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


# --- the load forecast's attributes -------------------------------------------


def _pt(i, value, **extra):
    return {
        "time": (T0 + timedelta(minutes=30 * i)).isoformat(),
        "value": value,
        **extra,
    }


def test_a_canonical_forecast_is_read_in_kw_with_its_bands():
    attrs = {
        "unit_of_measurement": "W",
        "forecast": [
            _pt(0, 1500, lower=1000, upper=2000),
            _pt(1, "2500"),
            {"time": "nope", "value": 1},
            {"time": _pt(2, 1)["time"]},
            "junk",
        ],
    }
    points, bands, error = sw._validate_and_parse_load_forecast_attrs(
        "sensor.load", attrs
    )
    assert error is None and bands is True
    assert points == [
        {
            "time": attrs["forecast"][0]["time"],
            "value": 1.5,
            "lower": 1000,
            "upper": 2000,
        },
        {"time": attrs["forecast"][1]["time"], "value": 2.5},
    ]


def test_a_scheduled_forecast_keyed_by_the_entity_is_read_without_bands():
    attrs = {
        "unit_of_measurement": "kW",
        "scheduled_forecast": [{"date": T0.isoformat(), "house": 1.2, "lower": 0.1}],
    }
    points, bands, error = sw._validate_and_parse_load_forecast_attrs(
        "sensor.house", attrs
    )
    assert (points, bands, error) == (
        [{"time": T0.isoformat(), "value": 1.2}],
        False,
        None,
    )


@pytest.mark.parametrize(
    ("attrs", "says"),
    [
        (
            {"forecast": [], "model_trained_at": None},
            "never completed a training cycle",
        ),
        (
            {"forecast": [], "model_trained_at": "x", "training_points": 0},
            "never completed",
        ),
        (
            {"forecast": [], "model_trained_at": "x", "training_points": 9},
            "first few days",
        ),
        ({"forecast": []}, "first few days"),
        ({"other": [1], "forecast": "x"}, "list-valued attributes present: ['other']"),
        ({}, "present: none"),
        (
            {"scheduled_forecast": [{"date": "x", "elsewhere": 1}]},
            "no usable 'forecast'",
        ),
        (
            {"forecast": [{"time": "x", "value": 1}]},
            "none parsed cleanly under keys 'time'/'value'",
        ),
        (
            {"unit_of_measurement": "kWh", "forecast": [_pt(0, 1)]},
            "'kWh', which is not a power unit",
        ),
        ({"unit_of_measurement": "A", "forecast": [_pt(0, 1)]}, "not a power unit"),
    ],
)
def test_an_unusable_forecast_is_refused_with_a_reason_a_household_can_act_on(
    attrs, says
):
    points, bands, error = sw._validate_and_parse_load_forecast_attrs(
        "sensor.load", attrs
    )
    assert points is None and bands is False
    assert says in error


# --- the misconfiguration notification -----------------------------------------


def test_the_load_forecast_error_notifies_once_per_error_then_clears(
    ha, monkeypatch, tmp_path
):
    marker = tmp_path / "notified.txt"
    monkeypatch.setattr(sw, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", str(marker))
    sw._notify_load_forecast_error_once("first problem")
    sw._notify_load_forecast_error_once("first problem")
    sw._notify_load_forecast_error_once(
        "same problem, new words", error_key="first problem"
    )
    assert len(ha.service_calls) == 1
    call = ha.service_calls[0]
    assert (call["domain"], call["service"]) == ("persistent_notification", "create")
    assert call["data"]["notification_id"] == "nimbus_solver_load_forecast_error"
    assert call["data"]["message"].startswith("first problem\n\n")
    sw._notify_load_forecast_error_once("second problem")
    assert len(ha.service_calls) == 2 and marker.read_text() == "second problem"
    sw._clear_load_forecast_error_notification_if_needed()
    assert ha.service_calls[-1]["service"] == "dismiss" and not marker.exists()
    sw._clear_load_forecast_error_notification_if_needed()
    assert len(ha.service_calls) == 3  # nothing left to dismiss


def test_a_failing_notification_never_fails_the_solve(ha, monkeypatch, tmp_path):
    monkeypatch.setattr(
        sw, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", str(tmp_path / "no" / "dir")
    )

    def boom(*_a):
        raise OSError("down")

    monkeypatch.setattr(sw, "ha_call_service", boom)
    sw._notify_load_forecast_error_once("x")
    (tmp_path / "no").mkdir()
    (tmp_path / "no" / "dir").write_text("old")
    sw._clear_load_forecast_error_notification_if_needed()
    assert (tmp_path / "no" / "dir").exists()


# --- how far a day's score can be trusted -----------------------------------------


@pytest.mark.parametrize(
    ("reliable", "reason", "out"),
    [
        (None, None, "achieved_soc_unverifiable"),
        (False, "sensor_gap", "achieved_soc_unreliable:sensor_gap"),
        (False, None, "achieved_soc_unreliable:unknown"),
        (True, "ignored", None),
    ],
)
def test_the_soc_half_of_the_reason(reliable, reason, out):
    assert sw._epr_soc_reason(reliable, reason) == out


@pytest.mark.parametrize(
    ("kwargs", "out"),
    [
        ({"soc": True, "regret": True}, True),
        ({"soc": False, "regret": True}, False),
        ({"soc": None, "regret": True}, None),
        ({"soc": True, "regret": False}, False),
        ({"soc": None, "regret": False}, False),
        ({"soc": True, "regret": True, "denominator": "zero"}, False),
        ({"soc": True, "regret": True, "stale": True}, False),
        ({"soc": True, "regret": True, "stale": "yes"}, True),
        ({"soc": True, "regret": True, "meter": "disagrees"}, False),
        ({"soc": True, "regret": True, "meter": "agrees"}, True),
    ],
)
def test_a_score_is_reliable_only_when_every_part_is(kwargs, out):
    got = sw._epr_reliability(
        kwargs["soc"],
        kwargs["regret"],
        kwargs.get("denominator"),
        kwargs.get("meter"),
        kwargs.get("stale", False),
    )
    assert got is out


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ({}, None),
        ({"epr_reliable": True}, "y"),
        ({"epr_reliable": None}, "u"),
        (
            {
                "epr_reliable": False,
                "price_stale_unusable": True,
                "regret_reliable": False,
            },
            "f",
        ),
        (
            {
                "epr_reliable": False,
                "regret_reliable": False,
                "epr_denominator_reason": "z",
            },
            "o",
        ),
        (
            {
                "epr_reliable": False,
                "epr_denominator_reason": "z",
                "epr_reason": "achieved_soc_x",
            },
            "d",
        ),
        ({"epr_reliable": False, "epr_reason": "achieved_soc_unverifiable"}, "s"),
        ({"epr_reliable": False, "soc_discrepancy_reliable": False}, "s"),
        ({"epr_reliable": False, "epr_reason": "grid_meter_disagrees"}, "m"),
        ({"epr_reliable": False, "meter_reconciliation": {"status": "disagrees"}}, "m"),
        ({"epr_reliable": False, "meter_reconciliation": {"status": "agrees"}}, "?"),
        ({"epr_reliable": False}, "?"),
    ],
)
def test_each_stored_day_carries_the_first_reason_it_cannot_be_trusted(entry, code):
    assert sw._epr_reliability_code(entry) == code


# --- re-scoring a day scored before its settlement landed -------------------------

NOW = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
CFG = {"solver_p2p_settlement_history_sensor": "sensor.settled"}


def _quality(history, latest="2026-09-24"):
    return {"state": "1", "attributes": {"history": history, "latest_date": latest}}


def test_the_oldest_provisional_day_whose_settlement_has_landed_is_rescored(
    ha, monkeypatch
):
    rescored = []
    monkeypatch.setattr(
        sw,
        "rescore_quality_history",
        lambda cfg, now, back, only_dates: (
            rescored.append((back, only_dates)) or {"ok": 1}
        ),
    )
    ha.states[sw.QUALITY_ENTITY_ID] = _quality(
        {
            "2026-09-24": {"p": 1},  # the latest day: still being scored
            "2026-09-23": {"p": 1},
            "2026-09-22": {"p": 1},
            "2026-09-21": {"x": 1},  # final
            "2026-08-01": {"p": 1},  # too old to re-score
            "garbage": {"p": 1},
            "2026-09-20": "not a row",
        }
    )
    ha.states["sensor.settled"] = {
        "state": "1",
        "attributes": {
            "history": {"2026-09-22": {}, "2026-09-23": {}, "2026-08-01": {}}
        },
    }
    out = sw.repair_provisional_quality_history(CFG, NOW)
    # One per cycle, oldest first.
    assert out == {"repaired": [{"date": "2026-09-22", "summary": {"ok": 1}}]}
    assert rescored == [(3, {"2026-09-22"})]


@pytest.mark.parametrize(
    ("quality", "settled", "cfg"),
    [
        (None, None, CFG),
        ({"state": "1", "attributes": {"history": []}}, None, CFG),
        (_quality({"2026-09-23": {"p": 1}}), None, CFG),
        (_quality({"2026-09-23": {"p": 1}}), {"state": "1", "attributes": {}}, CFG),
        (
            _quality({"2026-09-23": {"p": 1}}),
            {"state": "1", "attributes": {"history": {}}},
            CFG,
        ),
        (
            _quality({"2026-09-23": {"p": 1}}),
            {"state": "1", "attributes": {"history": {"2026-09-23": 1}}},
            {},
        ),
        (
            _quality({"2026-09-25": {"p": 1}}),
            {"state": "1", "attributes": {"history": {"2026-09-25": 1}}},
            CFG,
        ),
    ],
    ids=[
        "no_report",
        "no_history",
        "no_settlement",
        "settlement_without_history",
        "not_settled",
        "no_sensor",
        "today",
    ],
)
def test_nothing_is_rescored_without_a_landed_settlement_for_a_past_day(
    ha, monkeypatch, quality, settled, cfg
):
    monkeypatch.setattr(
        sw, "rescore_quality_history", lambda *a, **k: pytest.fail("rescored")
    )
    if quality is not None:
        ha.states[sw.QUALITY_ENTITY_ID] = quality
    if settled is not None:
        ha.states["sensor.settled"] = settled
    assert sw.repair_provisional_quality_history(cfg, NOW) is None
