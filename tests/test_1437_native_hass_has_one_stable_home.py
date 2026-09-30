"""nimbus issue #1437: the injected `hass` lives on a holder whose identity
never changes.

It used to be `solver_writer._NATIVE_HASS`, a module name REBOUND by
`set_native_hass()`. A rebound name cannot be imported -- an import freezes it
at `None` -- so every reader went through the deferred `_solver_writer()` seam,
and 201 test sites wrote it by plain assignment, which the no-op-patch gate
cannot see (#1434).

It is now `solver_shared.NATIVE.hass`. What is pinned here:

1. **No `_NATIVE_HASS` anywhere in code** -- production or tests. This is the
   check #1437 asked the sweep to be verified by, stated as a count rather than
   grep discipline. It matters most for tests: a test still assigning
   `solver_writer._NATIVE_HASS = stub` would not fail, it would silently write
   a name nothing reads and exercise the real path instead of the stub.
2. **One object.** `solver_writer.NATIVE is solver_shared.NATIVE`, and
   `set_native_hass()` mutates it rather than rebinding anything.
3. **The old name is gone from the module**, so a missed
   `patch.object(solver_writer, "_NATIVE_HASS", ...)` raises instead of
   patching a dead attribute.
4. **A misspelt write raises** (`__slots__`), closing the same silent-no-op
   class for the new object.
5. **A reader that imported the holder at module scope sees a later
   `set_native_hass()`** -- the property the rebound name could not have.
"""

import ast
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
import solver_shared
import solver_writer
from solver_inputs import extra_batteries

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD = "_NATIVE_HASS"

# Measurement tests that search FOR the old name, to assert it is now zero.
# The string is data there, not a use -- listed by path so a new file cannot
# quietly join them.
MEASURES_THE_OLD_NAME = {
    "tests/test_1434_static_direct_assignment_report.py",
    "tests/test_callers_mode_counts_only_real_references.py",
}


def _code_uses_of_old_name(source: str) -> list[int]:
    """Line numbers where `_NATIVE_HASS` appears as CODE: a bare name, an
    attribute, or a string that is exactly that name (the `patch.object(...,
    "_NATIVE_HASS", ...)` / `setattr` forms). Prose in comments and docstrings
    is history and is deliberately not counted."""
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Name) and node.id == OLD)
            or (isinstance(node, ast.Attribute) and node.attr == OLD)
            or (isinstance(node, ast.Constant) and node.value == OLD)
        ):
            lines.append(node.lineno)
    return lines


class TestTheOldNameIsGoneFromCode(unittest.TestCase):
    def test_no_code_anywhere_uses_the_rebound_name(self):
        this_file = Path(__file__).resolve()
        hits = {}
        for top in ("custom_components", "tests"):
            for path in sorted((REPO_ROOT / top).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if path.resolve() == this_file or rel in MEASURES_THE_OLD_NAME:
                    continue
                found = _code_uses_of_old_name(path.read_text(encoding="utf-8"))
                if found:
                    hits[rel] = found
        self.assertEqual(
            hits,
            {},
            "use solver_writer.NATIVE.hass (or patch.object(solver_writer."
            "NATIVE, 'hass', ...)); an assignment to the old name is read by "
            "nothing and silently leaves the real path under test",
        )

    def test_the_scan_is_not_vacuous(self):
        """The detector must actually fire on each of the three code forms,
        or the check above proves nothing."""
        src = (
            "solver_writer._NATIVE_HASS = stub\n"
            "patch.object(solver_writer, '_NATIVE_HASS', None)\n"
            "_NATIVE_HASS = None\n"
            "# _NATIVE_HASS in a comment is history, not code\n"
        )
        self.assertEqual(sorted(_code_uses_of_old_name(src)), [1, 2, 3])


class TestOneStableObject(unittest.TestCase):
    def setUp(self):
        self._prior = solver_shared.NATIVE.hass

    def tearDown(self):
        solver_shared.NATIVE.hass = self._prior

    def test_solver_writer_re_exports_the_same_holder(self):
        self.assertIs(solver_writer.NATIVE, solver_shared.NATIVE)

    def test_the_old_module_attribute_no_longer_exists(self):
        self.assertFalse(hasattr(solver_writer, OLD))
        self.assertFalse(hasattr(solver_shared, OLD))

    def test_set_native_hass_mutates_the_holder_rather_than_rebinding(self):
        holder = solver_shared.NATIVE
        hass = object()
        solver_writer.set_native_hass(hass)
        self.assertIs(solver_shared.NATIVE, holder, "the holder was rebound")
        self.assertIs(holder.hass, hass)
        solver_writer.set_native_hass(None)
        self.assertIsNone(holder.hass)

    def test_a_misspelt_write_raises_instead_of_silently_missing(self):
        with self.assertRaises(AttributeError):
            solver_shared.NATIVE.has = object()

    def test_a_module_scope_reference_sees_a_later_set(self):
        """`extra_batteries` holds `solver_shared` from import time and reads
        `solver_shared.NATIVE.hass` at call time -- the rebound name gave a
        module-scope import `None` forever."""
        hass = object()
        solver_writer.set_native_hass(hass)
        self.assertIs(extra_batteries.solver_shared.NATIVE.hass, hass)


if __name__ == "__main__":
    unittest.main()
