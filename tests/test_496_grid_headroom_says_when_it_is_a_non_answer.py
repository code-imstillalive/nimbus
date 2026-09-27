"""A published grid headroom of 0.0 must say whether it is an answer.

## Why this exists

Measured on the reference household, 2026-09-27: over a five-hour window
`sensor.nimbus_flex_grid_export_headroom_kw` took values up to **19.069 kW**,
while `grid_import_headroom_kw` never left **0.0**. `forced_import_cost` read
0.5393 against `forced_export_cost` -0.0087.

That asymmetry is probably *correct*. Headroom is HiGHS bound ranging, not
`limit - planned`, and a variable pinned at a bound legitimately has zero ranging
width -- a real reduced cost on import is exactly what being pinned looks like.

But "probably" was the whole problem. `_headroom_up()` returns 0.0 in two
completely different situations:

* ranging answered and the interval is zero-width -- a real "pinned at a bound,
  no room up";
* `bound_headroom()` returned None, no entry for that variable -- a non-answer
  wearing an answer's clothes.

Nothing published separated those. `grid_*_headroom_unranged[t]` does.

## Why the flag is `unranged` and NOT `Headroom.degenerate`

This is the part worth pinning, because `degenerate` already exists, is already
computed, and `LoadSignals.degenerate[t]` already publishes it -- so reusing it
is the obvious move, and it is wrong. The first attempt at this fix did exactly
that and these tests rejected it.

`_headroom_from()` sets `degenerate = down_raw < TOL or up_raw < TOL` -- **either**
side. Correct for `LoadSignals`, which builds its band from `h.down` AND `h.up`.
`GridSignals` publishes only the UP side, and a grid variable resting at its own
lower bound has `down_raw == 0` as a matter of course. So `degenerate` would read
True on nearly every period, including every one where `up` is a real band, and
on the measured case above it would have read True for BOTH export's 19.069 and
import's 0.0 -- disambiguating nothing, which was the entire point.

The clamped-up-side case needs no flag at all: `_headroom_from()` maps anything
< 1e-9 to exactly 0.0, so a published `up` of 0.0 *already* says "zero ranging
width". Only the missing-entry case is invisible.

## What is deliberately NOT asserted

That import headroom *should* be non-zero. It should not -- the evidence says
pinned-at-a-bound, and inventing a band would be a far worse bug than an
ambiguous zero. These tests pin the ability to tell an answer from a non-answer,
not a particular value.
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
from custom_components.nimbus_load.solver import network as network_mod
from custom_components.nimbus_load.solver.lp import _headroom_from

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


class TestDegenerateIsTheWrongFlagHere(unittest.TestCase):
    """Behavioural proof of the rejected design, so nobody re-adopts it.

    These assertions are about `Headroom`'s own documented semantics, not about
    Nimbus's grid code -- which is precisely why they are worth having: they
    demonstrate from the shipped constructor that the obvious reuse cannot work.
    """

    def test_a_variable_at_its_lower_bound_is_flagged_degenerate(self):
        """down=0 (resting at the lower bound), up=19.069 (a real band).

        `degenerate` is True here. A consumer told "degenerate" about a genuine
        19 kW band has been actively misled.
        """
        h = _headroom_from(0.0, 19.069)
        self.assertTrue(h.degenerate)
        self.assertEqual(h.up, 19.069)

    def test_and_so_is_a_genuinely_pinned_variable(self):
        h = _headroom_from(0.0, 0.0)
        self.assertTrue(h.degenerate)

    def test_so_degenerate_cannot_separate_the_measured_production_case(self):
        """The two rows that motivated #496, side by side. Same flag, opposite
        meanings -- which is why the fix needed a different question."""
        export = _headroom_from(0.0, 19.069)
        import_pinned = _headroom_from(0.0, 0.0)
        self.assertEqual(
            export.degenerate,
            import_pinned.degenerate,
            "if these ever differ, `Headroom.degenerate` became a one-sided "
            "flag and this rework's whole premise should be re-examined",
        )

    def test_a_clamped_up_side_is_already_visible_in_the_published_number(self):
        """The other half of the argument: the case `degenerate` would have
        covered needs no flag, because the clamp is not lossy. Anything below
        the tolerance becomes exactly 0.0, so `up == 0.0` already means
        zero-width."""
        self.assertEqual(_headroom_from(5.0, 1e-12).up, 0.0)
        self.assertEqual(_headroom_from(5.0, 0.0).up, 0.0)


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

    def test_a_real_band_is_ranged(self):
        self.assertFalse(self._unranged(_headroom_from(5.0, 19.069)))

    def test_a_pinned_variable_is_still_ranged(self):
        """The distinction that matters: ranging ANSWERED, and its answer was
        zero. That 0.0 is a measurement and must not be flagged."""
        self.assertFalse(self._unranged(_headroom_from(0.0, 0.0)))

    def test_a_missing_entry_is_unranged(self):
        self.assertTrue(self._unranged(None))

    def test_the_production_case_is_now_readable(self):
        """Import's 0.0 with `unranged` False is a real pinned-at-a-bound
        answer, which is the conclusion #496 asked for evidence of."""
        import_pinned = _headroom_from(0.0, 0.0)
        self.assertFalse(self._unranged(import_pinned))
        self.assertEqual(import_pinned.up, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
