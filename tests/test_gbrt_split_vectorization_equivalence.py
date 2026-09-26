"""Differential equivalence test for the #366 GBRT split-search vectorization.

_build_tree()'s inner `for i in distinct:` split-search loop was rewritten
from pure-Python per-candidate arithmetic to vectorized numpy array ops for
performance (7-23s per tree fit at production row counts -> a fraction of
that). This is a genuine risk: it changes the actual split decisions the
model learns from, not just plumbing around it.

This file pins a verbatim copy of the ORIGINAL pure-Python implementation as
a reference oracle and asserts the real (vectorized) gbrt._build_tree
produces bit-for-bit identical trees (same feature/threshold at every node,
same leaf values, same predictions) across many random datasets covering
the edge cases that make tie-breaking and masking easy to get subtly wrong:
duplicate feature values, ties in gain, min_samples_leaf boundary cases,
single-row-per-leaf-eligible splits, multi-feature datasets, and quantile
trees (which share the same split-finding code, only the leaf value differs).
"""

from __future__ import annotations

import zlib

import _ml_path  # noqa: F401
import numpy as np
import pytest
from nimbus_load.ml import gbrt


def _leaf_value_reference(residuals: np.ndarray, quantile: float | None) -> float:
    if quantile is None:
        return float(np.mean(residuals))
    return float(np.percentile(residuals, quantile * 100))


def _build_tree_reference(
    x: np.ndarray,
    residuals: np.ndarray,
    max_depth: int,
    min_samples_leaf: int,
    depth: int = 0,
    quantile: float | None = None,
) -> gbrt._TreeNode:
    """Verbatim copy of the pre-vectorization _build_tree, kept here as an
    independent oracle. Do not "clean up" to match gbrt.py -- the whole
    point is that this file is frozen and gbrt.py is what changed.
    """
    n, n_features = x.shape
    if depth >= max_depth or n < 2 * min_samples_leaf:
        return gbrt._TreeNode(value=_leaf_value_reference(residuals, quantile))

    best_gain = 0.0
    best_feature: int | None = None
    best_threshold: float | None = None
    parent_sse = float(np.sum((residuals - np.mean(residuals)) ** 2))

    for f in range(n_features):
        col = x[:, f]
        order = np.argsort(col)
        sorted_col = col[order]
        sorted_res = residuals[order]

        distinct = np.where(np.diff(sorted_col) > 1e-12)[0]
        if distinct.size == 0:
            continue

        cum_sum = np.cumsum(sorted_res)
        cum_sum_sq = np.cumsum(sorted_res**2)
        total_sum = cum_sum[-1]
        total_sum_sq = cum_sum_sq[-1]

        for i in distinct:
            n_left = i + 1
            n_right = n - n_left
            if n_left < min_samples_leaf or n_right < min_samples_leaf:
                continue
            left_sum = cum_sum[i]
            left_sse = cum_sum_sq[i] - (left_sum**2) / n_left
            right_sum = total_sum - left_sum
            right_sse = (total_sum_sq - cum_sum_sq[i]) - (right_sum**2) / n_right
            gain = parent_sse - (left_sse + right_sse)
            if gain > best_gain:
                best_gain = gain
                best_feature = f
                best_threshold = float((sorted_col[i] + sorted_col[i + 1]) / 2.0)

    if best_feature is None or best_gain <= 1e-9:
        return gbrt._TreeNode(value=_leaf_value_reference(residuals, quantile))

    mask = x[:, best_feature] <= best_threshold
    return gbrt._TreeNode(
        feature=best_feature,
        threshold=best_threshold,
        left=_build_tree_reference(
            x[mask], residuals[mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
        right=_build_tree_reference(
            x[~mask], residuals[~mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
    )


def _tree_to_tuple(node: gbrt._TreeNode) -> tuple:
    if node.is_leaf():
        return ("leaf", node.value)
    return (
        "split",
        node.feature,
        node.threshold,
        _tree_to_tuple(node.left),
        _tree_to_tuple(node.right),
    )


def _assert_trees_identical(a: gbrt._TreeNode, b: gbrt._TreeNode) -> None:
    assert _tree_to_tuple(a) == _tree_to_tuple(b)


CASES = [
    ("small_1feature", 20, 1, 3, 2, None),
    ("small_multifeature", 40, 4, 3, 2, None),
    ("min_samples_leaf_boundary", 12, 2, 4, 5, None),
    ("many_ties_low_cardinality", 60, 3, 4, 3, None),
    ("wide_features", 30, 10, 3, 2, None),
    ("large_ish", 400, 5, 4, 5, None),
    ("quantile_low", 60, 3, 3, 3, 0.1),
    ("quantile_high", 60, 3, 3, 3, 0.9),
    ("quantile_median", 60, 3, 3, 3, 0.5),
]


@pytest.mark.parametrize(
    "name,n_rows,n_features,max_depth,min_samples_leaf,quantile", CASES
)
def test_vectorized_matches_reference_across_seeds(
    name, n_rows, n_features, max_depth, min_samples_leaf, quantile
):
    for seed in range(30):
        # nimbus issue #1282. Was `hash(name) % 997`, and Python RANDOMISES
        # string hashing per process -- so despite the name, this test
        # explored a DIFFERENT random space on every run. That is what made
        # it fail once in CI on a diff that touched only manifest.json and
        # CHANGELOG.md, then pass on re-run of the same commit.
        #
        # This repo already documents the identical trap in production code:
        # test_1217_retrain_is_staggered.py's own
        # `test_it_does_not_use_pythons_randomised_string_hash`, written when
        # #1217's retrain stagger deliberately chose sha256 over hash() so a
        # subentry lands on the same minute across restarts.
        #
        # crc32 is stable across processes and interpreters, so a CI failure
        # here is now reproducible from the seed alone.
        rng = np.random.default_rng(seed * 1000 + zlib.crc32(name.encode()) % 997)
        x = rng.integers(0, 6, size=(n_rows, n_features)).astype(np.float64)
        residuals = rng.normal(size=n_rows)

        expected = _build_tree_reference(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        actual = gbrt._build_tree(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        _assert_trees_identical(expected, actual)


def test_the_known_tie_break_divergence_is_an_exact_tie_not_a_wrong_split():
    """nimbus issue #1282: the one real divergence the randomised seeding hid.

    A deterministic sweep of the full seed space this test samples from --
    269,190 (case, seed) combinations across every entry in CASES -- found
    exactly ONE tree mismatch: `wide_features` at `default_rng(13631)`.

    It is a TIE, not a wrong split, and this test is what says so rather than
    leaving "the trees differ" as the whole story:

        node root.R.L, 5 rows
          reference : feature 2, threshold 0.5  -> left rows {1, 2, 4}
          vectorized: feature 1, threshold 2.5  -> left rows {0, 3}
          gain, both : 1.316964862796671   (bit-identical)

    The two features cut the node into complementary halves with the same
    sum-of-squares gain, which is why the child leaf values come out equal but
    swapped. Both trees are correct; the paths break the tie on different
    features.

    So this asserts the property that actually matters -- the two candidate
    splits are exactly equally good -- rather than asserting the trees match,
    which they legitimately do not here.

    **What this deliberately does NOT do:** change the tie-break in
    `gbrt._build_tree` so the paths agree. That would be a change to the
    trained model's output on real installs to satisfy a test, and it is a
    decision for whoever owns the ML path, not a side effect of a flake
    investigation. Recorded on #1282 instead.
    """
    rng = np.random.default_rng(13631)
    x = rng.integers(0, 6, size=(30, 10)).astype(np.float64)
    residuals = rng.normal(size=30)

    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)

    # The divergence is real and still present -- if it ever disappears, the
    # tie-break was changed and this test should be revisited deliberately.
    assert _tree_to_tuple(expected) != _tree_to_tuple(actual), (
        "the known #1282 divergence at seed 13631 is gone -- if the tie-break "
        "was made to agree, delete this test and say so on that issue"
    )

    # Descend to the disputed node and prove the two candidate splits tie.
    node = expected
    subset_x, subset_r = x, residuals
    for step in ("R", "L"):
        mask = subset_x[:, node.feature] <= node.threshold
        keep = ~mask if step == "R" else mask
        subset_x, subset_r = subset_x[keep], subset_r[keep]
        node = node.right if step == "R" else node.left

    def _gain(feature: int, threshold: float) -> float:
        mask = subset_x[:, feature] <= threshold
        left, right = subset_r[mask], subset_r[~mask]

        def ss(a: np.ndarray) -> float:
            return float(a.sum() ** 2 / len(a)) if len(a) else 0.0

        return ss(left) + ss(right)

    assert _gain(2, 0.5) == _gain(1, 2.5), (
        "the #1282 divergence was only acceptable because the two splits are "
        "an EXACT tie; if their gains now differ, one path is choosing a "
        "genuinely worse split and this is a real bug"
    )


def test_the_seeding_is_reproducible_across_processes():
    """The root cause of #1282's flakiness, pinned.

    `hash()` on a str is salted per process, so a test seeded from it samples
    a different space on every run -- which is how a genuine divergence sat
    undetected and then surfaced on an unrelated release PR.

    Parsed rather than grepped: a text search would match the explanatory
    comment above the line and pass for the wrong reason. The same technique
    `test_1217_retrain_is_staggered.py` uses for the identical trap in
    production code.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(
        textwrap.dedent(
            inspect.getsource(test_vectorized_matches_reference_across_seeds)
        )
    )
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "hash" not in called, (
        "this test seeds from Python's randomised string hash, so it explores "
        "a different space every run and its failures are not reproducible "
        "(nimbus issue #1282)"
    )


def test_vectorized_matches_reference_continuous_features():
    for seed in range(20):
        rng = np.random.default_rng(seed + 5000)
        x = rng.normal(size=(150, 6))
        residuals = rng.normal(size=150) * 3.0

        expected = _build_tree_reference(x.copy(), residuals.copy(), 4, 5, 0, None)
        actual = gbrt._build_tree(x.copy(), residuals.copy(), 4, 5, 0, None)
        _assert_trees_identical(expected, actual)


def test_vectorized_matches_reference_all_identical_feature_values():
    # No valid split anywhere for this feature -- must fall through to a leaf,
    # not raise on an all-invalid/empty gain array.
    rng = np.random.default_rng(42)
    x = np.ones((10, 2))
    residuals = rng.normal(size=10)
    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)
    _assert_trees_identical(expected, actual)
    assert actual.is_leaf()


def test_vectorized_matches_reference_exact_tie_gain_prefers_first_feature():
    # Two duplicate feature columns should tie on gain at every split point;
    # the reference picks the first (lowest-index) feature on a tie because
    # it only replaces the champion on strict `>`. The vectorized version
    # must reproduce this exact tie-break, not e.g. numpy's own argmax
    # semantics under a different reduction order.
    rng = np.random.default_rng(7)
    col = rng.integers(0, 5, size=30).astype(np.float64)
    x = np.column_stack([col, col.copy(), rng.integers(0, 5, size=30)])
    residuals = rng.normal(size=30)

    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)
    _assert_trees_identical(expected, actual)


def test_vectorized_matches_reference_full_fit_predictions():
    # End-to-end through GBRT.fit()/predict(), not just _build_tree directly.
    rng = np.random.default_rng(99)
    x = rng.normal(size=(200, 4))
    y = x[:, 0] * 2.0 - x[:, 1] + rng.normal(size=200) * 0.1

    model = gbrt.GBRT(n_estimators=15, max_depth=3, min_samples_leaf=5)
    model.fit(x, y)
    preds = model.predict(x)

    # Cross-check against the reference oracle by monkeypatching _build_tree
    # for a fresh model fit over the same data.
    original = gbrt._build_tree
    gbrt._build_tree = _build_tree_reference
    try:
        reference_model = gbrt.GBRT(n_estimators=15, max_depth=3, min_samples_leaf=5)
        reference_model.fit(x, y)
        reference_preds = reference_model.predict(x)
    finally:
        gbrt._build_tree = original

    np.testing.assert_array_equal(preds, reference_preds)
