"""The stub suite's in-process Home Assistant (#2289 item 5, B.2b).

`use_native_fake(monkeypatch, sw, fake)` runs `solver_writer` in native mode,
the mode every install uses, against a `golden.fake_ha.FakeHA`:

- `NATIVE.hass` is a `golden.fake_native.FakeNativeHass` over the same fake,
  so state reads, writes, service calls and `add_job` go where they go on a
  real install;
- `native_state_changes`, the solve path's one recorder read, answers from
  the fake's history.

Nothing is put in `sys.modules`: the golden child installs fake
`homeassistant` modules in its own interpreter, which must never happen in
the pytest process (`tests/test_1329_*`). Reads are recorded as
`NATIVE_GET` / `NATIVE_HISTORY`, as in the golden master.
"""

from __future__ import annotations

import importlib
from typing import Any

from golden.fake_ha import FakeHA
from golden.fake_native import (
    FakeNativeHass,
    _history_states,
    register_managed_entity_handlers,
)


def use_native_fake(monkeypatch: Any, sw: Any, fake: FakeHA, **hass_kw: Any) -> Any:
    """Point `sw` (either import of solver_writer) at an in-process fake.

    `monkeypatch` undoes the patches; the caller closes the returned hass
    (its loop thread) after the test.
    """
    shared = importlib.import_module(type(sw.NATIVE).__module__)
    hass = FakeNativeHass(fake=fake, **hass_kw)

    def changes(entity_id, start, end, *, no_attributes):
        fake.requests.append(
            {
                "method": "NATIVE_HISTORY",
                "path": f"/recorder/{entity_id}",
                "query": f"start={start.isoformat()}&end={end.isoformat()}",
            }
        )
        return _history_states(fake, entity_id, start, end)

    monkeypatch.setattr(shared.NATIVE, "hass", hass)
    monkeypatch.setattr(shared, "native_state_changes", changes)
    # Handlers are module state: give this test its own table.
    monkeypatch.setattr(
        shared, "_ENTITY_UPDATE_HANDLERS", dict(shared._ENTITY_UPDATE_HANDLERS)
    )
    if hasattr(sw, "_ENTITY_UPDATE_HANDLERS"):
        monkeypatch.setattr(
            sw, "_ENTITY_UPDATE_HANDLERS", shared._ENTITY_UPDATE_HANDLERS
        )
    register_managed_entity_handlers(sw, hass)
    return hass
