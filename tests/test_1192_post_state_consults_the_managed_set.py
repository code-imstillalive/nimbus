"""nimbus #1192: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in
tests/v2/orchestration/test_native_managed_set.py."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_shared

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


class TestTheSetIsStillTheRealGuard(unittest.TestCase):
    """Pins that the set is actually consulted, so the invariant above
    stays load-bearing rather than guarding a variable nothing reads."""

    def test_ha_post_state_consults_the_set(self):
        # nimbus issue #1301 (spec 001, #1347): ha_post_state() -- and the
        # guard clause below -- moved into solver_shared.py in Phase 2a.
        # solver_writer.ha_post_state is a re-export of the identical
        # function object (see test_solver_shared_facade_identity.py), so
        # this checks the source file where the check now actually lives,
        # not solver_writer.py's own file.
        source = Path(solver_shared.__file__).read_text(encoding="utf-8")
        self.assertIn("if entity_id in _NATIVE_MANAGED_ENTITY_IDS:", source)
