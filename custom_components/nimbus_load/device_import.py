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
            binding = _binding(entity_id, path, key in confirmed_keys)
            if len(found) > 1:
                # Every key that held this value, so a view writes back to
                # exactly those and never fills one that was empty.
                binding["provenance"]["also_source_paths"] = [p for p, _v in found[1:]]
            bindings[device][role] = binding

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

    unmapped: list[dict[str, Any]] = []
    authorised = _map_subentries(
        snapshot.get("subentries") or (), devices, conflicts, unmapped
    )

    document: dict[str, Any] = {
        "schema_version": dc.SCHEMA_VERSION,
        "site": {
            "id": "site",
            "name": site_in.get("name") or "Home",
            "timezone": site_in.get("timezone"),
            "currency": site_in.get("currency"),
            # Imported authority stays as it is (household, 18 Oct 2026):
            # `authorised` only where a Controllable Load already commands a
            # device entity; nothing here grants new control.
            "control_policy": "authorised" if authorised else "observation_only",
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


# --- step 2: subentries ------------------------------------------------------


def _slug(text: Any, fallback: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in str(text or "").lower())
    out = "_".join(part for part in out.split("_") if part)
    if not out or not out[0].isalpha():
        out = f"{fallback}_{out}" if out else fallback
    return out[:40]


def _unique(base: str, taken: set[str]) -> str:
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}_{n}", n + 1
    taken.add(ident)
    return ident


def _entity_binding(sub: dict[str, Any], key: str, entity: str) -> dict[str, Any]:
    return {
        "source": {"kind": "entity_state", "entity_id": entity},
        "provenance": {
            "origin": "legacy_import",
            "source_path": f"subentry:{sub.get('subentry_id')}.{key}",
            "confirmed": False,
        },
    }


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: A Power Signal's role -> the hub device it measures.
_SIGNAL_DEVICE = {"grid": "grid", "battery": "battery", "solar": "solar"}


def _map_subentries(
    subentries: Any,
    devices: list[dict[str, Any]],
    conflicts: list[Conflict],
    unmapped: list[dict[str, Any]],
) -> bool:
    """Subentries onto the document, in creation order (DC-004 step 2).
    Returns whether any device keeps imported control authority.

    Kept as `unmapped`, never guessed:
    - a Power Signal of role "other" (the whole house, or a circuit: the
      household's call), temperature or humidity;
    - a battery Power Signal with no battery device to bind to (a battery
      needs a capacity, DC-001);
    - topology equipment (power source, PV string, battery tower): DC-005.
    """
    by_id = {d["id"]: d for d in devices}
    taken = set(by_id) | {"site"}
    authorised = False

    def keep(sub: dict[str, Any], code: str) -> None:
        unmapped.append(
            {
                "code": code,
                "subentry_id": sub.get("subentry_id"),
                "subentry_type": sub.get("subentry_type"),
                "title": sub.get("title"),
            }
        )

    for sub in subentries:
        kind = sub.get("subentry_type")
        data = dict(sub.get("data") or {})
        sid = sub.get("subentry_id")
        if kind == "power_signal":
            role = data.get("signal_role")
            entity = _text(data.get("load_sensor"))
            if entity is None or role not in _SIGNAL_DEVICE:
                keep(sub, "unmapped_signal_role")
                continue
            target = by_id.get(_SIGNAL_DEVICE[role])
            if target is None:
                if role == "battery":
                    keep(sub, "unmapped_battery_without_capacity")
                    continue
                target = {
                    "id": role,
                    "kind": role,
                    "name": role.title(),
                    "bindings": {},
                    "constraints": {},
                }
                devices.append(target)
                by_id[role] = target
            existing = target["bindings"].get("power")
            if existing is None:
                binding = _entity_binding(sub, "load_sensor", entity)
                binding["history_subentry_id"] = sid
                target["bindings"]["power"] = binding
            elif existing["source"].get("entity_id") != entity:
                conflicts.append(
                    Conflict(
                        "conflict_role_sources",
                        f"devices.{target['id']}.bindings.power",
                        (
                            (
                                existing["provenance"]["source_path"],
                                existing["source"]["entity_id"],
                            ),
                            (f"subentry:{sid}.load_sensor", entity),
                        ),
                    )
                )
                del target["bindings"]["power"]
            else:
                existing["history_subentry_id"] = sid
                existing["provenance"].setdefault("also_source_paths", []).append(
                    f"subentry:{sid}.load_sensor"
                )
        elif kind == "load":
            ident = _unique(_slug(sub.get("title"), "load"), taken)
            device: dict[str, Any] = {
                "id": ident,
                "kind": "load",
                "name": sub.get("title") or ident,
                "load_class": "circuit",
                "bindings": {},
                "flexibility": {"mode": "none"},
                "constraints": {},
                "history_subentry_id": sid,
            }
            entity = _text(data.get("load_sensor"))
            if entity:
                device["bindings"]["power"] = _entity_binding(
                    sub, "load_sensor", entity
                )
            start, end = data.get("schedule_start_hour"), data.get("schedule_end_hour")
            if start is not None and end is not None:
                device["constraints"]["fixed_hours"] = {
                    "start": start,
                    "end": end,
                    "expected_kw": data.get("expected_load_kw"),
                }
            # Inclusion in the whole-house total is not assumed (DC-003): the
            # household confirms it. Accounting stays inclusive on the house.
            devices.append(device)
            by_id[ident] = device
        elif kind == "battery_participant":
            name = data.get("battery_participant_name") or sub.get("title")
            ident = _unique(_slug(name, "battery"), taken)
            device = {
                "id": ident,
                "kind": "battery",
                "name": name or ident,
                "bindings": {},
                "constraints": {},
                "history_subentry_id": sid,
            }
            soc = _text(data.get("battery_participant_soc_sensor"))
            if soc:
                device["bindings"]["soc"] = _entity_binding(
                    sub, "battery_participant_soc_sensor", soc
                )
            power = _text(data.get("battery_participant_power_sensor"))
            if power:
                binding = _entity_binding(
                    sub, "battery_participant_power_sensor", power
                )
                binding["normalization"] = {
                    "target_unit": "kW",
                    "positive_direction": "discharge",
                    "native_positive_direction": "charge"
                    if data.get("battery_participant_power_positive_is_charge")
                    else "discharge",
                }
                device["bindings"]["power"] = binding
            cap = data.get("battery_participant_capacity_kwh")
            if _is_num(cap):
                device["capacity"] = {"value": cap, "unit": "kWh", "basis": "usable"}
            for key, name_ in (
                ("battery_participant_min_soc_percent", "soc_min"),
                ("battery_participant_max_soc_percent", "soc_max"),
                ("battery_participant_max_charge_kw", "charge_limit"),
                ("battery_participant_max_discharge_kw", "discharge_limit"),
            ):
                if _is_num(data.get(key)):
                    device["constraints"][name_] = data[key]
            devices.append(device)
            by_id[ident] = device
        elif kind == "controllable_load":
            name = data.get("controllable_load_name") or sub.get("title")
            ident = _unique(_slug(name, "load"), taken)
            device = {
                "id": ident,
                "kind": "load",
                "name": name or ident,
                "load_class": "appliance",
                "bindings": {},
                "flexibility": {"mode": data.get("controllable_load_kind") or "none"},
                "constraints": {},
                "history_subentry_id": sid,
            }
            power = _text(data.get("controllable_load_power_sensor"))
            if power:
                device["bindings"]["power"] = _entity_binding(
                    sub, "controllable_load_power_sensor", power
                )
            endpoint = _text(data.get("controllable_load_device_entity"))
            if endpoint:
                # Existing authority, carried across unchanged (household,
                # 18 Oct 2026): Nimbus already commands this entity.
                device["control"] = {
                    "entity_id": endpoint,
                    "authorised": True,
                    "provenance": _entity_binding(
                        sub, "controllable_load_device_entity", endpoint
                    )["provenance"],
                }
                authorised = True
            devices.append(device)
            by_id[ident] = device
        elif kind in ("power_source", "pv_string", "battery_tower"):
            keep(sub, "unmapped_topology_equipment")
        else:
            keep(sub, "unmapped_subentry")
    return authorised


# --- step 4: compatibility views and the dry run -----------------------------

#: Hub constraints -> the live number each came from (and goes back to).
_HUB_NUMBERS = {(d, c): k for d, c, k in _CONSTRAINTS}
#: Battery-participant constraints -> its subentry keys.
_PARTICIPANT_KEYS = {
    "soc_min": "battery_participant_min_soc_percent",
    "soc_max": "battery_participant_max_soc_percent",
    "charge_limit": "battery_participant_max_charge_kw",
    "discharge_limit": "battery_participant_max_discharge_kw",
}


def _write(view: dict[str, Any], path: str, value: Any) -> None:
    """Put `value` at a provenance path: options.<key> or subentry:<id>.<key>."""
    if path.startswith("options."):
        view["options"][path.split(".", 1)[1]] = value
    elif path.startswith("subentry:"):
        sid, key = path[len("subentry:") :].split(".", 1)
        view["subentries"].setdefault(sid, {})[key] = value


def legacy_view(document: dict[str, Any]) -> dict[str, Any]:
    """Today's settings generated from `document`: one way, owned keys only
    (DC-004 "Compatibility views"). Each binding goes back to exactly the
    key(s) it was imported from, so a field that was empty stays empty. No
    subentry is created or removed: a device without a source subentry
    writes nothing."""
    view: dict[str, Any] = {"options": {}, "numbers": {}, "subentries": {}}
    hub_ids = set(_IDS.values())
    for device in document.get("devices") or ():
        sid = device.get("history_subentry_id")
        hub = device["id"] in hub_ids
        for binding in (device.get("bindings") or {}).values():
            prov = binding.get("provenance") or {}
            entity = (binding.get("source") or {}).get("entity_id")
            paths = [prov.get("source_path"), *(prov.get("also_source_paths") or [])]
            for path in paths:
                if path:
                    _write(view, path, entity)
            native = (binding.get("normalization") or {}).get(
                "native_positive_direction"
            )
            if native is None or device["kind"] != "battery":
                continue
            if prov.get("source_path") == "options.solver_battery_power_sensor":
                view["options"]["solver_battery_power_positive_is_charge"] = (
                    native == "charge"
                )
            elif sid and not hub:
                view["subentries"].setdefault(sid, {})[
                    "battery_participant_power_positive_is_charge"
                ] = native == "charge"
        constraints = device.get("constraints") or {}
        capacity = (device.get("capacity") or {}).get("value")
        if hub:
            short = next((k for k, v in _IDS.items() if v == device["id"]), None)
            for name, value in constraints.items():
                key = _HUB_NUMBERS.get((short, name))
                if key:
                    view["numbers"][key] = value
            if device["kind"] == "battery" and capacity is not None:
                view["numbers"]["solver_battery_capacity_kwh"] = capacity
            continue
        if not sid:
            continue
        sub = view["subentries"].setdefault(sid, {})
        if device["kind"] == "battery":
            for name, key in _PARTICIPANT_KEYS.items():
                if name in constraints:
                    sub[key] = constraints[name]
            if capacity is not None:
                sub["battery_participant_capacity_kwh"] = capacity
        hours = constraints.get("fixed_hours")
        if hours:
            sub["schedule_start_hour"] = hours.get("start")
            sub["schedule_end_hour"] = hours.get("end")
            sub["expected_load_kw"] = hours.get("expected_kw")
        control = device.get("control")
        if control:
            sub["controllable_load_device_entity"] = control.get("entity_id")
    view["subentries"] = {k: v for k, v in view["subentries"].items() if v}
    return view


@dataclass
class Preview:
    document: dict[str, Any]
    conflicts: list[Conflict]
    unmapped: list[dict[str, Any]]
    problems: list[dc.Problem]
    #: (path, today's value, the view's value) for every owned key that would
    #: change. Empty = migration changes no consumer's input.
    effective_diff: list[tuple[str, Any, Any]]

    @property
    def clean(self) -> bool:
        return not (self.effective_diff or self.conflicts or self.problems)


_SUBENTRY_OWNED: dict[str, tuple[str, ...]] = {
    "load": (
        "load_sensor",
        "schedule_start_hour",
        "schedule_end_hour",
        "expected_load_kw",
    ),
    "power_signal": ("load_sensor",),
    "battery_participant": (
        "battery_participant_soc_sensor",
        "battery_participant_power_sensor",
        "battery_participant_power_positive_is_charge",
        "battery_participant_capacity_kwh",
        *_PARTICIPANT_KEYS.values(),
    ),
    "controllable_load": (
        "controllable_load_power_sensor",
        "controllable_load_device_entity",
    ),
}


def owned_keys(snapshot: dict[str, Any]) -> list[str]:
    """Every key path a view may write for this snapshot. Keys outside it
    (tunables, unmapped subentries) are never touched."""
    paths = {f"options.{k}" for _d, _r, keys in _ROLE_SOURCES for k in keys}
    paths.add("options.solver_battery_power_positive_is_charge")
    paths |= {f"numbers.{k}" for _d, _c, k in _CONSTRAINTS}
    paths.add("numbers.solver_battery_capacity_kwh")
    for sub in snapshot.get("subentries") or ():
        kind = sub.get("subentry_type")
        keys = _SUBENTRY_OWNED.get(kind, ())
        role = (sub.get("data") or {}).get("signal_role")
        if kind == "power_signal" and role not in _SIGNAL_DEVICE:
            keys = ()  # an unmapped Power Signal is left alone
        paths |= {f"subentry:{sub.get('subentry_id')}.{k}" for k in keys}
    return sorted(paths)


def _today(snapshot: dict[str, Any], path: str) -> Any:
    options = snapshot.get("options") or {}
    if path.startswith("options."):
        return options.get(path.split(".", 1)[1])
    if path.startswith("numbers."):
        key = path.split(".", 1)[1]
        return _number(snapshot.get("numbers") or {}, options, key)
    sid, key = path[len("subentry:") :].split(".", 1)
    for sub in snapshot.get("subentries") or ():
        if sub.get("subentry_id") == sid:
            return (sub.get("data") or {}).get(key)
    return None


def _from_view(view: dict[str, Any], path: str) -> Any:
    if path.startswith("options."):
        return view["options"].get(path.split(".", 1)[1])
    if path.startswith("numbers."):
        return view["numbers"].get(path.split(".", 1)[1])
    sid, key = path[len("subentry:") :].split(".", 1)
    return view["subentries"].get(sid, {}).get(key)


def _same(a: Any, b: Any) -> bool:
    if a in (None, "", []) and b in (None, "", []):
        return True
    if a in (None, False) and b in (None, False):
        return True  # an unset flag and False mean the same to every reader
    if _is_num(a) and _is_num(b):
        return float(a) == float(b)
    return bool(a == b)


def preview(snapshot: dict[str, Any]) -> Preview:
    """The dry run (DC-004): import, generate the views, compare every owned
    key with today's value. Writes nothing."""
    res = import_legacy(snapshot)
    view = legacy_view(res.document)
    diff = [
        (path, before, after)
        for path in owned_keys(snapshot)
        if not _same(before := _today(snapshot, path), after := _from_view(view, path))
    ]
    return Preview(res.document, res.conflicts, res.unmapped, res.problems, diff)
