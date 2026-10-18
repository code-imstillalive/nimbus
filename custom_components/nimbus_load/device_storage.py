"""DC-004 step 3: the canonical store (docs/specs/device-contract/
004-migration-and-compatibility.md, "Storage, revisions and commit").

One store per config entry, `nimbus_load_<entry_id>_device_contract`, separate
from the config entry so its `VERSION` does not change. HA's `Store` is used
with `atomic_writes=True` (its default is False).

The logic is HA-free and runs on an injected backend (`async_load()`,
`async_save(data)`), so it is tested without Home Assistant; `ha_backend()`
gives the real one. **Nothing reads the stored document yet** (DC-006), and
every entry is `legacy` until a household moves it.

Rules:
- **Compare-and-swap:** `async_commit(expected_revision, ...)` refuses with
  `revision_conflict` when the stored revision is not the expected one. Two
  tabs, a Repair and a service cannot overwrite each other.
- **Validated first:** `device_contract.validate()` must find no problem. A
  refused commit leaves the store exactly as it was.
- **Monotonic:** a revision number is never reused, a revert included.
- **History:** the last `KEEP_PREVIOUS` revisions stay in `previous`; a revert
  is a new commit of an old document.
- **The mode is stored, never inferred.** `legacy` → `shadow` → `active`, each
  a household action, and `active` → `legacy` always allowed.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

try:
    from . import device_contract as dc
except ImportError:  # pragma: no cover - loaded as a top-level module in tests
    import device_contract as dc  # type: ignore[no-redef]

STORE_VERSION = 1
KEEP_PREVIOUS = 10
MODES = ("legacy", "shadow", "active")

#: Allowed mode changes (DC-004 "One authority: three modes per entry").
TRANSITIONS = frozenset(
    {
        ("legacy", "shadow"),
        ("shadow", "active"),
        ("shadow", "legacy"),
        ("active", "legacy"),
    }
)


def store_key(entry_id: str) -> str:
    return f"nimbus_load_{entry_id}_device_contract"


class Backend(Protocol):
    async def async_load(self) -> dict[str, Any] | None: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Result:
    ok: bool
    code: str | None = None
    revision: int | None = None
    problems: tuple[dc.Problem, ...] = ()


def _empty() -> dict[str, Any]:
    return {
        "mode": "legacy",
        "revision": 0,
        "document": None,
        "committed_at": None,
        "origin": None,
        "previous": [],
    }


class ContractStore:
    """The one writable device document of a config entry."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._lock = asyncio.Lock()

    async def async_read(self) -> dict[str, Any]:
        """The stored state, or an empty `legacy` state. A copy: editing it
        changes nothing."""
        data = await self._backend.async_load()
        return copy.deepcopy(data) if data else _empty()

    async def async_commit(
        self,
        expected_revision: int,
        document: dict[str, Any],
        *,
        origin: str,
        now: datetime,
    ) -> Result:
        """Store `document` as the next revision, or refuse and change nothing."""
        problems = tuple(dc.validate(document))
        if problems:
            return Result(False, "document_invalid", problems=problems)
        async with self._lock:
            state = await self.async_read()
            if state["revision"] != expected_revision:
                return Result(False, "revision_conflict", revision=state["revision"])
            if state["document"] is not None:
                if dc.serialise(state["document"]) == dc.serialise(document):
                    # Idempotent: an unchanged document is not a new revision.
                    return Result(True, "unchanged", revision=state["revision"])
                state["previous"] = [
                    {
                        "revision": state["revision"],
                        "document": state["document"],
                        "committed_at": state["committed_at"],
                        "origin": state["origin"],
                    },
                    *state["previous"],
                ][:KEEP_PREVIOUS]
            state["revision"] = int(state.get("high_water", state["revision"])) + 1
            state["high_water"] = state["revision"]
            state["document"] = copy.deepcopy(document)
            state["committed_at"] = now.isoformat()
            state["origin"] = origin
            await self._backend.async_save(state)
            return Result(True, revision=state["revision"])

    async def async_set_mode(self, expected_revision: int, mode: str) -> Result:
        """Move between modes. Leaving `legacy` needs a stored document;
        `active` -> `legacy` is always allowed."""
        if mode not in MODES:
            return Result(False, "mode_unknown")
        async with self._lock:
            state = await self.async_read()
            if state["revision"] != expected_revision:
                return Result(False, "revision_conflict", revision=state["revision"])
            if (state["mode"], mode) not in TRANSITIONS:
                return Result(False, "mode_transition_not_allowed")
            if mode != "legacy" and state["document"] is None:
                return Result(False, "no_document")
            state["mode"] = mode
            await self._backend.async_save(state)
            return Result(True, revision=state["revision"])

    async def async_revert(
        self, expected_revision: int, to_revision: int, *, now: datetime
    ) -> Result:
        """Commit an earlier revision's document again, as a new revision."""
        state = await self.async_read()
        old = next((p for p in state["previous"] if p["revision"] == to_revision), None)
        if old is None:
            return Result(False, "revision_not_kept")
        return await self.async_commit(
            expected_revision, old["document"], origin=f"revert:{to_revision}", now=now
        )


def ha_backend(hass: Any, entry_id: str) -> Any:  # pragma: no cover - HA glue
    """The real backend: HA's Store, atomic writes on."""
    from homeassistant.helpers.storage import Store

    return Store(hass, STORE_VERSION, store_key(entry_id), atomic_writes=True)
