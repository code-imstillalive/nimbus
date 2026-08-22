"""Multi-source forecast blending -- the real, measurable form of "every
forecast is wrong" (Mark Purcell's own framing, relayed 2026-08-21):
combining several independent forecasts for the same physical quantity
reduces expected error, because each source's own mistakes are at least
partially uncorrelated -- this is the same reason ensemble weather
models consistently beat any single model, not just a nice intuition.

Deliberately pure numpy, zero Home Assistant dependencies, matching
every other module in this package (ml/gbrt.py, ml/model.py) -- these
functions are plain math, testable and reusable independent of how (or
whether, yet) any particular source gets wired into a live config flow.

Two distinct, complementary things live here, matching the two-stage
design already scoped (Nimbus Solver stochastic/blended forecasting
plan, 2026-08-21):

1. `blend_point_estimate()`/`blend_forecast_array()` -- combine several
   sources into ONE better point estimate, weighted by real measured
   accuracy where available.
2. `cross_source_spread()` -- the disagreement BETWEEN sources, which is
   itself a genuine, earned uncertainty signal (not an arbitrary knob)
   -- feeds the existing risk_aversion mechanism (see solver/network.py's
   own _risk_adjusted()) as a real confidence band widener, and later
   the scenario spread for genuine stochastic dispatch.

Weighting is bootstrap-honest: `weights_from_mae()` falls back to equal
weighting whenever a source's own real measured accuracy isn't known yet
(e.g. this project's own forecast-capture-and-compare mechanism,
audit item #9, had only a single real snapshot as of 2026-08-21 -- nowhere
near enough matured data to justify anything more precise than "trust
every configured source equally until proven otherwise"). This is a
real, deliberate design choice, not a placeholder to be embarrassed
about -- pretending day-one weights are evidence-based when they can't
be yet would be a worse mistake than being honestly equal.
"""

import numpy as np
from numpy.typing import NDArray


def weights_from_mae(maes: list[float | None]) -> list[float]:
    """Inverse-MAE weighting, normalized to sum to 1.0 -- a source with
    HALF the mean-absolute-error of another gets roughly TWICE the
    weight (`1/mae`, normalized), the standard, simple form of
    accuracy-weighted ensembling.

    Any entry that is `None` (this source's own real accuracy hasn't
    been measured yet -- see this module's own docstring) OR exactly
    `0.0` (a source claiming zero real-world error is not something to
    trust blindly, and `1/0` isn't a number anyway) is treated as
    "unknown", not "perfect" or "worthless" -- if EVERY entry is
    unknown, this returns equal weights (the honest bootstrap case). If
    only SOME entries are unknown, those get the same weight as the
    single least-accurate KNOWN source, rather than being silently
    dropped to zero (a totally untested source shouldn't be trusted
    MORE than the worst-measured one, but excluding it outright would
    make the "blend" secretly a "pick one" the moment any source lacks
    real accuracy data).
    """
    n = len(maes)
    if n == 0:
        msg = "weights_from_mae: need at least one source"
        raise ValueError(msg)
    known = [m for m in maes if m is not None and m > 0.0]
    if not known:
        return [1.0 / n] * n
    fallback_mae = max(known)  # least-accurate KNOWN source -- see docstring
    effective = [m if (m is not None and m > 0.0) else fallback_mae for m in maes]
    inv = [1.0 / m for m in effective]
    total = sum(inv)
    return [w / total for w in inv]


def blend_point_estimate(
    values: list[float], weights: list[float] | None = None
) -> float:
    """Weighted average of several sources' point estimates for the same
    quantity right now. `weights=None` (the default) is equal-weight --
    the honest bootstrap case, see this module's own docstring."""
    if not values:
        msg = "blend_point_estimate: need at least one value"
        raise ValueError(msg)
    if weights is None:
        weights = [1.0 / len(values)] * len(values)
    if len(weights) != len(values):
        msg = f"blend_point_estimate: {len(values)} values but {len(weights)} weights"
        raise ValueError(msg)
    return float(np.average(values, weights=weights))


def blend_forecast_array(
    arrays: list[NDArray[np.float64]], weights: list[float] | None = None
) -> NDArray[np.float64]:
    """Same as blend_point_estimate(), elementwise, for several sources'
    own full {time, value} forecast arrays that are already aligned to
    the same real timestamps (alignment/resampling across genuinely
    different sources' own native resolutions is a real, separate
    concern for whatever wires this into a live source -- deliberately
    not this function's job, it assumes aligned input, same convention
    as solver/network.py's own PeriodGrid-aligned arrays)."""
    if not arrays:
        msg = "blend_forecast_array: need at least one array"
        raise ValueError(msg)
    n = len(arrays[0])
    for i, arr in enumerate(arrays):
        if len(arr) != n:
            msg = f"blend_forecast_array: array {i} has {len(arr)} points, expected {n} (all sources must already be aligned)"
            raise ValueError(msg)
    if weights is None:
        weights = [1.0 / len(arrays)] * len(arrays)
    if len(weights) != len(arrays):
        msg = f"blend_forecast_array: {len(arrays)} arrays but {len(weights)} weights"
        raise ValueError(msg)
    stacked = np.stack(arrays, axis=0)  # shape (n_sources, n_periods)
    return np.average(stacked, axis=0, weights=weights)


def cross_source_spread(arrays: list[NDArray[np.float64]]) -> NDArray[np.float64]:
    """The real, earned uncertainty signal: how much do the configured
    sources actually disagree, per period, right now? A single source
    (len(arrays)==1) has no disagreement to measure -- returns all
    zeros, not an error, so callers don't need a special case for
    "blending is configured but only one source is actually live right
    now" (a real, expected transient state, not a config error).

    Deliberately max-min (the full spread), not a weighted standard
    deviation -- with as few as 2-3 sources (the realistic near-term
    case for this household), a "standard deviation" computed from 2-3
    points is a fragile, easily-misleading statistic; the plain range
    between the most optimistic and most pessimistic configured source
    is a more honest, more easily explained uncertainty measure at this
    small a sample size. Revisit if/when genuinely more sources (4+)
    are ever configured for the same quantity.
    """
    if not arrays:
        msg = "cross_source_spread: need at least one array"
        raise ValueError(msg)
    if len(arrays) == 1:
        return np.zeros_like(arrays[0])
    n = len(arrays[0])
    for i, arr in enumerate(arrays):
        if len(arr) != n:
            msg = f"cross_source_spread: array {i} has {len(arr)} points, expected {n} (all sources must already be aligned)"
            raise ValueError(msg)
    stacked = np.stack(arrays, axis=0)
    return np.max(stacked, axis=0) - np.min(stacked, axis=0)
