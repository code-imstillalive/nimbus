"""DC-004 step 5a: the Home Assistant side of the device document's preview
and mode (docs/specs/device-contract/004-migration-and-compatibility.md).

Two services, both the household's to call:
- `nimbus_load.device_contract_preview`: the dry run (`device_import.preview`)
  on this install's settings, returned as the response. Writes nothing.
- `nimbus_load.device_contract_set_mode`: `legacy` <-> `shadow`. Entering
  `shadow` imports the settings and commits the document to the store, and
  is refused unless the dry run is clean (no change to any input, no
  conflict, no problem). **It writes only the device store, never the
  options, subentries or number entities**: in `shadow` the legacy settings
  stay the master.
- **`active`** (DC-004 step 5b) is entered from `shadow` only, again on a
  clean dry run. The views then equal today's settings, so activating
  changes no setting: it records `device_contract_view_revision` in the
  options. From then on a commit follows the spec's order (store, then views
  and the marker, then the store's `view_revision`), and every start runs
  `async_recover`, which regenerates the views when the store is ahead of the
  marker and falls back to `shadow` with a Repair when the options are ahead.
  `active` -> `legacy` stops generating views and leaves the options as they
  are.
- **Step 5c, in `active` only:** a drifted owned key (edited in a form, a
  subentry or a service) raises one fixable Repair per key at the next start,
  which every such edit triggers; *Keep my change* commits it as a new
  revision and *Restore* writes the document's value back. A contract-owned
  number commits a new revision when it changes, so the slider edits the
  document. One lock per entry serialises every commit (Mark Purcell, #1878
  review: the reload counter is per entry, so commits must not overlap).

Nothing here sends a command to a device (DC-R15): it reads states and the
entity registry, and writes one `Store`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import (
    device_bindings,
    device_discovery,
    device_forecasts,
    device_import,
    device_readiness,
    device_resolver,
    device_storage,
    device_units,
    known_sources,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: The options key that records which revision the legacy views hold
#: (DC-004 "Commit order in `active` mode").
VIEW_REVISION_KEY = "device_contract_view_revision"
#: The Repair raised when the options say a later revision than the store.
ISSUE_INCONSISTENT = "device_contract_inconsistent"
#: The fixable Repair raised for each drifted owned key (step 5c).
ISSUE_DRIFT = "device_contract_drift"
#: The fixable Repair raised for each power sensor whose unit the household
#: must declare before `active` (DC-006 observation slice; #1893).
ISSUE_UNIT = "device_unit_undeclared"
#: hass.data key: one asyncio.Lock per entry, held by every commit.
_LOCKS = f"{DOMAIN}_device_contract_locks"


def _lock(hass: Any, entry_id: str) -> asyncio.Lock:
    return hass.data.setdefault(_LOCKS, {}).setdefault(entry_id, asyncio.Lock())


#: Live Solver numbers the import reads (device_import._CONSTRAINTS + capacity).
_NUMBER_KEYS = tuple(
    sorted(
        {k for _d, _c, k in device_import._CONSTRAINTS}
        | {"solver_battery_capacity_kwh"}
    )
)


def snapshot(hass: Any, entry: Any) -> dict[str, Any]:
    """This install's settings as the plain dict `import_legacy` reads."""
    registry = er.async_get(hass)
    numbers: dict[str, float] = {}
    for key in _NUMBER_KEYS:
        eid = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
        state = hass.states.get(eid) if eid else None
        try:
            numbers[key] = float(state.state) if state is not None else None
        except (TypeError, ValueError):
            continue
        if numbers[key] is None:
            del numbers[key]
    subentries = [
        {
            "subentry_id": sid,
            "subentry_type": sub.subentry_type,
            "title": sub.title,
            "data": dict(sub.data),
        }
        for sid, sub in entry.subentries.items()
    ]
    return {
        "options": dict(entry.options),
        "numbers": numbers,
        "subentries": subentries,
        "setup_builder_done": list(entry.options.get("setup_builder_done") or ()),
        "site": _site(hass),
        # Solcast and Open-Meteo as the registry finds them: the solar
        # forecast binding's auto-included members (DC-006 slice 3).
        "known_sources": _known_sources(hass),
    }


def _site(hass: Any) -> dict[str, Any]:
    """The site's identity from Home Assistant's own settings."""
    return {
        "name": hass.config.location_name,
        "timezone": hass.config.time_zone,
        "currency": hass.config.currency,
    }


async def async_active_solar_forecast(hass: Any, entry: Any) -> dict[str, Any] | None:
    """The committed document's Solar `generation_forecast` binding while the
    device setup is `active`, for the Solver to read through the contract
    (DC-006 slice 3); None in every other mode, or when the binding is not
    one the forecast adapters can read, so the Solver reads as today."""
    state = await _store(hass, entry).async_read()
    document = state.get("document")
    if state.get("mode") != "active" or document is None:
        return None
    for device in document.get("devices") or ():
        if device.get("kind") != "solar":
            continue
        binding = (device.get("bindings") or {}).get("generation_forecast")
        if device_forecasts.readable(binding):
            return binding
    return None


async def async_active_load_forecast(hass: Any, entry: Any) -> dict[str, Any] | None:
    """The committed document's whole-house `demand_forecast` binding while
    the device setup is `active` (DC-006 slice 3, the load total); None in
    every other mode, or when the forecast adapters cannot read it, so the
    Solver reads as today."""
    state = await _store(hass, entry).async_read()
    document = state.get("document")
    if state.get("mode") != "active" or document is None:
        return None
    for device in document.get("devices") or ():
        if device.get("kind") != "load" or device.get("load_class") != "aggregate":
            continue
        binding = (device.get("bindings") or {}).get("demand_forecast")
        if device_forecasts.readable(binding):
            return binding
    return None


def _known_sources(hass: Any) -> dict[str, Any]:
    try:
        return known_sources.resolve(hass)
    except Exception:  # a lookup failure leaves them out
        _LOGGER.debug("Nimbus: known_sources lookup failed", exc_info=True)
        return dict(known_sources.EMPTY)


def _preview_response(pv: device_import.Preview) -> dict[str, Any]:
    return {
        "clean": pv.clean,
        "effective_diff": [
            {"path": p, "now": b, "after": a} for p, b, a in pv.effective_diff
        ],
        "conflicts": [
            {"code": c.code, "path": c.path, "sources": [list(s) for s in c.sources]}
            for c in pv.conflicts
        ],
        "problems": [
            {"code": p.code, "path": p.path, "message": p.message} for p in pv.problems
        ],
        "unmapped": pv.unmapped,
        "devices": [
            {"id": d["id"], "kind": d["kind"], "name": d.get("name")}
            for d in pv.document.get("devices") or ()
        ],
    }


async def async_preview(hass: Any, entry: Any) -> dict[str, Any]:
    """The dry run on this install. Writes nothing."""
    pv = device_import.preview(snapshot(hass, entry))
    store = device_storage.ContractStore(
        device_storage.ha_backend(hass, entry.entry_id)
    )
    state = await store.async_read()
    return {
        "mode": state["mode"],
        "revision": state["revision"],
        **_preview_response(pv),
    }


#: hass.data key: per entry, how many update-listener calls the view
#: write itself caused and the listener should let pass without a reload.
_OWN_UPDATES = f"{DOMAIN}_device_contract_own_updates"


def writing_views(hass: Any, entry_id: str) -> bool:
    """Called by the update listener: True (and counted off) when this call
    was caused by a view write, which reloads once itself when it is done.
    HA fires the listeners once per update that changed something, and the
    view write counts exactly those, so nothing depends on timing."""
    pending = hass.data.get(_OWN_UPDATES, {})
    if pending.get(entry_id, 0) > 0:
        pending[entry_id] -= 1
        return True
    return False


def _store(hass: Any, entry: Any) -> device_storage.ContractStore:
    return device_storage.ContractStore(device_storage.ha_backend(hass, entry.entry_id))


async def _async_write_views(
    hass: Any,
    entry: Any,
    document: dict[str, Any],
    revision: int,
    writes: dict[str, Any] | None = None,
) -> tuple[str | None, bool]:
    """Step 2 of the commit order: write every owned key that differs from the
    view of `document`, then the options with the marker last, so the marker
    is set only once the rest is written. Each update the listener will see
    is counted, so the listener lets those pass instead of reloading
    half-way; the caller reloads once afterwards if a setting changed.
    Returns (error code or None, whether a setting changed); a marker-only
    write changes no setting."""
    from .flows import live_numbers

    if writes is None:
        writes = device_import.view_writes(
            device_import.legacy_view(document), snapshot(hass, entry)
        )
    changed = any(writes[part] for part in ("options", "numbers", "subentries"))
    own = hass.data.setdefault(_OWN_UPDATES, {})
    # Count only what a listener will see: at start-up recovery none is
    # registered yet, and a stray count would swallow a real edit later.
    listening = bool(getattr(entry, "update_listeners", None))
    if writes["numbers"]:
        skipped = await live_numbers.async_apply(
            hass, entry.entry_id, writes["numbers"]
        )
        if skipped:
            return "number_not_written", changed

    def _own(update: Any) -> None:
        # HA starts listener tasks eagerly, inside the update call itself, so
        # the count is in place before it and taken back if nothing changed
        # (then no listener fires).
        if listening:
            own[entry.entry_id] = own.get(entry.entry_id, 0) + 1
        if not update() and listening:
            own[entry.entry_id] -= 1

    for sid, fields in writes["subentries"].items():
        sub = entry.subentries[sid]
        _own(
            lambda sub=sub, fields=fields: hass.config_entries.async_update_subentry(
                entry, sub, data={**sub.data, **fields}
            )
        )
    _own(
        lambda: hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, **writes["options"], VIEW_REVISION_KEY: revision},
        )
    )
    return None, changed


async def async_commit_active(
    hass: Any,
    entry: Any,
    expected_revision: int,
    document: dict[str, Any],
    origin: str,
) -> dict[str, Any]:
    """A commit in `active` mode, in the spec's order: the store, then the
    views and the marker, then the store's `view_revision`. A failure between
    steps is repaired by `async_recover` at the next start. Serialised per
    entry."""
    async with _lock(hass, entry.entry_id):
        return await _async_commit_active(
            hass, entry, expected_revision, document, origin
        )


async def _async_commit_active(
    hass: Any,
    entry: Any,
    expected_revision: int,
    document: dict[str, Any],
    origin: str,
) -> dict[str, Any]:
    store = _store(hass, entry)
    state = await store.async_read()
    if state["mode"] != "active":
        return {"ok": False, "code": "not_active", "mode": state["mode"]}
    committed = await store.async_commit(
        expected_revision, document, origin=origin, now=dt_util.utcnow()
    )
    if not committed.ok:
        return {"ok": False, "code": committed.code, "mode": "active"}
    code, changed = await _async_write_views(hass, entry, document, committed.revision)
    if code:
        return {"ok": False, "code": code, "mode": "active"}
    await store.async_set_view_revision(committed.revision)
    if changed:
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return {
        "ok": True,
        "code": committed.code,
        "mode": "active",
        "revision": committed.revision,
    }


async def async_recover(hass: Any, entry: Any) -> str:
    """At every start (DC-004 "On every start"). Only `active` acts:
    - the store ahead of the options' marker: regenerate the views from the
      stored document (a pure function, so repeating it is safe);
    - the options ahead of the store, which the commit order never produces:
      fall back to `shadow` and raise a Repair. Nimbus never guesses which
      side is right.
    Returns `none`, `regenerated` (settings rewritten), `marked` (only the
    store's marker caught up), `fell_back`, or an error code."""
    async with _lock(hass, entry.entry_id):
        return await _async_recover(hass, entry)


async def _async_recover(hass: Any, entry: Any) -> str:
    store = _store(hass, entry)
    state = await store.async_read()
    if state["mode"] != "active" or state["document"] is None:
        return "none"
    marker = entry.options.get(VIEW_REVISION_KEY)
    if marker is not None and int(marker) > state["revision"]:
        await store.async_set_mode(state["revision"], "legacy")
        await store.async_set_mode(state["revision"], "shadow")
        from homeassistant.helpers import issue_registry as ir

        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_INCONSISTENT}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INCONSISTENT,
            translation_placeholders={
                "options_revision": str(marker),
                "store_revision": str(state["revision"]),
            },
        )
        return "fell_back"
    behind = marker is None or int(marker) < state["revision"]
    changed = False
    if behind:
        code, changed = await _async_write_views(
            hass, entry, state["document"], state["revision"]
        )
        if code:
            return code
    if behind or state.get("view_revision") != state["revision"]:
        await store.async_set_view_revision(state["revision"])
        # Only a change to a setting needs the components to reload.
        return "regenerated" if changed else "marked"
    return "none"


async def async_set_mode(hass: Any, entry: Any, mode: str) -> dict[str, Any]:
    """`legacy`, `shadow` and `active` (module docstring). Returns what
    happened."""
    async with _lock(hass, entry.entry_id):
        return await _async_set_mode(hass, entry, mode)


async def _async_set_mode(hass: Any, entry: Any, mode: str) -> dict[str, Any]:
    store = _store(hass, entry)
    state = await store.async_read()
    if mode in ("shadow", "active"):
        if mode == "active" and state["mode"] != "shadow":
            return {
                "ok": False,
                "code": "mode_transition_not_allowed",
                "mode": state["mode"],
            }
        pv = device_import.preview(snapshot(hass, entry))
        if not pv.clean:
            return {
                "ok": False,
                "code": "dry_run_not_clean",
                "mode": state["mode"],
                **_preview_response(pv),
            }
        # A unit the household declared in shadow survives the re-import.
        document = device_units.carry_declared(state["document"], pv.document)
        if mode == "active":
            # #1893: `active` waits until every power sensor's unit is known.
            pending = device_units.undeclared(document, _state_reader(hass))
            if pending:
                return {
                    "ok": False,
                    "code": "units_undeclared",
                    "mode": state["mode"],
                    "sensors": [p["entity_id"] for p in pending],
                }
        committed = await store.async_commit(
            state["revision"],
            document,
            origin="legacy_import",
            now=dt_util.utcnow(),
        )
        if not committed.ok:
            return {"ok": False, "code": committed.code, "mode": state["mode"]}
        state = await store.async_read()
    result = await store.async_set_mode(state["revision"], mode)
    if result.ok and mode == "active":
        # The dry run was clean, so the views equal today's settings: this
        # writes the marker and nothing else.
        code, _changed = await _async_write_views(
            hass, entry, state["document"], state["revision"]
        )
        if code:
            return {
                "ok": False,
                "code": code,
                "mode": "active",
                "revision": state["revision"],
            }
        await store.async_set_view_revision(state["revision"])
    after = await store.async_read()
    return {
        "ok": result.ok,
        "code": result.code,
        "mode": after["mode"],
        "revision": after["revision"],
    }


def _drift_issue_id(entry_id: str, item: dict[str, Any]) -> str:
    where = item["subentry_id"] or item["part"]
    return f"{ISSUE_DRIFT}_{entry_id}_{where}_{item['key']}"


async def async_check_drift(hass: Any, entry: Any) -> int:
    """Raise one fixable Repair per drifted owned key, and clear the ones that
    no longer drift (step 5c). `active` only; every other mode clears them.
    Returns the number of drifted keys."""
    from homeassistant.helpers import issue_registry as ir

    state = await _store(hass, entry).async_read()
    items: list[dict[str, Any]] = []
    if state["mode"] == "active" and state["document"] is not None:
        items = device_import.drift(state["document"], snapshot(hass, entry))
    wanted = {_drift_issue_id(entry.entry_id, item): item for item in items}
    prefix = f"{ISSUE_DRIFT}_{entry.entry_id}_"
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, item in wanted.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DRIFT,
            translation_placeholders={
                "key": item["key"],
                "view": str(item["view"]),
                "current": str(item["current"]),
            },
            data={
                "kind": ISSUE_DRIFT,
                "entry_id": entry.entry_id,
                "part": item["part"],
                "subentry_id": item["subentry_id"],
                "key": item["key"],
            },
        )
    return len(items)


def _unit_issue_id(entry_id: str, item: dict[str, Any]) -> str:
    return f"{ISSUE_UNIT}_{entry_id}_{item['device_id']}_{item['role']}"


async def async_check_units(hass: Any, entry: Any) -> int:
    """Raise one fixable Repair per power sensor whose unit must be declared,
    and clear the ones that no longer need it (DC-006 observation slice).
    `shadow` and `active` only: in `legacy` nothing reads the contract.
    Returns how many are pending."""
    from homeassistant.helpers import issue_registry as ir

    state = await _store(hass, entry).async_read()
    items: list[dict[str, Any]] = []
    if state["mode"] in ("shadow", "active") and state["document"] is not None:
        items = device_units.undeclared(state["document"], _state_reader(hass))
    wanted = {_unit_issue_id(entry.entry_id, item): item for item in items}
    prefix = f"{ISSUE_UNIT}_{entry.entry_id}_"
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, item in wanted.items():
        stated = item["stated_unit"]
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNIT,
            translation_placeholders={
                "entity_id": item["entity_id"],
                "stated": f"'{stated}'" if stated not in (None, "") else "no unit",
            },
            data={
                "kind": ISSUE_UNIT,
                "entry_id": entry.entry_id,
                "device_id": item["device_id"],
                "role": item["role"],
            },
        )
    return len(items)


async def async_declare_unit(
    hass: Any, entry: Any, device_id: str, role: str, unit: str
) -> str | None:
    """The household's answer to a unit Repair: commit `unit` as the
    binding's declared unit, as a new revision. Returns None, or why not."""
    async with _lock(hass, entry.entry_id):
        store = _store(hass, entry)
        state = await store.async_read()
        if state["mode"] not in ("shadow", "active") or state["document"] is None:
            return "not_in_use"
        document = device_units.declare(state["document"], device_id, role, unit)
        if document is None:
            return "no_longer_applies"
        origin = f"unit_declared:{device_id}.{role}"
        if state["mode"] == "active":
            result = await _async_commit_active(
                hass, entry, state["revision"], document, origin
            )
            code = None if result["ok"] else result["code"]
        else:
            committed = await store.async_commit(
                state["revision"], document, origin=origin, now=dt_util.utcnow()
            )
            code = None if committed.ok else committed.code
    await async_check_units(hass, entry)
    return code


async def async_keep(
    hass: Any, entry: Any, part: str, subentry_id: str | None, key: str
) -> str | None:
    """*Keep my change* for one drifted key: commit it as a new revision.
    Returns None, or why not."""
    async with _lock(hass, entry.entry_id):
        state = await _store(hass, entry).async_read()
        if state["mode"] != "active" or state["document"] is None:
            return "not_active"
        kept = device_import.adopt(
            state["document"], snapshot(hass, entry), part, subentry_id, key
        )
        if kept.conflicts or kept.problems:
            return "keep_not_possible"
        result = await _async_commit_active(
            hass, entry, state["revision"], kept.document, f"drift_keep:{key}"
        )
    await async_check_drift(hass, entry)
    return None if result["ok"] else result["code"]


async def async_restore(
    hass: Any, entry: Any, part: str, subentry_id: str | None, key: str
) -> str | None:
    """*Restore* for one drifted key: write the document's value back to that
    key only. No new revision. Returns None, or why not."""
    async with _lock(hass, entry.entry_id):
        state = await _store(hass, entry).async_read()
        if state["mode"] != "active" or state["document"] is None:
            return "not_active"
        view = device_import.legacy_view(state["document"])
        writes: dict[str, Any] = {"options": {}, "numbers": {}, "subentries": {}}
        if part == "subentries":
            value = view["subentries"].get(subentry_id or "", {}).get(key)
            writes["subentries"] = {subentry_id: {key: value}}
        else:
            value = view[part].get(key)
            writes[part] = {key: value}
        code, changed = await _async_write_views(
            hass, entry, state["document"], state["revision"], writes=writes
        )
        if code:
            return code
    if changed:
        hass.config_entries.async_schedule_reload(entry.entry_id)
    await async_check_drift(hass, entry)
    return None


async def async_number_changed(hass: Any, entry: Any, key: str, value: float) -> None:
    """A Nimbus number changed. In `active`, a contract-owned one commits a
    new revision, so the slider edits the document (DC-004, "Number entities
    are the hard case"). A write the document itself made is the same value
    and commits nothing ("unchanged")."""
    if key not in device_import.owned_number_keys():
        return
    async with _lock(hass, entry.entry_id):
        state = await _store(hass, entry).async_read()
        if state["mode"] != "active" or state["document"] is None:
            return
        edited = device_import.with_number(state["document"], key, value)
        if edited is None:
            return
        await _async_commit_active(
            hass, entry, state["revision"], edited, f"number:{key}"
        )


# --- DC-005: discovery on this install ----------------------------------------
#
# Read-only: the Energy Dashboard's preferences (never `energy/save_prefs`),
# the entity registry and live states. Nothing here calls a service (DC-R15).
# `async_confirm` is the one writer: it commits through DC-004, after the
# household's choices are applied and every changed binding is resolved by
# DC-002 against the live state.

#: Provider integrations discovery recognises (device_discovery's matrix).
_PROFILE_DOMAINS = frozenset(device_discovery.PROFILES) | frozenset(
    device_discovery.REPORTED_ONLY
)

#: DC-002 reasons that mean the BINDING is wrong, not that its data is
#: missing right now: a switch to one is refused (DC-005 decision 6). Every
#: unit refusal (`unit_*`) is one too.
_BINDING_FAULTS = frozenset(
    {
        "adapter_unknown",
        "adapter_field_unknown",
        "adapter_reads_prices_only",
        "source_kind_unknown",
        "role_unknown",
        "no_entity",
        "no_statistic",
        "energy_unit_as_power",
        "currency_mismatch",
        "site_currency_missing",
        "calculation_unknown",
        "dependency_dimension_differs",
        "matched_rate_is_export_side",
        "settlement_is_not_a_forecast",
        "action_not_read_only",
        "rows_only_in_action_response",
    }
)


def _is_binding_fault(reason: str | None) -> bool:
    return bool(reason) and (reason in _BINDING_FAULTS or reason.startswith("unit_"))


def _profile_key(platform: str, uid: str, config_entry_id: str | None):
    """(instance, key) of one provider entity, from its built-in unique_id
    (device_discovery's PROFILES comment), or (None, None)."""
    if platform == "localvolts_v2":
        prefix = f"{config_entry_id}_"
        if config_entry_id and uid.startswith(prefix):
            return config_entry_id, uid[len(prefix) :]
        return None, None
    if platform == "amber_express":
        for suffix in ("_general_price", "_feed_in_price"):
            if uid.endswith(suffix):
                return uid[: -len(suffix)], suffix[1:]
        return None, None
    if platform == "amberelectric":
        if "-forecasts-" in uid:
            site, rest = uid.rsplit("-forecasts-", 1)
            return site, f"forecasts-{rest}"
        return None, None
    # Reported-only providers: one instance per config entry, no keys.
    return config_entry_id, None


def profile_instances(hass: Any) -> list[dict[str, Any]]:
    """Each configured provider instance, with its entities by key: what
    `device_discovery.add_provider_profiles` reads. Enabled entities only,
    found by unique_id, so a renamed entity is still found."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for e in er.async_get(hass).entities.values():
        if e.platform not in _PROFILE_DOMAINS or e.disabled_by is not None:
            continue
        instance, key = _profile_key(e.platform, str(e.unique_id), e.config_entry_id)
        if instance is None:
            continue
        item = found.setdefault(
            (e.platform, instance),
            {"profile": e.platform, "instance": instance, "keys": {}},
        )
        if key:
            item["keys"][key] = e.entity_id
    return [found[k] for k in sorted(found)]


async def _async_energy_prefs(hass: Any) -> dict[str, Any] | None:
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        return manager.data
    except Exception:  # noqa: BLE001 -- no Energy Dashboard is a finding, not a fault
        return None


async def async_propose(hass: Any) -> tuple[Any, dict[str, str]]:
    """DC-005's proposal from Home Assistant alone: the Energy Dashboard, the
    recognised providers and history pairing. It needs no Nimbus entry, so
    the new-install setup (DC-007) can show it before the hub exists.
    Returns the proposal and the entity registry's ids. Writes nothing."""
    prefs = await _async_energy_prefs(hass)
    registry = er.async_get(hass)
    regs = {e.entity_id: e.id for e in registry.entities.values()}
    proposal = device_discovery.propose_from_energy_dashboard(
        prefs, site=_site(hass), registry_ids=regs
    )
    proposal = device_discovery.add_provider_profiles(
        proposal, profile_instances(hass), registry_ids=regs
    )
    proposal = await _async_add_history_pairing(hass, proposal, regs)
    return proposal, regs


async def _async_discovery(hass: Any, entry: Any):
    proposal, regs = await async_propose(hass)
    state = await _store(hass, entry).async_read()
    rediscovery = (
        device_discovery.rediscover(
            state["document"],
            proposal,
            present_registry_ids=frozenset(regs.values()),
        )
        if state["document"] is not None
        else None
    )
    return proposal, state, rediscovery


def _problems(problems: Any) -> list[dict[str, Any]]:
    return [{"code": p.code, "path": p.path, "message": p.message} for p in problems]


async def async_discover(hass: Any, entry: Any) -> dict[str, Any]:
    """`nimbus_load.device_contract_discover`: the proposal, or with a stored
    document the differences against it. Writes nothing."""
    proposal, state, rediscovery = await _async_discovery(hass, entry)
    out: dict[str, Any] = {"mode": state["mode"], "revision": state["revision"]}
    if rediscovery is None:
        out.update(
            {
                "document": proposal.document,
                "questions": proposal.questions,
                "findings": proposal.findings,
                "problems": _problems(proposal.problems),
            }
        )
    else:
        out.update(
            {
                "differences": rediscovery.differences,
                "questions": rediscovery.questions,
                "findings": rediscovery.findings,
            }
        )
    return out


def _state_reader(hass: Any):
    def read(entity_id: str) -> dict[str, Any] | None:
        st = hass.states.get(entity_id)
        if st is None:
            return None
        return {
            "state": st.state,
            "attributes": dict(st.attributes),
            "last_changed": st.last_changed.isoformat(),
            "last_updated": st.last_updated.isoformat(),
            "last_reported": st.last_reported.isoformat(),
        }

    return read


async def _async_statistics(
    hass: Any, ids: set[str]
) -> dict[str, dict[str, Any]] | None:
    """The latest row of each statistic, for validating a binding to it: its
    existence and unit, not its arithmetic. None when the recorder cannot be
    read (not loaded, or an API this HA does not have): then a statistic
    binding is flagged as not validated, never refused on that account."""
    if not ids:
        return {}
    try:
        return await _async_statistics_rows(hass, ids)
    except Exception:  # noqa: BLE001 -- see docstring
        return None


async def _async_statistics_rows(hass: Any, ids: set[str]) -> dict[str, dict[str, Any]]:
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
            get_metadata,
        )
    except ImportError:
        return {}  # pragma: no cover - the recorder ships with HA
    recorder = get_instance(hass)
    meta = await recorder.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=ids)
    )
    out: dict[str, dict[str, Any]] = {}
    for sid in ids:
        if sid not in meta:
            continue
        rows = await recorder.async_add_executor_job(
            lambda sid=sid: get_last_statistics(hass, 1, sid, False, {"state", "sum"})
        )
        row = (rows.get(sid) or [None])[0]
        if not row:
            continue

        def _iso(value: Any) -> str | None:
            if isinstance(value, (int, float)):
                return dt_util.utc_from_timestamp(value).isoformat()
            return value.isoformat() if hasattr(value, "isoformat") else value

        out[sid] = {
            "value": row.get("state")
            if row.get("state") is not None
            else row.get("sum"),
            "unit": meta[sid][1].get("unit_of_measurement"),
            "start": _iso(row.get("start")),
            "end": _iso(row.get("end")),
        }
    return out


async def _async_validate(
    hass: Any, document: dict[str, Any], changed: list[tuple[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(refusals, flags) for each changed binding, resolved by DC-002 against
    the live state (DC-005 "a source switch is validated")."""
    devices = {d["id"]: d for d in document.get("devices") or []}
    stats = {
        (devices[d]["bindings"][r].get("source") or {}).get("statistic_id")
        for d, r in changed
        if (devices[d]["bindings"][r].get("source") or {}).get("kind")
        == "recorder_statistic"
    }
    rows = await _async_statistics(hass, {s for s in stats if s})
    readers = device_bindings.Readers(
        state=_state_reader(hass),
        statistic=lambda sid, _semantics: (rows or {}).get(sid),
    )
    now = dt_util.utcnow()
    refusals: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    for device_id, role in changed:
        resolved = device_bindings.resolve(document, device_id, role, readers, now)
        reason = resolved.reason
        if rows is None and reason == "no_statistic":
            flags.append(
                {
                    "device": device_id,
                    "role": role,
                    "state": resolved.state,
                    "reason": "statistics_not_readable",
                }
            )
        elif resolved.state == device_bindings.MISSING and _is_binding_fault(reason):
            refusals.append({"device": device_id, "role": role, "reason": reason})
        elif resolved.state != device_bindings.VALID:
            flags.append(
                {
                    "device": device_id,
                    "role": role,
                    "state": resolved.state,
                    "reason": reason,
                }
            )
    return refusals, flags


async def async_confirm(
    hass: Any, entry: Any, revision: int, choices: list[dict[str, Any]]
) -> dict[str, Any]:
    """`nimbus_load.device_contract_confirm`: commit what the household
    accepted from discovery (DC-005 "Confirming a proposal"). `shadow` or
    `active` only; refused, with nothing changed, on a revision that moved,
    a choice discovery did not offer, or a binding DC-002 cannot resolve."""
    async with _lock(hass, entry.entry_id):
        state = await _store(hass, entry).async_read()
        if state["mode"] == "legacy" or state["document"] is None:
            return {"ok": False, "code": "choose_shadow_first", "mode": state["mode"]}
        if state["revision"] != revision:
            return {
                "ok": False,
                "code": "revision_conflict",
                "revision": state["revision"],
            }
        _proposal, _state, rediscovery = await _async_discovery(hass, entry)
        document, refused, changed = device_discovery.apply_confirmation(
            state["document"], rediscovery, choices
        )
        if refused:
            return {"ok": False, "code": "choice_refused", "refused": refused}
        if not changed:
            return {"ok": False, "code": "nothing_to_confirm"}
        invalid, flags = await _async_validate(hass, document, changed)
        if invalid:
            return {"ok": False, "code": "binding_refused", "refused": invalid}
        if state["mode"] == "active":
            result = await _async_commit_active(
                hass, entry, revision, document, "discovery_confirm"
            )
        else:
            committed = await _store(hass, entry).async_commit(
                revision, document, origin="discovery_confirm", now=dt_util.utcnow()
            )
            result = {
                "ok": committed.ok,
                "code": committed.code,
                "mode": "shadow",
                "revision": committed.revision,
                **(
                    {"problems": _problems(committed.problems)}
                    if committed.problems
                    else {}
                ),
            }
    if result.get("ok"):
        result["changed"] = [{"device": d, "role": r} for d, r in changed]
        result["flags"] = flags
    return result


# --- DC-005 step 5b: history pairing on this install (decision 4) ------------

#: How far back pairing reads (decision 4).
PAIRING_DAYS = 7

F_PAIRING_NO_DEVICE = "history_pairing_no_device"
F_PAIRING_NO_CANDIDATES = "history_pairing_no_candidates"
F_PAIRING_UNREADABLE = "history_pairing_history_unreadable"


def _is_power_sensor(hass: Any, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    if state is None:
        return False
    unit = state.attributes.get("unit_of_measurement")
    factor, _unit, _refused = device_bindings.unit_scale("power", unit, None)
    return factor is not None


def pairing_candidates(
    hass: Any, proposal: Any
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """({device id: power sensors to try}, findings) for each pairing target.
    The candidates are the power sensors on the same HA device as the
    target's energy counter: a narrowing hint, never the decision (#1574,
    Mark: "same Home Assistant device should be a candidate-selection
    hint"). A sensor statistic's id is its entity_id."""
    registry = er.async_get(hass)
    rows = [
        {"entity_id": e.entity_id, "device_id": e.device_id}
        for e in registry.entities.values()
    ]
    out: dict[str, list[str]] = {}
    findings: list[dict[str, Any]] = []
    for device_id, (out_stat, _in_stat) in sorted(
        device_discovery.pairing_targets(proposal).items()
    ):
        counter = registry.async_get(out_stat)
        if counter is None or counter.device_id is None:
            findings.append(
                {
                    "code": F_PAIRING_NO_DEVICE,
                    "device": device_id,
                    "statistic_id": out_stat,
                    "message": "the energy counter belongs to no Home Assistant "
                    "device, so there is nothing to narrow a search for its power "
                    "sensor; choose it by hand",
                }
            )
            continue
        candidates = [
            eid
            for eid in device_resolver.entities_on_device(rows, counter.device_id)
            if eid != out_stat and _is_power_sensor(hass, eid)
        ]
        if not candidates:
            findings.append(
                {
                    "code": F_PAIRING_NO_CANDIDATES,
                    "device": device_id,
                    "statistic_id": out_stat,
                    "message": "no power sensor sits on the same Home Assistant "
                    "device as this energy counter",
                }
            )
            continue
        out[device_id] = candidates
    return out, findings


def _series(states: list[Any]) -> list[tuple[Any, float | None]]:
    """Recorder states as a resolver Series: a gap is None, never zero."""
    out: list[tuple[Any, float | None]] = []
    for st in states:
        try:
            value: float | None = float(st.state)
        except (TypeError, ValueError):
            value = None
        out.append((st.last_changed, value))
    return out


async def _async_history(
    hass: Any, entity_ids: list[str], start: Any, end: Any
) -> dict[str, list[tuple[Any, float | None]]]:
    from homeassistant.components.recorder import get_instance, history

    def _read() -> dict[str, list[Any]]:
        return history.get_significant_states(
            hass,
            start,
            end,
            entity_ids=entity_ids,
            significant_changes_only=False,
            no_attributes=True,
        )

    raw = await get_instance(hass).async_add_executor_job(_read)
    return {eid: _series(list(states)) for eid, states in raw.items()}


async def _async_add_history_pairing(hass: Any, proposal: Any, regs: dict) -> Any:
    candidates, findings = pairing_candidates(hass, proposal)
    proposal.findings.extend(findings)
    if not candidates:
        return proposal
    targets = device_discovery.pairing_targets(proposal)
    counters = {stat for device_id in candidates for stat in targets[device_id] if stat}
    wanted = sorted(counters | {e for ids in candidates.values() for e in ids})
    end = dt_util.utcnow()
    start = end - timedelta(days=PAIRING_DAYS)
    try:
        histories = await _async_history(hass, wanted, start, end)
    except Exception:  # noqa: BLE001 -- no recorder, or an API this HA lacks
        proposal.findings.append(
            {
                "code": F_PAIRING_UNREADABLE,
                "devices": sorted(candidates),
                "message": "the recorder's history could not be read, so no "
                "power sensor was paired",
            }
        )
        return proposal
    offered = {
        device_id: {eid: histories.get(eid, []) for eid in ids}
        for device_id, ids in candidates.items()
    }
    statistics = {stat: histories[stat] for stat in counters if stat in histories}
    return device_discovery.add_history_pairing(
        proposal, offered, statistics, start, end, registry_ids=regs
    )


# --- DC-006 slice 1b: readiness on this install --------------------------------


def _bound_roles(document: dict[str, Any]):
    for device in document.get("devices") or []:
        for role, binding in (device.get("bindings") or {}).items():
            yield device, role, binding or {}


def _model_states(entry: Any, document: dict[str, Any]) -> dict[str, str]:
    """{load device id: "trained" | "training"} from each load's forecaster
    (`entry.runtime_data`, one coordinator per subentry). A load maps to its
    subentry through its bindings' `subentry:<id>.<key>` source paths; the
    whole-house load is trained when any load's model is."""
    coordinators = getattr(entry, "runtime_data", None) or {}

    def trained(coordinator: Any) -> bool:
        data = getattr(coordinator, "data", None) or {}
        return bool(data.get("trained_at")) or data.get("forecast_readiness") == "ready"

    by_subentry = {sid: trained(c) for sid, c in coordinators.items()}
    out: dict[str, str] = {}
    for device in document.get("devices") or []:
        if device.get("kind") != "load":
            continue
        if device.get("load_class") == "aggregate":
            out[device["id"]] = "trained" if any(by_subentry.values()) else "training"
            continue
        subentries = {
            str((b.get("provenance") or {}).get("source_path") or "")
            .removeprefix("subentry:")
            .split(".", 1)[0]
            for b in (device.get("bindings") or {}).values()
            if str((b.get("provenance") or {}).get("source_path") or "").startswith(
                "subentry:"
            )
        }
        if subentries & set(by_subentry):
            out[device["id"]] = (
                "trained" if any(by_subentry.get(s) for s in subentries) else "training"
            )
    return out


def _scored_days(hass: Any, entry: Any) -> int:
    registry = er.async_get(hass)
    eid = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_nimbus_solver_quality_report"
    )
    state = hass.states.get(eid) if eid else None
    if state is None:
        return 0
    history = state.attributes.get("history")
    if isinstance(history, list):
        return len(history)
    return 1 if state.attributes.get("latest_date") else 0


async def async_readiness(hass: Any, entry: Any) -> dict[str, Any]:
    """Readiness per function on this install (DC-006, DC-R14): the stored
    device document, or in `legacy` today's settings imported (nothing is
    stored), with each bound entity's live state, each load model's
    training state and the scored days. Reads only."""
    stored = await _store(hass, entry).async_read()
    document = stored["document"]
    if document is None:
        document = device_import.import_legacy(snapshot(hass, entry)).document
    readers = device_bindings.Readers(state=_state_reader(hass))
    now = dt_util.utcnow()
    data_states: dict[tuple[str, str], str] = {}
    for device, role, binding in _bound_roles(document):
        kind = (binding.get("source") or {}).get("kind")
        if kind not in ("entity_state", "entity_attribute"):
            continue
        try:
            resolved = device_bindings.resolve(
                document, device["id"], role, readers, now
            )
        except Exception:
            _LOGGER.debug(
                "Nimbus: readiness could not resolve %s.%s",
                device["id"],
                role,
                exc_info=True,
            )
            continue
        data_states[(device["id"], role)] = resolved.state
    functions = device_readiness.evaluate(
        document,
        data_states=data_states,
        models=_model_states(entry, document),
        scored_days=_scored_days(hass, entry),
    )
    return {
        "mode": stored["mode"],
        "revision": stored["revision"],
        "ready": device_readiness.ready_count(functions),
        "functions": functions,
    }
