"""The discovery rules must live in exactly one place (nimbus issue
#954, Mark Purcell, 48-hour IV&V #950).

`_resolve_geocoded_region_and_prefix()` (#495) and
`_discover_aemo_30min_forecast_sensor()` (#452) were methods on
`NimbusSolverConfigSensor`, and their only coverage was hand-copied
mirrors of the logic inside `test_nem_region.py` and
`test_aemo_30min_forecast_discovery.py`. Grepping `tests/` for either
method name returned nothing at all.

The mirrors agreed with the real code, which is exactly why this was
worth fixing rather than shrugging at: **agreement is not coverage.** An
edit to either shipped method would have kept the entire suite green
while the shipped path silently lost its only tests -- the "tested
helper wired to nothing" shape this project has already been burned by
twice (#538's `sensor.py` bridge tables, #692's switch equivalent).

Both rules now live in `sensor_discovery.py` as plain functions the
tests call directly, and the two former mirrors call them.

This file is the durable half. Re-extracting is a one-time fix; nothing
about it stops the NEXT person from pasting a copy of the filter back
into `sensor.py`, or into a test, at which point the mirror problem
returns without anything failing. So: the suffix literals that DEFINE
each rule may appear in executable code in exactly one module.

Docstrings and comments are deliberately not counted -- `sensor.py`,
`nem_region.py` and `aemo_crosscheck.py` all legitimately name these
entity shapes in prose, and prose cannot drift into a second
implementation.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION = _REPO_ROOT / "custom_components" / "nimbus_load"
_OWNER = "sensor_discovery.py"

# The literals that define the rules. A second implementation has to
# name the suffix it filters on, so these are what a mirror cannot hide.
_DEFINING_LITERALS = ("_geocoded_location", "_current_30min_forecast")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every string node that is a module/class/function
    docstring, so prose can be excluded from the sweep."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _modules_with_literal_in_code(literal: str) -> set[str]:
    hits: set[str] = set()
    for path in sorted(_INTEGRATION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in docstrings:
                continue
            if isinstance(node.value, str) and literal in node.value:
                hits.add(path.name)
    return hits


class TestDiscoveryRulesAreNotMirrored(unittest.TestCase):
    def test_each_rule_is_defined_in_exactly_one_module(self):
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                owners = _modules_with_literal_in_code(literal)
                self.assertEqual(
                    owners,
                    {_OWNER},
                    f"the {literal!r} discovery rule must be defined only in "
                    f"{_OWNER}, but executable code naming it was found in "
                    f"{sorted(owners)}. A second copy is how #954 happened: "
                    f"two implementations that agree today, one of which the "
                    f"tests exercise and the other of which ships. Call the "
                    f"function in {_OWNER} instead of re-filtering.",
                )

    def test_the_sweep_can_actually_see_code(self):
        """The mechanism, not the data: if `_modules_with_literal_in_code`
        ever stops finding anything -- a moved package, a changed
        literal, an AST walk that quietly excludes too much -- the test
        above would pass vacuously by comparing an empty set against an
        empty expectation.
        """
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                self.assertIn(
                    _OWNER,
                    _modules_with_literal_in_code(literal),
                    f"{_OWNER} no longer contains executable code naming "
                    f"{literal!r} -- either the rule moved (update _OWNER) or "
                    f"this sweep has stopped seeing code at all",
                )

    def test_prose_is_excluded_rather_than_the_files_being_lucky(self):
        """`sensor.py` still describes both entity shapes in its
        docstrings, and must keep being allowed to. If the docstring
        exclusion broke, the guard above would start failing on prose --
        so pin that the exclusion is doing real work here, not passing
        because nobody happens to mention these names.
        """
        sensor_py = (_INTEGRATION / "sensor.py").read_text(encoding="utf-8")
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                self.assertIn(
                    literal,
                    sensor_py,
                    "sensor.py no longer mentions this entity shape at all, so "
                    "this test is no longer proving the prose exclusion works",
                )


if __name__ == "__main__":
    unittest.main()
