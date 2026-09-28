"""nimbus issue #1386: a relaxed thermal guarantee is published, not only logged.

`solver/network.py` retries the solve with each thermal load's hard "must reach
target by the deadline" constraint relaxed to a soft shortfall price whenever the
hard-constrained solve comes back infeasible, and records which loads that
happened to in `Plan.thermal_guarantee_relaxed`.

It already logs a WARNING naming them, so this is not about visibility in the
moment — it is about **the moment being gone**. Production's own error log is
truncated at every container restart: measured on #1360, read at 18:18 AEST it
began at 16:09, 2 h 8 min of history, and it cannot reach an episode from a week
earlier at all. So a log line cannot answer *"how often has this been
happening"*, which is the only question worth asking about it.

## Why the answer to that question matters

The natural cause of *repeated* relaxation is configuration, not weather:
`thermal_max_power_kw` too low for the window, a window too narrow for the tank,
or a learned `heating_rate_c_per_kwh` that has drifted low. Each of those makes
the constraint permanently unsatisfiable, and each is absorbed silently by the
retry on every single cycle. The household sees "hot water is sometimes not hot"
and has nothing to look at.

#774's argument for making the guarantee a HARD constraint was that a bound
cannot be traded away by a tie-break or an objective-weight bug. That argument
covers the bound. It says nothing about the retry — which is the one event that
turns the guarantee soft for a cycle.

## Why `solve_diagnostics`, verified rather than assumed

`solve_diagnostics` is flattened onto `sensor.nimbus_solver_solve_seconds`, and
that entity's attributes really are stored in recorder history — measured
read-only against the reference household: **144 rows in 40 minutes, every one
carrying all eight existing diagnostic keys**. (`docs/entities.md` calls the
dict-valued attributes "live-only by nature", which is about them having no
*child sensor* with its own history, not about the flattened keys going
unrecorded. Worth knowing, because reading that line alone would suggest this
field cannot work — and it does.)

## Two layers, because neither alone is enough

The first pins the published shape: a list, present even when empty, so "nothing
relaxed" is distinguishable from "an older release that does not publish this".
The second walks `solver_writer.py`'s own AST and asserts the real publish path
reads `plan.thermal_guarantee_relaxed`, so the shape test cannot quietly pass
while the shipped expression drifts. Same two-layer approach as
`test_solve_diagnostics_load_counts.py`, for the same reason.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import _solver_path
from _writer_source import writer_trees

WRITER = pathlib.Path(
    _solver_path._SOLVER_PARENT  # type: ignore[attr-defined]
).joinpath("solver_writer.py")
FIELD = "thermal_guarantee_relaxed"


def _published(relaxed) -> dict:
    """Reproduce what the publish path puts in `solve_diagnostics` for this
    field, from the same expression, for a plan whose attribute holds
    `relaxed`.

    Asserts the shape contract rather than importing the native publish path
    (which needs a live hass) — the same tradeoff
    `test_solve_diagnostics_load_counts.py` documents.
    """

    class _Plan:
        pass

    plan = _Plan()
    if relaxed is not ...:
        plan.thermal_guarantee_relaxed = relaxed
    return {FIELD: list(getattr(plan, FIELD, []) or [])}


class TestThePublishedShape(unittest.TestCase):
    def test_nothing_relaxed_publishes_an_empty_list(self):
        """Empty rather than omitted, so a reader can tell "nothing relaxed
        today" from "this release does not report it"."""
        self.assertEqual(_published([])[FIELD], [])

    def test_a_relaxed_load_is_named(self):
        self.assertEqual(_published(["Tank"])[FIELD], ["Tank"])

    def test_several_relaxed_loads_are_all_named(self):
        """A list rather than a count, because WHICH load matters on a
        multi-load install and a count cannot say."""
        self.assertEqual(_published(["Tank", "Pool"])[FIELD], ["Tank", "Pool"])

    def test_a_plan_without_the_attribute_does_not_raise(self):
        """`getattr` with a default, matching the bare-`getattr` convention
        this file's own module already uses for `plan.thermal_loads` — a Plan
        built by an older release, or a test double, must not break publishing
        the whole cycle's diagnostics."""
        self.assertEqual(_published(...)[FIELD], [])

    def test_none_is_normalised_to_empty(self):
        self.assertEqual(_published(None)[FIELD], [])

    def test_the_value_is_always_a_list(self):
        """A tuple would serialise differently through the state machine, and
        this attribute is read back from recorder history where the type has to
        be stable."""
        for relaxed in ([], ["Tank"], ("Tank",), None, ...):
            with self.subTest(relaxed=relaxed):
                self.assertIsInstance(_published(relaxed)[FIELD], list)


class TestThePublishPathActuallyReadsIt(unittest.TestCase):
    """The shape tests above pin the contract; this pins that the shipped
    publish path implements it, so the two cannot drift apart."""

    def _solve_diagnostics_dicts(self) -> list[ast.Dict]:
        # nimbus #1304 (spec 005): the solve_diagnostics literal moved to
        # solver_publish.py with publish_plan. One synthetic Module over every
        # writer module keeps ast.walk() and this loop exactly as written.
        tree = ast.Module(
            body=[n for _p, _t in writer_trees() for n in _t.body],
            type_ignores=[],
        )
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "n_controllable_loads" in keys:
                out.append(node)
        return out

    def test_solve_diagnostics_publishes_the_field(self):
        dicts = self._solve_diagnostics_dicts()
        self.assertTrue(dicts, "no solve_diagnostics dict found in solver_writer.py")
        for node in dicts:
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            with self.subTest(keys=len(keys)):
                self.assertIn(
                    FIELD,
                    keys,
                    f"solve_diagnostics does not publish {FIELD!r}. Without it a "
                    f"relaxed hot-water guarantee exists only in a log that is "
                    f"truncated on every container restart (#1360), so the rate "
                    f"cannot be measured after the fact.",
                )

    def test_the_expression_reads_it_off_the_plan(self):
        """Guards against the field being published from something other than
        the plan -- a hardcoded empty list would satisfy the key check above
        and report "never relaxed" forever."""
        for node in self._solve_diagnostics_dicts():
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == FIELD:
                    expr = ast.unparse(value)
                    with self.subTest(expr=expr):
                        self.assertIn(
                            "plan",
                            expr,
                            f"{FIELD} is published as {expr!r}, which does not "
                            f"read the plan.",
                        )
                        self.assertIn(FIELD, expr)


if __name__ == "__main__":
    unittest.main()
