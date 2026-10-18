"""DC-004 step 3: the canonical store (device_storage.py) on an in-memory
backend. F10's storage cases: commit, revision race, idempotent re-commit,
refused invalid document, monotonic revisions after a revert, history kept,
mode transitions. Synthetic documents only."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dc = _load("device_contract")
di = _load("device_import")
ds = _load("device_storage")

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)


class Memory:
    def __init__(self):
        self.data = None
        self.saves = 0

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.saves += 1
        self.data = copy.deepcopy(data)


def _doc(capacity=40.0):
    snap = {
        "site": {
            "name": "Synthetic",
            "timezone": "Australia/Brisbane",
            "currency": "AUD",
        },
        "options": {"solver_battery_soc_sensor": "sensor.soc"},
        "numbers": {"solver_battery_capacity_kwh": capacity},
    }
    res = di.import_legacy(snap)
    assert res.problems == []
    return res.document


def run(coro):
    return asyncio.run(coro)


def test_an_empty_store_reads_as_legacy_with_no_document():
    state = run(ds.ContractStore(Memory()).async_read())
    assert (
        state["mode"] == "legacy"
        and state["revision"] == 0
        and state["document"] is None
    )


def test_commit_and_read_back():
    mem = Memory()
    store = ds.ContractStore(mem)
    res = run(store.async_commit(0, _doc(), origin="legacy_import", now=NOW))
    assert res.ok and res.revision == 1
    state = run(store.async_read())
    assert dc.serialise(state["document"]) == dc.serialise(_doc())
    assert state["origin"] == "legacy_import" and state["mode"] == "legacy"


def test_revision_race_one_wins_the_other_changes_nothing():
    mem = Memory()
    store = ds.ContractStore(mem)
    run(store.async_commit(0, _doc(), origin="legacy_import", now=NOW))
    a = run(store.async_commit(1, _doc(41.0), origin="tab_a", now=NOW))
    b = run(store.async_commit(1, _doc(42.0), origin="tab_b", now=NOW))
    assert a.ok and not b.ok and b.code == "revision_conflict" and b.revision == 2
    assert run(store.async_read())["document"] == _doc(41.0)


def test_an_unchanged_document_is_not_a_new_revision():
    mem = Memory()
    store = ds.ContractStore(mem)
    run(store.async_commit(0, _doc(), origin="legacy_import", now=NOW))
    saves = mem.saves
    res = run(store.async_commit(1, _doc(), origin="legacy_import", now=NOW))
    assert (
        res.ok and res.code == "unchanged" and res.revision == 1 and mem.saves == saves
    )


def test_an_invalid_document_is_refused_and_nothing_is_written():
    mem = Memory()
    store = ds.ContractStore(mem)
    bad = _doc()
    bad["devices"] = [d for d in bad["devices"] if d["kind"] != "grid"]
    res = run(store.async_commit(0, bad, origin="x", now=NOW))
    assert not res.ok and res.code == "document_invalid"
    assert "grid_missing" in [p.code for p in res.problems]
    assert mem.saves == 0


def test_history_is_kept_and_capped():
    store = ds.ContractStore(Memory())
    rev = 0
    for i in range(ds.KEEP_PREVIOUS + 3):
        rev = run(store.async_commit(rev, _doc(10.0 + i), origin="t", now=NOW)).revision
    state = run(store.async_read())
    assert len(state["previous"]) == ds.KEEP_PREVIOUS
    assert state["previous"][0]["revision"] == rev - 1


def test_a_revert_is_a_new_revision_and_numbers_are_never_reused():
    store = ds.ContractStore(Memory())
    run(store.async_commit(0, _doc(40.0), origin="t", now=NOW))
    run(store.async_commit(1, _doc(41.0), origin="t", now=NOW))
    res = run(store.async_revert(2, 1, now=NOW))
    assert res.ok and res.revision == 3
    state = run(store.async_read())
    assert state["document"] == _doc(40.0) and state["origin"] == "revert:1"
    assert not run(store.async_revert(3, 99, now=NOW)).ok


def test_modes_move_only_along_the_allowed_path():
    store = ds.ContractStore(Memory())
    assert run(store.async_set_mode(0, "shadow")).code == "no_document"
    run(store.async_commit(0, _doc(), origin="t", now=NOW))
    assert run(store.async_set_mode(1, "active")).code == "mode_transition_not_allowed"
    assert run(store.async_set_mode(1, "shadow")).ok
    assert run(store.async_set_mode(1, "active")).ok
    assert run(store.async_set_mode(1, "legacy")).ok  # always allowed back
    assert run(store.async_set_mode(1, "bogus")).code == "mode_unknown"
    assert run(store.async_set_mode(0, "shadow")).code == "revision_conflict"


def test_the_store_key_is_per_entry():
    assert ds.store_key("abc") == "nimbus_load_abc_device_contract"


def test_the_logic_imports_no_home_assistant_at_module_level():
    head = Path(ds.__file__).read_text(encoding="utf-8").split("def ha_backend")[0]
    assert "homeassistant" not in head


def test_the_view_marker_follows_the_stored_revision_only():
    """DC-004 step 5b, commit order step 3: the store records which revision
    the legacy views hold, and refuses a revision it does not hold, so the
    marker can never run ahead of the document."""
    store = ds.ContractStore(Memory())
    first = run(store.async_commit(0, _doc(), origin="legacy_import", now=NOW))
    ahead = run(store.async_set_view_revision(first.revision + 1))
    assert ahead.ok is False and ahead.code == "revision_conflict"
    assert "view_revision" not in run(store.async_read())
    assert run(store.async_set_view_revision(first.revision)).ok
    assert run(store.async_read())["view_revision"] == first.revision
