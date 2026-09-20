"""IV&V finding (c881b523..6d3a6d0 pass, 2026-09-21): #1178's own
`_imports_section()` guard (test_1067_section_requires_a_flat_save_path.py,
written for nimbus issue #1067) only recognises a `section` reference
reached via a 2-level attribute chain rooted at a bare `ast.Name` --
e.g. `data_entry_flow.section(...)` after `from homeassistant import
data_entry_flow`.

A real, valid Python import style slips past it entirely: a plain
`import homeassistant.data_entry_flow` (no `from`, no alias), used as
`homeassistant.data_entry_flow.section(...)`. That reference is a
3-level attribute chain -- `node.value` is itself an `ast.Attribute`
(`homeassistant.data_entry_flow`), not an `ast.Name` -- so the guard's
`isinstance(value, ast.Name)` check fails and it reports no `section`
usage at all.

The codebase's own convention today is exclusively `from X import Y`
(confirmed: zero `^import homeassistant\\.` hits anywhere in
custom_components/nimbus_load/), so this specific bypass hasn't
occurred in practice -- but nothing enforces that convention, and a
future flow file written the fully-qualified way would add a real
`section()` (with the exact flat-save-path bug #1067's own guard exists
to catch) while this guard stays silent.

This test imports the guard's real source file as a module (not a
reimplementation) and drives its actual `_imports_section()` function
against a synthetic source string using the fully-qualified form.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

import pytest

_GUARD_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "test_1067_section_requires_a_flat_save_path.py"
)

_spec = importlib.util.spec_from_file_location(
    "_test_1067_guard_module", _GUARD_MODULE_PATH
)
_guard_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard_module)
_imports_section = _guard_module._imports_section

_FULLY_QUALIFIED_SECTION_USE = """
import homeassistant.data_entry_flow


def foo():
    return homeassistant.data_entry_flow.section({}, {"collapsed": True})
"""


class TestTheGuardSeesAFullyQualifiedImport(unittest.TestCase):
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "nimbus issue #1187: _imports_section()'s ast.Attribute branch "
            "only unwraps one level (isinstance(value, ast.Name)), so a "
            "3-level chain reached via `import homeassistant.data_entry_flow` "
            "+ `homeassistant.data_entry_flow.section(...)` is invisible to "
            "the guard -- fix by walking an arbitrary-depth attribute chain "
            "back to its root Name, or adding a dedicated check for the "
            "bare `import homeassistant.data_entry_flow` form."
        ),
    )
    def test_a_fully_qualified_section_use_is_detected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as fh:
            fh.write(_FULLY_QUALIFIED_SECTION_USE)
            path = pathlib.Path(fh.name)
        try:
            self.assertTrue(
                _imports_section(path),
                "a real, valid `import homeassistant.data_entry_flow` + "
                "`homeassistant.data_entry_flow.section(...)` usage was "
                "not detected by the guard meant to catch every path a "
                "flow file can reach `section` through",
            )
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
