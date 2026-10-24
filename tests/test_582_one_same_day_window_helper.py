"""nimbus #582: the same-day-window fix lives in one helper, not four
copies (extracted under #485). Checks that read the source tree, kept apart
until the decision on source-reading tests (#1837); the helper's behaviour
is in tests/v2/orchestration/test_controllable_load_build.py.

`_build_daily_adequacy_windows()` evaluates the same predicate as one arm
of a larger conditional, a superset rather than a copy, and is pinned as
such so nobody folds it in on the strength of a grep.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer
from _writer_source import writer_source


class TestTheCopiesAreGone(unittest.TestCase):
    """The point of the extraction. Source-checked because the defect
    class is duplication itself, which no behavioural test can see."""

    def test_the_helper_has_exactly_four_callers(self):
        """Counted over the AST's real references, not the source text.

        This was a `writer_source().count(...) == 5` check until nimbus #1357,
        and a plain text count cannot tell a CALL from a mention in a comment or
        a docstring. That cuts both ways and the second way is the dangerous
        one: a module that explains in prose why it does NOT use this helper
        pushed the count to 6 and failed the gate, and equally, someone adding a
        real fifth call site while deleting one prose mention in the same change
        would have kept the count at 5 and passed it. Counting name LOADS --
        whether bare (`ast.Name`) or reached through the `sw.` seam
        (`ast.Attribute`) -- distinguishes the two, while a definition is
        neither.
        """
        import ast

        from _writer_source import writer_trees

        defined = 0
        called = 0
        for _path, tree in writer_trees():
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "_earliest_period_for_same_day_window"
                ):
                    defined += 1
                elif (
                    isinstance(node, ast.Name)
                    and node.id == "_earliest_period_for_same_day_window"
                    and isinstance(node.ctx, ast.Load)
                ):
                    called += 1
                elif (
                    isinstance(node, ast.Attribute)
                    and node.attr == "_earliest_period_for_same_day_window"
                    and isinstance(node.ctx, ast.Load)
                ):
                    # nimbus #1305 (spec 006, Phase 6): the two call sites inside
                    # apply_commanded_state_guard moved to solver_dispatch/guard.py
                    # and now reach this helper through the deferred seam, as
                    # `sw._earliest_period_for_same_day_window(...)` -- an
                    # ast.Attribute, not an ast.Name. Counting only Names would
                    # have silently dropped them and reported 2 of 4, i.e. this
                    # guard would have started under-counting rather than failing.
                    # `sw` itself is an ast.Name with a different id, so there is
                    # no double count.
                    called += 1
        self.assertEqual(defined, 1, "expected exactly one definition")
        self.assertEqual(
            called,
            4,
            "expected exactly four call sites (two in build_controllable_loads, "
            "two in apply_commanded_state_guard) -- a fifth copy of this logic "
            "reappearing inline is the exact drift #485 asked about, and it has "
            "already cost this project one real inconsistency",
        )

    def test_no_inline_copy_of_the_predicate_remains_in_those_functions(self):
        """`earliest_today` should now appear only inside the helper and
        inside `_build_daily_adequacy_windows()`, which is a superset
        rather than a copy."""
        import ast

        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name in (
                "_earliest_period_for_same_day_window",
                "_build_daily_adequacy_windows",
            ):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id == "earliest_today"
                ):
                    offenders.append(f"{fn.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "an inline copy of #582's same-day computation has reappeared "
            f"outside the helper: {offenders}. Call "
            "_earliest_period_for_same_day_window() instead -- four copies "
            "is how this started.",
        )


class TestTheFifthSiteIsNotACopy(unittest.TestCase):
    """`_build_daily_adequacy_windows()` shares the predicate but is a
    superset, and is deliberately excluded. Pinned so nobody folds it in
    later on the strength of a grep, and so that if it ever IS reduced to
    the same shape, this test says why that would now be safe.
    """

    def test_it_handles_cases_the_helper_does_not(self):

        text = writer_source()
        start = text.index("def _build_daily_adequacy_windows(")
        end = text.index("def ", start + 10)
        body = text[start:end]
        for marker in ("today_done", "now > deadline_today", "day_target_kwh"):
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    "_build_daily_adequacy_windows() no longer handles the "
                    "cases that made it a superset rather than a copy of "
                    "the #582 guard. If it has genuinely been reduced to "
                    "the same shape, it can now call "
                    "_earliest_period_for_same_day_window() -- and should.",
                )


if __name__ == "__main__":
    unittest.main()
