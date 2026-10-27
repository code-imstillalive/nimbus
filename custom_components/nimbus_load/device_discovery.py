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

import copy
import re
from collections.abc import Collection, Mapping
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
                        {
                            "source": source,
                            "binding_source": make(source, evidence)["source"],
                            "evidence": [evidence],
                        }
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


# --- Step 2: provider profiles (DC-005 "Provider adapters and the support
# matrix") ---------------------------------------------------------------------
#
# A profile maps one integration's built-in unique_id keys to Grid roles.
# The HA side finds each configured instance's entities by unique_id and
# passes `{key: entity_id}`; a renamed entity is still found.
#
# What each row binds was read from the provider, not guessed:
# * LocalVolts v2 (purcell-lab/localvolts_v2 at b4cfbaee, sensor.py): the
#   `<entry>_current_buy_rate` and `_current_sell_rate` sensors carry a
#   `forecast` attribute of the raw per-direction rows DC-002's localvolts_v2
#   adapter reads (`direction`, `quality`, `intervalEnd`, `flexUp`);
#   `buy_flex_up` / `sell_flex_up` are the current prices pricing_autodetect
#   already proposes.
# * Amber Express: `<site>_general_price` / `_feed_in_price` carry `forecast`
#   and `detailedForecast` (DC-002's amber_express adapter).
# * Amber Electric (core): `<site>-forecasts-general` / `-forecasts-feed_in`
#   carry `forecasts` (DC-002's amberelectric adapter).

PROFILE_VERSION = 1

_LV = "localvolts_v2"
_AMBER_EXPRESS = "amber_express"
_AMBER_CORE = "amberelectric"

# profile -> ((role, key, attribute_path or None, adapter spec or None), ...)
PROFILES: dict[str, tuple[tuple[str, str, str | None, dict[str, Any] | None], ...]] = {
    _LV: (
        ("import_price", "buy_flex_up", None, None),
        ("export_price", "sell_flex_up", None, None),
        (
            "import_price_forecast",
            "current_buy_rate",
            "forecast",
            {"name": _LV, "version": 1, "field": "flexUp", "direction": "Buy"},
        ),
        (
            "export_price_forecast",
            "current_sell_rate",
            "forecast",
            {"name": _LV, "version": 1, "field": "flexUp", "direction": "Sell"},
        ),
    ),
    _AMBER_EXPRESS: (
        ("import_price", "general_price", None, None),
        ("export_price", "feed_in_price", None, None),
        (
            "import_price_forecast",
            "general_price",
            "forecast",
            {"name": _AMBER_EXPRESS, "version": 1},
        ),
        (
            "export_price_forecast",
            "feed_in_price",
            "forecast",
            {"name": _AMBER_EXPRESS, "version": 1},
        ),
    ),
    _AMBER_CORE: (
        (
            "import_price_forecast",
            "forecasts-general",
            "forecasts",
            {"name": _AMBER_CORE, "version": 1},
        ),
        (
            "export_price_forecast",
            "forecasts-feed_in",
            "forecasts",
            {"name": _AMBER_CORE, "version": 1},
        ),
    ),
}

# Recognised, but DC-001 has no role to hold what they publish yet, or the
# provider does not say enough to propose it (spec decision 3's matrix).
REPORTED_ONLY = {
    "aemo_nem": "AEMO's regional wholesale forecast extends a retail feed; "
    "DC-001 has no regional-spot role to hold it yet",
    "nem_pd7day": "PD7DAY continues a retail feed past its horizon; DC-001 "
    "has no continuation role to hold it yet (DC-002 decision 5)",
    "openadr3_ven": "OpenADR states neither its currency nor whether a price "
    "is a full tariff, so it is reported and never proposed (pricing_autodetect)",
}
# LocalVolts keys with no DC-001 role of their own: the matched P2P rate and
# the settlement history would need the export price role, which the current
# sell price already holds (DC-002's open question 2 on #1574).
_LV_NO_ROLE = ("sell_matched_cost", "p2p_settlement_history")

F_PROFILE_SEVERAL = "profile_several_instances"
F_PROFILE_REPORTED = "profile_reported_only"
F_PROFILE_NO_ROLE = "profile_key_has_no_role"
F_PROFILE_UNKNOWN = "profile_unknown"


def _identity(binding: Mapping[str, Any]) -> tuple[Any, ...]:
    source = binding.get("source") or {}
    adapter = source.get("adapter") or {}
    return (
        source.get("kind"),
        source.get("entity_id") or source.get("statistic_id"),
        source.get("attribute_path"),
        adapter.get("name"),
        adapter.get("field"),
        adapter.get("direction"),
    )


def _profile_binding(
    profile: str,
    instance: str,
    key: str,
    entity_id: str,
    path: str | None,
    adapter: dict[str, Any] | None,
    registry_ids: Mapping[str, str],
) -> dict[str, Any]:
    source: dict[str, Any] = (
        {"kind": "entity_state", "entity_id": entity_id}
        if path is None
        else {
            "kind": "entity_attribute",
            "entity_id": entity_id,
            "attribute_path": path,
            "adapter": dict(adapter or {}),
        }
    )
    if entity_id in registry_ids:
        source["registry_id"] = registry_ids[entity_id]
    binding: dict[str, Any] = {
        "source": source,
        "provenance": {
            "origin": "provider_profile",
            "confirmed": False,
            "overridden": False,
            "evidence": [
                {
                    "kind": "provider_profile",
                    "profile": profile,
                    "profile_version": PROFILE_VERSION,
                    "instance": instance,
                    "key": key,
                }
            ],
        },
    }
    if path is not None:
        binding["normalization"] = {"quality": "forecast"}
    return binding


def _candidate(binding: Mapping[str, Any]) -> dict[str, Any]:
    source = binding["source"]
    return {
        "source": source.get("entity_id") or source.get("statistic_id"),
        "binding_source": copy.deepcopy(source),
        "evidence": copy.deepcopy(binding["provenance"]["evidence"]),
    }


def add_provider_profiles(
    proposal: Proposal,
    detected: list[Mapping[str, Any]],
    *,
    registry_ids: Mapping[str, str] | None = None,
) -> Proposal:
    """`proposal` with each detected provider profile's Grid roles added
    (DC-005 step 2). `detected` is one item per configured instance:
    `{"profile": name, "instance": its config entry or site, "keys": {key:
    entity_id}}`.

    A profile with more than one instance proposes nothing (spec: "several =
    ambiguous"). A role that already holds a different source, from the
    Energy Dashboard or another profile, becomes a question listing every one
    and loses its binding; the same source twice is corroboration, both
    pieces of evidence kept."""
    regs = registry_ids or {}
    document = copy.deepcopy(proposal.document)
    questions = copy.deepcopy(proposal.questions)
    findings = copy.deepcopy(proposal.findings)
    grid = next(d for d in document["devices"] if d["kind"] == "grid")

    instances: dict[str, list[Mapping[str, Any]]] = {}
    for item in detected:
        instances.setdefault(str(item.get("profile")), []).append(item)

    candidates: dict[str, list[dict[str, Any]]] = {}
    for role, binding in grid["bindings"].items():
        candidates.setdefault(role, []).append(binding)
    for profile, items in sorted(instances.items()):
        names = sorted(str(i.get("instance")) for i in items)
        if profile in REPORTED_ONLY:
            findings.append(
                {
                    "code": F_PROFILE_REPORTED,
                    "profile": profile,
                    "instances": names,
                    "message": REPORTED_ONLY[profile],
                }
            )
            continue
        if profile not in PROFILES:
            findings.append(
                {
                    "code": F_PROFILE_UNKNOWN,
                    "profile": profile,
                    "instances": names,
                    "message": "not in the support matrix; its roles stay manual",
                }
            )
            continue
        if len(items) > 1:
            findings.append(
                {
                    "code": F_PROFILE_SEVERAL,
                    "profile": profile,
                    "instances": names,
                    "message": "more than one is configured; nothing is "
                    "proposed rather than guessing which one the site uses",
                }
            )
            continue
        (item,) = items
        keys = item.get("keys") or {}
        for role, key, path, adapter in PROFILES[profile]:
            entity_id = keys.get(key)
            if isinstance(entity_id, str) and entity_id:
                candidates.setdefault(role, []).append(
                    _profile_binding(
                        profile,
                        str(item.get("instance")),
                        key,
                        entity_id,
                        path,
                        adapter,
                        regs,
                    )
                )
        if profile == _LV:
            findings.extend(
                {
                    "code": F_PROFILE_NO_ROLE,
                    "profile": profile,
                    "key": key,
                    "entity_id": keys[key],
                    "message": "the P2P matched rate and settlement have no "
                    "DC-001 role of their own yet; the export price role holds "
                    "the current sell price",
                }
                for key in _LV_NO_ROLE
                if isinstance(keys.get(key), str) and keys[key]
            )

    # A role step 1 already asked about stays a question: a profile's source
    # joins its candidates, it never settles them.
    asked = {
        q["role"]: q
        for q in questions
        if q.get("code") == Q_SEVERAL_SOURCES and q.get("device") == grid["id"]
    }
    bindings: dict[str, Any] = {}
    for role, found in candidates.items():
        if role in asked:
            known = {
                _identity({"source": c.get("binding_source") or {}})
                for c in asked[role]["candidates"]
            }
            for b in found:
                if _identity(b) not in known:
                    known.add(_identity(b))
                    asked[role]["candidates"].append(_candidate(b))
            continue
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for binding in found:
            ident = _identity(binding)
            if ident in groups:
                groups[ident]["provenance"]["evidence"].extend(
                    copy.deepcopy(binding["provenance"]["evidence"])
                )
            else:
                groups[ident] = copy.deepcopy(binding)
        if len(groups) == 1:
            bindings[role] = next(iter(groups.values()))
            continue
        questions.append(
            {
                "code": Q_SEVERAL_SOURCES,
                "device": grid["id"],
                "role": role,
                "candidates": [_candidate(b) for b in groups.values()],
            }
        )
    grid["bindings"] = bindings
    return Proposal(
        document=document,
        questions=questions,
        findings=findings,
        problems=dc.validate(document),
    )


# --- Step 3: history pairing (DC-005 "Evidence", decision 4) -----------------
#
# Only for a device whose energy the proposal binds but whose power it does
# not: the Energy Dashboard named a counter and no `stat_rate`. #1590's
# `match_power_sensor` integrates each candidate power sensor hour by hour
# against that counter, both signs. History may not decide alone (the spec's
# evidence table), so even a clean match is a question to confirm, with its
# error and sign; it never becomes a binding here.

Q_CONFIRM_MATCH = "confirm_history_match"
F_PAIRING = "history_pairing_inconclusive"

# kind -> (role the positive counter fills, role the opposite one fills)
_PAIRING_COUNTERS = {
    "grid": ("import_energy", "export_energy"),
    "battery": ("discharge_energy", "charge_energy"),
    "solar": ("energy", None),
    "load": ("energy", None),
}
_OPPOSITE = {
    "import": "export",
    "discharge": "charge",
    "generation": "consumption",
    "consumption": "generation",
}


def pairing_targets(proposal: Proposal) -> dict[str, tuple[str, str | None]]:
    """{device id: (positive counter statistic, opposite counter or None)}
    for every device that has energy but no power: what the HA side must
    fetch history for, and for which devices it must offer candidates."""
    out: dict[str, tuple[str, str | None]] = {}
    for device in proposal.document["devices"]:
        roles = _PAIRING_COUNTERS.get(device["kind"])
        bindings = device.get("bindings") or {}
        if roles is None or "power" in bindings:
            continue
        stats = [
            (bindings.get(role) or {}).get("source", {}).get("statistic_id")
            if role
            else None
            for role in roles
        ]
        if stats[0]:
            out[device["id"]] = (stats[0], stats[1])
    return out


def add_history_pairing(
    proposal: Proposal,
    candidates: Mapping[str, Mapping[str, dr.Series]],
    statistics: Mapping[str, dr.Series],
    start: Any,
    end: Any,
    *,
    registry_ids: Mapping[str, str] | None = None,
) -> Proposal:
    """`proposal` with a question or a finding for each pairing target
    (`pairing_targets`). `candidates[device_id]` are the power sensors the HA
    side offers for that device (the same HA device narrows the search, never
    decides it); `statistics` holds each counter's history over [start, end).
    A device asked about already, or offered no candidates, is left alone."""
    regs = registry_ids or {}
    document = copy.deepcopy(proposal.document)
    questions = copy.deepcopy(proposal.questions)
    findings = copy.deepcopy(proposal.findings)
    asked = {(q.get("device"), q.get("role")) for q in questions}
    kinds = {d["id"]: d["kind"] for d in document["devices"]}

    for device_id, (out_stat, in_stat) in sorted(pairing_targets(proposal).items()):
        offered = candidates.get(device_id)
        if not offered or (device_id, "power") in asked:
            continue
        energy_out = statistics.get(out_stat)
        energy_in = statistics.get(in_stat) if in_stat else None
        if energy_out is None or (in_stat and energy_in is None):
            findings.append(
                {
                    "code": F_PAIRING,
                    "device": device_id,
                    "status": dr.INSUFFICIENT,
                    "reason": "no counter history to compare against",
                }
            )
            continue
        match = dr.match_power_sensor(
            offered, start, end, energy_out=energy_out, energy_in=energy_in
        )
        direction = CANONICAL_POWER_DIRECTION[kinds[device_id]]
        evidence = {
            "kind": "history_pairing",
            "window": [str(start), str(end)],
            "status": match.status,
            "error": match.error,
            "ranking": [list(r) for r in match.ranking],
        }
        if match.status == dr.CANDIDATE and match.entity_id is not None:
            native = direction if match.sign != -1 else _OPPOSITE[direction]
            source: dict[str, Any] = {
                "kind": "entity_state",
                "entity_id": match.entity_id,
            }
            if match.entity_id in regs:
                source["registry_id"] = regs[match.entity_id]
            questions.append(
                {
                    "code": Q_CONFIRM_MATCH,
                    "device": device_id,
                    "role": "power",
                    "candidates": [
                        {
                            "source": match.entity_id,
                            "binding_source": source,
                            "normalization": {
                                "positive_direction": direction,
                                "native_positive_direction": native,
                            },
                            "evidence": [evidence],
                        }
                    ],
                }
            )
        elif match.status == dr.AMBIGUOUS:
            questions.append(
                {
                    "code": Q_SEVERAL_SOURCES,
                    "device": device_id,
                    "role": "power",
                    "candidates": [
                        {
                            "source": entity_id,
                            "binding_source": {
                                "kind": "entity_state",
                                "entity_id": entity_id,
                                **(
                                    {"registry_id": regs[entity_id]}
                                    if entity_id in regs
                                    else {}
                                ),
                            },
                            "evidence": [evidence],
                        }
                        for entity_id, _err in match.ranking
                        if _err <= match.error + dr.TIE_ABS
                    ],
                }
            )
        else:
            findings.append(
                {
                    "code": F_PAIRING,
                    "device": device_id,
                    "status": match.status,
                    "reason": match.reason,
                }
            )
    return Proposal(
        document=document,
        questions=questions,
        findings=findings,
        problems=dc.validate(document),
    )


CANONICAL_POWER_DIRECTION = {
    kind: dc.CANONICAL_ROLE_DIRECTION[(kind, "power")]
    for kind in ("grid", "battery", "solar", "load")
}


# --- Step 4: rediscovery against a stored document (DC-005 "Rediscovery") ----
#
# A fresh proposal is compared with the stored document and turned into a
# list of differences. Nothing is applied: a difference is shown, and only
# the household's confirmation (step 5, DC-004's commit) changes the store.
#
# * Identity is DC-001's: the registry id where both sides have one, else
#   entity_id plus attribute path, else statistic id. Never entity_id alone
#   when a registry id says otherwise, and never list position (decision 2).
# * A confirmed binding is never replaced: a different discovered source is
#   an `alternative`, shown with its evidence (decision 5).
# * A device is matched by the sources it shares with a stored device of the
#   same kind; the Grid and the whole-house load are the site's own. A
#   discovered device matching no stored one is `new_device`; one matching
#   two is a question. A stored device discovery no longer sees is reported,
#   never removed: absence of evidence is not removal.

D_FILL = "fill"
D_ALTERNATIVE = "alternative"
D_RENAMED = "renamed"
D_NEW_DEVICE = "new_device"
D_NOT_SEEN = "not_seen"
Q_REPLACED = "replaced_equipment"
Q_DEVICE_MATCH = "device_match_ambiguous"


@dataclass
class Rediscovery:
    differences: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)


def _same_source(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if a.get("registry_id") and b.get("registry_id"):
        return a["registry_id"] == b["registry_id"]
    if a.get("entity_id") or b.get("entity_id"):
        return (a.get("entity_id"), a.get("attribute_path")) == (
            b.get("entity_id"),
            b.get("attribute_path"),
        )
    return a.get("statistic_id") is not None and a.get("statistic_id") == b.get(
        "statistic_id"
    )


def _sources(device: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        (b or {}).get("source") or {} for b in (device.get("bindings") or {}).values()
    ]


def _site_device(device: Mapping[str, Any]) -> str | None:
    if device.get("kind") == "grid":
        return "grid"
    if device.get("kind") == "load" and device.get("load_class") == "aggregate":
        return "house"
    return None


def rediscover(
    stored: Mapping[str, Any],
    proposal: Proposal,
    *,
    present_registry_ids: frozenset[str] | None = None,
) -> Rediscovery:
    """The differences between `stored` (a DC-001 document) and a fresh
    `proposal` (section comment above). `present_registry_ids` are the entity
    registry entries that exist now, so a stored source whose entry has gone
    can be told from one that was merely renamed; without it, replaced
    equipment is never inferred."""
    out = Rediscovery(
        questions=copy.deepcopy(proposal.questions),
        findings=copy.deepcopy(proposal.findings),
    )
    stored_devices = list(stored.get("devices") or [])
    matched_stored: set[str] = set()
    # proposal device id -> the stored device it is, so a question about it
    # names the device the household has, not discovery's own id.
    as_stored: dict[str, str] = {}

    for found in proposal.document["devices"]:
        site = _site_device(found)
        if site is not None:
            matches = [d for d in stored_devices if _site_device(d) == site]
        else:
            matches = [
                d
                for d in stored_devices
                if d.get("kind") == found["kind"]
                and _site_device(d) is None
                and any(
                    _same_source(s, f) for s in _sources(d) for f in _sources(found)
                )
            ]
        if len(matches) > 1:
            out.questions.append(
                {
                    "code": Q_DEVICE_MATCH,
                    "device": found["id"],
                    "stored_devices": sorted(d["id"] for d in matches),
                    "message": "this discovered device shares sources with more "
                    "than one stored device",
                }
            )
            continue
        if not matches:
            if site is None:
                out.differences.append(
                    {"kind": D_NEW_DEVICE, "device": copy.deepcopy(found)}
                )
            continue
        (target,) = matches
        matched_stored.add(target["id"])
        as_stored[found["id"]] = target["id"]
        _compare_bindings(target, found, present_registry_ids, out)

    for question in out.questions:
        if question.get("device") in as_stored:
            question["device"] = as_stored[question["device"]]
    for device in stored_devices:
        if device["id"] not in matched_stored and _site_device(device) is None:
            out.findings.append(
                {
                    "code": D_NOT_SEEN,
                    "device": device["id"],
                    "message": "discovery did not find this device's sources; "
                    "it stays as stored",
                }
            )
    return out


def _compare_bindings(
    stored: Mapping[str, Any],
    found: Mapping[str, Any],
    present: frozenset[str] | None,
    out: Rediscovery,
) -> None:
    have = stored.get("bindings") or {}
    for role, binding in (found.get("bindings") or {}).items():
        new = binding.get("source") or {}
        old_binding = have.get(role)
        if old_binding is None:
            out.differences.append(
                {
                    "kind": D_FILL,
                    "device": stored["id"],
                    "role": role,
                    "binding": copy.deepcopy(binding),
                }
            )
            continue
        old = old_binding.get("source") or {}
        if _same_source(old, new):
            if old.get("entity_id") and old.get("entity_id") != new.get("entity_id"):
                out.differences.append(
                    {
                        "kind": D_RENAMED,
                        "device": stored["id"],
                        "role": role,
                        "registry_id": old.get("registry_id"),
                        "stored_entity_id": old.get("entity_id"),
                        "entity_id": new.get("entity_id"),
                    }
                )
            continue
        gone = (
            present is not None
            and old.get("registry_id") is not None
            and old["registry_id"] not in present
        )
        if gone:
            out.questions.append(
                {
                    "code": Q_REPLACED,
                    "device": stored["id"],
                    "role": role,
                    "stored_source": copy.deepcopy(old),
                    "candidate": _candidate(binding),
                    "message": "the stored source no longer exists and discovery "
                    "found another; confirm it is the replacement",
                }
            )
            continue
        out.differences.append(
            {
                "kind": D_ALTERNATIVE,
                "device": stored["id"],
                "role": role,
                "stored_source": copy.deepcopy(old),
                "stored_confirmed": bool(
                    (old_binding.get("provenance") or {}).get("confirmed")
                ),
                "candidate": _candidate(binding),
            }
        )


# --- Step 5: applying the household's confirmation (pure) --------------------
#
# A confirmation names what the household accepted, by what discovery
# offered: a difference (`fill`, `renamed`, `alternative`, `new_device`) or a
# question's candidate (`several_sources`, `confirm_history_match`,
# `replaced_equipment`). Anything not offered is refused, and one refusal
# refuses the whole confirmation: it is applied completely or not at all.
# Each accepted binding is `confirmed: true`; its evidence is kept.

R_NOT_OFFERED = "not_offered"
R_CAPACITY_NEEDED = "capacity_needed"
R_UNKNOWN_DEVICE = "unknown_device"

# Where an accepted binding came from, in DC-001's provenance vocabulary. A
# history match has no origin of its own in DC-001: the household chose it,
# so it is recorded as `manual` with the history evidence kept beside it.
_ORIGIN = {
    "energy_dashboard": "energy_dashboard",
    "provider_profile": "provider_profile",
    "history_pairing": "manual",
}


def _accepted(candidate: Mapping[str, Any]) -> dict[str, Any]:
    evidence = copy.deepcopy(candidate.get("evidence") or [])
    kind = evidence[0].get("kind") if evidence else None
    binding: dict[str, Any] = {
        "source": copy.deepcopy(candidate["binding_source"]),
        "provenance": {
            "origin": _ORIGIN.get(str(kind), "manual"),
            "confirmed": True,
            "overridden": False,
            "evidence": evidence,
        },
    }
    if candidate.get("normalization"):
        binding["normalization"] = copy.deepcopy(candidate["normalization"])
    return binding


def _confirmed(binding: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(binding))
    out.setdefault("provenance", {})
    out["provenance"]["confirmed"] = True
    out["provenance"].setdefault("overridden", False)
    return out


def apply_confirmation(
    stored: Mapping[str, Any],
    rediscovery: Rediscovery,
    choices: list[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, str]]]:
    """(new document, refusals, changed (device, role) pairs).

    Each choice is `{"kind", "device", "role", "entity_id"}`; a `new_device`
    choice names the discovered device by `device` (its proposed id) and, for
    a battery, carries `capacity: {value, unit, basis}`. With any refusal the
    document returned is `stored`, unchanged."""
    document = copy.deepcopy(dict(stored))
    devices = {d["id"]: d for d in document.get("devices") or []}
    refusals: list[dict[str, Any]] = []
    changed: list[tuple[str, str]] = []

    def refuse(choice: Mapping[str, Any], reason: str) -> None:
        refusals.append({"choice": dict(choice), "reason": reason})

    for choice in choices:
        kind = choice.get("kind")
        device_id = choice.get("device")
        role = choice.get("role")
        entity_id = choice.get("entity_id")

        if kind == D_NEW_DEVICE:
            offer = next(
                (
                    d["device"]
                    for d in rediscovery.differences
                    if d["kind"] == D_NEW_DEVICE and d["device"]["id"] == device_id
                ),
                None,
            )
            if offer is None:
                refuse(choice, R_NOT_OFFERED)
                continue
            device = copy.deepcopy(offer)
            if device["kind"] == "battery":
                capacity = choice.get("capacity")
                if not isinstance(capacity, Mapping):
                    refuse(choice, R_CAPACITY_NEEDED)
                    continue
                device["capacity"] = dict(capacity)
            device["id"] = _unique(device["id"], set(devices))
            device["bindings"] = {
                r: _confirmed(b) for r, b in (device.get("bindings") or {}).items()
            }
            document.setdefault("devices", []).append(device)
            devices[device["id"]] = device
            changed.extend((device["id"], r) for r in device["bindings"])
            continue

        if device_id not in devices:
            refuse(choice, R_UNKNOWN_DEVICE)
            continue
        target = devices[device_id]

        if kind == D_RENAMED:
            offer = next(
                (
                    d
                    for d in rediscovery.differences
                    if d["kind"] == D_RENAMED
                    and (d["device"], d["role"]) == (device_id, role)
                    and d["entity_id"] == entity_id
                ),
                None,
            )
            if offer is None:
                refuse(choice, R_NOT_OFFERED)
                continue
            target["bindings"][role]["source"]["entity_id"] = entity_id
            changed.append((device_id, role))
            continue

        if kind == D_FILL:
            offer = next(
                (
                    d
                    for d in rediscovery.differences
                    if d["kind"] == D_FILL
                    and (d["device"], d["role"]) == (device_id, role)
                    and (
                        d["binding"]["source"].get("entity_id")
                        or d["binding"]["source"].get("statistic_id")
                    )
                    == entity_id
                ),
                None,
            )
            if offer is None:
                refuse(choice, R_NOT_OFFERED)
                continue
            target.setdefault("bindings", {})[role] = _confirmed(offer["binding"])
            changed.append((device_id, role))
            continue

        candidate: Mapping[str, Any] | None = None
        if kind == D_ALTERNATIVE:
            candidate = next(
                (
                    d["candidate"]
                    for d in rediscovery.differences
                    if d["kind"] == D_ALTERNATIVE
                    and (d["device"], d["role"]) == (device_id, role)
                    and d["candidate"]["source"] == entity_id
                ),
                None,
            )
        elif kind == Q_REPLACED:
            candidate = next(
                (
                    q["candidate"]
                    for q in rediscovery.questions
                    if q.get("code") == Q_REPLACED
                    and (q["device"], q["role"]) == (device_id, role)
                    and q["candidate"]["source"] == entity_id
                ),
                None,
            )
        elif kind in (Q_SEVERAL_SOURCES, Q_CONFIRM_MATCH):
            candidate = next(
                (
                    c
                    for q in rediscovery.questions
                    if q.get("code") == kind
                    and (q.get("device"), q.get("role")) == (device_id, role)
                    for c in q.get("candidates") or ()
                    if c.get("source") == entity_id and c.get("binding_source")
                ),
                None,
            )
        if candidate is None:
            refuse(choice, R_NOT_OFFERED)
            continue
        target.setdefault("bindings", {})[role] = _accepted(candidate)
        changed.append((device_id, role))

    if refusals:
        return copy.deepcopy(dict(stored)), refusals, []
    return document, [], changed


# --- DC-007 slice 1: a new install's answers -----------------------------------

#: The devices a new install always keeps: DC-R01's minimal valid site.
_ALWAYS_KEPT = frozenset({_GRID_ID, _HOUSE_ID})


def answer_proposal(
    proposal: Proposal,
    keep: Collection[str],
    answers: Mapping[int, Any],
) -> dict[str, Any]:
    """The document a new install confirms (DC-007 slice 1): `proposal`'s,
    with only the devices in `keep` (the grid and the house are always kept),
    and each answered question applied.

    `answers` maps a question's index in `proposal.questions` to:
    - for `several_sources` and `confirm_history_match`: the chosen
      candidate's `source`, or None to leave the role as proposed;
    - for `fact_needed` (capacity): `{"value": kWh, "basis": "usable" |
      "nominal"}`, or None to leave it unset.

    Every binding that remains is marked confirmed: the household saw it and
    kept it. Relationships to a device that was not kept are dropped."""
    document = copy.deepcopy(proposal.document)
    kept = set(keep) | _ALWAYS_KEPT
    document["devices"] = [d for d in document.get("devices") or () if d["id"] in kept]
    by_id = {d["id"]: d for d in document["devices"]}

    def _ends(rel: Mapping[str, Any]) -> list[Any]:
        return [rel[k] for k in ("from", "to", "parent", "child") if k in rel] + list(
            rel.get("devices") or ()
        )

    document["relationships"] = [
        r
        for r in document.get("relationships") or ()
        if all(end in by_id for end in _ends(r))
    ]
    for index, answer in answers.items():
        if answer is None or not 0 <= index < len(proposal.questions):
            continue
        question = proposal.questions[index]
        device = by_id.get(question.get("device"))
        if device is None:
            continue
        if question.get("code") == Q_FACT_NEEDED:
            if question.get("fact") == "capacity" and isinstance(answer, Mapping):
                device["capacity"] = {
                    "value": float(answer["value"]),
                    "unit": "kWh",
                    "basis": str(answer.get("basis") or "usable"),
                }
            continue
        candidate = next(
            (
                c
                for c in question.get("candidates") or ()
                if c.get("source") == answer and c.get("binding_source")
            ),
            None,
        )
        if candidate is not None:
            device.setdefault("bindings", {})[question["role"]] = _accepted(candidate)
    for device in document["devices"]:
        device["bindings"] = {
            role: _confirmed(b) for role, b in (device.get("bindings") or {}).items()
        }
    return document
