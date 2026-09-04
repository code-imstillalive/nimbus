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
        rng = np.random.default_rng(seed * 1000 + hash(name) % 997)
        x = rng.integers(0, 6, size=(n_rows, n_features)).astype(np.float64)
        residuals = rng.normal(size=n_rows)

        expected = _build_tree_reference(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        actual = gbrt._build_tree(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        _assert_trees_identical(expected, actual)


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
