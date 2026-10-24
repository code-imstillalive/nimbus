"""Forecast sources through the device contract (DC-006, #1574), against
what a household's providers publish (#1837).

Each provider shape read by its own adapter; a source spanning several
entities; a member that fails reported and dropped, never zero-filled; the
equal-weight mean of the members that resolve, with its band widened by
their disagreement; and the legacy solar settings mapped to a binding.
Covers what lived in #1574, #2004 and the device-contract forecast files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import device_forecasts as df
import pytest
import solver_writer as sw

T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
GRID = [T0 + timedelta(minutes=30 * i) for i in range(4)]


def _resolve(binding, entities):
    return df.resolve_forecast(
        binding,
        entities.get,
        GRID,
        sw.resample_forecast,
        resample_instants=sw.resample_instants,
    )


def _generic(values, lower=None, upper=None):
    rows = []
    for i, v in enumerate(values):
        row = {"time": GRID[i].isoformat(), "value": v}
        if lower is not None:
            row["lower"], row["upper"] = lower[i], upper[i]
        rows.append(row)
    return {"forecast": rows}


def _solcast(values, start=T0):
    return {
        "detailedForecast": [
            {
                "period_start": (start + timedelta(minutes=30 * i)).isoformat(),
                "pv_estimate": v,
                "pv_estimate10": v * 0.5,
                "pv_estimate90": None,
            }
            for i, v in enumerate(values)
        ]
    }


def test_each_provider_shape_reads_in_kw():
    assert df._generic({"forecast": []}) is None
    rows = df._solcast(_solcast([2.0]))
    assert rows == [{"time": T0.isoformat(), "value": 2.0, "lower": 1.0, "upper": 0.0}]
    assert df._solcast({}) is None
    meteo = df._open_meteo({"watts": {T0.isoformat(): 1500}})
    assert meteo == [
        {"time": T0 + df.OPEN_METEO_WATTS_LEAD, "value": 1.5, "instant": True}
    ]
    assert df._open_meteo({"watts": {}}) is None


@pytest.mark.parametrize(
    ("spec", "found"),
    [
        ({"name": "solcast", "version": 1}, "solcast"),
        ({"name": "solcast", "version": 2}, None),
        ({"name": "solcast", "version": True}, None),
        ({"name": "solcast", "version": "1"}, None),
        ("solcast", None),
        (None, None),
    ],
)
def test_an_adapter_is_found_only_by_its_name_and_integer_version(spec, found):
    adapter = df.forecast_adapter({"adapter": spec})
    assert (adapter.name if adapter else None) == found


def test_a_source_spanning_several_entities_reads_them_all_anchored_on_the_first():
    source = df.forecast_source(["sensor.today", "sensor.tomorrow"], "solcast")
    assert source == {
        "kind": "entity_attribute",
        "entity_id": "sensor.today",
        "attribute_path": "detailedForecast",
        "adapter": {"name": "solcast", "version": 1},
        "continued_by": ["sensor.tomorrow"],
    }
    entities = {
        "sensor.today": _solcast([1.0, 2.0]),
        "sensor.tomorrow": _solcast([3.0, 4.0], start=GRID[2]),
    }
    out = _resolve({"source": source}, entities)
    assert out.state == "ok" and out.value == (1.0, 2.0, 3.0, 4.0)
    assert out.lower == (0.5, 1.0, 1.5, 2.0)
    assert out.upper == out.value  # p90 absent: never below the point
    # Without the anchor the source does not resolve.
    gone = _resolve(
        {"source": source}, {"sensor.tomorrow": entities["sensor.tomorrow"]}
    )
    assert (gone.state, gone.members[0].reason) == ("missing", "unavailable")


def test_an_instant_shape_is_read_at_each_periods_midpoint():
    watts = {
        (T0 + timedelta(minutes=15 * i) - df.OPEN_METEO_WATTS_LEAD).isoformat(): 1000.0
        * i
        for i in range(10)
    }
    out = _resolve(
        {"source": df.forecast_source(["sensor.meteo"], "open_meteo_solar")},
        {"sensor.meteo": {"watts": watts}},
    )
    # The reading at 00:15 (1 kW) is period 0's midpoint, at 00:45 (3 kW) period 1's.
    assert out.value == (1.0, 3.0, 5.0, 7.0)


@pytest.mark.parametrize(
    ("source", "entities", "reason"),
    [
        (
            {
                "kind": "entity_attribute",
                "entity_id": "sensor.x",
                "adapter": {"name": "nope", "version": 1},
            },
            {},
            "adapter_unknown",
        ),
        (
            {**df.forecast_source(["sensor.x"], "nimbus_forecast"), "kind": "template"},
            {},
            "adapter_unknown",
        ),
        (df.forecast_source(["sensor.x"], "nimbus_forecast"), {}, "unavailable"),
        (
            df.forecast_source(["sensor.x"], "nimbus_forecast"),
            {"sensor.x": {"forecast": []}},
            "shape_not_recognized",
        ),
        (
            df.forecast_source(["sensor.x"], "solcast"),
            {"sensor.x": {"detailedForecast": [{"pv_estimate": 1}]}},
            "malformed",
        ),
        (
            df.forecast_source(["sensor.x"], "nimbus_forecast"),
            {"sensor.x": {"forecast": [{"time": "x", "value": 1}]}},
            "malformed",
        ),
    ],
)
def test_a_member_that_cannot_be_read_says_why(source, entities, reason):
    member = df.resolve_member(
        source,
        entities.get,
        GRID,
        sw.resample_forecast,
        resample_instants=sw.resample_instants,
    )
    assert (member.state, member.reason, member.value) == ("missing", reason, None)


def test_members_combine_as_their_mean_and_a_failed_one_is_dropped_not_zeroed():
    binding = {
        "sources": [
            df.forecast_source(["sensor.a"], "nimbus_forecast"),
            df.forecast_source(["sensor.b"], "nimbus_forecast"),
            df.forecast_source(["sensor.gone"], "nimbus_forecast"),
        ],
        "combine": "mean",
    }
    entities = {
        "sensor.a": _generic([2.0, 2.0, 2.0, 2.0], lower=[1.5] * 4, upper=[2.5] * 4),
        "sensor.b": _generic([4.0, 2.0, -1.0, 2.0]),
    }
    out = _resolve(binding, entities)
    assert out.state == "ok"
    assert out.value == (3.0, 2.0, 1.0, 2.0)  # b's -1 is clipped to 0 first
    assert [m.state for m in out.members] == ["ok", "ok", "missing"]
    # Lowest lower and highest upper, each widened by half the spread.
    assert out.lower == (1.5 - 1.0, 1.5, 0.0, 1.5)
    assert out.upper == (4.0 + 1.0, 2.5, 2.5 + 1.0, 2.5)


def test_one_member_is_its_own_value_and_none_is_missing():
    entities = {"sensor.a": _generic([1.0, 2.0, 3.0, 4.0])}
    one = _resolve(
        {
            "sources": [df.forecast_source(["sensor.a"], "nimbus_forecast")],
            "combine": "mean",
        },
        entities,
    )
    assert one.value == (1.0, 2.0, 3.0, 4.0) and one.lower == one.value
    none = _resolve(
        {
            "sources": [df.forecast_source(["sensor.b"], "nimbus_forecast")],
            "combine": "mean",
        },
        entities,
    )
    assert (none.state, none.reason, none.value) == (
        "missing",
        "no_member_resolved",
        None,
    )
    assert _resolve(None, entities).reason == "unbound"
    assert (
        _resolve({"sources": [], "combine": "median"}, entities).reason
        == "combine_unknown"
    )


@pytest.mark.parametrize(
    ("options", "auto", "known", "expected"),
    [
        ({}, False, {}, None),
        (
            {"solver_solar_forecast_sensor": "sensor.mine"},
            False,
            {},
            [("sensor.mine", "nimbus_forecast")],
        ),
        (
            {"solver_solar_forecast_sensor": "sensor.sc"},
            False,
            {"solcast": ["sensor.sc"]},
            [("sensor.sc", "solcast")],
        ),
        (
            {"solver_solar_forecast_sensor": "sensor.om"},
            False,
            {"open_meteo": ["sensor.om"]},
            [("sensor.om", "open_meteo_solar")],
        ),
        (
            {
                "solver_solar_forecast_sensor": "sensor.sc",
                "solver_solar_forecast_sensor_3": "sensor.mine",
            },
            True,
            {"solcast": ["sensor.sc", "sensor.sc2"], "open_meteo": ["sensor.om", 5]},
            [
                ("sensor.om", "open_meteo_solar"),
                ("sensor.sc", "solcast"),
                ("sensor.mine", "nimbus_forecast"),
            ],
        ),
    ],
)
def test_the_legacy_solar_settings_become_one_binding(options, auto, known, expected):
    binding = df.legacy_solar_binding(options, auto, known)
    if expected is None:
        assert binding is None
        return
    sources = [binding["source"]] if "source" in binding else binding["sources"]
    assert [(s["entity_id"], s["adapter"]["name"]) for s in sources] == expected
    assert ("combine" in binding) == (len(expected) > 1)
    if auto and known.get("solcast"):
        assert sources[1]["continued_by"] == ["sensor.sc2"]
