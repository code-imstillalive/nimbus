"""nimbus issue #1192: `_NATIVE_MANAGED_ENTITY_IDS` had fallen behind the
handlers it is supposed to cover, and the set being incomplete is a bug
rather than a choice.

## The contract

`solver_writer._NATIVE_MANAGED_ENTITY_IDS` states its own scope in its own
comment: it is the entity_ids *"this integration registers
register_entity_handler() for, in native mode"*. The whole point (#312) is
that for those entity_ids, a missing handler means **"the real
SensorEntity has not re-registered yet"** — transient, during setup or a
reload's unload window — never *"this entity_id will never have one"*. So
`ha_post_state()` skips its raw `states.async_set()` fallback for them:

> Writing a raw, non-restored state here would occupy that entity_id in
> the state machine and make the real entity's OWN registration a moment
> later collide with it.

## What was wrong

`sensor.py` calls `register_entity_handler()` for **eleven** entity_ids.
The set listed **eight**. The three missing:

    sensor.nimbus_flex_signals
    sensor.nimbus_flex_report
    sensor.nimbus_offer_curve

So during a reload window those three took the raw fallback and wrote
exactly the non-restored ghost state the guard exists to prevent.

That matches #1192's own reproduction, which is the sharpest evidence
here: on a freshly-restarted devhub, one reload produced collision
warnings for **five entities and no others** — precisely
`FLATTENED_ATTRS_FLEX_REPORT`'s five rows, on the `nimbus_flex` family,
with live (not `restored`) states carrying that day's real values. A
generic reload race would have hit every family; only the flex family was
unguarded.

Stated honestly: closing this gap removes the documented mechanism for
that ghost, on the entity_ids that were missing it. Whether it accounts
for **all** of #1192's observed warnings is not asserted here — that
needs a reload on a live instance to confirm, and the count has already
been shown to depend on how long the instance has been running.

## Why a derived test rather than a longer list

The list was hand-maintained, and hand-maintained lists fall behind — this
one did, three times over. These tests derive the required set from
`sensor.py`'s own call sites by parsing them, so the next entity to get a
handler cannot be forgotten. That is the same discipline the sibling repo
applies to `job_health_check.py`'s job list: register the new thing in the
monitor in the same change that creates it.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

_SENSOR_SRC = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "sensor.py"
)


def _registered_entity_ids() -> set[str]:
    """Every literal entity_id `sensor.py` passes to
    `register_entity_handler()`.

    Parsed rather than grepped so a call spanning several lines (all of
    them do) is read as one call, and so a first argument that is NOT a
    plain string literal is reported instead of silently skipped — a
    computed entity_id would need its own handling here rather than
    quietly dropping out of the invariant.
    """
    tree = ast.parse(_SENSOR_SRC.read_text(encoding="utf-8"))
    found: set[str] = set()
    non_literal: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name != "register_entity_handler" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
        else:
            non_literal.append(getattr(node, "lineno", -1))
    if non_literal:
        raise AssertionError(
            "register_entity_handler() is called with a non-literal "
            f"entity_id at sensor.py line(s) {non_literal}. This test can "
            "no longer derive the full set statically -- extend it rather "
            "than letting the invariant silently shrink."
        )
    return found


class TestEveryRegisteredHandlerIsNativeManaged(unittest.TestCase):
    """The invariant. This is the test that would have caught the gap."""

    def test_no_registered_entity_id_is_missing_from_the_set(self):
        registered = _registered_entity_ids()
        managed = set(solver_writer._NATIVE_MANAGED_ENTITY_IDS)
        missing = sorted(registered - managed)
        self.assertEqual(
            missing,
            [],
            "these entity_ids have a registered native handler but are "
            "absent from _NATIVE_MANAGED_ENTITY_IDS, so ha_post_state() "
            "will take its raw states.async_set() fallback for them during "
            "any setup/reload window and write the non-restored ghost "
            "state that guard exists to prevent (nimbus #1192, mechanism "
            f"#312): {missing}",
        )

    def test_the_three_that_were_missing_are_present(self):
        """Explicit, so a future refactor that rebuilds the set cannot
        silently drop exactly these again."""
        for entity_id in (
            "sensor.nimbus_flex_signals",
            "sensor.nimbus_flex_report",
            "sensor.nimbus_offer_curve",
        ):
            with self.subTest(entity_id=entity_id):
                self.assertIn(entity_id, solver_writer._NATIVE_MANAGED_ENTITY_IDS)

    def test_the_set_does_not_claim_entity_ids_nothing_registers(self):
        """The other direction, and deliberately a weaker assertion.

        An entry with no handler would be inert rather than harmful — the
        guard would skip a fallback for an entity_id that never gets a
        real entity, silently dropping its updates. That is worth knowing
        about, so it is reported; it is not worth failing over a name that
        is registered somewhere this parse does not see, so the message
        says which.
        """
        registered = _registered_entity_ids()
        extra = sorted(set(solver_writer._NATIVE_MANAGED_ENTITY_IDS) - registered)
        self.assertEqual(
            extra,
            [],
            "these entity_ids are in _NATIVE_MANAGED_ENTITY_IDS but "
            "sensor.py registers no handler for them. If one is registered "
            "elsewhere, teach _registered_entity_ids() about it; if it is "
            "genuinely stale, remove it -- an entry with no handler makes "
            f"ha_post_state() drop that entity's updates entirely: {extra}",
        )


class TestTheSetIsStillTheRealGuard(unittest.TestCase):
    """Pins that the set is actually consulted, so the invariant above
    stays load-bearing rather than guarding a variable nothing reads."""

    def test_ha_post_state_consults_the_set(self):
        source = Path(solver_writer.__file__).read_text(encoding="utf-8")
        self.assertIn("if entity_id in _NATIVE_MANAGED_ENTITY_IDS:", source)

    def test_every_member_is_a_sensor_entity_id(self):
        """The guard compares against `entity_id`, so a bare object id or
        a unique_id in here would never match anything."""
        for entity_id in solver_writer._NATIVE_MANAGED_ENTITY_IDS:
            with self.subTest(entity_id=entity_id):
                self.assertTrue(entity_id.startswith("sensor."))
                self.assertEqual(entity_id, entity_id.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
