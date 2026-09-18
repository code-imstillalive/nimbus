"""nimbus #937: when both validation criteria exist, say whether they
agree — so the fallback's blind spot becomes observable instead of
hand-measured.

Half the reference household's circuits cannot compute
`validation_recursive_mae` at all and therefore select on one-step error
**permanently** — that is observation density, not warm-up, and v0.94.391
made the log message say so. What it did not say is whether the fallback
criterion actually reaches a different answer.

**Measured by hand across the fleet on 2026-09-18**, on the nine circuits
carrying both metrics:

    circuit      one-step pick   recursive pick   agree
    pw_pool1     naive           naive            YES
    pw_pool_2    naive           naive            YES
    pw_comms     gbrt            knn              NO
    pw_ldry      gbrt            gbrt             YES
    pw_l1        gbrt            knn              NO
    lt_l1        gbrt            gbrt             YES
    pw_l2        knn             knn              YES
    pw_ac_l2     gbrt            gbrt             YES
    pw_ac_b1     gbrt            gbrt             YES
                                                  7 of 9

**Both disagreements are one-step preferring `gbrt` where recursive
prefers `knn`. Neither goes the other way.** That direction is predicted
by this package's own module docstring: k-NN's prediction is a weighted
average of observed `y_train` values — a convex combination, structurally
bounded — while GBRT is an unbounded additive sum with no clipping.
Recursive validation is the only one of the two criteria that feeds
predictions back as inputs, so it is the only one that can see the
difference. One-step selection therefore does not fail randomly; it
systematically over-picks the model class most exposed to recursive
drift.

A hand-run query across one install at one moment cannot show an
agreement **rate**. A line per retrain per load can, which is why this is
logged rather than computed once.

Logged rather than published as an attribute, deliberately: it is
evidence for a decision #937 has not taken, not a signal anything
consumes. Publishing it would invite something to depend on it before the
decision is made.
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
