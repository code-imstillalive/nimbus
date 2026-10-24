"""nimbus #1290: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in
tests/v2/orchestration/test_calendar_default_path.py."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver_inputs import extra_batteries

_EXTRA_BATTERIES_SRC = Path(extra_batteries.__file__).read_text(encoding="utf-8")


class TestTheCallSiteIsGuarded(unittest.TestCase):
    """Structural half: the only caller sits inside a conditional that tests
    the trip-calendar config value."""

    def test_there_is_exactly_one_caller_in_the_package(self):
        """A second, unguarded caller elsewhere would silently void the
        carve-out this test earns, so the count is pinned rather than the
        guard alone."""
        package = Path(extra_batteries.__file__).parents[1]
        callers = []
        for path in sorted(package.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "solver_writer.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "fetch_calendar_trips"
                ) or (isinstance(node, ast.Name) and node.id == "fetch_calendar_trips"):
                    # as_posix() so the expected value below is one string on
                    # every platform. A separator-dependent assertion is the
                    # same class of trap as EXE001 on #1315: it passes on the
                    # machine it was written on and fails on CI.
                    callers.append(path.relative_to(package).as_posix())
        self.assertEqual(
            sorted(set(callers)),
            ["solver_inputs/extra_batteries.py"],
            f"expected exactly one caller outside solver_writer.py, found "
            f"{sorted(set(callers))}",
        )

    def test_the_call_is_inside_a_trip_calendar_guard(self):
        """Asserted on the AST, not on line proximity: the call must be
        lexically inside an `if` whose test reads the trip-calendar value."""
        tree = ast.parse(_EXTRA_BATTERIES_SRC)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "trip_calendar" not in test_names:
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "fetch_calendar_trips" in body:
                guarded = True
        self.assertTrue(
            guarded,
            "fetch_calendar_trips() must be called only inside an `if` that "
            "tests the configured trip-calendar entity -- that guard is what "
            "makes #1290 unreachable for an install that has not opted in",
        )
