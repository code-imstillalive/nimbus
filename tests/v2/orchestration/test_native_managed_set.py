"""The native managed entity set covers every handler (#1192) (#1837)."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import solver_writer

_SENSOR_SRC = (
    Path(__file__).resolve().parents[3]
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
        # test_no_registered_entity_id_is_missing_from_the_set
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

        # test_the_three_that_were_missing_are_present: Explicit, so a future refactor that rebuilds the set cannot
        for entity_id in (
            "sensor.nimbus_flex_signals",
            "sensor.nimbus_flex_report",
            "sensor.nimbus_offer_curve",
        ):
            with self.subTest(entity_id=entity_id):
                self.assertIn(entity_id, solver_writer._NATIVE_MANAGED_ENTITY_IDS)

        # test_the_set_does_not_claim_entity_ids_nothing_registers: The other direction, and deliberately a weaker assertion.
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

    def test_every_member_is_a_sensor_entity_id(self):
        """The guard compares against `entity_id`, so a bare object id or
        a unique_id in here would never match anything."""
        for entity_id in solver_writer._NATIVE_MANAGED_ENTITY_IDS:
            with self.subTest(entity_id=entity_id):
                self.assertTrue(entity_id.startswith("sensor."))
                self.assertEqual(entity_id, entity_id.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
