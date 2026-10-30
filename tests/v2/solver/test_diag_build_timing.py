"""TEMPORARY diag: time HiGHS model build, row by row vs batched. Not for merge."""

import time

import highspy
import numpy as np

from custom_components.nimbus_load.solver import lp


def _build(n_cols, n_rows, batched):
    rng = np.random.default_rng(0)
    rows = [
        (rng.choice(n_cols, size=4, replace=False), rng.normal(size=4))
        for _ in range(n_rows)
    ]
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    t0 = time.monotonic()
    for _ in range(n_cols):
        h.addVariable(lb=0.0, ub=10.0)
    t1 = time.monotonic()
    if batched:
        starts = np.arange(0, 4 * n_rows, 4, dtype=np.int32)
        idx = np.concatenate([r[0] for r in rows]).astype(np.int32)
        val = np.concatenate([r[1] for r in rows])
        h.addRows(
            n_rows,
            np.full(n_rows, -highspy.kHighsInf),
            np.ones(n_rows),
            len(idx),
            starts,
            idx,
            val,
        )
    else:
        for ix, c in rows:
            lp._add_row(h, zip(ix.tolist(), c.tolist()), -highspy.kHighsInf, 1.0)
    t2 = time.monotonic()
    return t1 - t0, t2 - t1


def test_diag_build_timing():
    out = []
    for n_cols, n_rows in ((4000, 5000), (8000, 10000), (16000, 20000)):
        for batched in (False, True):
            v, r = _build(n_cols, n_rows, batched)
            out.append(
                f"cols={n_cols} rows={n_rows} batched={batched}: vars {v:.2f}s rows {r:.2f}s"
            )
    raise AssertionError("\n".join(out))
