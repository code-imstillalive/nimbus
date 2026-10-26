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
    # test_each_provider_shape_reads_in_kw
    assert df._generic({"forecast": []}) is None
    rows = df._solcast(_solcast([2.0]))
    assert rows == [{"time": T0.isoformat(), "value": 2.0, "lower": 1.0, "upper": 0.0}]
    assert df._solcast({}) is None
    meteo = df._open_meteo({"watts": {T0.isoformat(): 1500}})
    assert meteo == [
        {"time": T0 + df.OPEN_METEO_WATTS_LEAD, "value": 1.5, "instant": True}
    ]
    assert df._open_meteo({"watts": {}}) is None

    # test_an_adapter_is_found_only_by_its_name_and_integer_version
    for spec, found in [
        ({"name": "solcast", "version": 1}, "solcast"),
        ({"name": "solcast", "version": 2}, None),
        ({"name": "solcast", "version": True}, None),
        ({"name": "solcast", "version": "1"}, None),
        ("solcast", None),
        (None, None),
    ]:
        adapter = df.forecast_adapter({"adapter": spec})
        assert (adapter.name if adapter else None) == found

    # test_a_member_that_cannot_be_read_says_why
    for source, entities, reason in [
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
    ]:
        member = df.resolve_member(
            source,
            entities.get,
            GRID,
            sw.resample_forecast,
            resample_instants=sw.resample_instants,
        )
        assert (member.state, member.reason, member.value) == ("missing", reason, None)

    # test_one_member_is_its_own_value_and_none_is_missing
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
