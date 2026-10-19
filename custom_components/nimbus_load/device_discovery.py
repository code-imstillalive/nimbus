"""Discovery: what Home Assistant already knows, as a proposal for the
household to confirm (DC-005, docs/specs/device-contract/
005-discovery-and-provider-selection.md; nimbus #1574).

**A proposal, never a mapping.** Every binding this module returns has
`provenance.confirmed: false`. A role whose evidence is ambiguous or
insufficient gets no binding and a question instead. Nothing here stores,
commits or calls a service; a proposal becomes stored state only through
DC-004's commit, after the household confirms it (DC-R13, DC-R15).

Step 1 (this module so far): the Energy Dashboard
-------------------------------------------------
What the household already told Home Assistant's Energy Dashboard, read by
`device_resolver` (#1590), becomes:

* one Grid: its import and export energy statistics, HA's power statistic
  (`stat_rate`, positive from the grid) and its import and export price
  entities;
* one Solar per solar source (two inverters are two devices, DC-001 / F02),
  with its energy statistic and power;
* one Battery per battery source, with its charge and discharge energy, power
  (HA's sign is the contract's: positive discharging) and SoC. A battery's
  capacity is not in the Energy Dashboard, so it is always a question;
* the whole-house Load, and one Load per individual device, with its energy
  and power. `included_in_stat` becomes an `included_in_measurement`
  relationship to the device whose total contains it; a top-level device is
  included in the whole house, which is how the Energy Dashboard itself
  counts untracked consumption.

Several sources for one role (an older grid source with two import flows,
or two grid sources) are a question listing every one: list position never
decides (Mark's review of #1590). A fixed `number_energy_price` is reported,
not proposed, as `hub_options` does today: Nimbus wants a live price.

Pure: no Home Assistant import. The HA side gathers the preferences and the
registry ids (DC-005 step 5).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from . import device_contract as dc
    from . import device_resolver as dr
except ImportError:  # loaded standalone (tests, the standalone path)
    import device_contract as dc  # type: ignore[no-redef]
    import device_resolver as dr  # type: ignore[no-redef]

# Long-term statistics the Energy Dashboard reads are hourly changes.
STATISTIC_SEMANTICS = "hourly_change"

# Question and finding codes.
Q_SEVERAL_SOURCES = "several_sources"
Q_FACT_NEEDED = "fact_needed"
F_FIXED_PRICE = "fixed_price_not_proposed"
F_INCLUDED_IN_UNKNOWN = "included_in_unknown_statistic"
F_SOLAR_FORECAST = "solar_forecast_config_entry"
F_NO_ENERGY_DASHBOARD = "no_energy_dashboard"

_GRID_ID = "grid"
_HOUSE_ID = "load_house"


@dataclass
class Proposal:
    """DC-005 "The proposal"."""

    document: dict[str, Any]
    questions: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dc.Problem] = field(default_factory=list)


def _evidence(field_name: str, index: int, section: str) -> dict[str, Any]:
    return {
        "kind": dr.ENERGY_DASHBOARD,
        "section": section,
        "index": index,
        "field": field_name,
    }


def _provenance(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin": "energy_dashboard",
        "confirmed": False,
        "overridden": False,
        "evidence": [evidence],
    }


def _entity(
    entity_id: str,
    evidence: dict[str, Any],
    registry_ids: Mapping[str, str],
    direction: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {"kind": "entity_state", "entity_id": entity_id}
    if entity_id in registry_ids:
        source["registry_id"] = registry_ids[entity_id]
    binding: dict[str, Any] = {"source": source, "provenance": _provenance(evidence)}
    if direction is not None:
        # HA's Energy Dashboard power is already in the contract's sign
        # (device_resolver's module doc): native and canonical agree.
        binding["normalization"] = {
            "positive_direction": direction,
            "native_positive_direction": direction,
        }
    return binding


def _statistic(statistic_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "kind": "recorder_statistic",
            "statistic_id": statistic_id,
            "interval_semantics": STATISTIC_SEMANTICS,
        },
        "provenance": _provenance(evidence),
    }


def _slug(text: str | None, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = fallback
    return slug[:50]


def _unique(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


class _Builder:
    def __init__(self, registry_ids: Mapping[str, str]) -> None:
        self.registry_ids = registry_ids
        self.questions: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []

    def one(
        self,
        device: dict[str, Any],
        role: str,
        candidates: list[tuple[str, dict[str, Any]]],
        make: Any,
    ) -> None:
        """Bind `role` when exactly one distinct source was found; ask when
        several were; do nothing when none was."""
        distinct = list(dict.fromkeys(source for source, _ev in candidates))
        if len(distinct) == 1:
            source, evidence = candidates[0]
            device["bindings"][role] = make(source, evidence)
        elif len(distinct) > 1:
            self.questions.append(
                {
                    "code": Q_SEVERAL_SOURCES,
                    "device": device["id"],
                    "role": role,
                    "candidates": [
                        {"source": source, "evidence": evidence}
                        for source, evidence in candidates
                    ],
                }
            )


def propose_from_energy_dashboard(
    prefs: Mapping[str, Any] | None,
    *,
    site: Mapping[str, Any] | None = None,
    registry_ids: Mapping[str, str] | None = None,
) -> Proposal:
    """The Energy Dashboard's preferences as a DC-005 proposal (module doc).

    `site` supplies `name`, `timezone` and `currency` from Home Assistant's
    own configuration; `registry_ids` maps an entity_id to its entity
    registry entry id, so a later rediscovery can match it after a rename
    (DC-005 decision 2)."""
    b = _Builder(registry_ids or {})
    sources = dr.parse_energy_sources(prefs)
    consumers = dr.device_consumption(prefs)
    raw_sources = [s for s in (prefs or {}).get("energy_sources") or []]
    if not sources and not consumers:
        b.findings.append(
            {
                "code": F_NO_ENERGY_DASHBOARD,
                "message": "the Energy Dashboard names no grid, solar, battery "
                "or individual device",
            }
        )

    taken = {_GRID_ID, _HOUSE_ID}
    grid: dict[str, Any] = {
        "id": _GRID_ID,
        "kind": "grid",
        "name": "Grid connection",
        "boundary": {"id": "point_of_connection", "domain": "ac", "phases": "all"},
        "bindings": {},
        "constraints": {},
    }
    taken.add("point_of_connection")
    house: dict[str, Any] = {
        "id": _HOUSE_ID,
        "kind": "load",
        "name": "Whole house",
        "load_class": "aggregate",
        "boundary": {"id": "total_end_use", "domain": "ac", "phases": "all"},
        "bindings": {},
        "flexibility": {"mode": "none"},
        "constraints": {},
    }
    taken.add("total_end_use")
    devices: list[dict[str, Any]] = [grid, house]
    relationships: list[dict[str, Any]] = [
        {"kind": "supplies", "from": _GRID_ID, "to": _HOUSE_ID}
    ]

    # --- Grid: every grid source pools into the one Grid, per role ----------
    grid_roles: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "import_energy": [],
        "export_energy": [],
        "power": [],
        "import_price": [],
        "export_price": [],
    }
    for src in sources:
        if src.kind != "grid":
            continue
        for role, values, name in (
            ("import_energy", src.energy_from, "stat_energy_from"),
            ("export_energy", src.energy_to, "stat_energy_to"),
            ("power", src.rate, "stat_rate"),
            ("import_price", src.price_import, "entity_energy_price"),
            ("export_price", src.price_export, "entity_energy_price_export"),
        ):
            for value in values:
                grid_roles[role].append(
                    (value, _evidence(name, src.index, "energy_sources"))
                )
        raw = raw_sources[src.index] if src.index < len(raw_sources) else {}
        _fixed_prices(raw, src.index, b)

    regs = b.registry_ids
    b.one(grid, "import_energy", grid_roles["import_energy"], _statistic_maker())
    b.one(grid, "export_energy", grid_roles["export_energy"], _statistic_maker())
    b.one(grid, "power", grid_roles["power"], _entity_maker(regs, "import"))
    b.one(grid, "import_price", grid_roles["import_price"], _entity_maker(regs))
    b.one(grid, "export_price", grid_roles["export_price"], _entity_maker(regs))

    # --- Solar and Battery: one device per source ----------------------------
    for src in sources:

        def evidence(name: str, src: dr.EnergySource = src) -> dict[str, Any]:
            return _evidence(name, src.index, "energy_sources")

        if src.kind == "solar":
            device = {
                "id": _unique("solar", taken),
                "kind": "solar",
                "name": src.name or "Solar",
                "bindings": {},
                "constraints": {},
            }
            b.one(
                device,
                "energy",
                [(v, evidence("stat_energy_from")) for v in src.energy_from],
                _statistic_maker(),
            )
            b.one(
                device,
                "power",
                [(v, evidence("stat_rate")) for v in src.rate],
                _entity_maker(regs, "generation"),
            )
            for entry in src.forecast_entries:
                b.findings.append(
                    {
                        "code": F_SOLAR_FORECAST,
                        "device": device["id"],
                        "config_entry_id": entry,
                        "message": "a solar forecast integration is configured "
                        "for this source; its forecast entity is matched by a "
                        "provider profile, not by the Energy Dashboard",
                    }
                )
            devices.append(device)
            relationships.append(
                {"kind": "supplies", "from": device["id"], "to": _HOUSE_ID}
            )
        elif src.kind == "battery":
            device = {
                "id": _unique("battery", taken),
                "kind": "battery",
                "name": src.name or "Battery",
                "bindings": {},
                "constraints": {},
            }
            b.one(
                device,
                "discharge_energy",
                [(v, evidence("stat_energy_from")) for v in src.energy_from],
                _statistic_maker(),
            )
            b.one(
                device,
                "charge_energy",
                [(v, evidence("stat_energy_to")) for v in src.energy_to],
                _statistic_maker(),
            )
            b.one(
                device,
                "power",
                [(v, evidence("stat_rate")) for v in src.rate],
                _entity_maker(regs, "discharge"),
            )
            if src.soc:
                b.one(
                    device,
                    "soc",
                    [(src.soc, evidence("stat_soc"))],
                    _entity_maker(regs),
                )
            # The Energy Dashboard has no capacity, and DC-001 requires one.
            b.questions.append(
                {
                    "code": Q_FACT_NEEDED,
                    "device": device["id"],
                    "fact": "capacity",
                    "message": "how much energy the battery holds, in kWh, and "
                    "whether that is its usable or nominal capacity",
                }
            )
            devices.append(device)
            relationships.append(
                {"kind": "supplies", "from": device["id"], "to": _HOUSE_ID}
            )

    # --- Loads: the Energy Dashboard's individual devices -------------------
    by_stat: dict[str, str] = {}
    loads: list[tuple[dr.DeviceConsumption, dict[str, Any]]] = []
    for item in consumers:
        device = {
            "id": _unique(
                "load_" + _slug(item.name or item.stat.split(".", 1)[-1], "device"),
                taken,
            ),
            "kind": "load",
            "name": item.name or item.stat,
            # No load_class: the Energy Dashboard does not say whether a
            # device is a circuit or an appliance, and DC-001 does not need it.
            "bindings": {},
            "flexibility": {"mode": "none"},
            "constraints": {},
        }
        b.one(
            device,
            "energy",
            [
                (
                    item.stat,
                    _evidence("stat_consumption", item.index, "device_consumption"),
                )
            ],
            _statistic_maker(),
        )
        if item.rate:
            b.one(
                device,
                "power",
                [(item.rate, _evidence("stat_rate", item.index, "device_consumption"))],
                _entity_maker(regs, "consumption"),
            )
        by_stat.setdefault(item.stat, device["id"])
        loads.append((item, device))
        devices.append(device)

    for item, device in loads:
        evidence = _evidence(
            "included_in_stat" if item.included_in else "top_level",
            item.index,
            "device_consumption",
        )
        if item.included_in is None:
            parent = _HOUSE_ID
        elif item.included_in in by_stat and by_stat[item.included_in] != device["id"]:
            parent = by_stat[item.included_in]
        else:
            b.findings.append(
                {
                    "code": F_INCLUDED_IN_UNKNOWN,
                    "device": device["id"],
                    "statistic_id": item.included_in,
                    "message": "included_in_stat names a statistic no "
                    "proposed device owns; no inclusion is proposed",
                }
            )
            continue
        relationships.append(
            {
                "kind": "included_in_measurement",
                "child": device["id"],
                "parent": parent,
                "quantity": "consumption",
                "confirmed": False,
                "evidence": [evidence],
            }
        )

    site_in = site or {}
    document: dict[str, Any] = {
        "schema_version": dc.SCHEMA_VERSION,
        "site": {
            "id": "site",
            "name": site_in.get("name") or "Home",
            "timezone": site_in.get("timezone"),
            "currency": site_in.get("currency"),
            # Discovery never grants control (DC-R15; DC-007 owns that).
            "control_policy": "observation_only",
        },
        "devices": devices,
        "relationships": relationships,
        "accounting": {
            "demand": {
                "mode": "inclusive",
                "root_load_id": _HOUSE_ID,
                "separate_load_ids": [],
            }
        },
        "shared_constraints": [],
    }
    return Proposal(
        document=document,
        questions=b.questions,
        findings=b.findings,
        problems=dc.validate(document),
    )


def _statistic_maker() -> Any:
    return lambda source, evidence: _statistic(source, evidence)


def _entity_maker(registry_ids: Mapping[str, str], direction: str | None = None) -> Any:
    return lambda source, evidence: _entity(source, evidence, registry_ids, direction)


def _fixed_prices(raw: Any, index: int, b: _Builder) -> None:
    if not isinstance(raw, Mapping):
        return
    places: list[tuple[str, Any]] = [
        ("number_energy_price", raw.get("number_energy_price")),
        ("number_energy_price_export", raw.get("number_energy_price_export")),
    ]
    for direction in ("flow_from", "flow_to"):
        for flow in raw.get(direction) or []:
            if isinstance(flow, Mapping):
                places.append(
                    (
                        f"{direction}.number_energy_price",
                        flow.get("number_energy_price"),
                    )
                )
    for name, value in places:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            b.findings.append(
                {
                    "code": F_FIXED_PRICE,
                    "device": _GRID_ID,
                    "evidence": _evidence(name, index, "energy_sources"),
                    "value": value,
                    "message": "a fixed price is not proposed: Nimbus plans "
                    "against a live price sensor",
                }
            )
