"""nimbus #937: the recursive-validation fallback log named the wrong
cause for half the circuits it fires on.

`model_type` is decided from `validation_recursive_mae`, falling back to
one-step `validation_mae` when that cannot be computed. The fallback is
logged rather than silent -- but it said:

    "Too few origins for recursive validation -- falling back to
     one-step selection -> using %s"

for **both** ways the metric can come back empty, and only one of them is
about origins.

**Measured on the reference household, 2026-09-18.** Nine of eighteen
circuits publish an empty `validation_recursive_mae` and therefore select
on one-step error. Several carry 4,100-4,300 training points:

    empty:      hws_l1(4296) hws_l3(4290) ac_l1(4194) oven(4194)
                b1(4194) ctp(4194) lounge(4271) heater(4194) lt_l2(521)
    populated:  lt_l1(4167) pool1(1097) pool_2(1040) comms(4167)
                ldry(1285) pw_l1(4166) pw_l2(4167) ac_l2(2740) ac_b1(505)

Training volume does not separate those groups -- the empty side holds
most of the *largest* sets, and the two smallest circuits land on
opposite sides (521 empty, 505 populated).

The actual gate is in `_recursive_multistep_mae()`, which counts an error
only for a genuinely OBSERVED forward point:

    if (actual is not None and math.isfinite(actual)
            and i < len(load_observed) and load_observed[i]):
        errors.append(abs(pred - actual))
    ...
    if not errors:
        return None

So the metric is empty when no origin's forward window contains an
observed actual. That is observation **density**, not history length, and
on a circuit whose sensor idles and reports on change it does not improve
with time. Those loads select on one-step error **permanently**.

"Too few origins" sent a reader looking for more history, which cannot
help. The distinction being tested here is the difference between "wait"
and "this will never happen" -- which is the entire reason a diagnostic
names a cause at all, and the same lesson `mixed_window_not_decisive`
(#1073) and `participant_away_during_window` (#1098) each cost a real
investigation to learn.
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
