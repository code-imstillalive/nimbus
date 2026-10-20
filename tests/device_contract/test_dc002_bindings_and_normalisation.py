"""DC-002 acceptance (docs/specs/device-contract/002-bindings-and-normalisation.md,
"Acceptance"): the production resolver, `device_bindings.py`, run against the
DC-000 reference oracles, never against itself. Each test names the
requirement and fixture it covers.

Synthetic data only. The oracles (`oracles_signals.py`, `oracles_structure.py`)
import no production code, so agreement here is evidence, not a tautology.
"""

from __future__ import annotations

import ast
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import device_bindings as db
import price_intervals
import pytest

from tests.device_contract.oracles_signals import (
    PriceInterval,
    Row,
    UnitError,
    counter_delta,
    power_kw,
    price_over,
    price_with_fees,
    reading,
)
from tests.device_contract.oracles_structure import (
    dependency_problems,
    observed_grid_kw,
)

HERE = Path(__file__).parent
PKG = HERE.parents[1] / "custom_components" / "nimbus_load"
FIX = HERE / "fixtures"
TOL = 1e-9
T = datetime.fromisoformat


def _manifest(name: str) -> dict[str, Any]:
    return json.loads((FIX / name / "fixture.json").read_text(encoding="utf-8"))


F04 = _manifest("F04_price_intervals")
F09 = _manifest("F09_units_and_samples")
F11 = _manifest("F11_dependencies_and_readiness")
LEGACY_UNITS = [
    c["unit"]
    for c in json.loads(
        (HERE / "snapshots" / "legacy" / "F09_power_units.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
]
REG = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
DIVERGENCES = json.loads(
    (HERE / "contract_divergences.json").read_text(encoding="utf-8")
)["divergences"]

NOW = T("2026-10-05T17:00:00+10:00")


# --- a synthetic site and recording readers -------------------------------------


class Recorder:
    """Every read the resolver makes, in order. Nothing else can reach it."""

    def __init__(
        self,
        states: dict[str, Any] | None = None,
        statistics: dict[str, Any] | None = None,
        response: Any = None,
        models: dict[str, Any] | None = None,
    ) -> None:
        self.states = states or {}
        self.statistics = statistics or {}
        self.response = response
        self.models = models or {}
        self.calls: list[tuple[Any, ...]] = []

    def state(self, entity_id: str) -> Any:
        self.calls.append(("state", entity_id))
        return self.states.get(entity_id)

    def statistic(self, statistic_id: str, semantics: str) -> Any:
        self.calls.append(("statistic", statistic_id, semantics))
        return self.statistics.get(statistic_id)

    def action(self, action: str, target: Any, parameters: Any) -> Any:
        self.calls.append(("action", action, target, parameters))
        return self.response

    def model(self, model: str, target: str) -> Any:
        self.calls.append(("model", model, target))
        return self.models.get(model)

    def readers(self) -> db.Readers:
        return db.Readers(
            state=self.state,
            statistic=self.statistic,
            action_response=self.action,
            model_output=self.model,
        )


def _binding(source: dict[str, Any], **norm: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": source,
        "provenance": {"origin": "manual", "confirmed": True},
    }
    if norm:
        out["normalization"] = norm
    return out


def _entity(entity_id: str, **norm: Any) -> dict[str, Any]:
    return _binding({"kind": "entity_state", "entity_id": entity_id}, **norm)


def _attribute(
    entity_id: str, adapter: str, path: str = "forecast", **norm: Any
) -> dict[str, Any]:
    source = {
        "kind": "entity_attribute",
        "entity_id": entity_id,
        "attribute_path": path,
        "adapter": {"name": adapter, "version": 1},
    }
    return _binding(source, **norm)


def _doc(*devices: dict[str, Any], currency: str = "AUD") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "site": {
            "id": "site_home",
            "timezone": "Australia/Brisbane",
            "currency": currency,
        },
        "devices": list(devices),
    }


def _device(device_id: str, kind: str, **bindings: Any) -> dict[str, Any]:
    return {
        "id": device_id,
        "kind": kind,
        "boundary": {"id": f"{device_id}_boundary", "domain": "ac", "phases": "all"},
        "bindings": bindings,
    }


def _state(value: str, unit: Any = None, **times: Any) -> dict[str, Any]:
    attrs = {} if unit is None else {"unit_of_measurement": unit}
    return {"state": value, "attributes": attrs, **times}


def _resolve(doc: dict[str, Any], device: str, role: str, rec: Recorder, **kw: Any):
    return db.resolve(doc, device, role, rec.readers(), NOW, **kw)


def _power(value: str, unit: Any, **norm: Any) -> db.Resolved:
    doc = _doc(_device("grid_main", "grid", power=_entity("sensor.grid", **norm)))
    rec = Recorder({"sensor.grid": _state(value, unit)})
    return _resolve(doc, "grid_main", "power", rec)


def _oracle_kw(value: float, unit: Any) -> float | None:
    try:
        return power_kw(value, unit)
    except UnitError:
        return None


# --- provider-shaped rows from the fixture's neutral intervals --------------------


def _aemo(intervals: list[list[Any]]) -> list[dict[str, Any]]:
    return [{"start_time": a, "end_time": b, "price": p} for a, b, p in intervals]


def _amber_core(intervals: list[list[Any]]) -> list[dict[str, Any]]:
    """HA core Amber: start one second past the boundary, as Amber gives it."""
    rows = []
    for a, b, p in intervals:
        start, end = T(a), T(b)
        rows.append(
            {
                "start_time": (start + timedelta(seconds=1)).isoformat(),
                "end_time": end.isoformat(),
                "duration": int((end - start).total_seconds() // 60),
                "per_kwh": p,
            }
        )
    return rows


def _pd7day(intervals: list[list[Any]], tariff: bool = False) -> list[dict[str, Any]]:
    rows = []
    for a, b, p in intervals:
        row: dict[str, Any] = {"time": a, "nemtime": b, "value": p}
        if tariff:
            row["network_rate"] = 0.12
        rows.append(row)
    return rows


def _blocks(raw: list[list[Any]]) -> list[list[Any]]:
    out = []
    for first, minutes, count, price in raw:
        for k in range(count):
            a = T(first) + timedelta(minutes=minutes * k)
            out.append(
                [a.isoformat(), (a + timedelta(minutes=minutes)).isoformat(), price]
            )
    return out


def _oracle_intervals(raw: list[list[Any]]) -> list[PriceInterval]:
    return [PriceInterval(T(a), T(b), p) for a, b, p in raw]


def _price_series(
    rows: list[dict[str, Any]],
    adapter: str,
    window: tuple[datetime, datetime] | None = None,
    role: str = "import_price_forecast",
    fetched: datetime = NOW,
    **norm: Any,
) -> db.Resolved:
    path = "forecasts" if adapter == "amberelectric" else "forecast"
    doc = _doc(
        _device(
            "grid_main", "grid", **{role: _attribute("sensor.p", adapter, path, **norm)}
        )
    )
    rec = Recorder(
        {
            "sensor.p": {
                "state": "0.1",
                "attributes": {path: rows},
                "last_updated": fetched.isoformat(),
            }
        }
    )
    return _resolve(doc, "grid_main", role, rec, window=window)


# --- DC-R03 / F09: units, dimension, sign, provenance -----------------------------


def test_dc_r03_f09_the_legacy_record_has_the_21_unit_spellings() -> None:
    assert len(LEGACY_UNITS) == 21


@pytest.mark.parametrize("unit", LEGACY_UNITS)
def test_dc_r03_f09_each_unit_spelling_resolves_as_the_oracle_says(unit: Any) -> None:
    got = _power("2.5", unit)
    expected = _oracle_kw(2.5, unit)
    if expected is None:
        assert got.value is None and got.state == db.MISSING, got
        assert got.reason, got  # refused WITH a reason, never a guessed kW
    else:
        assert got.state == db.VALID and got.unit == "kW"
        assert math.isclose(got.value, expected, rel_tol=1e-12, abs_tol=TOL), got


@pytest.mark.parametrize(("value", "unit", "kw"), F09["cases"]["units"])
def test_dc_r03_f09_the_manifest_units(value: float, unit: str, kw: float) -> None:
    got = _power(str(value), unit)
    assert got.value is not None and abs(got.value - kw) <= TOL


def test_dc_r03_f09_milliwatt_and_megawatt_differ() -> None:
    mw_milli, mw_mega = _power("2.5", "mW").value, _power("2.5", "MW").value
    assert mw_milli is not None and mw_mega is not None
    assert math.isclose(mw_mega / mw_milli, 1e9)
    assert _oracle_kw(2.5, "mW") == mw_milli and _oracle_kw(2.5, "MW") == mw_mega


@pytest.mark.parametrize(
    ("unit", "reason"),
    [
        ("Wh", "energy_unit_as_power"),
        ("kWh", "energy_unit_as_power"),
        ("A", "unit_not_power"),
        ("kVA", "unit_not_power"),
        ("furlong", "unit_not_power"),
        ("kw", "unit_not_exact"),
        (None, "unit_missing"),
    ],
)
def test_dc_r03_f09_a_wrong_dimension_is_refused_with_a_reason(
    unit: Any, reason: str
) -> None:
    got = _power("2.5", unit)
    assert (got.value, got.state, got.reason) == (None, db.MISSING, reason)
    assert _oracle_kw(2.5, unit) is None


def test_dc_r03_power_is_refused_as_energy() -> None:
    doc = _doc(
        _device(
            "grid_main",
            "grid",
            import_energy=_binding({"kind": "constant", "value": 5, "unit": "kW"}),
        )
    )
    got = _resolve(doc, "grid_main", "import_energy", Recorder())
    assert (got.value, got.reason) == (None, "power_unit_as_energy")


def test_dc_r03_f09_a_declared_native_sign_is_flipped_exactly_once() -> None:
    flipped = _power(
        "1500", "W", positive_direction="import", native_positive_direction="export"
    )
    assert flipped.value == -1.5
    assert flipped.provenance["native_sign"] == "export"
    assert flipped.provenance["sign_flipped"] is True
    same = _power(
        "1500", "W", positive_direction="import", native_positive_direction="import"
    )
    assert same.value == 1.5 and same.provenance["sign_flipped"] is False
    # Resolving again reads the source again: never a second flip.
    again = _power(
        "1500", "W", positive_direction="import", native_positive_direction="export"
    )
    assert again.value == -1.5
    # A measured zero carries no sign either way.
    zero = _power(
        "0", "W", positive_direction="import", native_positive_direction="export"
    )
    assert zero.value == 0.0 and math.copysign(1.0, zero.value) == 1.0


def test_dc_r03_paired_import_and_export_resolve_separately_unnetted() -> None:
    doc = _doc(
        _device(
            "grid_main",
            "grid",
            import_power=_entity(
                "sensor.imp",
                positive_direction="import",
                native_positive_direction="import",
            ),
            export_power=_entity(
                "sensor.exp",
                positive_direction="export",
                native_positive_direction="export",
            ),
        )
    )
    rec = Recorder({"sensor.imp": _state("2000", "W"), "sensor.exp": _state("3", "kW")})
    imp = _resolve(doc, "grid_main", "import_power", rec)
    exp = _resolve(doc, "grid_main", "export_power", rec)
    assert (imp.value, exp.value) == (2.0, 3.0)


def test_dc_r03_every_value_states_its_unit_sign_boundary_and_source() -> None:
    q = _power(
        "1500", "W", positive_direction="import", native_positive_direction="import"
    )
    assert q.unit == "kW"
    p = q.provenance
    assert p["boundary"] == "grid_main_boundary"
    assert (p["source_kind"], p["entity_id"], p["native_unit"]) == (
        "entity_state",
        "sensor.grid",
        "W",
    )
    assert (p["origin"], p["confirmed"], p["canonical_sign"]) == (
        "manual",
        True,
        "import",
    )
    s = _price_series(_aemo(F04["cases"]["gap"]["intervals"]), "aemo_nem")
    assert s.unit == "AUD/kWh"
    assert s.provenance["adapter"] == {"name": "aemo_nem", "version": 1}
    assert s.provenance["boundary"] == "grid_main_boundary"


def test_dc_r03_model_output_carries_its_coverage_and_training_state() -> None:
    doc = _doc(
        _device(
            "load_house",
            "load",
            demand_forecast=_binding(
                {
                    "kind": "model_output",
                    "model": "nimbus_load",
                    "target": "load_house.power",
                }
            ),
        )
    )
    hole = F04["cases"]["hole"]
    rec = Recorder(
        models={
            "nimbus_load": {
                "unit": "kW",
                "training_state": "training",
                "intervals": hole["intervals"],
            }
        }
    )
    window = tuple(map(T, hole["window"]))
    got = _resolve(doc, "load_house", "demand_forecast", rec, window=window)
    assert isinstance(got, db.Series)
    assert got.provenance["training_state"] == "training"
    assert (
        abs(got.coverage.covered_fraction - F04["expected"]["hole"]["coverage"]) <= TOL
    )


# --- DC-R04 / F09: the five states ---------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), F09["cases"]["states"])
def test_dc_r04_f09_missing_unavailable_and_zero_stay_distinct(
    raw: str, expected: str | None
) -> None:
    got = _power(raw, "kW")
    oracle = reading(raw)
    assert got.value == oracle
    assert oracle is None if expected is None else oracle == float(expected)
    if raw in ("unavailable", "unknown"):
        assert got.state == db.UNAVAILABLE
    elif oracle is None:
        assert got.state == db.MISSING  # nan, inf
    else:
        assert got.state == db.VALID and got.value == 0.0  # a recorded "0"


def test_dc_r04_a_missing_entity_or_unbound_role_is_missing_never_zero() -> None:
    doc = _doc(_device("grid_main", "grid", power=_entity("sensor.gone")))
    got = _resolve(doc, "grid_main", "power", Recorder())
    assert (got.value, got.state, got.reason) == (None, db.MISSING, "no_entity")
    unbound = _resolve(doc, "grid_main", "import_power", Recorder())
    assert (unbound.value, unbound.state, unbound.reason) == (
        None,
        db.MISSING,
        "unbound",
    )


def _breaker(rule: dict[str, Any], **times: Any) -> db.Resolved:
    doc = _doc(
        _device("load_pool", "load", power=_entity("sensor.pool", freshness=rule))
    )
    rec = Recorder({"sensor.pool": _state("0", "W", **times)})
    return _resolve(doc, "load_pool", "power", rec)


def test_dc_r04_f09_a_breaker_idle_at_zero_overnight_is_held_not_stale() -> None:
    nine_hours_ago = (NOW - timedelta(hours=9)).isoformat()
    held = _breaker(
        {"mode": "change_only", "max_interval_s": 900},
        last_changed=nine_hours_ago,
        last_reported=nine_hours_ago,
    )
    assert (held.state, held.value) == (db.HELD, 0.0)
    assert held.value == reading("0")
    # The same reading under a sampled rule IS stale: the rule is per source.
    stale = _breaker(
        {"mode": "sampled", "max_interval_s": 900},
        last_changed=nine_hours_ago,
        last_reported=nine_hours_ago,
    )
    assert (stale.state, stale.value) == (db.STALE, 0.0)  # carried, flagged


def test_dc_r04_freshness_reads_the_live_last_reported_not_last_changed() -> None:
    """#1705: an unchanged value re-reported 30 s ago is fresh, whatever its
    last change."""
    got = _breaker(
        {"mode": "sampled", "max_interval_s": 300},
        last_changed=(NOW - timedelta(hours=2)).isoformat(),
        last_reported=(NOW - timedelta(seconds=30)).isoformat(),
    )
    assert got.state == db.VALID


def test_dc_r04_f04_a_forecast_past_its_refresh_period_is_stale() -> None:
    rows = _aemo(F04["cases"]["gap"]["intervals"])
    old = _price_series(rows, "aemo_nem", fetched=NOW - timedelta(seconds=301))
    fresh = _price_series(rows, "aemo_nem", fetched=NOW - timedelta(seconds=299))
    assert old.state == db.STALE and fresh.state == db.VALID
    assert old.intervals == fresh.intervals  # carried, flagged, not nulled


def test_dc_r04_every_price_adapter_is_stale_after_five_minutes() -> None:
    """Mark Purcell, #1774: "nothing should be more than 5 minutes, as
    default". No adapter is left undeclared."""
    assert {a.max_age_s for a in db.ADAPTERS.values()} == {300.0}
    assert {name for name, _ in db.ADAPTERS} == {
        "aemo_nem",
        "nem_pd7day",
        "amber_express",
        "amberelectric",
        "openadr3_ven",
        "localvolts_v2",
    }


def test_dc_r04_a_binding_may_declare_its_own_max_age() -> None:
    rows = _aemo(F04["cases"]["gap"]["intervals"])
    ten_min = NOW - timedelta(minutes=10)
    default = _price_series(rows, "aemo_nem", fetched=ten_min)
    longer = _price_series(
        rows, "aemo_nem", fetched=ten_min, freshness={"max_age_s": 900}
    )
    assert (default.state, longer.state) == (db.STALE, db.VALID)
    assert longer.provenance["freshness"]["max_age_s"] == 900.0


# --- DC-R06 / F04: intervals and coverage -------------------------------------------

_SHAPES = {
    "thirty_min_window_over_5_min": ("amberelectric", _amber_core),
    "thirty_min_series": ("aemo_nem", _aemo),
    "gap": ("aemo_nem", _aemo),
    "hole": ("aemo_nem", _aemo),
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_dc_r06_f04_5_and_30_minute_series_resolve_to_the_oracles_intervals(
    name: str,
) -> None:
    case, expected = F04["cases"][name], F04["expected"][name]
    adapter, shape = _SHAPES[name]
    window = tuple(map(T, case["window"]))
    series = _price_series(shape(case["intervals"]), adapter, window)
    assert isinstance(series, db.Series) and series.state == db.VALID
    assert [(i.start, i.end, i.value) for i in series.intervals] == [
        (i.start, i.end, i.price) for i in _oracle_intervals(case["intervals"])
    ]
    price, fraction = db.value_over(series, *window)
    o_price, o_fraction = price_over(_oracle_intervals(case["intervals"]), *window)
    assert price is not None and o_price is not None
    assert abs(price - o_price) <= TOL and abs(price - expected["price"]) <= TOL
    assert abs(fraction - o_fraction) <= TOL
    assert abs(series.coverage.covered_fraction - expected["coverage"]) <= TOL


def test_dc_r06_f04_a_multi_day_series_reports_its_24_hour_hole() -> None:
    case, expected = F04["cases"]["multi_day"], F04["expected"]["multi_day"]
    raw = _blocks(case["blocks"])
    window = tuple(map(T, case["window"]))
    series = _price_series(_pd7day(raw), "nem_pd7day", window)
    assert len(series.intervals) == 96
    price, _ = db.value_over(series, *window)
    o_price, o_fraction = price_over(_oracle_intervals(raw), *window)
    assert price is not None and abs(price - o_price) <= TOL
    assert abs(series.coverage.covered_fraction - o_fraction) <= TOL
    assert abs(o_fraction - expected["coverage"]) <= TOL
    assert [list(map(T, g)) for g in case["gaps"]] == [
        list(g) for g in series.coverage.gaps
    ]


def test_dc_r06_f04_coverage_over_a_window_with_a_hole_reports_the_hole() -> None:
    case = F04["cases"]["hole"]
    window = tuple(map(T, case["window"]))
    series = _price_series(_aemo(case["intervals"]), "aemo_nem", window)
    assert [list(g) for g in series.coverage.gaps] == [
        list(map(T, g)) for g in case["gaps"]
    ]
    # Inside the hole there is no price at all: nothing was filled.
    hole = tuple(map(T, case["gaps"][0]))
    assert db.value_over(series, *hole) == (None, 0.0)
    assert price_over(_oracle_intervals(case["intervals"]), *hole) == (None, 0.0)


def test_dc_r06_f04_overlap_is_refused() -> None:
    case = F04["cases"]["overlap"]
    assert F04["expected"]["overlap"] == "refused"
    got = _price_series(_aemo(case["intervals"]), "aemo_nem")
    assert (got.state, got.reason) == (db.MISSING, "overlapping_intervals")
    with pytest.raises(ValueError, match="overlapping"):
        price_over(_oracle_intervals(case["intervals"]), *map(T, case["window"]))


def test_dc_r06_f04_an_expired_price_is_not_extended() -> None:
    case = F04["cases"]["gap"]
    window = tuple(map(T, case["window"]))
    series = _price_series(_aemo(case["intervals"]), "aemo_nem", window)
    last_end = series.intervals[-1].end
    assert series.coverage.gaps == ((last_end, window[1]),)
    after = (last_end, window[1])
    assert db.value_over(series, *after) == (None, 0.0)
    assert price_over(_oracle_intervals(case["intervals"]), *after) == (None, 0.0)


def test_dc_r06_f04_a_negative_price_survives() -> None:
    case = F04["cases"]["thirty_min_window_over_5_min"]
    series = _price_series(_amber_core(case["intervals"]), "amberelectric")
    negative = [i for i in series.intervals if i.value < 0]
    assert [i.value for i in negative] == [-0.06]
    # The UTC-stamped row is the same instant as its +10:00 neighbours'.
    assert negative[0].start == T("2026-10-05T17:10:00+10:00")
    o_price, _ = price_over(
        [PriceInterval(negative[0].start, negative[0].end, -0.06)],
        negative[0].start,
        negative[0].end,
    )
    assert db.value_over(series, negative[0].start, negative[0].end)[0] == o_price


def test_dc_r06_f04_a_feed_in_only_provider_yields_no_import_series() -> None:
    case, expected = F04["cases"]["feed_in_only"], F04["expected"]["feed_in_only"]
    window = tuple(map(T, case["window"]))
    doc = _doc(
        _device(
            "grid_main",
            "grid",
            export_price_forecast=_attribute("sensor.feed_in", "aemo_nem"),
        )
    )
    rec = Recorder(
        {
            "sensor.feed_in": {
                "state": "0.08",
                "attributes": {"forecast": _aemo(case["export_intervals"])},
                "last_updated": NOW.isoformat(),
            }
        }
    )
    imp = _resolve(doc, "grid_main", "import_price_forecast", rec, window=window)
    exp = _resolve(doc, "grid_main", "export_price_forecast", rec, window=window)
    assert isinstance(imp, db.Quantity) and imp.value is None
    assert (imp.state, imp.reason) == (db.MISSING, "unbound")
    assert imp.value is expected["import"]
    assert db.value_over(exp, *window)[0] == expected["export"]


def test_dc_r06_nothing_published_is_missing_not_zero() -> None:
    case = F04["cases"]["nothing_published"]
    got = _price_series(_aemo(case["intervals"]), "aemo_nem")
    assert (got.state, got.reason) == (db.MISSING, "no_rows")
    assert price_over([], *map(T, case["window"])) == (None, 0.0)


# --- DC-R05 / F03, F04: tariff roles, fees, actions, statistics --------------------


def test_dc_r05_f04_fees_are_added_once_by_the_consumer_never_the_resolver() -> None:
    case, expected = F04["cases"]["fees"], F04["expected"]["fees"]
    one = [["2026-10-05T17:00:00+10:00", "2026-10-05T17:30:00+10:00", case["spot"]]]
    window = (T(one[0][0]), T(one[0][1]))
    wholesale = _price_series(_aemo(one), "aemo_nem", window)
    tariff = _price_series(_pd7day(one, tariff=True), "nem_pd7day", window)
    assert (wholesale.fees_included, tariff.fees_included) == (False, True)
    for series in (wholesale, tariff):  # the resolver adds nothing
        assert [i.value for i in series.intervals] == [case["spot"]]
    paid = {
        s.fees_included: price_with_fees(
            db.value_over(s, *window)[0], case["fees"], bool(s.fees_included)
        )
        for s in (wholesale, tariff)
    }
    assert abs(paid[False] - expected["fees_not_included"]) <= TOL
    assert paid[True] == expected["fees_included"]
    # A binding can declare it when the provider does not say.
    declared = _price_series(_aemo(one), "aemo_nem", window, fees_included=True)
    assert declared.fees_included is True


def _p2p_doc(quality: str, role: str = "export_price") -> dict[str, Any]:
    return _doc(
        _device(
            "grid_main",
            "grid",
            export_price_forecast=_attribute("sensor.fcst", "aemo_nem"),
            **{role: _attribute("sensor.p2p", "aemo_nem", quality=quality)},
        )
    )


def test_dc_r05_f04_forecast_matched_and_settled_are_separate_values() -> None:
    one = [["2026-10-05T17:00:00+10:00", "2026-10-05T17:30:00+10:00", 0.09]]
    two = [["2026-10-05T17:00:00+10:00", "2026-10-05T17:30:00+10:00", 0.14]]
    rec = Recorder(
        {
            "sensor.fcst": {"state": "0.09", "attributes": {"forecast": _aemo(one)}},
            "sensor.p2p": {"state": "0.14", "attributes": {"forecast": _aemo(two)}},
        }
    )
    doc = _p2p_doc("matched")
    forecast = _resolve(doc, "grid_main", "export_price_forecast", rec)
    matched = _resolve(doc, "grid_main", "export_price", rec)
    assert [i.quality for i in forecast.intervals] == ["forecast"]
    assert [i.quality for i in matched.intervals] == ["matched"]
    assert forecast.intervals[0].value != matched.intervals[0].value
    settled = _resolve(_p2p_doc("settled"), "grid_main", "export_price", rec)
    assert [i.quality for i in settled.intervals] == ["settled"]
    # A settlement row is never read as a forecast.
    wrong = _resolve(
        _p2p_doc("settled", role="import_price_forecast"),
        "grid_main",
        "import_price_forecast",
        rec,
    )
    assert (wrong.state, wrong.reason) == (db.MISSING, "settlement_is_not_a_forecast")


def test_dc_r05_f04_an_empty_matched_feed_reading_unknown_is_unavailable() -> None:
    rec = Recorder({"sensor.p2p": {"state": "unknown", "attributes": {"forecast": []}}})
    got = _resolve(_p2p_doc("matched"), "grid_main", "export_price", rec)
    assert (got.state, got.value) == (db.UNAVAILABLE, None)  # not a zero price


def _action_doc(action: str, **norm: Any) -> dict[str, Any]:
    source = {
        "kind": "action_response",
        "action": action,
        "target": "sensor.openadr_price",
        "response_path": "sensor.openadr_price.forecast",
        "adapter": {"name": "openadr3_ven", "version": 1},
    }
    return _doc(
        _device("grid_main", "grid", import_price_forecast=_binding(source, **norm))
    )


_OPENADR = {
    "sensor.openadr_price": {
        "payload_type": "PRICE",
        "unit": "$/kWh",
        "forecast": [
            {"datetime": "2026-10-05T07:00:00Z", "value": 0.31, "interval_minutes": 30}
        ],
    }
}


@pytest.mark.parametrize(
    "action", ["switch.turn_on", "script.turn_on", "openadr3_ven.set_opt_out", ""]
)
def test_dc_r05_f03_an_action_not_on_the_read_only_list_is_refused_and_never_called(
    action: str,
) -> None:
    rec = Recorder(response=_OPENADR)
    got = _resolve(_action_doc(action), "grid_main", "import_price_forecast", rec)
    assert (got.state, got.reason) == (db.MISSING, "action_not_read_only")
    assert rec.calls == []  # zero service calls, zero reads of any kind


def test_dc_r05_f03_an_allowed_read_only_action_is_called_once_and_read() -> None:
    assert db.READ_ONLY_ACTIONS == {
        "openadr3_ven.get_forecast",
        "amberelectric.get_forecasts",
    }
    rec = Recorder(response=_OPENADR)
    doc = _action_doc("openadr3_ven.get_forecast", native_unit="AUD/kWh")
    got = _resolve(doc, "grid_main", "import_price_forecast", rec)
    assert [c[0] for c in rec.calls] == ["action"]
    assert [(i.start, i.end, i.value) for i in got.intervals] == [
        (T("2026-10-05T17:00:00+10:00"), T("2026-10-05T17:30:00+10:00"), 0.31)
    ]
    # The response block states its unit; with none, the price is refused,
    # never assumed.
    unitless = {"sensor.openadr_price": {**_OPENADR["sensor.openadr_price"]}}
    del unitless["sensor.openadr_price"]["unit"]
    bare = _resolve(
        _action_doc("openadr3_ven.get_forecast"),
        "grid_main",
        "import_price_forecast",
        Recorder(response=unitless),
    )
    assert bare.reason == "unit_undeclared"


def test_dc_r05_f03_openadr_rows_are_never_read_from_its_summary_attributes() -> None:
    doc = _doc(
        _device(
            "grid_main",
            "grid",
            import_price_forecast=_attribute("sensor.openadr_price", "openadr3_ven"),
        )
    )
    rec = Recorder({"sensor.openadr_price": {"state": "0.31", "attributes": {}}})
    got = _resolve(doc, "grid_main", "import_price_forecast", rec)
    assert got.reason == "rows_only_in_action_response"
    assert rec.calls == []


def test_dc_r05_f03_f09_a_statistic_without_an_entity_resolves_as_energy() -> None:
    rows = [Row(T(at), s) for at, s in F09["cases"]["edge_reading"]["rows"]]
    oracle_kwh = counter_delta(
        rows, T("2026-10-05T09:00:00+10:00"), T("2026-10-05T10:00:00+10:00")
    )
    assert oracle_kwh == F09["expected"]["edge_reading_kwh"]["09-10"]
    source = {
        "kind": "recorder_statistic",
        "statistic_id": "sensor.gone_grid_import",
        "interval_semantics": "hourly_change",
    }
    doc = _doc(_device("grid_main", "grid", import_energy=_binding(source)))
    stats = {
        "sensor.gone_grid_import": {
            "value": 3000.0,
            "unit": "Wh",
            "start": "2026-10-05T09:00:00+10:00",
            "end": "2026-10-05T10:00:00+10:00",
        }
    }
    rec = Recorder(statistics=stats)  # no entity exists for it
    got = _resolve(doc, "grid_main", "import_energy", rec)
    assert (got.state, got.unit) == (db.VALID, "kWh")
    assert got.value is not None and abs(got.value - oracle_kwh) <= TOL
    assert [c[0] for c in rec.calls] == ["statistic"]


@pytest.mark.parametrize(
    ("unit", "value", "expected", "reason"),
    [
        ("$/kWh", "0.265", 0.265, None),
        ("c/kWh", "26.5", 0.265, None),
        ("¢/kWh", "26.5", 0.265, None),
        ("AUD/kWh", "0.265", 0.265, None),
        ("$/MWh", "265", 0.265, None),
        ("AUD/MWh", "265", 0.265, None),
        ("USD/kWh", "0.265", None, "currency_mismatch"),
        ("€/kWh", "0.265", None, "currency_mismatch"),
        ("kW", "0.265", None, "unit_not_price"),
        # Exactly $/kWh, $/MWh and c/kWh (Mark Purcell, #1774): nothing else.
        ("c/MWh", "26500", None, "unit_not_price"),
        ("$/kwh", "0.265", None, "unit_not_price"),
        ("$", "0.265", None, "unit_not_price"),
        ("", "0.265", None, "unit_missing"),
        (None, "0.265", None, "unit_missing"),
    ],
)
def test_dc_r05_a_price_is_scaled_once_and_another_currency_refused(
    unit: Any, value: str, expected: float | None, reason: str | None
) -> None:
    doc = _doc(_device("grid_main", "grid", import_price=_entity("sensor.buy")))
    got = _resolve(
        doc, "grid_main", "import_price", Recorder({"sensor.buy": _state(value, unit)})
    )
    if expected is None:
        assert (got.value, got.reason) == (None, reason)
    else:
        assert got.value is not None and abs(got.value - expected) <= TOL
        assert got.unit == "AUD/kWh"


def test_dc_r05_a_price_with_no_site_currency_is_refused_not_labelled() -> None:
    doc = _doc(_device("grid_main", "grid", import_price=_entity("sensor.buy")))
    doc["site"].pop("currency")
    rec = Recorder({"sensor.buy": _state("26.5", "c/kWh")})
    got = _resolve(doc, "grid_main", "import_price", rec)
    assert (got.value, got.unit, got.reason) == (None, None, "site_currency_missing")


# --- F03: the providers' own response shapes (Mark Purcell's capture, #1774) ----------

F03 = _manifest("F03_provider_responses")


def _triples(raw: list[list[Any]]) -> list[tuple[datetime, datetime, float]]:
    return [(T(a), T(b), v) for a, b, v, *_ in raw]


def _got(series: db.Resolved) -> list[tuple[datetime, datetime, float]]:
    assert isinstance(series, db.Series), series
    return [(i.start, i.end, round(i.value, 12)) for i in series.intervals]


def _action_binding(action: str, target: str, path: str, adapter: str) -> Any:
    source = {
        "kind": "action_response",
        "action": action,
        "target": target,
        "response_path": path,
        "adapter": {"name": adapter, "version": 1},
    }
    return _binding(source)


def test_dc_r05_f03_amber_get_forecasts_gives_its_published_intervals() -> None:
    """Shape validated against Mark's pasted response, not a live call: the
    interval starts on the boundary, one second before Amber's start_time,
    and per_kwh is the price (never advanced_price_predicted)."""
    case = F03["cases"]["amber_get_forecasts"]
    expected = F03["expected"]["amber_get_forecasts"]
    binding = _action_binding(
        "amberelectric.get_forecasts",
        "sensor.general_price",
        "forecasts",
        "amberelectric",
    )
    doc = _doc(_device("grid_main", "grid", import_price_forecast=binding))
    window = tuple(map(T, case["window"]))
    rec = Recorder(response=case["response"])
    got = _resolve(doc, "grid_main", "import_price_forecast", rec, window=window)
    assert _got(got) == _triples(expected["intervals"])
    price, fraction = db.value_over(got, *window)
    assert price is not None and abs(price - expected["price"]) <= TOL
    assert abs(fraction - expected["coverage"]) <= TOL
    assert [c[0] for c in rec.calls] == ["action"]


def test_dc_r05_f03_openadr_get_forecast_gives_variable_length_intervals() -> None:
    """Shape validated against Mark's pasted response: interval_minutes of 5,
    20 and 30, the block's own `$/kWh` unit, and a final row with no value
    dropped rather than extended."""
    case = F03["cases"]["openadr_get_forecast"]
    expected = F03["expected"]["openadr_get_forecast"]
    binding = _action_binding(
        "openadr3_ven.get_forecast", case["target"], "", "openadr3_ven"
    )
    doc = _doc(_device("grid_main", "grid", export_price_forecast=binding))
    window = tuple(map(T, case["window"]))
    rec = Recorder(response=case["response"])
    got = _resolve(doc, "grid_main", "export_price_forecast", rec, window=window)
    assert _got(got) == _triples(expected["intervals"])
    assert [round((i.end - i.start).total_seconds() / 60) for i in got.intervals] == [
        5,
        20,
        30,
        30,
    ]
    assert got.provenance["native_unit"] == "$/kWh"
    price, _ = db.value_over(got, *window)
    assert price is not None and abs(price - expected["price"]) <= TOL
    longer = _resolve(
        doc,
        "grid_main",
        "export_price_forecast",
        Recorder(response=case["response"]),
        window=tuple(map(T, case["past_the_last_row"])),
    )
    assert (
        abs(longer.coverage.covered_fraction - expected["past_the_last_row_coverage"])
        <= TOL
    )


def _lv_binding(field: str, direction: str | None, **norm: Any) -> Any:
    source = {
        "kind": "entity_attribute",
        "entity_id": "sensor.localvolts",
        "attribute_path": "forecast",
        "adapter": {
            "name": "localvolts_v2",
            "version": 1,
            "field": field,
            "direction": direction,
        },
    }
    return _binding(source, **norm)


def _lv_resolve(role: str, binding: Any) -> db.Resolved:
    doc = _doc(_device("grid_main", "grid", **{role: binding}))
    rows = F03["cases"]["localvolts_v2"]["rows"]
    rec = Recorder(
        {
            "sensor.localvolts": {
                "state": "31.5",
                "attributes": {"forecast": rows},
                "last_updated": NOW.isoformat(),
            }
        }
    )
    return db.resolve(doc, "grid_main", role, rec.readers(), NOW)


def test_dc_r05_f03_localvolts_v2_directions_pair_on_interval_end() -> None:
    """One row per direction, paired on intervalEnd; rates in cents scaled
    once; elapsed (Act) rows are not a forecast."""
    expected = F03["expected"]["localvolts_v2"]
    buy = _lv_resolve("import_price_forecast", _lv_binding("flexUp", "Buy"))
    sell = _lv_resolve("export_price_forecast", _lv_binding("flexUp", "Sell"))
    assert _got(buy) == _triples(expected["buy_flexup"])
    assert _got(sell) == _triples(expected["sell_flexup"])
    assert [(i.start, i.end) for i in buy.intervals] == [
        (i.start, i.end) for i in sell.intervals
    ]
    assert buy.provenance["native_unit"] == "c/kWh"
    assert (buy.fees_included, sell.fees_included) == (True, True)
    settled = _lv_resolve(
        "export_price", _lv_binding("flexUp", "Sell", quality="settled")
    )
    assert _got(settled) == _triples(expected["sell_settled_flexup"])


def test_dc_r05_f03_a_matched_p2p_rate_prices_a_blocks_export() -> None:
    """Mark Purcell, #1774: P2P rates in terms of the current block export.
    The matched rate is an export-side series; a no-match interval is kept
    and tagged, so a block's export is priced at the match only where there
    was one."""
    expected = F03["expected"]["localvolts_v2"]
    matched = _lv_resolve(
        "export_price", _lv_binding("matched_rate", "Sell", quality="matched")
    )
    assert isinstance(matched, db.Series) and matched.unit == "AUD/kWh"
    assert [
        (i.start, i.end, round(i.value, 12), i.quality) for i in matched.intervals
    ] == [(T(a), T(b), v, q) for a, b, v, q in expected["sell_matched"]]
    assert matched.fees_included is None  # a peer's price: not stated
    block = F03["cases"]["localvolts_v2"]["block"]
    revenue = sum(
        block["export_kw"] * (i.end - i.start).total_seconds() / 3600 * i.value
        for i in matched.intervals
        if i.quality == "matched"
    )
    assert abs(revenue - expected["block_matched_revenue"]) <= TOL
    wrong_side = _lv_resolve(
        "import_price", _lv_binding("matched_rate", "Sell", quality="matched")
    )
    assert wrong_side.reason == "matched_rate_is_export_side"


def test_dc_r06_f03_localvolts_without_a_direction_overlaps_and_is_refused() -> None:
    got = _lv_resolve("import_price_forecast", _lv_binding("flexUp", None))
    assert (got.state, got.reason) == (db.MISSING, "overlapping_intervals")
    unknown = _lv_resolve("import_price_forecast", _lv_binding("costsFlexUp", "Buy"))
    assert unknown.reason == "adapter_field_unknown"


def test_dc_r05_f03_the_matched_rate_agrees_with_the_solvers_own_reader() -> None:
    """Reuse, pinned: the pure reader's matched rate is the arithmetic
    solver_writer's `_p2p_triple_intervals` (#1537) applies, row for row."""
    import solver_writer

    rows = [
        r
        for r in F03["cases"]["localvolts_v2"]["rows"]
        if r["direction"] == "Sell" and r["quality"] == "Fcst"
    ]
    ours = price_intervals.localvolts_v2_rows(rows, price_intervals.LV_MATCHED_RATE)
    theirs = solver_writer._p2p_triple_intervals(rows)
    assert [
        (i.interval.start, i.interval.end, i.matched, i.interval.value) for i in ours
    ] == [
        (start, end, state == solver_writer.P2P_RATE, value)
        for start, end, state, value in theirs
    ]


# --- DC-R16 / F11: derived dependencies at run time ---------------------------------

# F11's abstract signal names, bound to real roles on a synthetic site.
_ROLE_OF = {
    "a": ("grid_main", "import_energy"),
    "b": ("grid_main", "export_energy"),
    "c": ("load_house", "energy"),
}


def _graph_doc(graph: dict[str, list[str]]) -> dict[str, Any]:
    devices: dict[str, dict[str, Any]] = {
        "grid_main": _device("grid_main", "grid"),
        "load_house": _device("load_house", "load"),
    }
    for name, deps in graph.items():
        device, role = _ROLE_OF[name]
        source = {
            "kind": "derived",
            "calculation": "sum",
            "depends_on": [".".join(_ROLE_OF[d]) for d in deps],
        }
        devices[device]["bindings"][role] = _binding(source)
    return _doc(*devices.values())


@pytest.mark.parametrize("name", ["self", "cycle"])
def test_dc_r16_f11_a_run_time_cycle_is_refused(name: str) -> None:
    graph = F11["cases"][name]
    assert any(
        "cycle" in p or "itself" in p
        for p in dependency_problems({k: set(v) for k, v in graph.items()})
    )
    first = next(iter(graph))
    rec = Recorder()
    got = _resolve(_graph_doc(graph), *_ROLE_OF[first], rec)
    assert got.value is None and got.state == db.MISSING
    assert got.reason is not None and got.reason.startswith("dependency_cycle"), got


def _balance_doc() -> dict[str, Any]:
    grid_power = _binding(
        {
            "kind": "derived",
            "calculation": "difference",
            "depends_on": ["load_house.power", "solar_roof.power"],
        }
    )
    return _doc(
        _device("grid_main", "grid", power=grid_power),
        _device("load_house", "load", power=_entity("sensor.house")),
        _device("solar_roof", "solar", power=_entity("sensor.pv")),
    )


def test_dc_r16_f01_an_acyclic_derived_value_matches_the_oracle() -> None:
    rec = Recorder(
        {"sensor.house": _state("14", "kW"), "sensor.pv": _state("16000", "W")}
    )
    got = _resolve(_balance_doc(), "grid_main", "power", rec)
    expected = observed_grid_kw(None, [("load", 14.0), ("solar", 16.0)])
    assert got.state == db.VALID and got.unit == "kW"
    assert got.value is not None and expected is not None
    assert abs(got.value - expected) <= TOL


def test_dc_r16_f01_a_missing_dependency_makes_the_result_missing() -> None:
    rec = Recorder(
        {"sensor.house": _state("14", "kW"), "sensor.pv": _state("unavailable")}
    )
    got = _resolve(_balance_doc(), "grid_main", "power", rec)
    assert observed_grid_kw(None, [("load", 14.0), ("solar", None)]) is None
    assert (got.value, got.state) == (None, db.MISSING)
    assert got.reason == "dependency_missing: solar_roof.power"  # not a partial sum


@pytest.mark.parametrize(
    ("pv_mode", "expected"),
    [("change_only", db.VALID), ("sampled", db.STALE)],
    ids=["held_dependency_stays_current", "stale_dependency_propagates"],
)
def test_dc_r16_a_held_dependency_is_current_a_stale_one_propagates(
    pv_mode: str, expected: str
) -> None:
    """Mark's question on #1774: `held` is current by the source's own rules,
    so a derived value built on it is as current as one built on `valid`
    inputs; only `stale` carries through to the result."""
    nine_hours_ago = (NOW - timedelta(hours=9)).isoformat()
    doc = _balance_doc()
    doc["devices"][2]["bindings"]["power"] = _entity(
        "sensor.pv", freshness={"mode": pv_mode, "max_interval_s": 900}
    )
    rec = Recorder(
        {
            "sensor.house": _state("14", "kW"),
            "sensor.pv": _state(
                "0", "W", last_changed=nine_hours_ago, last_reported=nine_hours_ago
            ),
        }
    )
    pv = _resolve(doc, "solar_roof", "power", rec)
    assert pv.state == (db.HELD if pv_mode == "change_only" else db.STALE)
    got = _resolve(doc, "grid_main", "power", rec)
    assert (got.state, got.value) == (expected, 14.0)


# --- the divergence ledger for F04 (legacy price_intervals vs the oracle) ------------


def _f04_legacy_divergences() -> dict[str, str]:
    """What today's price_intervals.py does where the oracle refuses."""
    found: dict[str, str] = {}
    overlap = price_intervals.aemo_nem_intervals(
        _aemo(F04["cases"]["overlap"]["intervals"])
    )
    if len(overlap) == 1:
        found["overlap"] = "first_row_kept"
    gap = price_intervals.aemo_nem_intervals(_aemo(F04["cases"]["gap"]["intervals"]))
    values, real = price_intervals.on_grid(gap, [T("2026-10-05T17:45:00+10:00")])
    if values[0] == gap[-1].value and not real[0]:
        found["expired_price"] = "held"
    # Mark Purcell, #1774: a price is $/kWh, $/MWh or c/kWh. The legacy
    # scale reads a unitless price as $/kWh and scales c/MWh; the resolver
    # refuses both (test_dc_r05_a_price_is_scaled_once_...).
    if price_intervals.price_unit_scale(None) == 1.0:
        found["price_unit_missing"] = "read_as_dollars_per_kwh"
    if price_intervals.price_unit_scale("c/MWh") == 1e-05:
        found["c_per_mwh"] = "scaled"
    return found


def test_f04_every_divergence_is_declared_and_still_real() -> None:
    declared = {d["case"]: d["legacy"] for d in DIVERGENCES if d["fixture"] == "F04"}
    assert _f04_legacy_divergences() == declared
    for d in DIVERGENCES:
        if d["fixture"] == "F04":
            assert d["owner"] == "DC-002" and d["class"] == "open_decision", d


# --- boundaries of the module itself ---------------------------------------------------


def test_no_consumer_reads_the_bindings_resolver_yet() -> None:
    """Until DC-006 (DC-002 "Acceptance", pinned as in DC-001). One reader is
    not a consumer: DC-005's confirmation resolves a switched binding only
    to refuse one DC-002 cannot read ("a source switch is validated before
    it is committed"); no value it resolves drives anything."""
    validators = {"device_contract_ha.py"}
    offenders = [
        str(p.relative_to(PKG))
        for p in PKG.rglob("*.py")
        if p.name != "device_bindings.py"
        and p.name not in validators
        and "device_bindings" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_resolver_cannot_call_a_service_or_reach_home_assistant() -> None:
    """Its only reads are the injected ones; it holds no `hass` and calls no
    service (DC-R15), and it imports nothing from Home Assistant."""
    tree = ast.parse((PKG / "device_bindings.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "hass" not in names
    assert not {"async_call", "call_service", "services"} & attrs
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert not any(m.startswith("homeassistant") for m in imported), imported


def test_every_dc002_requirement_has_a_named_test() -> None:
    owned = {r["id"] for r in REG["requirements"] if "DC-002" in r["owning_specs"]}
    assert owned == {"DC-R03", "DC-R04", "DC-R05", "DC-R06", "DC-R16"}
    named = {
        f"DC-R{m.group(1)}"
        for name in globals()
        if (m := re.match(r"test_dc_r(\d\d)_", name))
    }
    assert owned <= named, owned - named
    assert named <= owned, named - owned


# --- a declared power unit (DC-006 observation slice) ---------------------------


@pytest.mark.parametrize(
    ("unit", "declared", "kw"),
    [
        (None, "W", 0.0025),
        ("", "kW", 2.5),
        ("kw", "kW", 2.5),
        (" kW ", "kW", 2.5),
        ("w", "W", 0.0025),
        ("KW", "W", 0.0025),
    ],
)
def test_dc_r03_f09_a_declared_unit_settles_a_missing_or_misspelt_one(
    unit: Any, declared: str, kw: float
) -> None:
    """Household, 19 Oct 2026 (#1893): a unitless or misspelt power sensor is
    refused by the contract, and the household may declare W or kW for it in
    a Repair rather than fix the sensor."""
    got = _power("2.5", unit, native_unit=declared)
    assert got.state == db.VALID
    assert got.value == pytest.approx(kw)
    assert got.unit == "kW"
    assert got.provenance["unit_declared"] == declared
    assert got.provenance["native_unit"] == unit


def test_dc_r03_f09_a_stated_unit_that_disagrees_with_the_declared_one_is_refused():
    got = _power("2.5", "W", native_unit="kW")
    assert (got.value, got.state, got.reason) == (
        None,
        db.MISSING,
        "declared_unit_conflicts",
    )
    # Agreeing is fine, and the sensor's own unit is what is used.
    same = _power("2500", "W", native_unit="W")
    assert same.value == pytest.approx(2.5)
    assert "unit_declared" not in same.provenance


@pytest.mark.parametrize("unit", ["A", "kVA", "Wh", "kWh"])
def test_dc_r03_f09_a_declared_unit_never_turns_another_dimension_into_power(
    unit: str,
) -> None:
    got = _power("2.5", unit, native_unit="kW")
    assert got.value is None and got.state == db.MISSING
    assert got.reason in ("unit_not_power", "energy_unit_as_power")


def test_dc_r03_f09_a_declared_unit_must_itself_be_exact():
    got = _power("2.5", None, native_unit="kw")
    assert (got.value, got.reason) == (None, "declared_unit_invalid")
