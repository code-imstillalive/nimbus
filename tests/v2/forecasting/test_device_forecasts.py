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
    assert df._generic({"forecast": []}) is None
    rows = df._solcast(_solcast([2.0]))
    assert rows == [{"time": T0.isoformat(), "value": 2.0, "lower": 1.0, "upper": 0.0}]
    assert df._solcast({}) is None
    meteo = df._open_meteo({"watts": {T0.isoformat(): 1500}})
    assert meteo == [
        {"time": T0 + df.OPEN_METEO_WATTS_LEAD, "value": 1.5, "instant": True}
    ]
    assert df._open_meteo({"watts": {}}) is None


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
