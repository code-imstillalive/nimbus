"""The canonical device model: DC-001 (docs/specs/device-contract/
001-canonical-device-model.md, revision 0.2, approved by Mark Purcell on #1651).

A pure, Home Assistant-free module, so the standalone path can validate a
document too (DC-001's own "where the schema lives in code" decision). It reads
no sensors and makes no HA calls.

What is here, and only this (DC-001 "Responsibility"):
* the document's shape, as plain JSON-compatible dicts and lists;
* stable identity and `schema_version`;
* `validate(document) -> list[Problem]`: structural validation, everything
  checkable without reading a sensor;
* `serialise` / `parse`: canonical round-trip.

Not here: converting values, units or signs (DC-002), accounting (DC-003),
storage and migration (DC-004), discovery (DC-005), readiness (DC-006).

**No consumer reads this yet** (DC-001 acceptance; until DC-006). A test pins
that no other module under custom_components/ imports it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

KINDS = ("grid", "solar", "load", "battery")
CONTROL_POLICIES = frozenset({"observation_only", "authorised"})
DOMAINS = frozenset({"ac", "dc"})
PHASES = frozenset({"L1", "L2", "L3"})
LOAD_CLASSES = frozenset({"aggregate", "circuit", "appliance"})
CAPACITY_BASES = frozenset({"nominal", "usable"})
PROVENANCE_ORIGINS = frozenset(
    # `legacy_import`: DC-004's import of today's settings (device_import.py).
    {"manual", "energy_dashboard", "provider_profile", "derived", "legacy_import"}
)
RELATIONSHIP_KINDS = frozenset(
    {"supplies", "included_in_measurement", "shares_equipment"}
)
ACCOUNTING_MODES = frozenset({"inclusive", "residual_plus_children"})

# Roles each kind accepts (DC-001 "Roles each kind accepts").
ROLES: dict[str, frozenset[str]] = {
    "grid": frozenset(
        {
            "power",
            "import_power",
            "export_power",
            "import_energy",
            "export_energy",
            "import_price",
            "export_price",
            "import_price_forecast",
            "export_price_forecast",
            "planned_exchange",
        }
    ),
    "solar": frozenset(
        {"power", "energy", "generation_forecast", "planned_curtailment"}
    ),
    "load": frozenset(
        {"power", "energy", "temperature", "demand_forecast", "scheduled_consumption"}
    ),
    "battery": frozenset(
        {
            "soc",
            "power",
            "charge_power",
            "discharge_power",
            "charge_energy",
            "discharge_energy",
            "availability",
            "target_soc",
            "planned_dispatch",
            "planned_soc",
        }
    ),
}

CONSTRAINT_KEYS: dict[str, frozenset[str]] = {
    "grid": frozenset(
        {
            "import_limit",
            "export_limit",
            "tariff_provider",
            "network_fees",
            "p2p_settlement",
        }
    ),
    "solar": frozenset({"generation_limit", "inverter_limit", "curtailment_allowed"}),
    "load": frozenset(
        {
            "fixed_hours",
            "min_runtime",
            "energy_requirement",
            "operating_window",
            "thermal_comfort",
            "sheddable",
        }
    ),
    "battery": frozenset(
        {
            "soc_min",
            "soc_max",
            "reserve",
            "charge_limit",
            "discharge_limit",
            "charge_efficiency",
            "discharge_efficiency",
            "departure_target",
        }
    ),
}

# One canonical sign per kind (#1574): Grid +import, Battery +discharge,
# Solar +generation, Load +consumption. That is the sign of each kind's
# signed `power` role.
CANONICAL_DIRECTION = {
    "grid": "import",
    "battery": "discharge",
    "solar": "generation",
    "load": "consumption",
}

# (kind, role) -> the direction a positive value means, for every role
# that carries a flow. A magnitude role is positive in its own direction
# (a Grid `export_power` is positive for export). Roles not listed (SoC,
# prices, temperature) have no sign, so a declared direction is not checked.
CANONICAL_ROLE_DIRECTION: dict[tuple[str, str], str] = {
    ("grid", "power"): "import",
    ("grid", "import_power"): "import",
    ("grid", "export_power"): "export",
    ("grid", "import_energy"): "import",
    ("grid", "export_energy"): "export",
    ("battery", "power"): "discharge",
    ("battery", "discharge_power"): "discharge",
    ("battery", "charge_power"): "charge",
    ("battery", "discharge_energy"): "discharge",
    ("battery", "charge_energy"): "charge",
    ("solar", "power"): "generation",
    ("solar", "energy"): "generation",
    ("load", "power"): "consumption",
    ("load", "energy"): "consumption",
}

# source.kind -> required fields (DC-001 "Source variants").
SOURCE_REQUIRED: dict[str, tuple[str, ...]] = {
    "entity_state": ("entity_id",),
    "entity_attribute": ("entity_id", "attribute_path", "adapter"),
    "action_response": ("action", "target", "response_path", "adapter"),
    "recorder_statistic": ("statistic_id", "interval_semantics"),
    "constant": ("value", "unit"),
    "derived": ("calculation", "depends_on"),
    "model_output": ("model", "target"),
}

#: The roles whose binding may hold an ordered `sources` list, combined by
#: `combine` (DC-006 decision 10b; device_forecasts.py). Load totals through
#: DC-003 instead, a separate mechanism.
COMBINE_ROLES: frozenset[tuple[str, str]] = frozenset(
    {("solar", "generation_forecast")}
)
COMBINE_KINDS: frozenset[str] = frozenset({"mean"})


@dataclass(frozen=True)
class Problem:
    """One structural problem: a stable code, a path into the document, a
    sentence."""

    code: str
    path: str
    message: str


# --- serialisation ---------------------------------------------------------


def serialise(document: dict[str, Any]) -> str:
    """Canonical form: keys sorted, lists in their stored order, unknown
    fields kept. JSON only, no YAML tags."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def parse(text: str) -> dict[str, Any]:
    """The document a canonical string holds. Raises for anything
    that is not JSON (ValueError) or not a JSON object (TypeError)."""
    value = json.loads(text)
    if not isinstance(value, dict):
        msg = "a device document is a JSON object"
        raise TypeError(msg)
    return value


# --- validation ------------------------------------------------------------


def validate(document: dict[str, Any]) -> list[Problem]:
    """Every structural problem in `document` (DC-001 "Structural
    validation"). An absent binding is not a problem: it is a capability gap
    for readiness (DC-006)."""
    out: list[Problem] = []

    def add(code: str, path: str, message: str) -> None:
        out.append(Problem(code, path, message))

    version = document.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        add(
            "schema_version_unsupported",
            "schema_version",
            f"schema_version {version!r} is not one this reader knows "
            f"({sorted(SUPPORTED_SCHEMA_VERSIONS)})",
        )
        return out  # nothing else can be read safely

    site = document.get("site") or {}
    devices = document.get("devices") or []
    relationships = document.get("relationships") or []
    accounting = document.get("accounting") or {}
    shared = document.get("shared_constraints") or []

    # Identity: format, and uniqueness across site, devices and boundaries.
    seen: dict[str, str] = {}

    def claim(identifier: Any, path: str) -> None:
        if not isinstance(identifier, str) or not ID_PATTERN.match(identifier):
            add("id_invalid", path, f"{identifier!r} is not a valid id")
            return
        if identifier in seen:
            add(
                "id_duplicate",
                path,
                f"{identifier!r} is already used at {seen[identifier]}",
            )
            return
        seen[identifier] = path

    claim(site.get("id"), "site.id")
    kinds: dict[str, str] = {}
    for i, device in enumerate(devices):
        base = f"devices[{i}]"
        claim(device.get("id"), f"{base}.id")
        if isinstance(device.get("id"), str):
            kinds[device["id"]] = device.get("kind")
        boundary = device.get("boundary") or {}
        if "id" in boundary:
            claim(boundary.get("id"), f"{base}.boundary.id")

    for i, device in enumerate(devices):
        _validate_device(device, f"devices[{i}]", add)

    if "grid" not in kinds.values():
        add(
            "grid_missing",
            "devices",
            "no grid device: the logical Grid exists even when its "
            "measurement does not",
        )

    _validate_relationships(relationships, kinds, add)
    _validate_accounting(accounting, kinds, add)
    for i, entry in enumerate(shared):
        for j, ref in enumerate(entry.get("devices") or []):
            if ref not in kinds:
                add(
                    "reference_unresolved",
                    f"shared_constraints[{i}].devices[{j}]",
                    f"{ref!r} is not a device",
                )
    _validate_binding_sources(devices, add)
    _validate_dependencies(devices, kinds, add)
    return out


def _validate_device(device: dict[str, Any], base: str, add: Any) -> None:
    kind = device.get("kind")
    if kind not in KINDS:
        add(
            "role_not_allowed_for_kind",
            f"{base}.kind",
            f"{kind!r} is not a device kind",
        )
        return
    for role, binding in (device.get("bindings") or {}).items():
        path = f"{base}.bindings.{role}"
        if role not in ROLES[kind]:
            add("role_not_allowed_for_kind", path, f"a {kind} has no {role!r} role")
            continue
        _validate_binding(binding or {}, kind, role, path, add)
    for key in device.get("constraints") or {}:
        if key not in CONSTRAINT_KEYS[kind]:
            add(
                "constraint_key_not_allowed_for_kind",
                f"{base}.constraints.{key}",
                f"a {kind} has no {key!r} constraint",
            )
    if kind == "battery":
        capacity = device.get("capacity")
        if capacity is None:
            # Household, #1728 (14 Oct): a battery the Solver cannot size is
            # not a usable battery, so its capacity is required, not optional.
            add(
                "capacity_missing",
                f"{base}.capacity",
                "a battery must state its capacity",
            )
        elif (capacity or {}).get("basis") not in CAPACITY_BASES:
            add(
                "capacity_basis_missing",
                f"{base}.capacity",
                "battery capacity must say whether it is nominal or usable",
            )
        constraints = device.get("constraints") or {}
        lo: Any = constraints.get("soc_min")
        hi: Any = constraints.get("soc_max")
        if _is_number(lo) and _is_number(hi) and lo > hi:
            add(
                "soc_bounds_inverted",
                f"{base}.constraints",
                f"soc_min {lo} is greater than soc_max {hi}",
            )


def _validate_binding(
    binding: dict[str, Any], kind: str, role: str, path: str, add: Any
) -> None:
    if "sources" in binding:
        _validate_combined(binding, kind, role, path, add)
    else:
        _validate_source(binding.get("source") or {}, f"{path}.source", add)
    direction = (binding.get("normalization") or {}).get("positive_direction")
    canonical = CANONICAL_ROLE_DIRECTION.get((kind, role))
    if direction is not None and canonical is not None and direction != canonical:
        add(
            "sign_not_canonical",
            f"{path}.normalization.positive_direction",
            f"a {kind} {role} is positive for {canonical!r}, not {direction!r}",
        )


def _validate_source(source: dict[str, Any], path: str, add: Any) -> None:
    variant = str(source.get("kind"))
    required = SOURCE_REQUIRED.get(variant)
    if required is None:
        add(
            "source_fields_missing",
            f"{path}.kind",
            f"{variant!r} is not a source kind",
        )
        return
    missing = [f for f in required if source.get(f) in (None, "", [])]
    if missing:
        add(
            "source_fields_missing",
            path,
            f"a {variant} source needs {', '.join(missing)}",
        )
    continued = source.get("continued_by")
    if continued is not None and (
        variant != "entity_attribute"
        or not isinstance(continued, list)
        or not all(isinstance(e, str) and e for e in continued)
    ):
        add(
            "source_fields_missing",
            f"{path}.continued_by",
            "continued_by is a list of entity_ids on an entity_attribute source",
        )


def _validate_combined(
    binding: dict[str, Any], kind: str, role: str, path: str, add: Any
) -> None:
    """An ordered `sources` list (DC-006 decision 10b): only on a role that
    may combine, with a `combine` this reader knows, and each member a
    valid source."""
    if (kind, role) not in COMBINE_ROLES:
        add(
            "combine_not_allowed",
            f"{path}.sources",
            f"a {kind} {role} binding names one source",
        )
        return
    if binding.get("combine") not in COMBINE_KINDS:
        add(
            "combine_unknown",
            f"{path}.combine",
            f"{binding.get('combine')!r} is not a combine ({sorted(COMBINE_KINDS)})",
        )
    sources = binding.get("sources")
    if not isinstance(sources, list) or not sources:
        add("source_fields_missing", f"{path}.sources", "sources is a non-empty list")
        return
    for j, source in enumerate(sources):
        _validate_source(
            source if isinstance(source, dict) else {}, f"{path}.sources[{j}]", add
        )


def _validate_relationships(
    relationships: list[dict[str, Any]], kinds: dict[str, str], add: Any
) -> None:
    parents: dict[Any, set[Any]] = {}
    for i, rel in enumerate(relationships):
        base = f"relationships[{i}]"
        kind = rel.get("kind")
        refs: tuple[tuple[str, Any], ...]
        if kind == "supplies":
            refs = (("from", rel.get("from")), ("to", rel.get("to")))
        elif kind == "included_in_measurement":
            refs = (("child", rel.get("child")), ("parent", rel.get("parent")))
            if rel.get("child") is not None and rel.get("child") == rel.get("parent"):
                add(
                    "relationship_self",
                    base,
                    f"{rel.get('child')!r} cannot include itself",
                )
            else:
                parents.setdefault(rel.get("child"), set()).add(rel.get("parent"))
        elif kind == "shares_equipment":
            refs = tuple(
                (f"devices[{j}]", ref) for j, ref in enumerate(rel.get("devices") or [])
            )
        else:
            add(
                "reference_unresolved",
                f"{base}.kind",
                f"{kind!r} is not a relationship kind",
            )
            continue
        for name, ref in refs:
            if ref not in kinds:
                add(
                    "reference_unresolved", f"{base}.{name}", f"{ref!r} is not a device"
                )
    for cycle in _cycles(parents):
        add(
            "inclusion_cycle",
            "relationships",
            "included_in_measurement cycle: " + " -> ".join(cycle),
        )


def _validate_accounting(
    accounting: dict[str, Any], kinds: dict[str, str], add: Any
) -> None:
    demand = accounting.get("demand") or {}
    root = demand.get("root_load_id")
    refs = [("accounting.demand.root_load_id", root)] if root is not None else []
    refs += [
        (f"accounting.demand.separate_load_ids[{i}]", ref)
        for i, ref in enumerate(demand.get("separate_load_ids") or [])
    ]
    for path, ref in refs:
        if ref not in kinds:
            add("reference_unresolved", path, f"{ref!r} is not a device")
        elif kinds[ref] != "load":
            add(
                "accounting_kind_mismatch",
                path,
                f"{ref!r} is a {kinds[ref]}, not a load",
            )


def _source_identity(source: dict[str, Any]) -> tuple[Any, ...] | None:
    """The identity two bindings share when they read the same source
    (DC-001 `binding_source_duplicate`): registry_id, else entity_id plus
    attribute_path, else statistic_id."""
    if source.get("registry_id"):
        return ("registry", source["registry_id"])
    if source.get("entity_id"):
        return ("entity", source["entity_id"], source.get("attribute_path"))
    if source.get("statistic_id"):
        return ("statistic", source["statistic_id"])
    return None


def _validate_binding_sources(devices: list[dict[str, Any]], add: Any) -> None:
    owner: dict[tuple[Any, ...], tuple[Any, str]] = {}
    for i, device in enumerate(devices):
        for role, binding in (device.get("bindings") or {}).items():
            for path, source in _binding_sources(binding or {}):
                ident = _source_identity(source)
                if ident is None:
                    continue
                key = (role, *ident)
                if key in owner and owner[key][0] != device.get("id"):
                    add(
                        "binding_source_duplicate",
                        f"devices[{i}].bindings.{role}.{path}",
                        f"the same source is bound as {role!r} on "
                        f"{owner[key][0]!r} and {device.get('id')!r}",
                    )
                else:
                    owner.setdefault(key, (device.get("id"), role))


def _binding_sources(binding: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """A binding's sources with their paths: its `source`, or each of an
    ordered `sources` list."""
    sources = binding.get("sources")
    if isinstance(sources, list):
        return [
            (f"sources[{j}]", s) for j, s in enumerate(sources) if isinstance(s, dict)
        ]
    return [("source", binding.get("source") or {})]


def _validate_dependencies(
    devices: list[dict[str, Any]], kinds: dict[str, str], add: Any
) -> None:
    """DC-R16: a derived or model_output binding cannot depend on itself,
    directly or transitively. Nodes are `device_id.role`."""
    edges: dict[str, set[str]] = {}
    for i, device in enumerate(devices):
        for role, binding in (device.get("bindings") or {}).items():
            source = (binding or {}).get("source") or {}
            node = f"{device.get('id')}.{role}"
            if source.get("kind") == "derived":
                targets = list(source.get("depends_on") or [])
            elif source.get("kind") == "model_output":
                targets = [source.get("target")] if source.get("target") else []
            else:
                continue
            for j, target in enumerate(targets):
                ref = str(target).split(".", 1)[0]
                if ref not in kinds:
                    add(
                        "reference_unresolved",
                        f"devices[{i}].bindings.{role}.source[{j}]",
                        f"{target!r} names no device",
                    )
                edges.setdefault(node, set()).add(str(target))
    for cycle in _cycles(edges):
        add("dependency_cycle", "devices", "dependency cycle: " + " -> ".join(cycle))


def _cycles(edges: dict[Any, set[Any]]) -> list[list[Any]]:
    """Each distinct cycle in a directed graph, once, as a closed path."""
    found: list[list[Any]] = []
    seen_sets: set[frozenset[Any]] = set()
    state: dict[Any, int] = {}  # 1 visiting, 2 done

    def visit(node: Any, path: list[Any]) -> None:
        state[node] = 1
        path.append(node)
        for nxt in sorted(edges.get(node, ()), key=str):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt) :] + [nxt]
                key = frozenset(cycle)
                if key not in seen_sets:
                    seen_sets.add(key)
                    found.append(cycle)
            elif state.get(nxt) is None:
                visit(nxt, path)
        path.pop()
        state[node] = 2

    for node in sorted(edges, key=str):
        if state.get(node) is None:
            visit(node, [])
    return found


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
