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

Quantile regression (added 2026-08-15): pass `quantile` (e.g. 0.1, 0.9)
to fit a model that predicts a given percentile of the target rather
than its mean -- used for genuinely model-derived confidence bands, not
just post-hoc residual tracking. Deliberately a practical approximation,
not textbook-exact gradient boosting for the pinball loss: split-finding
stays squared-error-based (unchanged from the mean-regression case,
since finding pinball-loss-optimal splits is real extra work for
marginal benefit at this data scale), but each leaf's OUTPUT is the
target quantile of the residuals landing in it rather than their mean --
the same "quantile regression forest" approximation used when full
pinball-loss gradient boosting isn't implemented. Documented here as a
real scoping choice, not hidden as if it were the textbook algorithm.

Early stopping (added 2026-08-15): pass x_val/y_val/early_stopping_rounds
to fit() to stop adding trees once held-out validation error stops
improving, rather than always running the full fixed n_estimators --
standard practice, avoids overfitting the last few boosting rounds and
saves real compute on a load whose signal saturates early.
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


def _leaf_value(residuals: np.ndarray, quantile: float | None) -> float:
    if quantile is None:
        return float(np.mean(residuals))
    return float(np.percentile(residuals, quantile * 100))


def _build_tree(
    x: np.ndarray,
    residuals: np.ndarray,
    max_depth: int,
    min_samples_leaf: int,
    depth: int = 0,
    quantile: float | None = None,
) -> _TreeNode:
    n, n_features = x.shape
    if depth >= max_depth or n < 2 * min_samples_leaf:
        return _TreeNode(value=_leaf_value(residuals, quantile))

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
        return _TreeNode(value=_leaf_value(residuals, quantile))

    mask = x[:, best_feature] <= best_threshold
    return _TreeNode(
        feature=best_feature,
        threshold=best_threshold,
        left=_build_tree(
            x[mask], residuals[mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
        right=_build_tree(
            x[~mask], residuals[~mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
    )


def _predict_tree(node: _TreeNode, x: np.ndarray) -> np.ndarray:
    out = np.empty(x.shape[0])
    _predict_tree_rec(node, x, np.arange(x.shape[0]), out)
    return out


def _predict_tree_rec(
    node: _TreeNode, x: np.ndarray, idx: np.ndarray, out: np.ndarray
) -> None:
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
    # None = standard mean-regression (squared-error boosting, unchanged
    # default behaviour). 0.1 / 0.9 / etc = quantile regression -- see
    # module docstring for what's approximated vs textbook-exact.
    quantile: float | None = None
    trees: list[_TreeNode] = field(default_factory=list)
    init_value: float = 0.0

    def _error(self, y: np.ndarray, pred: np.ndarray) -> float:
        if self.quantile is None:
            return float(np.mean((y - pred) ** 2))
        diff = y - pred
        return float(
            np.mean(np.maximum(self.quantile * diff, (self.quantile - 1) * diff))
        )

    def fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        *,
        x_val: np.ndarray | None = None,
        y_val: np.ndarray | None = None,
        early_stopping_rounds: int | None = None,
    ) -> "GBRT":
        """x_val/y_val/early_stopping_rounds are all-or-nothing: pass all
        three to enable stopping once validation error hasn't improved
        for `early_stopping_rounds` consecutive boosting rounds (the
        ensemble is trimmed back to its best-known-round length, not
        just halted where it happens to be). Omit all three for the
        original fixed-n_estimators behaviour, unchanged.
        """
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        self.init_value = (
            float(np.percentile(y, self.quantile * 100))
            if self.quantile is not None
            else float(np.mean(y))
        )
        pred = np.full(y.shape, self.init_value)
        self.trees = []

        use_early_stopping = (
            early_stopping_rounds is not None
            and x_val is not None
            and y_val is not None
        )
        if use_early_stopping:
            x_val_arr = np.asarray(x_val, dtype=np.float64)
            y_val_arr = np.asarray(y_val, dtype=np.float64)
            val_pred = np.full(y_val_arr.shape, self.init_value)
            best_val_error = self._error(y_val_arr, val_pred)
            best_round = 0
            rounds_since_improvement = 0

        for round_idx in range(self.n_estimators):
            residuals = y - pred
            tree = _build_tree(
                x,
                residuals,
                self.max_depth,
                self.min_samples_leaf,
                quantile=self.quantile,
            )
            pred = pred + self.learning_rate * _predict_tree(tree, x)
            self.trees.append(tree)

            if use_early_stopping:
                val_pred = val_pred + self.learning_rate * _predict_tree(
                    tree, x_val_arr
                )
                val_error = self._error(y_val_arr, val_pred)
                if val_error < best_val_error - 1e-9:
                    best_val_error = val_error
                    best_round = round_idx + 1
                    rounds_since_improvement = 0
                else:
                    rounds_since_improvement += 1
                    if rounds_since_improvement >= early_stopping_rounds:
                        self.trees = self.trees[:best_round]
                        break
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        pred = np.full(x.shape[0], self.init_value)
        for tree in self.trees:
            pred = pred + self.learning_rate * _predict_tree(tree, x)
        return pred
