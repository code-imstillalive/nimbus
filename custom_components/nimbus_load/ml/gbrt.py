"""Pure-numpy Gradient Boosted Regression Trees (GBRT).

Same constraint as ml/model.py's own k-NN: no scikit-learn/xgboost/
lightgbm available (no C compiler in Home Assistant's container, no wheel
for this Python version). GBRT itself isn't exotic, though -- XGBoost/
LightGBM are highly-optimized compiled implementations of a conceptually
simple algorithm (fit a shallow tree to the current residuals, add it into
the ensemble scaled by a small learning rate, repeat); the value they add
over a plain implementation is raw speed at large scale, not a different
algorithm. For a household's few-thousand-row-per-load dataset this
unoptimized numpy version is entirely fast enough, confirmed via real
backtesting against this project's own live data (2026-08-14) rather than
assumed.

Deliberately kept small and dependency-free (numpy only) -- this trains
once a day inside a HA executor thread, not a long-running training job.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class _TreeNode:
    feature: int | None = None
    threshold: float | None = None
    left: "_TreeNode | None" = None
    right: "_TreeNode | None" = None
    value: float | None = None

    def is_leaf(self) -> bool:
        return self.value is not None


def _build_tree(
    x: np.ndarray, residuals: np.ndarray, max_depth: int, min_samples_leaf: int, depth: int = 0
) -> _TreeNode:
    n, n_features = x.shape
    if depth >= max_depth or n < 2 * min_samples_leaf:
        return _TreeNode(value=float(np.mean(residuals)))

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
        return _TreeNode(value=float(np.mean(residuals)))

    mask = x[:, best_feature] <= best_threshold
    return _TreeNode(
        feature=best_feature,
        threshold=best_threshold,
        left=_build_tree(x[mask], residuals[mask], max_depth, min_samples_leaf, depth + 1),
        right=_build_tree(x[~mask], residuals[~mask], max_depth, min_samples_leaf, depth + 1),
    )


def _predict_tree(node: _TreeNode, x: np.ndarray) -> np.ndarray:
    out = np.empty(x.shape[0])
    _predict_tree_rec(node, x, np.arange(x.shape[0]), out)
    return out


def _predict_tree_rec(node: _TreeNode, x: np.ndarray, idx: np.ndarray, out: np.ndarray) -> None:
    if node.is_leaf():
        out[idx] = node.value
        return
    col = x[idx, node.feature]
    left_mask = col <= node.threshold
    if np.any(left_mask):
        _predict_tree_rec(node.left, x, idx[left_mask], out)
    if np.any(~left_mask):
        _predict_tree_rec(node.right, x, idx[~left_mask], out)


@dataclass
class GBRT:
    """scikit-learn-shaped API (fit/predict) so it drops in next to the
    existing k-NN model. `trees`/`init_value` are the only real state --
    kept as plain attributes (not numpy arrays) so the whole object stays
    picklable via plain `pickle`, same as the existing TrainedModel.
    """

    n_estimators: int = 60
    max_depth: int = 3
    learning_rate: float = 0.1
    min_samples_leaf: int = 5
    trees: list[_TreeNode] = field(default_factory=list)
    init_value: float = 0.0

    def fit(self, x: np.ndarray, y: np.ndarray) -> "GBRT":
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        self.init_value = float(np.mean(y))
        pred = np.full(y.shape, self.init_value)
        self.trees = []
        for _ in range(self.n_estimators):
            residuals = y - pred
            tree = _build_tree(x, residuals, self.max_depth, self.min_samples_leaf)
            pred = pred + self.learning_rate * _predict_tree(tree, x)
            self.trees.append(tree)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        pred = np.full(x.shape[0], self.init_value)
        for tree in self.trees:
            pred = pred + self.learning_rate * _predict_tree(tree, x)
        return pred
