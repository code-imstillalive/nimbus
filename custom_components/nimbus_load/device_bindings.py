"""Resolving bindings into canonical quantities and intervals: DC-002
(docs/specs/device-contract/002-bindings-and-normalisation.md, revision 0.1).

DC-001's canonical model says WHAT a device's roles are bound to. This module
reads one binding and says what it currently MEANS: a quantity in a canonical
unit and sign, or a series of half-open intervals, with its freshness,
coverage and provenance. It decides nothing about what to do with the value.

Pure apart from the reads, which are injected (`Readers`): a state reader, a
statistics reader, a read-only action-response reader and a model-output
reader. It imports nothing from Home Assistant and makes no service calls
itself; an `action_response` binding whose action is not on
`READ_ONLY_ACTIONS` is refused before any reader is called.

Reused, not rewritten (DC-002 "What already exists"):
* power units: `power_units.py` (#1570), accepting only the exact Home
  Assistant spellings the DC-000 oracle states;
* dimension refusals: `power_input_check.py` (#1562 energy, #1643 A/kVA);
* provider rows and coverage: `price_intervals.py` (#1606-#1613);
* price scaling: `price_intervals.price_unit_scale` (#1537, #1615).

Not to be confused with `device_resolver.py` (#1590), which proposes
candidate bindings for DC-005; this module only reads bindings already made.

**No consumer reads this yet** (until DC-006). A test pins that no other module
under custom_components/ imports it.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

try:
    from . import power_input_check, power_units, price_intervals
    from .solver_shared import parse_iso
except ImportError:  # standalone/cron deployment
    import power_input_check  # type: ignore[no-redef]
    import power_units  # type: ignore[no-redef]
    import price_intervals  # type: ignore[no-redef]
    from solver_shared import parse_iso  # type: ignore[no-redef]

# --- the five states (DC-R04) ------------------------------------------------

VALID = "valid"
MISSING = "missing"
UNAVAILABLE = "unavailable"
STALE = "stale"
HELD = "held"
STATES = (VALID, MISSING, UNAVAILABLE, STALE, HELD)

# Read-only response actions (DC-002 decision, DC-R15). Additions by
# reviewed PR only. Anything else is refused and never called.
READ_ONLY_ACTIONS = frozenset(
    {"openadr3_ven.get_forecast", "amberelectric.get_forecasts"}
)

# Home Assistant's UnitOfPower spellings, exactly. `power_units` supplies the
# factor; a miscased spelling it would also accept is refused here, as the
# DC-000 oracle refuses it (contract_divergences.json, F09).
EXACT_POWER_UNITS = frozenset({"mW", "W", "kW", "MW", "GW", "TW", "BTU/h"})
ENERGY_TO_KWH = {"mWh": 1e-6, "Wh": 1e-3, "kWh": 1.0, "MWh": 1e3, "GWh": 1e6}
_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY"}
# A dollar sign, or a cent sign (c, ¢), names no currency of its own: it is
# read as the site's currency, the way the provider readers always have. Built
# from one string so no bare dollar literal reads as a published unit (#1293).
_SITE_MONEY_SYMBOLS = frozenset("$c¢")
_PER = ("kWh", "MWh")

_POWER_ROLES = (
    "power",
    "import_power",
    "export_power",
    "charge_power",
    "discharge_power",
    "generation_forecast",
    "demand_forecast",
    "planned_exchange",
    "planned_dispatch",
    "planned_curtailment",
    "scheduled_consumption",
)
_ENERGY_ROLES = (
    "energy",
    "import_energy",
    "export_energy",
    "charge_energy",
    "discharge_energy",
)
_PRICE_ROLES = (
    "import_price",
    "export_price",
    "import_price_forecast",
    "export_price_forecast",
)
# Role -> dimension, for every role DC-001 names.
DIMENSION: dict[str, str] = {
    **dict.fromkeys(_POWER_ROLES, "power"),
    **dict.fromkeys(_ENERGY_ROLES, "energy"),
    **dict.fromkeys(_PRICE_ROLES, "price"),
    **dict.fromkeys(("soc", "target_soc", "planned_soc"), "percent"),
    "temperature": "temperature",
    "availability": "fraction",
}
_CANONICAL_UNIT: dict[str, str | None] = {
    "power": "kW",
    "energy": "kWh",
    "percent": "%",
    "temperature": "°C",
    "fraction": None,
}


# --- results -----------------------------------------------------------------


@dataclass(frozen=True)
class Quantity:
    """A measurement, in canonical unit and sign, or `value=None`."""

    role: str
    device_id: str
    value: float | None
    unit: str | None
    state: str
    observed_at: datetime | None
    provenance: Mapping[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class Interval:
    """One published value over `[start, end)`."""

    start: datetime
    end: datetime
    value: float
    quality: str


@dataclass(frozen=True)
class Coverage:
    """What the intervals cover of `[start, end)`; gaps listed, never filled."""

    start: datetime | None
    end: datetime | None
    gaps: tuple[tuple[datetime, datetime], ...]
    covered_fraction: float


@dataclass(frozen=True)
class Series:
    """A price or forecast: intervals as published, scaled once."""

    role: str
    device_id: str
    unit: str | None
    intervals: tuple[Interval, ...]
    coverage: Coverage
    state: str
    issued_at: datetime | None
    fetched_at: datetime | None
    provenance: Mapping[str, Any]
    fees_included: bool | None = None
    reason: str | None = None


Resolved = Quantity | Series


@dataclass(frozen=True)
class Readers:
    """The only way this module sees the outside world.

    * `state(entity_id)` -> `{state, attributes, last_changed, last_updated,
      last_reported}` or None. Freshness for an unchanged value needs the LIVE
      state object's `last_reported`: HA's serialised views keep a cached,
      older one (DC-002 "Source variants", #1705).
    * `statistic(statistic_id, interval_semantics)` -> `{value, unit, start,
      end}` or None.
    * `action_response(action, target, parameters)` -> the response. Only
      ever called for an action on `READ_ONLY_ACTIONS`.
    * `model_output(model, target)` -> `{value | intervals, unit,
      training_state, ...}` or None.
    """

    state: Callable[[str], Mapping[str, Any] | None] | None = None
    statistic: Callable[[str, str], Mapping[str, Any] | None] | None = None
    action_response: Callable[[str, Any, Mapping[str, Any]], Any] | None = None
    model_output: Callable[[str, str], Mapping[str, Any] | None] | None = None


def _sum(values: Sequence[float]) -> float:
    return math.fsum(values)


def _difference(values: Sequence[float]) -> float:
    return values[0] - math.fsum(values[1:])


# Registered `derived` calculations (DC-001: `calculation` is a registered
# name). Callers may pass their own registry.
CALCULATIONS: dict[str, Callable[[Sequence[float]], float]] = {
    "sum": _sum,
    "difference": _difference,
}


@dataclass(frozen=True)
class _Ctx:
    readers: Readers
    now: datetime
    currency: str | None
    devices: Mapping[Any, Mapping[str, Any]]
    calculations: Mapping[str, Callable[[Sequence[float]], float]]
    window: tuple[datetime, datetime] | None
    stack: tuple[str, ...] = field(default=())


# --- entry point ---------------------------------------------------------------


def resolve(
    document: Mapping[str, Any],
    device_id: str,
    role: str,
    readers: Readers,
    now: datetime,
    *,
    window: tuple[datetime, datetime] | None = None,
    calculations: Mapping[str, Callable[[Sequence[float]], float]] | None = None,
) -> Resolved:
    """The resolved value of `device_id`'s `role` binding in a DC-001
    document. An unbound role is `missing`, never zero. `window`, for a
    series, is the span coverage is measured over."""
    _aware(now)
    if window is not None:
        _check_window(*window)
    devices = {
        d.get("id"): d for d in document.get("devices") or [] if isinstance(d, Mapping)
    }
    ctx = _Ctx(
        readers=readers,
        now=now,
        currency=(document.get("site") or {}).get("currency"),
        devices=devices,
        calculations=calculations if calculations is not None else CALCULATIONS,
        window=window,
    )
    return _resolve_role(ctx, device_id, role)


def _resolve_role(ctx: _Ctx, device_id: str, role: str) -> Resolved:
    node = f"{device_id}.{role}"
    if node in ctx.stack:
        cycle = " -> ".join([*ctx.stack[ctx.stack.index(node) :], node])
        return _missing(role, device_id, {"node": node}, f"dependency_cycle: {cycle}")
    device = ctx.devices.get(device_id)
    if device is None:
        return _missing(role, device_id, {"node": node}, "device_unknown")
    binding = (device.get("bindings") or {}).get(role)
    if not binding:
        return _missing(role, device_id, {"node": node}, "unbound")
    source = binding.get("source") or {}
    prov = _provenance(device, binding, source)
    dimension = DIMENSION.get(role)
    if dimension is None:
        return _missing(role, device_id, prov, "role_unknown")
    handler = _HANDLERS.get(str(source.get("kind")))
    if handler is None:
        return _missing(role, device_id, prov, "source_kind_unknown")
    inner = dataclasses.replace(ctx, stack=(*ctx.stack, node))
    return handler(inner, device_id, role, dimension, binding, source, prov)


def _provenance(
    device: Mapping[str, Any], binding: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    declared = binding.get("provenance") or {}
    norm = binding.get("normalization") or {}
    prov: dict[str, Any] = {
        "origin": declared.get("origin"),
        "confirmed": declared.get("confirmed"),
        "source_kind": source.get("kind"),
        "boundary": (device.get("boundary") or {}).get("id"),
        "native_sign": norm.get("native_positive_direction"),
        "canonical_sign": norm.get("positive_direction"),
        "adapter": None,
    }
    for key in (
        "entity_id",
        "registry_id",
        "attribute_path",
        "statistic_id",
        "action",
        "target",
        "model",
        "calculation",
        "depends_on",
    ):
        if source.get(key) is not None:
            prov[key] = source[key]
    adapter = source.get("adapter")
    if isinstance(adapter, Mapping):
        prov["adapter"] = {
            "name": adapter.get("name"),
            "version": adapter.get("version"),
        }
    return prov


def _missing(
    role: str,
    device_id: str,
    prov: Mapping[str, Any],
    reason: str,
    state: str = MISSING,
) -> Quantity:
    return Quantity(role, device_id, None, None, state, None, dict(prov), reason)


# --- numbers, units and signs (DC-R03) --------------------------------------------


def _number(raw: Any) -> tuple[float | None, str | None]:
    """A finite float, or (None, reason). A measured 0 is 0."""
    if isinstance(raw, bool):
        return None, "not_numeric"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "not_numeric"
    if not math.isfinite(value):
        return None, "not_finite"
    return value, None


def unit_scale(
    dimension: str, unit: Any, currency: str | None = None
) -> tuple[float | None, str | None, str | None]:
    """(factor to the canonical unit, canonical unit, refusal reason).
    A refused unit has factor None and a reason; nothing is guessed."""
    if dimension == "power":
        return _power_scale(unit)
    if dimension == "energy":
        return _energy_scale(unit)
    if dimension == "price":
        return _price_scale(unit, currency)
    if dimension == "temperature":
        if unit == "°C":
            return 1.0, "°C", None
        return None, None, "unit_not_temperature"
    if dimension == "percent":
        return (1.0, "%", None) if unit == "%" else (None, None, "unit_not_percent")
    return 1.0, None, None  # fraction: unitless


def _power_scale(unit: Any) -> tuple[float | None, str | None, str | None]:
    if unit is None or (isinstance(unit, str) and not unit.strip()):
        return None, None, "unit_missing"
    if power_input_check.is_energy_unit(unit):
        return None, None, "energy_unit_as_power"
    if unit in EXACT_POWER_UNITS:
        return power_units.power_scale_to_kw(unit), "kW", None
    if power_units.is_power_unit(unit):
        return None, None, "unit_not_exact"  # e.g. "kw": HA never spells it so
    return None, None, "unit_not_power"  # A, kVA, %, anything unknown (#1643)


def _energy_scale(unit: Any) -> tuple[float | None, str | None, str | None]:
    if isinstance(unit, str) and unit in ENERGY_TO_KWH:
        return ENERGY_TO_KWH[unit], "kWh", None
    if unit is None or (isinstance(unit, str) and not unit.strip()):
        return None, None, "unit_missing"
    if power_units.is_power_unit(unit):
        return None, None, "power_unit_as_energy"
    return None, None, "unit_not_energy"


def _price_scale(
    unit: Any, currency: str | None
) -> tuple[float | None, str | None, str | None]:
    """Exactly $/kWh, $/MWh and c/kWh (Mark Purcell, #1774), scaled once by
    price_intervals.price_unit_scale. `¢/kWh` is the same cent unit, and the
    site's ISO code may stand for `$` (`AUD/kWh`, `AUD/MWh`). Another
    currency is refused (no FX); anything else, a unitless price included,
    is not a price unit."""
    text = str(unit or "").strip()
    if not text:
        return None, None, "unit_missing"
    if not currency:
        return None, None, "site_currency_missing"  # never labelled by guess
    money, _, per = text.partition("/")
    money = money.strip()
    code = currency.upper()
    accepted = {f"{m}/{p}" for m in _SITE_MONEY_SYMBOLS - {"c", "¢"} for p in _PER}
    accepted |= {f"{code}/{p}" for p in _PER} | {"c/kWh", "¢/kWh"}
    if text in accepted:
        return price_intervals.price_unit_scale(text), f"{currency}/kWh", None
    foreign = _CURRENCY_SYMBOLS.get(money, money.upper()) != code
    if per in _PER and money and money not in _SITE_MONEY_SYMBOLS and foreign:
        return None, None, "currency_mismatch"
    return None, None, "unit_not_price"


def _sign(binding: Mapping[str, Any]) -> float:
    """-1 when the declared native direction is not the canonical one."""
    norm = binding.get("normalization") or {}
    native = norm.get("native_positive_direction")
    canonical = norm.get("positive_direction")
    return -1.0 if native and canonical and native != canonical else 1.0


def _canonical(value: float, scale: float, sign: float) -> float:
    """Unit and sign applied exactly once. `+ 0.0` turns -0.0 into 0.0: a
    measured zero has no direction."""
    return value * scale * sign + 0.0


def _measured(
    value: float, scale: float, binding: Mapping[str, Any], prov: dict[str, Any]
) -> float:
    sign = _sign(binding)
    prov["sign_flipped"] = sign < 0
    return _canonical(value, scale, sign)


# --- entity_state ------------------------------------------------------------


def _resolve_entity_state(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    st = _read_state(ctx, source.get("entity_id"))
    if st is None:
        return _missing(role, device_id, prov, "no_entity")
    raw = st.get("state")
    if raw in ("unavailable", "unknown"):
        return _missing(role, device_id, prov, str(raw), state=UNAVAILABLE)
    if raw is None or raw == "":
        return _missing(role, device_id, prov, "no_state")
    value, why = _number(raw)
    unit = (st.get("attributes") or {}).get("unit_of_measurement")
    prov["native_unit"] = unit
    if value is None:
        return _missing(role, device_id, prov, str(why))
    scale, canonical_unit, refused = unit_scale(dimension, unit, ctx.currency)
    if scale is None:
        return _missing(role, device_id, prov, str(refused))
    value = _measured(value, scale, binding, prov)
    state, observed_at = _freshness(binding, st, ctx.now, prov)
    return Quantity(role, device_id, value, canonical_unit, state, observed_at, prov)


def _read_state(ctx: _Ctx, entity_id: Any) -> Mapping[str, Any] | None:
    if not entity_id or ctx.readers.state is None:
        return None
    return ctx.readers.state(str(entity_id))


def _when(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        return parse_iso(raw)
    except (TypeError, ValueError, AttributeError):
        return None


def _freshness(
    binding: Mapping[str, Any],
    st: Mapping[str, Any],
    now: datetime,
    prov: dict[str, Any],
) -> tuple[str, datetime | None]:
    """Per source kind, not one age (DC-002 "The five states").

    * `sampled`: stale once the live `last_reported` is older than its
      declared `max_interval_s`.
    * `change_only` (a breaker idle at 0 W all night): `held` once its last
      change is older than that, never stale on age alone.
    * undeclared: no rule, so no age judgement (recorded in provenance)."""
    rule = (binding.get("normalization") or {}).get("freshness") or {}
    prov["freshness"] = dict(rule) if rule else "undeclared"
    changed = _when(st.get("last_changed"))
    reported = (
        _when(st.get("last_reported")) or _when(st.get("last_updated")) or changed
    )
    mode, limit = rule.get("mode"), rule.get("max_interval_s")
    if mode not in ("sampled", "change_only") or not _is_number(limit):
        return VALID, reported
    span = timedelta(seconds=float(limit or 0))
    if mode == "change_only":
        if changed is not None and now - changed > span:
            return HELD, changed
        return VALID, reported
    if reported is not None and now - reported > span:
        return STALE, reported
    return VALID, reported


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- constant and recorder_statistic ----------------------------------------------


def _resolve_constant(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    value, why = _number(source.get("value"))
    prov["native_unit"] = source.get("unit")
    if value is None:
        return _missing(role, device_id, prov, str(why))
    scale, unit, refused = unit_scale(dimension, source.get("unit"), ctx.currency)
    if scale is None:
        return _missing(role, device_id, prov, str(refused))
    value = _measured(value, scale, binding, prov)
    return Quantity(role, device_id, value, unit, VALID, None, prov)


def _resolve_statistic(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    """Long-term statistics, per `interval_semantics`. An energy statistic
    stays energy; one with no current-state entity is still a valid binding."""
    prov["interval_semantics"] = source.get("interval_semantics")
    reader = ctx.readers.statistic
    row = (
        reader(str(source.get("statistic_id")), str(source.get("interval_semantics")))
        if reader is not None
        else None
    )
    if not row:
        return _missing(role, device_id, prov, "no_statistic")
    value, why = _number(row.get("value"))
    prov["native_unit"] = row.get("unit")
    if value is None:
        return _missing(role, device_id, prov, str(why))
    scale, unit, refused = unit_scale(dimension, row.get("unit"), ctx.currency)
    if scale is None:
        return _missing(role, device_id, prov, str(refused))
    end = _when(row.get("end"))
    prov["period"] = (_when(row.get("start")), end)
    value = _measured(value, scale, binding, prov)
    return Quantity(role, device_id, value, unit, VALID, end, prov)


# --- derived (DC-R16) and model_output --------------------------------------------


def _resolve_derived(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    """Each dependency first; a run-time cycle is refused; any dependency
    without a value makes the result missing, never a partial sum.

    A `stale` dependency makes the result `stale`. A `held` one does not:
    `held` is current by the source's own rules (a change-only sensor whose
    last change still stands), so the result is as current as one built
    from `valid` inputs (Mark's question on #1774)."""
    calc = ctx.calculations.get(str(source.get("calculation")))
    if calc is None:
        return _missing(role, device_id, prov, "calculation_unknown")
    values: list[float] = []
    units: set[str | None] = set()
    stale = False
    for dep in source.get("depends_on") or []:
        dep_device, _, dep_role = str(dep).partition(".")
        got = _resolve_role(ctx, dep_device, dep_role)
        if isinstance(got, Series):
            return _missing(role, device_id, prov, f"dependency_is_series: {dep}")
        if got.reason and got.reason.startswith("dependency_cycle"):
            return _missing(role, device_id, prov, got.reason)
        if got.value is None:
            return _missing(role, device_id, prov, f"dependency_missing: {dep}")
        stale = stale or got.state == STALE
        values.append(got.value)
        units.add(got.unit)
    if not values:
        return _missing(role, device_id, prov, "no_dependencies")
    unit = units.pop() if len(units) == 1 else "<mixed>"
    if dimension != "price" and unit != _CANONICAL_UNIT.get(dimension):
        return _missing(role, device_id, prov, "dependency_dimension_differs")
    value = _measured(calc(values), 1.0, binding, prov)
    return Quantity(role, device_id, value, unit, STALE if stale else VALID, None, prov)


def _resolve_model(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    """The Nimbus model's published output, with its own coverage and
    training state carried rather than hidden."""
    reader = ctx.readers.model_output
    out = (
        reader(str(source.get("model")), str(source.get("target")))
        if reader is not None
        else None
    )
    if not out:
        return _missing(role, device_id, prov, "no_model_output")
    prov["training_state"] = out.get("training_state")
    prov["native_unit"] = out.get("unit")
    scale, unit, refused = unit_scale(dimension, out.get("unit"), ctx.currency)
    if scale is None:
        return _missing(role, device_id, prov, str(refused))
    if "intervals" in out:
        rows = [
            price_intervals.PriceInterval(parse_iso(a), parse_iso(b), float(v))
            for a, b, v in out.get("intervals") or []
        ]
        quality = str(out.get("quality") or "forecast")
        return _series(ctx, device_id, role, rows, scale, unit, prov, quality, binding)
    value, why = _number(out.get("value"))
    if value is None:
        return _missing(role, device_id, prov, str(why))
    value = _measured(value, scale, binding, prov)
    observed = _when(out.get("computed_at"))
    return Quantity(role, device_id, value, unit, VALID, observed, prov)


# --- adapters: entity_attribute and action_response (DC-R05, DC-R06) -----------------

_Rows = list[price_intervals.PriceInterval]


@dataclass(frozen=True)
class Adapter:
    """A versioned reader of one provider's rows, through price_intervals.py.

    `native_unit(spec)` is what the rows are published in, from the binding's
    adapter spec (None: the provider does not say, so the binding or the
    response must); `fees_included` whether network and retail fees are inside
    them (None: not stated); `max_age_s` the age past which a fetched forecast
    is stale; `singles` gives each row's own interval so overlapping rows are
    SEEN -- the provider readers keep the first of two overlapping rows;
    `no_match` the starts of explicit P2P no-match intervals. `extra` is the
    adapter's own context: Amber Express's `detailedForecast`, or the
    LocalVolts field, direction and qualities."""

    name: str
    version: int
    native_unit: Callable[[Mapping[str, Any]], str | None]
    fees_included: Callable[[Any, Any], bool | None]
    max_age_s: float
    intervals: Callable[[Any, Any], _Rows]
    singles: Callable[[Any, Any], _Rows]
    action_only: bool = False
    no_match: Callable[[Any, Any], set[datetime]] | None = None


def _each(fn: Callable[[Any], _Rows]) -> Callable[[Any, Any], _Rows]:
    def singles(rows: Any, _extra: Any) -> _Rows:
        out: _Rows = []
        for row in rows if isinstance(rows, list) else []:
            out.extend(fn([row]))
        return out

    return singles


def _rows(fn: Callable[[Any], _Rows]) -> Callable[[Any, Any], _Rows]:
    return lambda rows, _extra: fn(rows)


def _amber_express(rows: Any, detailed: Any) -> _Rows:
    return price_intervals.amber_express_intervals(rows, detailed) or []


def _amber_express_singles(rows: Any, detailed: Any) -> _Rows:
    out: _Rows = []
    for row in rows if isinstance(rows, list) else []:
        out.extend(price_intervals.amber_express_intervals([row], detailed) or [])
    return out


def _pd7day_fees(rows: Any, _extra: Any) -> bool:
    # A tariff sensor's `value` carries its network rate; wholesale does not.
    return price_intervals.pd7day_basis(rows) == price_intervals.BASIS_TARIFF


def _lv(rows: Any, extra: Any) -> list[price_intervals.LocalVoltsInterval]:
    return price_intervals.localvolts_v2_rows(
        rows if isinstance(rows, list) else [],
        str(extra.get("field")),
        extra.get("direction"),
        extra.get("qualities"),
    )


def _lv_intervals(rows: Any, extra: Any) -> _Rows:
    return price_intervals.localvolts_v2_intervals(
        rows if isinstance(rows, list) else [],
        str(extra.get("field")),
        extra.get("direction"),
        extra.get("qualities"),
    )


def _lv_singles(rows: Any, extra: Any) -> _Rows:
    return [i.interval for i in _lv(rows, extra)]


def _lv_no_match(rows: Any, extra: Any) -> set[datetime]:
    return {i.interval.start for i in _lv(rows, extra) if not i.matched}


def _lv_unit(spec: Mapping[str, Any]) -> str | None:
    """Rates are cents per kWh; the matched rate comes from dollar amounts.
    LocalVolts trades in the NEM, so its dollars are Australian (#1293)."""
    field = spec.get("field")
    if field in price_intervals.LV_RATE_FIELDS:
        return "c/kWh"
    return "AUD/kWh" if field == price_intervals.LV_MATCHED_RATE else None


def _lv_fees(_rows: Any, extra: Any) -> bool | None:
    """LocalVolts' rates (Flex Up included) carry network and retail fees.
    The matched P2P rate is what a peer paid, so it is not stated."""
    return None if extra.get("field") == price_intervals.LV_MATCHED_RATE else True


def _aud(_spec: Mapping[str, Any]) -> str:
    return "AUD/kWh"


# Nothing older than five minutes, as default (Mark Purcell, #1774). A binding
# may declare its own `normalization.freshness.max_age_s`.
DEFAULT_MAX_AGE_S = 300.0

ADAPTERS: dict[tuple[str, int], Adapter] = {
    # AEMO's wholesale price: no network or retail fees.
    ("aemo_nem", 1): Adapter(
        "aemo_nem",
        1,
        _aud,
        lambda _rows, _extra: False,
        DEFAULT_MAX_AGE_S,
        _rows(price_intervals.aemo_nem_intervals),
        _each(price_intervals.aemo_nem_intervals),
    ),
    ("nem_pd7day", 1): Adapter(
        "nem_pd7day",
        1,
        _aud,
        _pd7day_fees,
        DEFAULT_MAX_AGE_S,
        _rows(price_intervals.pd7day_intervals),
        _each(price_intervals.pd7day_intervals),
    ),
    # Amber's retail price, fees inside.
    ("amber_express", 1): Adapter(
        "amber_express",
        1,
        _aud,
        lambda _rows, _extra: True,
        DEFAULT_MAX_AGE_S,
        _amber_express,
        _amber_express_singles,
    ),
    ("amberelectric", 1): Adapter(
        "amberelectric",
        1,
        _aud,
        lambda _rows, _extra: True,
        DEFAULT_MAX_AGE_S,
        _rows(price_intervals.amber_core_intervals),
        _each(price_intervals.amber_core_intervals),
    ),
    # OpenADR does not say whether a value is a full tariff, and publishes its
    # rows only through its action; the response block states the unit.
    ("openadr3_ven", 1): Adapter(
        "openadr3_ven",
        1,
        lambda _spec: None,
        lambda _rows, _extra: None,
        DEFAULT_MAX_AGE_S,
        _rows(price_intervals.openadr_intervals),
        _each(price_intervals.openadr_intervals),
        action_only=True,
    ),
    # LocalVolts v2: the binding's adapter spec names the `field` (flexUp,
    # flexDown or matched_rate) and the `direction` (Buy, Sell).
    ("localvolts_v2", 1): Adapter(
        "localvolts_v2",
        1,
        _lv_unit,
        _lv_fees,
        DEFAULT_MAX_AGE_S,
        _lv_intervals,
        _lv_singles,
        no_match=_lv_no_match,
    ),
}


def _adapter(source: Mapping[str, Any]) -> Adapter | None:
    spec = source.get("adapter")
    if not isinstance(spec, Mapping):
        return None
    version = spec.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    return ADAPTERS.get((str(spec.get("name")), version))


def _path(container: Any, path: Any) -> Any:
    keys = path if isinstance(path, (list, tuple)) else str(path or "").split(".")
    for key in keys:
        if key == "":
            continue
        if not isinstance(container, Mapping):
            return None
        container = container.get(key)
    return container


def _resolve_attribute(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    adapter = _adapter(source)
    if adapter is None:
        return _missing(role, device_id, prov, "adapter_unknown")
    if adapter.action_only:
        return _missing(role, device_id, prov, "rows_only_in_action_response")
    st = _read_state(ctx, source.get("entity_id"))
    if st is None:
        return _missing(role, device_id, prov, "no_entity")
    if st.get("state") in ("unavailable", "unknown"):
        return _missing(role, device_id, prov, str(st.get("state")), UNAVAILABLE)
    attrs = st.get("attributes") or {}
    rows = _path(attrs, source.get("attribute_path"))
    spec = source.get("adapter") or {}
    extra: Any = attrs.get(price_intervals.AMBER_EXPRESS_DETAILED_KEY)
    if adapter.name == "localvolts_v2":
        extra = _lv_extra(spec, role, binding)
        if extra is None:
            return _missing(role, device_id, prov, "adapter_field_unknown")
    fetched = _when(st.get("last_updated")) or _when(st.get("last_changed"))
    read = _Read(adapter, rows, extra, fetched, adapter.native_unit(spec))
    return _adapt(ctx, device_id, role, dimension, binding, read, prov)


def _lv_extra(
    spec: Mapping[str, Any], role: str, binding: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The LocalVolts field, direction and row qualities for this binding.
    A settled series reads elapsed rows only; anything else reads `Fcst`
    rows only, so a settlement row is never read as a forecast."""
    field = spec.get("field")
    direction = spec.get("direction")
    if field not in price_intervals.LV_RATE_FIELDS | {price_intervals.LV_MATCHED_RATE}:
        return None
    if direction is not None and direction not in price_intervals.LV_DIRECTIONS:
        return None
    settled = _quality(role, binding.get("normalization") or {}) == "settled"
    qualities = (
        price_intervals.LV_ELAPSED_QUALITIES
        if settled
        else price_intervals.LV_FORECAST_QUALITIES
    )
    return {"field": field, "direction": direction, "qualities": qualities}


def _resolve_action(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    prov: dict[str, Any],
) -> Resolved:
    """A KNOWN read-only action only. Anything else is refused before any
    reader is touched (DC-R15): nothing is called."""
    action = str(source.get("action"))
    if action not in READ_ONLY_ACTIONS:
        return _missing(role, device_id, prov, "action_not_read_only")
    adapter = _adapter(source)
    if adapter is None:
        return _missing(role, device_id, prov, "adapter_unknown")
    if ctx.readers.action_response is None:
        return _missing(role, device_id, prov, "no_action_reader")
    target = source.get("target")
    params = dict(source.get("parameters") or {})
    response = ctx.readers.action_response(action, target, params)
    if response is None:
        return _missing(role, device_id, prov, "no_response")
    native_unit = adapter.native_unit(source.get("adapter") or {})
    if adapter.name == "openadr3_ven":
        # Keyed by entity and checked to be a PRICE payload (#1583); the
        # block states its own unit (`$/kWh` in Mark Purcell's capture).
        rows = price_intervals.openadr_rows_from_response(response, str(target))
        block = response.get(str(target)) if isinstance(response, Mapping) else None
        if isinstance(block, Mapping) and isinstance(block.get("unit"), str):
            native_unit = block["unit"]
    else:
        rows = _path(response, source.get("response_path"))
    read = _Read(adapter, rows, None, ctx.now, native_unit)
    return _adapt(ctx, device_id, role, dimension, binding, read, prov)


@dataclass(frozen=True)
class _Read:
    adapter: Adapter
    rows: Any
    extra: Any
    fetched: datetime | None
    native_unit: str | None


def _adapt(
    ctx: _Ctx,
    device_id: str,
    role: str,
    dimension: str,
    binding: Mapping[str, Any],
    read: _Read,
    prov: dict[str, Any],
) -> Resolved:
    adapter = read.adapter
    if dimension != "price":
        return _missing(role, device_id, prov, "adapter_reads_prices_only")
    norm = binding.get("normalization") or {}
    quality = _quality(role, norm)
    if quality is None:
        return _missing(role, device_id, prov, "settlement_is_not_a_forecast")
    if quality == "matched" and role.startswith("import_"):
        # A matched P2P rate is what a peer pays for the household's export,
        # so it prices the export side only (a block's export, for one).
        return _missing(role, device_id, prov, "matched_rate_is_export_side")
    native_unit = norm.get("native_unit") or read.native_unit
    prov["native_unit"] = native_unit
    if not native_unit:
        return _missing(role, device_id, prov, "unit_undeclared")
    scale, unit, refused = _price_scale(native_unit, ctx.currency)
    if scale is None:
        return _missing(role, device_id, prov, str(refused))
    if not isinstance(read.rows, list) or not read.rows:
        return _missing(role, device_id, prov, "no_rows")
    singles = sorted(adapter.singles(read.rows, read.extra), key=lambda i: i.start)
    if any(b.start < a.end for a, b in pairwise(singles)):
        return _missing(role, device_id, prov, "overlapping_intervals")
    rows = adapter.intervals(read.rows, read.extra)
    fees = norm.get("fees_included", adapter.fees_included(read.rows, read.extra))
    series = _series(ctx, device_id, role, rows, scale, unit, prov, quality, binding)
    if adapter.no_match is not None:
        series = _tag_no_match(series, adapter.no_match(read.rows, read.extra))
    declared: Any = (norm.get("freshness") or {}).get("max_age_s")
    max_age = float(declared) if _is_number(declared) else adapter.max_age_s
    return _with_freshness(series, max_age, fees, read.fetched, ctx.now)


def _tag_no_match(series: Series, starts: set[datetime]) -> Series:
    """An explicit P2P no-match keeps its interval (it is the provider's own
    answer, not a gap) at 0.0, tagged `no_match` so it never reads as a match
    at a zero price."""
    if not starts:
        return series
    intervals = tuple(
        dataclasses.replace(i, quality="no_match") if i.start in starts else i
        for i in series.intervals
    )
    return dataclasses.replace(series, intervals=intervals)


def _quality(role: str, norm: Mapping[str, Any]) -> str | None:
    """forecast, matched or settled: separate values, never read as one
    another. A settlement bound under a forecast role is refused."""
    declared = norm.get("quality")
    if role.endswith("_forecast"):
        return None if declared == "settled" else str(declared or "forecast")
    return str(declared or "published")


def _with_freshness(
    series: Series,
    max_age_s: float,
    fees: bool | None,
    fetched: datetime | None,
    now: datetime,
) -> Series:
    """A forecast is stale when its fetch is older than `max_age_s` (the
    binding's own, else the adapter's five minutes), whatever the age of the
    HA state around it."""
    prov = dict(series.provenance)
    prov["freshness"] = {"mode": "forecast", "max_age_s": max_age_s}
    state = series.state
    if (
        state == VALID
        and fetched is not None
        and now - fetched > timedelta(seconds=max_age_s)
    ):
        state = STALE
    # issued_at stays None: none of these providers states one, and it is
    # never invented.
    return dataclasses.replace(
        series, state=state, fetched_at=fetched, provenance=prov, fees_included=fees
    )


def _series(
    ctx: _Ctx,
    device_id: str,
    role: str,
    rows: _Rows,
    scale: float,
    unit: str | None,
    prov: dict[str, Any],
    quality: str,
    binding: Mapping[str, Any],
) -> Series:
    """Intervals as published, scaled and signed once. No gap filled, no
    price extended past its end, negative values kept."""
    sign = _sign(binding)
    prov["sign_flipped"] = sign < 0
    intervals = tuple(
        Interval(r.start, r.end, _canonical(r.value, scale, sign), quality)
        for r in sorted(rows, key=lambda r: r.start)
    )
    return Series(
        role=role,
        device_id=device_id,
        unit=unit,
        intervals=intervals,
        coverage=coverage(intervals, ctx.window),
        state=VALID if intervals else MISSING,
        issued_at=None,
        fetched_at=None,
        provenance=prov,
        reason=None if intervals else "no_usable_rows",
    )


# --- coverage and values over a window (DC-R06) ------------------------------------


def coverage(
    intervals: Sequence[Interval], window: tuple[datetime, datetime] | None = None
) -> Coverage:
    """Measured from the intervals, over `window` (default: first start to
    final end, never the last START). Gaps are listed; nothing is filled."""
    if window is None:
        if not intervals:
            return Coverage(None, None, (), 0.0)
        window = (min(i.start for i in intervals), max(i.end for i in intervals))
    start, end = window
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    covered = timedelta()
    for iv in sorted(intervals, key=lambda i: i.start):
        lo, hi = max(iv.start, cursor), min(iv.end, end)
        if lo >= hi:
            continue
        if lo > cursor:
            gaps.append((cursor, lo))
        covered += hi - lo
        cursor = hi
    if cursor < end:
        gaps.append((cursor, end))
    return Coverage(start, end, tuple(gaps), covered / (end - start))


def value_over(
    series: Series, start: datetime, end: datetime
) -> tuple[float | None, float]:
    """The duration-weighted value over `[start, end)` and the fraction
    covered. Only published intervals count: a 5-minute spike is weighted,
    never sampled away, and an uncovered window is None, never a held price."""
    _check_window(start, end)
    weighted = 0.0
    seconds = 0.0
    for iv in series.intervals:
        lo, hi = max(iv.start, start), min(iv.end, end)
        if lo >= hi:
            continue
        span = (hi - lo).total_seconds()
        weighted += iv.value * span
        seconds += span
    if seconds == 0:
        return None, 0.0
    return weighted / seconds, seconds / (end - start).total_seconds()


def _aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"naive datetime {instant!r}")


def _check_window(start: datetime, end: datetime) -> None:
    _aware(start)
    _aware(end)
    if not start < end:
        raise ValueError("empty or reversed window")


_HANDLERS: dict[str, Callable[..., Resolved]] = {
    "entity_state": _resolve_entity_state,
    "entity_attribute": _resolve_attribute,
    "action_response": _resolve_action,
    "recorder_statistic": _resolve_statistic,
    "constant": _resolve_constant,
    "derived": _resolve_derived,
    "model_output": _resolve_model,
}
