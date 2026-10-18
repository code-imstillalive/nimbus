"""DC-004 legacy import: today's settings read into a DC-001 device document
(docs/specs/device-contract/004-migration-and-compatibility.md, "Legacy
import"; decisions recorded on #1574, 18 Oct 2026).

Pure, like `device_contract`: no Home Assistant import, no I/O. The caller
reads a plain snapshot from HA and passes it in; nothing here reads a sensor,
writes a store or calls a service (DC-R15). **Nothing reads the result yet**:
storage, the dry run's effective diff and the modes are later DC-004 steps,
and every install stays in `legacy` until a household switches it.

    import_legacy(snapshot) -> ImportResult

`snapshot` is a dict:
- `options`: the hub config entry's options;
- `numbers`: the live Solver number values, `{key: value}` (the solve reads
  these, not the options: #565/#567);
- `subentries`: `[{"subentry_id", "subentry_type", "title", "data"}]`, in
  creation order;
- `setup_builder_done`: keys the household confirmed through a #1587 Repair;
- `site`: `{"name", "timezone", "currency"}` from HA's own config.

This first step maps the hub-level settings: Grid, whole-house Load, Battery
and Solar, their bindings and constraints. **Every subentry is kept in
`unmapped` and reported, never dropped** (DC-004 `unmapped_subentry`); their
mapping is the next step.

Rules (DC-004):
- **Deterministic:** the same snapshot gives the same serialised document.
- **An empty field is not permission:** a role filled in one place and empty
  in another imports once, with the filled field's provenance.
- **Conflicts are named, never resolved by order:** the document keeps
  neither value, and the conflict carries both sources.
- **Provenance:** every binding and constraint records `origin:
  legacy_import` and its source key path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from . import device_contract as dc
except ImportError:  # pragma: no cover - loaded as a top-level module in tests
    import device_contract as dc  # type: ignore[no-redef]

#: number.py starts a hardware number at this placeholder: "not set".
PLACEHOLDER = 0.1

#: Same-role sources: (device, role, [(path, option key), ...]). The first
#: non-empty one is the binding; a second, different one is a conflict.
_ROLE_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("grid", "power", ("grid_sensor",)),
    ("grid", "import_price", ("solver_import_price_sensor",)),
    ("grid", "export_price", ("solver_export_price_sensor",)),
    ("battery", "soc", ("solver_battery_soc_sensor",)),
    ("battery", "power", ("solver_battery_power_sensor", "battery_sensor")),
    ("solar", "power", ("solver_solar_power_sensor", "solar_sensor")),
    ("solar", "generation_forecast", ("solver_solar_forecast_sensor",)),
    ("load", "power", ("solver_whole_house_cross_check_sensor",)),
    ("load", "demand_forecast", ("solver_load_forecast_sensor",)),
)

#: (device, constraint, number key). Percent constraints stay percent.
_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("grid", "import_limit", "solver_grid_max_import_kw"),
    ("grid", "export_limit", "solver_grid_max_export_kw"),
    ("battery", "soc_min", "solver_battery_min_soc_percent"),
    ("battery", "soc_max", "solver_battery_max_soc_percent"),
    ("battery", "charge_limit", "solver_max_charge_kw"),
    ("battery", "discharge_limit", "solver_max_discharge_kw"),
)

#: Hardware numbers whose 0.1 floor means "never set" (number.py).
_PLACEHOLDER_KEYS = frozenset(
    {
        "solver_battery_capacity_kwh",
        "solver_max_charge_kw",
        "solver_max_discharge_kw",
        "solver_grid_max_import_kw",
    }
)

_IDS = {"grid": "grid", "load": "load_house", "battery": "battery", "solar": "solar"}


@dataclass(frozen=True)
class Conflict:
    code: str
    path: str
    sources: tuple[tuple[str, Any], ...]


@dataclass
class ImportResult:
    document: dict[str, Any]
    conflicts: list[Conflict] = field(default_factory=list)
    unmapped: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dc.Problem] = field(default_factory=list)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(numbers: dict[str, Any], options: dict[str, Any], key: str) -> Any:
    """A live number, or None when unset or still on its placeholder."""
    value = numbers.get(key, options.get(key))
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if key in _PLACEHOLDER_KEYS and abs(float(value) - PLACEHOLDER) < 1e-9:
        return None
    return value


def _binding(entity_id: str, path: str, confirmed: bool) -> dict[str, Any]:
    return {
        "source": {"kind": "entity_state", "entity_id": entity_id},
        "provenance": {
            "origin": "legacy_import",
            "source_path": path,
            "confirmed": confirmed,
            **({"confirmed_by": "setup_builder"} if confirmed else {}),
        },
    }


def import_legacy(snapshot: dict[str, Any]) -> ImportResult:
    """Today's hub-level settings as a DC-001 document (module docstring)."""
    options = dict(snapshot.get("options") or {})
    numbers = dict(snapshot.get("numbers") or {})
    confirmed_keys = set(snapshot.get("setup_builder_done") or ())
    site_in = snapshot.get("site") or {}
    conflicts: list[Conflict] = []

    bindings: dict[str, dict[str, Any]] = {k: {} for k in _IDS}
    for device, role, keys in _ROLE_SOURCES:
        found = [(f"options.{k}", _text(options.get(k))) for k in keys]
        found = [(p, v) for p, v in found if v]
        distinct = {v for _p, v in found}
        if len(distinct) > 1:
            conflicts.append(
                Conflict(
                    "conflict_role_sources",
                    f"devices.{_IDS[device]}.bindings.{role}",
                    tuple(found),
                )
            )
            continue
        if found:
            path, entity_id = found[0]
            key = path.split(".", 1)[1]
            bindings[device][role] = _binding(entity_id, path, key in confirmed_keys)

    # Battery sign: Solver's own declaration (Forecaster declares none).
    battery_power = bindings["battery"].get("power")
    if battery_power is not None:
        positive_is_charge = bool(
            options.get("solver_battery_power_positive_is_charge", False)
        )
        battery_power["normalization"] = {
            "target_unit": "kW",
            "positive_direction": "discharge",
            "native_positive_direction": "charge"
            if positive_is_charge
            else "discharge",
        }

    constraints: dict[str, dict[str, Any]] = {k: {} for k in _IDS}
    for device, name, key in _CONSTRAINTS:
        live = _number(numbers, {}, key)
        stored = _number({}, options, key)
        if live is not None and stored is not None and float(live) != float(stored):
            conflicts.append(
                Conflict(
                    "conflict_setting_copies",
                    f"devices.{_IDS[device]}.constraints.{name}",
                    ((f"options.{key}", stored), (f"numbers.{key}", live)),
                )
            )
            continue
        value = live if live is not None else stored
        if value is not None:
            constraints[device][name] = value

    devices: list[dict[str, Any]] = [
        {
            "id": "grid",
            "kind": "grid",
            "name": "Grid connection",
            "boundary": {"id": "point_of_connection", "domain": "ac", "phases": "all"},
            "bindings": bindings["grid"],
            "constraints": constraints["grid"],
        },
        {
            "id": "load_house",
            "kind": "load",
            "name": "Whole house",
            "load_class": "aggregate",
            "boundary": {"id": "total_end_use", "domain": "ac", "phases": "all"},
            "bindings": bindings["load"],
            "flexibility": {"mode": "none"},
            "constraints": {},
        },
    ]
    if bindings["battery"] or constraints["battery"]:
        battery: dict[str, Any] = {
            "id": "battery",
            "kind": "battery",
            "name": "Battery",
            "bindings": bindings["battery"],
            "constraints": constraints["battery"],
        }
        capacity_live = _number(numbers, {}, "solver_battery_capacity_kwh")
        capacity_opt = _number({}, options, "solver_battery_capacity_kwh")
        if (
            capacity_live is not None
            and capacity_opt is not None
            and float(capacity_live) != float(capacity_opt)
        ):
            conflicts.append(
                Conflict(
                    "conflict_setting_copies",
                    "devices.battery.capacity",
                    (
                        ("options.solver_battery_capacity_kwh", capacity_opt),
                        ("numbers.solver_battery_capacity_kwh", capacity_live),
                    ),
                )
            )
        else:
            capacity = capacity_live if capacity_live is not None else capacity_opt
            if capacity is not None:
                # The wizard asks for usable capacity (#1800).
                battery["capacity"] = {
                    "value": capacity,
                    "unit": "kWh",
                    "basis": "usable",
                }
        devices.append(battery)
    if bindings["solar"]:
        devices.append(
            {
                "id": "solar",
                "kind": "solar",
                "name": "Solar",
                "bindings": bindings["solar"],
                "constraints": {},
            }
        )

    unmapped = [
        {
            "code": "unmapped_subentry",
            "subentry_id": sub.get("subentry_id"),
            "subentry_type": sub.get("subentry_type"),
            "title": sub.get("title"),
        }
        for sub in snapshot.get("subentries") or ()
    ]

    document: dict[str, Any] = {
        "schema_version": dc.SCHEMA_VERSION,
        "site": {
            "id": "site",
            "name": site_in.get("name") or "Home",
            "timezone": site_in.get("timezone"),
            "currency": site_in.get("currency"),
            # Imported authority stays as it is: nothing here grants control
            # (household, 18 Oct 2026). Controllable-load authority is mapped
            # with the subentries.
            "control_policy": "observation_only",
        },
        "devices": devices,
        "relationships": [{"kind": "supplies", "from": "grid", "to": "load_house"}],
        "accounting": {
            "demand": {
                "mode": "inclusive",
                "root_load_id": "load_house",
                "separate_load_ids": [],
            }
        },
        "shared_constraints": [],
    }
    return ImportResult(
        document=document,
        conflicts=conflicts,
        unmapped=unmapped,
        problems=dc.validate(document),
    )
