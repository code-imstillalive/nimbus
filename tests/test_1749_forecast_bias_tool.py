"""nimbus #1749: the read-only forecast-bias measurement tool.

`scripts/forecast_bias.py` measures forecast minus settled price by lead time
and hour of day, from an `export_decision_inputs` response or from Home
Assistant's recorder over REST (GET only). Synthetic data only, no household
telemetry. The export tests build their input through the REAL capture store
(`decision_inputs_store.async_capture` + `async_export`), so the tool is pinned
to the schema the service actually returns, not to a copy of it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import price_intervals
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import decision_inputs_store as store


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "forecast_bias", REPO / "scripts" / "forecast_bias.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["forecast_bias"] = module
    spec.loader.exec_module(module)
    return module


fb = _load_tool()

T0 = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)  # 14:00 AEST, a 5- and 30-min mark


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---- lead buckets ------------------------------------------------------------


@pytest.mark.parametrize(
    ("lead_minutes", "bucket"),
    [
        (0, "0-30 min"),
        (29.99, "0-30 min"),
        (30, "30-60 min"),
        (59.99, "30-60 min"),
        (60, "1-2 h"),
        (120, "2-4 h"),
        (240, "4-8 h"),
        (479.99, "4-8 h"),
        (480, "8-24 h"),
        (1439.99, "8-24 h"),
        (1440, "24 h+"),
        (5000, "24 h+"),
        (-0.01, None),  # its span had begun: lead 0 is the in-interval case
    ],
)
def test_lead_buckets(lead_minutes, bucket):
    assert fb.lead_bucket(timedelta(minutes=lead_minutes)) == bucket


def test_bucket_order_is_in_interval_then_increasing_lead():
    assert fb.BUCKET_ORDER == (
        "in-interval",
        "0-30 min",
        "30-60 min",
        "1-2 h",
        "2-4 h",
        "4-8 h",
        "8-24 h",
        "24 h+",
    )


def test_intervals_are_clock_aligned_for_5_and_30_minutes():
    t = datetime(2026, 10, 13, 4, 17, 30, tzinfo=UTC)
    assert fb.interval_start(fb.interval_index(t, 5), 5) == _at(15)
    assert fb.interval_start(fb.interval_index(t, 30), 30) == T0


# ---- settled value: the last value held in the interval ----------------------


def test_settled_is_the_last_value_held_in_the_interval():
    samples = [
        fb.Sample(_at(-3), 5.0),
        fb.Sample(_at(0) + timedelta(seconds=4), 7.0),  # opens on the forecast
        fb.Sample(_at(1), 20.3),  # the settled price arrives
        fb.Sample(_at(10) + timedelta(seconds=30), None),  # unavailable to its end
    ]
    s = fb.settle(
        samples,
        5,
        [(T0, _at(15))],
        require_sample_inside=False,
        min_inside_for_opening=1,
    )
    i0 = fb.interval_index(T0, 5)
    assert s.settled[i0] == 20.3
    assert s.opening[i0] == 7.0
    # No change in 04:05-04:10: HA records only changes, so it settled at the
    # value carried into it, counted as held.
    assert s.settled[i0 + 1] == 20.3 and s.held == 1
    assert i0 + 1 not in s.opening
    # Unavailable at the end of 04:10-04:15: no settled value, not a guess.
    assert i0 + 2 not in s.settled


def test_an_export_never_settles_an_interval_no_solve_ran_in():
    samples = [
        fb.Sample(_at(1), 10.0),
        fb.Sample(_at(4), 12.0),
        fb.Sample(_at(11), 9.0),
    ]
    s = fb.settle(
        samples,
        5,
        [(T0, _at(15))],
        require_sample_inside=True,
        min_inside_for_opening=2,
    )
    i0 = fb.interval_index(T0, 5)
    assert s.settled == {i0: 12.0, i0 + 2: 9.0}  # 04:05-04:10 is a gap, not 12.0
    assert s.opening == {i0: 10.0}  # 04:10 had one solve: opening == settled
    assert s.held == 0


def test_an_interval_cut_by_the_data_edge_is_not_settled():
    samples = [fb.Sample(_at(0.5), 10.0), fb.Sample(_at(6), 11.0)]
    s = fb.settle(
        samples,
        5,
        [(_at(2), _at(8))],  # starts and ends mid-interval
        require_sample_inside=False,
        min_inside_for_opening=1,
    )
    assert s.settled == {}


def test_a_forecast_whose_interval_has_no_settled_value_is_unpaired():
    settlement = fb.Settlement(settled={fb.interval_index(_at(5), 5): 20.0})
    forecasts = [
        fb.Forecast(T0, _at(5), _at(10), 12.0),
        fb.Forecast(T0, _at(10), _at(15), 12.0),  # no settled value: never filled
    ]
    counts = fb.Counts()
    pairs = fb.pair(forecasts, settlement, 5, UTC, counts)
    assert [p.bias for p in pairs] == [-8.0]
    assert counts.paired == 1 and counts.unpaired == 1


def test_a_30_minute_forecast_is_compared_with_the_mean_of_its_5_minute_settlements():
    i0 = fb.interval_index(_at(30), 5)
    settled = {i0 + k: v for k, v in enumerate([10.0, 12.0, 14.0, 16.0, 18.0, 20.0])}
    f = fb.Forecast(T0, _at(30), _at(60), 10.0)
    counts = fb.Counts()
    pairs = fb.pair([f], fb.Settlement(settled=settled), 5, UTC, counts)
    assert pairs[0].bias == pytest.approx(10.0 - 15.0)
    assert pairs[0].bucket == "30-60 min"

    del settled[i0 + 3]  # one 5-minute interval missing: no partial mean
    counts = fb.Counts()
    assert fb.pair([f], fb.Settlement(settled=settled), 5, UTC, counts) == []
    assert counts.unpaired == 1


def test_30_minute_settlement_pairs_a_forecast_inside_its_interval():
    i = fb.interval_index(_at(30), 30)
    counts = fb.Counts()
    pairs = fb.pair(
        [fb.Forecast(T0, _at(30), _at(35), 25.0)],
        fb.Settlement(settled={i: 20.0}),
        30,
        UTC,
        counts,
    )
    assert [(p.bucket, p.bias) for p in pairs] == [("30-60 min", 5.0)]


def test_a_forecast_seen_after_its_interval_began_is_not_a_forecast_ahead():
    settled = {fb.interval_index(T0, 5): 20.0}
    counts = fb.Counts()
    pairs = fb.pair(
        [fb.Forecast(_at(2), _at(3), _at(4), 9.0)],
        fb.Settlement(settled=settled),
        5,
        UTC,
        counts,
    )
    assert pairs == [] and counts.not_ahead == 1


def test_one_minute_rows_in_one_interval_count_once():
    rows = [fb.Forecast(T0, _at(m), _at(m + 1), 10.0 + m) for m in range(1, 5)]
    rows.append(fb.Forecast(T0, _at(5), _at(35), 30.0))
    merged = fb.merge_short(rows, 5)
    assert [(f.start, f.end) for f in merged] == [(_at(1), _at(5)), (_at(5), _at(35))]
    assert merged[0].value == pytest.approx(12.5)


def test_hour_of_day_is_in_the_requested_zone():
    i = fb.interval_index(_at(60), 5)
    from zoneinfo import ZoneInfo

    pairs = fb.pair(
        [fb.Forecast(T0, _at(60), _at(65), 1.0)],
        fb.Settlement(settled={i: 0.0}),
        5,
        ZoneInfo("Australia/Brisbane"),
        fb.Counts(),
    )
    assert pairs[0].hour == 15  # 05:00 UTC is 15:00 AEST


# ---- statistics ----------------------------------------------------------------


def test_summary_mean_median_p90_and_share_settled_above():
    s = fb.summarise([-3.0, -1.0, 0.0, 2.0, 10.0])
    assert s["n"] == 5
    assert s["mean"] == pytest.approx(1.6)
    assert s["median"] == pytest.approx(0.0)
    assert s["p90"] == pytest.approx(6.8)  # linear between the 4th and 5th
    assert s["settled_above_pct"] == pytest.approx(40.0)
    assert fb.summarise([])["mean"] is None


def test_by_hour_leaves_out_the_in_interval_pairs():
    pairs = [fb.Pair("in-interval", 4, -9.0), fb.Pair("0-30 min", 4, -1.0)]
    table = fb.tabulate(pairs)
    assert table["by_lead"]["in-interval"]["n"] == 1
    assert table["by_hour"]["4"]["n"] == 1 and table["by_hour"]["4"]["mean"] == -1.0
    assert table["by_lead_and_hour"]["in-interval"]["4"]["n"] == 1


# ---- read-only REST: GET and nothing else -------------------------------------


class _Opener:
    """Records every request; answers from `routes` (path prefix -> JSON)."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = routes or {}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        for prefix, body in self.routes.items():
            if ("/" + path).startswith(prefix):
                return io.BytesIO(json.dumps(body).encode())
        raise AssertionError(f"unexpected request {request.full_url}")


@pytest.mark.parametrize(
    "request_",
    [
        urllib.request.Request("http://ha.test:8123/api/services/x/y", data=b"{}"),
        urllib.request.Request("http://ha.test:8123/api/states/x", method="POST"),
        urllib.request.Request("http://ha.test:8123/api/states/x", method="DELETE"),
        urllib.request.Request("http://ha.test:8123/api/states/x", method="PUT"),
        urllib.request.Request("http://ha.test:8123/api/x", data=b"", method="GET"),
    ],
)
def test_a_non_get_request_is_refused_before_the_network(request_):
    opener = _Opener()
    client = fb.GetOnlyClient("http://ha.test:8123", "t", opener=opener)
    with pytest.raises(fb.RefusedRequestError):
        client.send(request_)
    assert opener.requests == []


def test_get_json_sends_a_plain_get_with_the_token():
    opener = _Opener({"/api/config": {"time_zone": "Australia/Brisbane"}})
    client = fb.GetOnlyClient("http://ha.test:8123/", "secret-token", opener=opener)
    assert client.get_json("/api/config") == {"time_zone": "Australia/Brisbane"}
    (req,) = opener.requests
    assert req.get_method() == "GET" and req.data is None
    assert req.get_header("Authorization") == "Bearer secret-token"


def test_base_url_must_be_http():
    with pytest.raises(ValueError):
        fb.GetOnlyClient("ftp://ha.test", "t")


def test_unit_scale_matches_the_integrations_own():
    for unit in ("c/kWh", "¢/kWh", "$/kWh", "AUD/kWh", "$/MWh", "c/MWh", None, ""):
        assert fb.price_scale_to_cents(unit) == pytest.approx(
            100.0 * price_intervals.price_unit_scale(unit)
        ), unit


def test_generic_forecast_rows_never_extend_past_their_points():
    attrs = {
        "unit_of_measurement": "$/kWh",
        "forecast": [
            {"time": _at(5).isoformat(), "value": 0.10},
            {"time": _at(10).isoformat(), "value": 0.12},
            {"time": _at(120).isoformat(), "value": 0.30},  # 110 min later: a hole
            {"time": _at(125).isoformat(), "value": 0.31},  # last point: no end
        ],
    }
    out = fb.forecast_rows(attrs, T0, timedelta(minutes=30))
    assert [(f.start, f.end, round(f.value, 6)) for f in out] == [
        (_at(5), _at(10), 10.0),
        (_at(120), _at(125), 30.0),
    ]


def test_explicit_interval_rows_are_read_as_dollars_per_kwh():
    attrs = {
        "unit_of_measurement": "$/MWh",  # the current state's unit, not the rows'
        "forecast": [
            {
                "start_time": _at(30).isoformat(),
                "end_time": _at(60).isoformat(),
                "price": 0.2,
            }
        ],
    }
    (f,) = fb.forecast_rows(attrs, T0, timedelta(minutes=30))
    assert (f.start, f.end, f.value) == (_at(30), _at(60), pytest.approx(20.0))


def _state(at: datetime, value: str, attrs: dict | None = None) -> dict:
    return {
        "entity_id": "sensor.buy",
        "state": value,
        "attributes": {"unit_of_measurement": "c/kWh", **(attrs or {})},
        "last_changed": at.isoformat(),
        "last_updated": at.isoformat(),
    }


def test_rest_path_end_to_end_reads_only_and_pairs_forecasts(tmp_path, capsys):
    forecast = [{"time": _at(m).isoformat(), "value": 10.0} for m in range(5, 65, 5)]
    rows = [_state(_at(-1), "9.0", {"forecast": forecast})]
    for m in range(0, 60, 5):
        rows.append(_state(_at(m) + timedelta(seconds=3), "10.0"))  # opens low
        rows.append(_state(_at(m + 1), "18.0"))  # settles 8c higher
    opener = _Opener(
        {
            "/api/config": {"time_zone": "Australia/Brisbane"},
            "/api/history/period/": [rows],
        }
    )
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    args = fb.parser().parse_args(
        [
            "rest",
            "--base-url",
            "http://ha.test:8123",
            "--token-file",
            str(token),
            "--start",
            T0.isoformat(),
            "--end",
            _at(60).isoformat(),
            "--import-entity",
            "sensor.buy",
        ]
    )
    report = fb.run_rest(args, opener=opener)
    assert all(r.get_method() == "GET" and r.data is None for r in opener.requests)
    assert all(
        r.get_header("Authorization") == "Bearer secret-token" for r in opener.requests
    )
    history_url = opener.requests[1].full_url
    assert "significant_changes_only=0" in history_url
    assert "filter_entity_id=sensor.buy" in history_url

    side = report["sides"]["import"]
    assert list(report["sides"]) == ["import"]
    assert report["timezone"].startswith("Australia/Brisbane")
    lead = side["by_lead"]
    # The forecast seen at 03:59 for 04:05-05:00 (11 spans) all read 10c
    # against a settled 18c.
    assert lead["0-30 min"]["n"] + lead["30-60 min"]["n"] + lead["1-2 h"]["n"] == 11
    assert lead["0-30 min"]["mean"] == pytest.approx(-8.0)
    assert lead["in-interval"]["n"] == 12 and lead["in-interval"][
        "median"
    ] == pytest.approx(-8.0)
    assert side["by_hour"]["14"]["mean"] == pytest.approx(
        -8.0
    )  # 04:xx UTC = 14:xx AEST


# ---- path 1: the real export_decision_inputs schema ------------------------------


CONFIG = {
    "friendly_name": "Nimbus Solver Config",
    "solver_import_price_sensor": "sensor.test_buy",
    "solver_export_price_sensor": "sensor.test_sell",
    "solver_price_forecast_array_sensor": "sensor.test_array",
}


class _Hass:
    def __init__(self, root: Path):
        self.states_by_id: dict[str, SimpleNamespace] = {}
        self.config = SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts)))
        self.states = SimpleNamespace(get=self.states_by_id.get)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def set(self, entity_id: str, state, attributes: dict, updated: datetime):
        self.states_by_id[entity_id] = SimpleNamespace(
            state=state, attributes=attributes, last_updated=updated
        )


def _plan(at: datetime, current: float, ahead: float) -> dict:
    """A tiered plan as the Solver publishes it: 1-minute rows to the next
    5-minute mark (carrying the current price), 5-minute rows for two hours,
    then 30-minute rows."""
    rows = []
    t = at.replace(second=0, microsecond=0)
    block_end = fb.interval_start(fb.interval_index(t, 5) + 1, 5)
    while t < at + timedelta(hours=6):
        step = 1 if t < block_end else (5 if t < at + timedelta(hours=2) else 30)
        price = current if t < block_end else ahead
        rows.append(
            {
                "time": t.isoformat(),
                "hours": round(step / 60, 4),
                "import_price": price + 0.05,  # landed: plus fees
                "import_price_raw": price,
                "import_price_source": "primary",
                "export_price": price - 0.02,
                "export_price_raw": price - 0.02,
                "export_price_source": "primary",
                "battery_kw": 0.0,
            }
        )
        t += timedelta(minutes=step)
    return {"forecast": rows, "generated_at": rows[0]["time"], "status": "optimal"}


@pytest.fixture
def exported(tmp_path, monkeypatch):
    """One hour of solves, one a minute, captured and exported by the real
    store. Each interval opens at 10c and settles at 18c two minutes in; every
    plan forecasts 10c ahead."""
    store.reset_decision_inputs_cache()
    clock = {"now": T0}
    monkeypatch.setattr(store, "dt_util", SimpleNamespace(utcnow=lambda: clock["now"]))
    hass = _Hass(tmp_path)
    hass.set("sensor.nimbus_solver_config", "configured", dict(CONFIG), T0)
    reaches = [{"time": _at(m).isoformat(), "value": 0.1} for m in (0, 24 * 60)]
    for eid in ("sensor.test_buy", "sensor.test_sell", "sensor.test_array"):
        hass.set(eid, "0.1", {"forecast": reaches}, T0)

    def export(cutoff_minutes: float | None = None) -> dict:
        if cutoff_minutes is not None:
            short = [{"time": _at(cutoff_minutes).isoformat(), "value": 0.1}]
            for eid in ("sensor.test_buy", "sensor.test_sell", "sensor.test_array"):
                hass.set(eid, "0.1", {"forecast": short}, T0)
        for m in range(60):
            at = _at(m) + timedelta(seconds=10)
            clock["now"] = at
            current = 0.10 if m % 5 < 2 else 0.18
            hass.set(
                "sensor.nimbus_solver_battery_forecast",
                0.0,
                _plan(at, current, 0.10),
                at,
            )
            asyncio.run(store.async_capture(hass, "E"))
        response = asyncio.run(
            store.async_export(hass, "E", T0, _at(60), ["head", "horizon"])
        )
        # What a caller gets back: JSON, datetimes already strings.
        return json.loads(json.dumps(response, default=str))

    yield export
    store.reset_decision_inputs_cache()


def _run_export(paths: list[Path], *extra: str) -> dict:
    args = fb.parser().parse_args(["export", *map(str, paths), "--tz", "UTC", *extra])
    return fb.run_export(args)


def test_export_response_is_measured_against_its_own_later_records(exported, tmp_path):
    response = exported()
    assert response["count"] == 60 and set(response) >= {
        "records",
        "configs",
        "start",
        "end",
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(response), encoding="utf-8")
    report = _run_export([path])

    imp = report["sides"]["import"]
    assert imp["counts"]["observations"] == 60
    assert imp["counts"]["intervals_settled"] == 12  # every 5 min of the hour
    lead = imp["by_lead"]
    # Every interval opened at 10c (its first solve) and settled at 18c (its
    # last): lead 0 runs 8c low, and so does every forecast ahead.
    assert lead["in-interval"]["n"] == 12
    assert lead["in-interval"]["mean"] == pytest.approx(-8.0)
    assert lead["0-30 min"]["n"] > 0 and lead["0-30 min"]["mean"] == pytest.approx(-8.0)
    assert lead["30-60 min"]["median"] == pytest.approx(-8.0)
    assert lead["30-60 min"]["settled_above_pct"] == pytest.approx(100.0)
    # Nothing after the exported hour has a settled value: unpaired, not filled.
    assert imp["counts"]["unpaired"] > 0
    assert lead["2-4 h"]["n"] == 0

    exp = report["sides"]["export"]
    assert exp["by_lead"]["0-30 min"]["mean"] == pytest.approx(-8.0)


def test_rows_in_row_0s_block_are_never_counted_as_forecasts(exported, tmp_path):
    """They carry the current price (solver_writer's settled override): if they
    were forecasts, the 1-minute rows at 10c inside an interval that settles at
    18c would show up at a lead under 5 minutes."""
    path = tmp_path / "export.json"
    path.write_text(json.dumps(exported()), encoding="utf-8")
    counts = fb.Counts()
    samples, forecasts, _windows = fb.export_observations(
        fb.load_exports([path]),
        "import",
        minutes=5,
        price_field="raw",
        include_extrapolated=False,
        counts=counts,
    )
    assert len(samples) == 60
    assert {round(s.value, 6) for s in samples} == {10.0, 18.0}
    assert forecasts
    for f in forecasts:
        # Strictly a later 5-minute interval than the one it was seen in,
        # and never one of the 10c-current rows.
        assert fb.interval_index(f.start, 5) > fb.interval_index(f.observed_at, 5)
        assert f.value == pytest.approx(10.0)
    # The 1-minute rows were all inside row 0's block, so no forecast is
    # shorter than an interval.
    assert min(f.end - f.start for f in forecasts) == timedelta(minutes=5)


def test_landed_prices_include_the_fees(exported, tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(exported()), encoding="utf-8")
    raw = _run_export([path])["sides"]["import"]["by_lead"]["0-30 min"]
    landed = _run_export([path], "--price-field", "landed")["sides"]["import"][
        "by_lead"
    ]
    # Fees are added to the forecast and to the settled reading alike.
    assert landed["0-30 min"]["mean"] == pytest.approx(raw["mean"])


def test_the_rest_wrapper_a_list_and_overlapping_files_all_read_the_same(
    exported, tmp_path
):
    response = exported()
    plain = tmp_path / "a.json"
    plain.write_text(json.dumps(response), encoding="utf-8")
    wrapped = tmp_path / "b.json"
    wrapped.write_text(
        json.dumps({"changed_states": [], "service_response": response}),
        encoding="utf-8",
    )
    listed = tmp_path / "c.json"
    listed.write_text(json.dumps([response]), encoding="utf-8")
    base = _run_export([plain])["sides"]
    assert _run_export([wrapped])["sides"] == base
    assert _run_export([listed])["sides"] == base
    # The same records in two files are one set of observations.
    both = _run_export([plain, wrapped])["sides"]
    assert both["import"]["counts"]["observations"] == 60
    assert both == base


def test_rows_past_the_sources_own_forecast_are_not_counted(exported, tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(exported(cutoff_minutes=40)), encoding="utf-8")
    report = _run_export([path])
    counts = report["sides"]["import"]["counts"]
    assert counts["beyond_source_forecast"] > 0
    assert (
        report["sides"]["import"]["by_lead"]["30-60 min"]["n"]
        < (
            _run_export([path], "--include-extrapolated")["sides"]["import"]["by_lead"][
                "30-60 min"
            ]["n"]
        )
    )


def test_a_file_that_is_not_an_export_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"state": "1"}), encoding="utf-8")
    with pytest.raises(ValueError):
        fb.load_exports([path])


def test_text_output_names_the_definitions(exported, tmp_path, capsys):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(exported()), encoding="utf-8")
    assert fb.main(["export", str(path), "--tz", "UTC"]) == 0
    out = capsys.readouterr().out
    assert "forecast - settled, c/kWh" in out
    assert "last solve published inside the interval" in out
    assert "No gap is filled" in out
    assert "in-interval" in out and "8-24 h" in out
