"""When both validation criteria exist, the model report says whether they
agree, so the one-step fallback's blind spot is observable (#937) (#1837).
"""

from __future__ import annotations

import ast
import unittest

import _ml_path  # noqa: F401
from nimbus_load.ml import model


def _recursive_branch_source() -> str:
    """The `if` arm of the recursive-vs-one-step selection."""
    source = model.__file__.replace(".pyc", ".py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("if len(recursive_mae) == len(candidate_mae):")
    end = text.index("        else:", start)
    return text[start:end]


class TestTheAgreementIsReported(unittest.TestCase):
    def test_the_fallback_choice_is_computed_in_the_recursive_branch(self):
        """It has to be computed where both dicts are in scope. Anywhere
        else and it is a second implementation of the selection rule."""
        branch = _recursive_branch_source()
        self.assertIn(
            "min(candidate_mae, key=candidate_mae.__getitem__)",
            branch,
            "the recursive branch no longer works out what one-step "
            "selection would have chosen, so the nine circuits that can "
            "only use one-step selection have no evidence attached to "
            "them again -- which is the whole of #937's open decision",
        )

    def test_both_outcomes_are_named(self):
        branch = _recursive_branch_source()
        self.assertIn("agrees", branch)
        self.assertIn("DIFFERS", branch)

    def test_a_disagreement_says_what_it_implies(self):
        """ "DIFFERS" alone is a curiosity. What makes it actionable is
        that this load is one the fallback would get wrong -- which is
        exactly the population the nine unmeasurable circuits sit in."""
        branch = _recursive_branch_source()
        self.assertIn("the fallback would get wrong", branch)

    def test_the_recursive_choice_is_still_what_is_used(self):
        """The report is a report. Selection must be unchanged, or this
        becomes a behaviour change #937 has not decided on."""
        branch = _recursive_branch_source()
        self.assertIn(
            "model_type = min(recursive_mae, key=recursive_mae.__getitem__)", branch
        )
        self.assertNotIn("model_type = one_step_choice", branch)

    def test_the_one_step_choice_never_assigns_model_type(self):
        """Source-level is not enough for this one -- an AST walk proves
        no assignment to `model_type` anywhere in the module reads the
        comparison variable, however it is spelled or nested."""
        source = model.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "model_type" not in targets:
                continue
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Name) and sub.id == "one_step_choice":
                    offenders.append(node.lineno)
        self.assertEqual(
            offenders,
            [],
            f"model_type is being assigned from the comparison value at "
            f"line(s) {offenders}. That variable exists to measure whether "
            f"the fallback criterion would differ; if it feeds selection "
            f"it can no longer answer that.",
        )


class TestTheFallbackBranchIsUntouched(unittest.TestCase):
    """v0.94.391's own fix lives in the `else` arm. This change is in the
    `if` arm and must not have disturbed it."""

    def _fallback_branch(self) -> str:
        source = model.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("        else:", text.index("if len(recursive_mae)"))
        return text[start : text.index("# MASE:", start)]

    def test_it_still_distinguishes_its_two_causes(self):
        branch = self._fallback_branch()
        self.assertIn("if not origins:", branch)
        self.assertIn("no validation origins", branch)
        self.assertIn("OBSERVED", branch)
        self.assertIn("will not change", branch)

    def test_it_still_selects_on_one_step(self):
        self.assertIn(
            "model_type = min(candidate_mae, key=candidate_mae.__getitem__)",
            self._fallback_branch(),
        )


if __name__ == "__main__":
    unittest.main()
