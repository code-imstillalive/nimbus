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

Nothing here sends a command to a device (DC-R15): it reads states and the
entity registry, and writes one `Store`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import device_import, device_storage
from .const import DOMAIN

#: The options key that records which revision the legacy views hold
#: (DC-004 "Commit order in `active` mode").
VIEW_REVISION_KEY = "device_contract_view_revision"
#: The Repair raised when the options say a later revision than the store.
ISSUE_INCONSISTENT = "device_contract_inconsistent"

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


def _store(hass: Any, entry: Any) -> device_storage.ContractStore:
    return device_storage.ContractStore(device_storage.ha_backend(hass, entry.entry_id))


async def _async_write_views(
    hass: Any, entry: Any, document: dict[str, Any], revision: int
) -> str | None:
    """Step 2 of the commit order: write every owned key that differs from the
    view of `document`, then the options with the marker last, so the marker
    is set only once the rest is written. Returns an error code, or None."""
    from .flows import live_numbers

    writes = device_import.view_writes(
        device_import.legacy_view(document), snapshot(hass, entry)
    )
    if writes["numbers"]:
        skipped = await live_numbers.async_apply(
            hass, entry.entry_id, writes["numbers"]
        )
        if skipped:
            return "number_not_written"
    for sid, changed in writes["subentries"].items():
        sub = entry.subentries[sid]
        hass.config_entries.async_update_subentry(
            entry, sub, data={**sub.data, **changed}
        )
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, **writes["options"], VIEW_REVISION_KEY: revision},
    )
    return None


async def async_commit_active(
    hass: Any,
    entry: Any,
    expected_revision: int,
    document: dict[str, Any],
    origin: str,
) -> dict[str, Any]:
    """A commit in `active` mode, in the spec's order: the store, then the
    views and the marker, then the store's `view_revision`. A failure between
    steps is repaired by `async_recover` at the next start."""
    store = _store(hass, entry)
    state = await store.async_read()
    if state["mode"] != "active":
        return {"ok": False, "code": "not_active", "mode": state["mode"]}
    committed = await store.async_commit(
        expected_revision, document, origin=origin, now=dt_util.utcnow()
    )
    if not committed.ok:
        return {"ok": False, "code": committed.code, "mode": "active"}
    code = await _async_write_views(hass, entry, document, committed.revision)
    if code:
        return {"ok": False, "code": code, "mode": "active"}
    await store.async_set_view_revision(committed.revision)
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
    if behind:
        code = await _async_write_views(
            hass, entry, state["document"], state["revision"]
        )
        if code:
            return code
    if behind or state.get("view_revision") != state["revision"]:
        await store.async_set_view_revision(state["revision"])
        # Only a write to the settings needs the components to reload.
        return "regenerated" if behind else "marked"
    return "none"


async def async_set_mode(hass: Any, entry: Any, mode: str) -> dict[str, Any]:
    """`legacy`, `shadow` and `active` (module docstring). Returns what
    happened."""
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
    if result.ok and mode == "active":
        # The dry run was clean, so the views equal today's settings: this
        # writes the marker and nothing else.
        code = await _async_write_views(
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
