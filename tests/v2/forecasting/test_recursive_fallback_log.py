"""When recursive validation cannot be computed and one-step error decides
the model, the log names that cause, not another (#937) (#1837).
"""

from __future__ import annotations

import ast
import unittest

import _ml_path  # noqa: F401
from nimbus_load.ml import model


def _fallback_branch_source() -> str:
    """The `else:` arm of the recursive-vs-one-step selection."""
    source = model.__file__.replace(".pyc", ".py")
    with open(source, encoding="utf-8") as f:
        text = f.read()
    marker = "if len(recursive_mae) == len(candidate_mae):"
    start = text.index(marker)
    # the MASE section begins right after the branch
    end = text.index("# MASE:", start)
    return text[start:end]


class TestTheFallbackDistinguishesItsTwoCauses(unittest.TestCase):
    def test_it_no_longer_blames_origins_unconditionally(self):
        branch = _fallback_branch_source()
        self.assertNotIn(
            "Too few origins for recursive validation",
            branch,
            "the fallback log names 'too few origins' for both causes "
            "again. On a circuit with 4,000+ training points that is "
            "wrong and actively misleading -- it sends a reader looking "
            "for more history when the real gate is sensor sparsity, "
            "which more history cannot fix",
        )

    def test_the_no_origins_case_is_named(self):
        branch = _fallback_branch_source()
        self.assertIn("if not origins:", branch)
        self.assertIn("no validation origins", branch)

    def test_the_sparse_observation_case_is_named(self):
        """The half of the split that was previously mis-described."""
        branch = _fallback_branch_source()
        self.assertIn("OBSERVED", branch)
        self.assertIn("sparsely", branch)

    def test_it_says_more_history_will_not_help(self):
        """The operative half of the message. A household reading
        'too few origins' waits; a household reading this one knows the
        load is permanently on one-step selection and can decide whether
        that matters."""
        branch = _fallback_branch_source()
        self.assertIn("will not change", branch)

    def test_the_origin_count_is_reported(self):
        """Without it the two branches read alike -- 'sparse' with 30
        origins available is a very different statement from 'sparse'
        with two."""
        branch = _fallback_branch_source()
        self.assertIn("len(origins)", branch)


class TestTheSelectionContractIsUnchanged(unittest.TestCase):
    """This change touches a log message only. The selection rule itself
    must be exactly as before, since altering which model is chosen is a
    behaviour change #937 has not decided on.
    """

    def test_recursive_still_wins_when_all_three_candidates_computed(self):
        branch = _fallback_branch_source()
        self.assertIn(
            "model_type = min(recursive_mae, key=recursive_mae.__getitem__)", branch
        )

    def test_the_fallback_still_selects_on_one_step(self):
        branch = _fallback_branch_source()
        self.assertIn(
            "model_type = min(candidate_mae, key=candidate_mae.__getitem__)", branch
        )

    def test_the_gate_is_still_all_three_candidates(self):
        """`len(recursive_mae) == len(candidate_mae)` -- a partial result
        (two of three candidates computed) must still fall back rather
        than select among whichever happened to succeed."""
        branch = _fallback_branch_source()
        self.assertIn("if len(recursive_mae) == len(candidate_mae):", branch)


class TestTheDiagnosticHorizonsAreStillDiagnostic(unittest.TestCase):
    """#937's own horizon curve must stay unable to influence selection --
    it exists to answer whether the winner changes with horizon, and a
    diagnostic that fed back into the decision could not answer that."""

    def test_selection_never_reads_the_by_horizon_dict(self):
        source = model.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "model_type" not in targets:
                continue
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Name) and "by_horizon" in sub.id:
                    offenders.append(node.lineno)
        self.assertEqual(
            offenders,
            [],
            "model_type is being assigned from a by-horizon diagnostic "
            f"at line(s) {offenders}. That dict exists to measure whether "
            "the selection horizon is wrong; if it feeds the selection it "
            "can no longer answer that question.",
        )


if __name__ == "__main__":
    unittest.main()
