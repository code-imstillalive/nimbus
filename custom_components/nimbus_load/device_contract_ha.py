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
  stay the master. `active` (the views written back) is step 5b and is
  refused here.

Nothing here sends a command to a device (DC-R15): it reads states and the
entity registry, and writes one `Store`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import device_import, device_storage
from .const import DOMAIN

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
        "site": {
            "name": hass.config.location_name,
            "timezone": hass.config.time_zone,
            "currency": hass.config.currency,
        },
    }


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


async def async_set_mode(hass: Any, entry: Any, mode: str) -> dict[str, Any]:
    """`legacy` <-> `shadow` only (module docstring). Returns what happened."""
    store = device_storage.ContractStore(
        device_storage.ha_backend(hass, entry.entry_id)
    )
    state = await store.async_read()
    if mode == "active":
        return {"ok": False, "code": "active_not_available_yet", "mode": state["mode"]}
    if mode == "shadow":
        pv = device_import.preview(snapshot(hass, entry))
        if not pv.clean:
            return {
                "ok": False,
                "code": "dry_run_not_clean",
                "mode": state["mode"],
                **_preview_response(pv),
            }
        committed = await store.async_commit(
            state["revision"],
            pv.document,
            origin="legacy_import",
            now=dt_util.utcnow(),
        )
        if not committed.ok:
            return {"ok": False, "code": committed.code, "mode": state["mode"]}
        state = await store.async_read()
    result = await store.async_set_mode(state["revision"], mode)
    after = await store.async_read()
    return {
        "ok": result.ok,
        "code": result.code,
        "mode": after["mode"],
        "revision": after["revision"],
    }
