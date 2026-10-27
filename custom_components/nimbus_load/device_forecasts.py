"""Forecast sources through the device contract (DC-006 slice 3, decision 10;
DC-002 forecast adapters; #1574).

Today the Solver reads its solar forecast in `solver_inputs/solar.py`: up to
three configured sensors, plus Open-Meteo Solar Forecast and Solcast when
auto-include is on, each read through `_solar_entries_from_attributes` and
`resample_forecast`, then blended with equal weights over the members that
returned data. This module states the same thing as contract bindings:

* **Forecast adapters** (decision 10a), versioned like DC-002's price
  adapters, each reading one provider's own shape: `nimbus_forecast` (the
  generic `forecast: [{time, value, lower, upper}]`), `solcast`
  (`detailedForecast`, `pv_estimate` with its p10 and p90) and
  `open_meteo_solar` (`watts: {time: W}`, scaled to kW). A source that spans
  several entities (Solcast's today and tomorrow, Open-Meteo's eight days)
  is one `entity_attribute` source whose `continued_by` names the rest; the
  first entity is the anchor, and without it the source does not resolve,
  as today.
* **`combine: mean`** (decision 10b): a `generation_forecast` binding may
  hold an ordered `sources` list. It resolves to the equal-weight mean of
  the members that resolve; each member's state and reason are reported,
  and a member that fails is dropped, never zero-filled. One member is its
  own value. The band is today's: the members' lowest lower and highest
  upper, widened by half the members' spread.

Load's `demand_forecast` is not combined here: it totals across load
devices through DC-003 (decision 10c). The two stay separate mechanisms so
the never-zero-fill rule cannot drift between them (Mark Purcell, #1574).

What is read stops here. The running-low gate (#1784) and the live anchor
transform the solar forecast after it is read; they are the consumer's.

Pure: no Home Assistant import. `read_attributes(entity_id)` gives an
entity's attributes, or None when it does not exist; `resample` and
`resample_instants` are the Solver's own `resample_forecast` and
`resample_instants` (#2004), passed in so the two sides cannot differ.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

try:
    from .device_contract import COMBINE_ROLES
    from .ml.blend import blend_forecast_array, cross_source_spread
    from .solver_shared import parse_iso
except ImportError:  # imported directly by the device-contract tests
    from device_contract import COMBINE_ROLES  # type: ignore[no-redef]
    from ml.blend import (  # type: ignore[no-redef]
        blend_forecast_array,
        cross_source_spread,
    )
    from solver_shared import parse_iso  # type: ignore[no-redef]

ReadAttributes = Callable[[str], Mapping[str, Any] | None]
Resample = Callable[[list[dict], str, list[datetime]], list[float]]

__all__ = ["COMBINE_ROLES"]

COMBINE_MEAN = "mean"


# --- adapters ------------------------------------------------------------------


def _generic(attrs: Mapping[str, Any]) -> list[dict] | None:
    return list(attrs.get("forecast") or []) or None


def _solcast(attrs: Mapping[str, Any]) -> list[dict] | None:
    detailed = attrs.get("detailedForecast")
    if not detailed:
        return None
    return [
        {
            "time": p["period_start"],
            "value": float(p.get("pv_estimate", 0.0) or 0.0),
            "lower": float(p.get("pv_estimate10", 0.0) or 0.0),
            "upper": float(p.get("pv_estimate90", 0.0) or 0.0),
        }
        for p in detailed
    ]


#: nimbus #2004: Open-Meteo's `watts[t]` is the power at t + 15 min, an
#: instant (the Solver's `solver_writer.OPEN_METEO_WATTS_LEAD`; a test keeps
#: the two equal).
OPEN_METEO_WATTS_LEAD = timedelta(minutes=15)


def _open_meteo(attrs: Mapping[str, Any]) -> list[dict] | None:
    watts = attrs.get("watts")
    if not watts:
        return None
    return [
        {
            "time": parse_iso(ts) + OPEN_METEO_WATTS_LEAD,
            "value": float(w) / 1000.0,
            "instant": True,
        }
        for ts, w in watts.items()
    ]


@dataclass(frozen=True)
class ForecastAdapter:
    """A versioned reader of one provider's forecast rows. `attribute` is the
    attribute it reads; `rows(attrs)` the rows, in kW, or None when the
    entity does not publish that shape. `instant`: each row is a reading at
    its own time, read at each period's midpoint (#2004), not a value held
    from its time."""

    name: str
    version: int
    attribute: str
    rows: Callable[[Mapping[str, Any]], list[dict] | None]
    instant: bool = False


FORECAST_ADAPTERS: dict[tuple[str, int], ForecastAdapter] = {
    ("nimbus_forecast", 1): ForecastAdapter("nimbus_forecast", 1, "forecast", _generic),
    # pv_estimate is the period's average kW, not the entity's kWh unit.
    ("solcast", 1): ForecastAdapter("solcast", 1, "detailedForecast", _solcast),
    ("open_meteo_solar", 1): ForecastAdapter(
        "open_meteo_solar", 1, "watts", _open_meteo, instant=True
    ),
}


def _adapter_of(spec: Any) -> ForecastAdapter | None:
    if not isinstance(spec, Mapping):
        return None
    version = spec.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    return FORECAST_ADAPTERS.get((str(spec.get("name")), version))


def forecast_adapter(source: Mapping[str, Any]) -> ForecastAdapter | None:
    return _adapter_of(source.get("adapter"))


def also_adapters(source: Mapping[str, Any]) -> list[ForecastAdapter] | None:
    """The source's `also_adapters`, in order: [] when it has none, None
    when one of them is not a known adapter."""
    found = [_adapter_of(spec) for spec in source.get("also_adapters") or []]
    if any(a is None for a in found):
        return None
    return [a for a in found if a is not None]


def forecast_source(
    entity_ids: Sequence[str],
    adapter: str,
    *,
    version: int = 1,
    also: Sequence[str] = (),
) -> dict[str, Any]:
    """An `entity_attribute` forecast source over `entity_ids`, the first
    being the anchor. `also`: adapters tried in order, at version 1, when
    `adapter` finds no rows (a configured sensor may publish any of the
    three shapes, #1574)."""
    spec = FORECAST_ADAPTERS[(adapter, version)]
    source: dict[str, Any] = {
        "kind": "entity_attribute",
        "entity_id": entity_ids[0],
        "attribute_path": spec.attribute,
        "adapter": {"name": adapter, "version": version},
    }
    if len(entity_ids) > 1:
        source["continued_by"] = list(entity_ids[1:])
    if also:
        source["also_adapters"] = [{"name": a, "version": 1} for a in also]
    return source


# --- resolution ----------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """One source of a forecast binding: `ok` with its kW arrays, or
    `missing` with a reason, never zeros. `adapter` is the one that read
    the rows, which may be one of the source's `also_adapters`."""

    entity_ids: tuple[str, ...]
    adapter: str | None
    state: str
    reason: str | None = None
    value: tuple[float, ...] | None = None
    lower: tuple[float, ...] | None = None
    upper: tuple[float, ...] | None = None


@dataclass(frozen=True)
class Forecast:
    """A resolved forecast role: `ok` with the combined arrays, or `missing`
    (`unbound`, `no_member_resolved`). Every member is reported."""

    state: str
    reason: str | None
    value: tuple[float, ...] | None
    lower: tuple[float, ...] | None
    upper: tuple[float, ...] | None
    members: tuple[Member, ...]


def _missing(ids: tuple[str, ...], adapter: str | None, reason: str) -> Member:
    return Member(ids, adapter, "missing", reason)


def resolve_member(
    source: Mapping[str, Any],
    read_attributes: ReadAttributes,
    grid_times: list[datetime],
    resample: Resample,
    *,
    resample_instants: Resample,
) -> Member:
    """One forecast source on `grid_times`. Negative values are clamped to
    0 (a model's excursion below zero); a source with bounds has its lower
    no higher, and its upper no lower, than its value; one without has a
    zero-width band. Reasons: `adapter_unknown`, `unavailable` (no anchor
    entity), `shape_not_recognized`, `malformed`. A source with
    `also_adapters` tries each in order when the ones before it find no
    rows; the member names the adapter that read them."""
    ids = tuple(
        str(e)
        for e in [source.get("entity_id"), *(source.get("continued_by") or [])]
        if e
    )
    adapter = forecast_adapter(source)
    name = adapter.name if adapter else None
    also = also_adapters(source)
    if adapter is None or also is None or source.get("kind") != "entity_attribute":
        return _missing(ids, name, "adapter_unknown")
    if not ids or read_attributes(ids[0]) is None:
        return _missing(ids, name, "unavailable")
    rows: list[dict] = []
    for candidate in [adapter, *also]:
        for entity_id in ids:
            attrs = read_attributes(entity_id)
            if attrs is None:
                continue
            try:
                found = candidate.rows(attrs)
            except (KeyError, TypeError, ValueError):
                return _missing(ids, candidate.name, "malformed")
            if found:
                rows.extend(found)
        if rows:
            adapter, name = candidate, candidate.name
            break
    if not rows:
        return _missing(ids, name, "shape_not_recognized")
    if len(ids) > 1:
        # One series over several entities, in time order, as the Solver's
        # own multi-entity readers sort it.
        rows.sort(key=lambda p: p["time"])
    n = len(grid_times)
    if adapter.instant:
        resample = resample_instants
    try:
        value = [max(0.0, v) for v in resample(rows, "value", grid_times)]
        if any(p.get("lower") is not None for p in rows):
            lower = [max(0.0, v) for v in resample(rows, "lower", grid_times)]
            upper = [max(0.0, v) for v in resample(rows, "upper", grid_times)]
            lower = [min(lower[i], value[i]) for i in range(n)]
            upper = [max(upper[i], value[i]) for i in range(n)]
        else:
            lower, upper = list(value), list(value)
    except (KeyError, TypeError, ValueError):
        return _missing(ids, name, "malformed")
    return Member(ids, name, "ok", None, tuple(value), tuple(lower), tuple(upper))


def combine_mean(members: Sequence[Member]) -> Forecast:
    """The equal-weight mean of the members that resolved. With one, its own
    arrays; with several, today's band (`build_solar_arrays`): the lowest
    lower and highest upper, each widened by half the members' spread. With
    none, `missing`: never a zero forecast."""
    ok = [m for m in members if m.state == "ok"]
    if not ok:
        return Forecast(
            "missing", "no_member_resolved", None, None, None, tuple(members)
        )
    if len(ok) == 1:
        only = ok[0]
        return Forecast("ok", None, only.value, only.lower, only.upper, tuple(members))
    values = [np.array(m.value) for m in ok]
    spread = cross_source_spread(values)
    value = [max(0.0, float(v)) for v in blend_forecast_array(values)]
    lowest = np.min(np.stack([np.array(m.lower) for m in ok]), axis=0)
    highest = np.max(np.stack([np.array(m.upper) for m in ok]), axis=0)
    lower = tuple(
        max(0.0, float(min(lowest[i], value[i]) - spread[i] / 2))
        for i in range(len(value))
    )
    upper = tuple(
        float(max(highest[i], value[i]) + spread[i] / 2) for i in range(len(value))
    )
    return Forecast("ok", None, tuple(value), lower, upper, tuple(members))


def resolve_forecast(
    binding: Mapping[str, Any] | None,
    read_attributes: ReadAttributes,
    grid_times: list[datetime],
    resample: Resample,
    *,
    resample_instants: Resample,
) -> Forecast:
    """A forecast binding: one `source`, or `sources` with `combine: mean`."""
    if not binding:
        return Forecast("missing", "unbound", None, None, None, ())
    sources = binding.get("sources")
    if sources is None:
        sources = [binding.get("source") or {}]
    elif binding.get("combine") != COMBINE_MEAN:
        return Forecast("missing", "combine_unknown", None, None, None, ())
    members = [
        resolve_member(
            s,
            read_attributes,
            grid_times,
            resample,
            resample_instants=resample_instants,
        )
        for s in sources
    ]
    return combine_mean(members)


# --- today's member list, as a binding --------------------------------------


def legacy_solar_binding(
    options: Mapping[str, Any],
    auto_include: bool,
    known_sources: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Today's solar forecast members (`build_solar_arrays`) as one
    `generation_forecast` binding, in today's order: the configured sensor,
    Open-Meteo and Solcast under auto-include, configured sensors 2 and 3.
    A configured sensor that is one of a known integration's own entities is
    read through that integration's adapter, and with auto-include on it is
    left out: the integration's own source already reads it with the rest
    of its days (#546). None when there is no member."""
    solcast = [e for e in known_sources.get("solcast") or [] if isinstance(e, str)]
    meteo = [e for e in known_sources.get("open_meteo") or [] if isinstance(e, str)]

    def configured(key: str) -> dict[str, Any] | None:
        entity_id = options.get(key)
        if not isinstance(entity_id, str) or not entity_id:
            return None
        if entity_id in solcast:
            return None if auto_include else forecast_source([entity_id], "solcast")
        if entity_id in meteo:
            if auto_include:
                return None
            return forecast_source([entity_id], "open_meteo_solar")
        # A configured sensor may publish any of the three shapes (a
        # template copy of Solcast's, say): read it in whichever it has.
        return forecast_source(
            [entity_id], "nimbus_forecast", also=("solcast", "open_meteo_solar")
        )

    sources: list[dict[str, Any] | None] = [configured("solver_solar_forecast_sensor")]
    if auto_include:
        if meteo:
            sources.append(forecast_source(meteo, "open_meteo_solar"))
        if solcast:
            sources.append(forecast_source(solcast, "solcast"))
    sources += [
        configured("solver_solar_forecast_sensor_2"),
        configured("solver_solar_forecast_sensor_3"),
    ]
    found = [s for s in sources if s is not None]
    if not found:
        return None
    if len(found) == 1:
        return {"source": found[0]}
    return {"sources": found, "combine": COMBINE_MEAN}
