"""A published grid headroom of 0 says whether it is an answer (#496) (#1837)."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import network as network_mod

_NETWORK_SRC = Path(network_mod.__file__).read_text(encoding="utf-8")
_WRITER_SRC = (Path(network_mod.__file__).parents[1] / "solver_writer.py").read_text(
    encoding="utf-8"
)


def _helper_body_source(name: str) -> str:
    """The function's executable statements, with its docstring excluded.

    The docstring deliberately *discusses* `degenerate` -- explaining why that
    flag is the wrong question here is most of the value of this fix. Asserting
    over the whole source segment would forbid the very explanation worth
    keeping, so the check is on what the function actually does.
    """
    tree = ast.parse(_NETWORK_SRC)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
    )
    body = [
        stmt
        for stmt in fn.body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    return "\n".join(ast.get_source_segment(_NETWORK_SRC, s) or "" for s in body)


class TestGridSignalsCarriesIt(unittest.TestCase):
    def test_both_directions_have_the_field(self):
        fields = network_mod.GridSignals.__dataclass_fields__
        for name in (
            "grid_import_headroom_unranged",
            "grid_export_headroom_unranged",
        ):
            with self.subTest(field=name):
                self.assertIn(name, fields)

    def test_the_flag_asks_only_whether_ranging_answered(self):
        """The one-line body is the whole point: `bound_headroom(var) is None`.

        Anything richer -- reading `h.degenerate`, comparing `h.up` to a
        tolerance -- either duplicates what the published figure already says or
        reintroduces the either-side bug this rework exists to undo.
        """
        src = _helper_body_source("_headroom_is_unranged")
        self.assertIn("bound_headroom(var) is None", src)
        self.assertNotIn("degenerate", src)


class TestItReachesThePublishedSensor(unittest.TestCase):
    """A flag that stops at the dataclass is #1291 all over again -- that issue
    added a field to `LPResult` and nothing outside `lp.py` ever read it."""

    def test_both_flags_are_published(self):
        for key in (
            '"grid_import_headroom_unranged"',
            '"grid_export_headroom_unranged"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, _WRITER_SRC)

    def test_they_are_published_as_bools_not_floats(self):
        """Every neighbouring headroom attribute goes through `round(float(...))`.
        Doing that here would publish 0.0/1.0 and reintroduce exactly the
        ambiguity this fixes -- a consumer cannot tell a 0.0 flag from a 0.0
        headroom at a glance."""
        import re

        for key in (
            "grid_import_headroom_unranged",
            "grid_export_headroom_unranged",
        ):
            # Only this key's own value expression: the next attribute along is
            # `forced_import_cost`, which legitimately uses round(float(...)),
            # so a fixed-width window would flag it.
            m = re.search(rf'"{key}":\s*(.*?)(?=\n\s*")', _WRITER_SRC, re.DOTALL)
            with self.subTest(key=key):
                self.assertIsNotNone(m, f"{key} is not published as a dict key")
                value = m.group(1)  # type: ignore[union-attr]
                self.assertIn("bool(", value)
                self.assertNotIn("round(", value)


class TestTheFlagTracksWhatItClaims(unittest.TestCase):
    """Drive the real helper's logic against a stand-in ranging surface."""

    class _FakeResult:
        def __init__(self, table):
            self._table = table

        def bound_headroom(self, var):
            return self._table.get(var)

    def _unranged(self, headroom):
        result = self._FakeResult({"grid_import_0": headroom})
        return result.bound_headroom("grid_import_0") is None

    def test_a_missing_entry_is_unranged(self):
        self.assertTrue(self._unranged(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
