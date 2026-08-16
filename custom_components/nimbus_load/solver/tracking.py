"""Control tracking -- extends the EPR/regret framework's positive framing
from ECONOMIC value capture to EXECUTION fidelity: given a commanded
setpoint and the real, measured output, how faithfully did the physical
system actually deliver what it was told to do?

## Why this file exists (item #6 of Mark Purcell's original 9-item audit)

Regret/EPR (regret.py, epr.py) answer "did we compute the right economic
plan" -- they compare a DISPATCH TRAJECTORY (a committed battery
charge/discharge schedule) against a perfect-foresight alternative,
assuming the trajectory itself gets executed exactly as committed. That
assumption is not always true. A real Sungrow inverter is commanded a
setpoint via Modbus; what it actually delivers can genuinely diverge from
that setpoint for reasons that have nothing to do with whether the plan
itself was economically sound -- e.g. this project's own live, real
finding (116KAT-HA-AI's own CLAUDE.md, 2026-08-17 session): the household's
two physical inverters periodically trade off which one carries the
discharge duty, and the real measured battery power genuinely drops to
near-zero for ~20-30s during that handoff, roughly every 45-90 minutes,
even while the commanded setpoint stays perfectly steady. Regret/EPR
would never see this at all -- they only ever compare committed
trajectories to each other, never a commanded trajectory to its own real
execution. Tracking fidelity is a genuinely different, complementary
question: not "was the plan right" but "did reality actually do what the
plan asked."

## Tracking Fidelity, TF, stated explicitly with units (same discipline
as regret.py's own J -- state the formula, state the units, state what
it cannot tell you)

    gap(t)  = commanded_kw(t) - actual_kw(t)                [kW, signed:
              positive = under-delivered, negative = over-delivered]
    TF = 1 - ( sum_t |gap(t)| * dt(t) ) / ( sum_t |commanded_kw(t)| * dt(t) )

dt(t) in hours, matching regret.py's own `hours` convention exactly (the
per-period duration array, not a fixed step). TF is bounded (-inf, 1] in
the general case, and behaves like EPR in the well-tracked regime: 1.0 is
perfect execution, 0.0 means the average tracking error is exactly as
large as the average commanded magnitude itself (i.e. no better than not
tracking at all), and it can go negative for a genuinely pathological
controller whose real output is worse than doing nothing. Zero commanded
activity (a flat, all-zero setpoint over the whole window) returns TF=1.0
by definition -- there's nothing to fail to track, same zero-denominator
handling as epr.py's own compute_epr().

**What TF deliberately cannot tell you (stated plainly, same discipline as
regret.py's own list of omitted terms):**
- WHY tracking failed. A low TF says execution diverged from command; it
  says nothing about mechanism (inverter handoff, communication glitch,
  a genuine hardware fault, or a different real cause entirely). Root
  cause always needs separate, real investigation -- as the 2026-08-17
  session that motivated this file did, at length, before writing
  anything down as fact.
- The ECONOMIC cost of the tracking error. TF is pure kW, price-free, by
  design -- comparable across a load, a battery, or a grid signal
  regardless of what any of them are worth per kWh. `tracking_error_cost()`
  below is the separate, explicit bridge to a real dollar figure, for
  when that's specifically wanted -- kept as an optional second step, not
  folded into TF itself, the same separation of concerns this package
  already keeps between a plan's own kW trajectory and its $ evaluation.
- Whether a real, present gap is even worth fixing. The 2026-08-17
  session's own real numbers (roughly $0.30-0.85/night from the inverter
  handoff pattern, against a real ~$20-30/night in total P2P revenue) are
  a genuine, small, honestly-computed magnitude, not a report that
  something is "broken" -- reporting a low TF for a known, small,
  already-characterized gap is doing its job correctly, not raising a
  false alarm.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class TrackingResult:
    """One commanded-vs-actual comparison over a real time window. All kW
    fields share whatever sign convention the caller's own commanded_kw/
    actual_kw arrays use (this module makes no assumption about charge
    vs discharge sign -- see compute_tracking_fidelity()'s own docstring).
    """

    tracking_fidelity: float
    mean_absolute_error_kw: float
    energy_shortfall_kwh: float
    worst_gap_kw: float
    worst_gap_index: int
    n_samples: int


def compute_tracking_fidelity(
    *,
    hours: NDArray[np.float64],
    commanded_kw: NDArray[np.float64],
    actual_kw: NDArray[np.float64],
) -> TrackingResult:
    """TF as defined in this module's own docstring, plus three
    supporting real-unit figures for context (a bare ratio alone doesn't
    tell you whether the underlying magnitude is 5W or 5kW):

    - mean_absolute_error_kw: plain average |gap|, kW -- the "how big is
      a typical miss" figure a ratio alone can't convey.
    - energy_shortfall_kwh: real energy NOT delivered relative to what was
      commanded, kWh -- only counts UNDER-delivery (gap > 0), since an
      under-delivery is the one direction with a direct, obvious economic
      reading (less exported/discharged than committed); an occasional
      over-delivery is not symmetrically "bad" in the same sense and is
      deliberately excluded here rather than netted against shortfalls,
      which would understate a real, one-directional problem.
    - worst_gap_kw / worst_gap_index: the single largest instantaneous
      deviation and where it sits in the input arrays, so a caller can
      go straight to that timestamp for a real root-cause look (exactly
      the kind of "check the actual values right when the biggest dip
      happened" step the 2026-08-17 investigation did by hand, made
      automatic here).

    hours/commanded_kw/actual_kw must be the same length and already
    time-aligned by the caller -- this module does no resampling or
    interpolation of its own, same as regret.py's own evaluate_realized_
    cost(). Sign convention is caller's choice (e.g. positive=discharge)
    and must be consistent between commanded_kw and actual_kw for `gap`
    to mean anything; this function does not itself enforce or infer one.
    """
    n = len(hours)
    if n == 0:
        return TrackingResult(
            tracking_fidelity=1.0,
            mean_absolute_error_kw=0.0,
            energy_shortfall_kwh=0.0,
            worst_gap_kw=0.0,
            worst_gap_index=-1,
            n_samples=0,
        )

    gap = commanded_kw - actual_kw
    abs_gap = np.abs(gap)

    commanded_activity = float(np.sum(np.abs(commanded_kw) * hours))
    gap_energy = float(np.sum(abs_gap * hours))
    tracking_fidelity = 1.0 if commanded_activity < 1e-9 else 1.0 - gap_energy / commanded_activity

    shortfall_kwh = float(np.sum(np.maximum(0.0, gap) * hours))
    worst_idx = int(np.argmax(abs_gap))

    return TrackingResult(
        tracking_fidelity=tracking_fidelity,
        mean_absolute_error_kw=float(np.mean(abs_gap)),
        energy_shortfall_kwh=shortfall_kwh,
        worst_gap_kw=float(abs_gap[worst_idx]),
        worst_gap_index=worst_idx,
        n_samples=n,
    )


def tracking_error_cost(
    *,
    hours: NDArray[np.float64],
    commanded_kw: NDArray[np.float64],
    actual_kw: NDArray[np.float64],
    export_price: NDArray[np.float64],
) -> float:
    """The explicit, separate $ bridge from a real tracking gap to its
    real economic cost -- kept out of compute_tracking_fidelity() itself
    per this module's own docstring (TF stays pure kW, comparable across
    any signal regardless of what it's worth). Prices ONLY the shortfall
    direction (under-delivery -> foregone export/discharge revenue,
    export_price in $/kWh) -- same one-directional reasoning as
    energy_shortfall_kwh above, and the same real-world question the
    2026-08-17 session answered by hand (~3-5c per handoff event) before
    this function existed to do it directly from real arrays.

    export_price must be the same length as hours/commanded_kw/actual_kw,
    one value per period -- a real, time-varying rate (e.g. the flat
    $0.50/kWh P2P placeholder during the window, $0 outside it), not a
    single scalar assumption baked into this function.
    """
    gap = commanded_kw - actual_kw
    shortfall_kw = np.maximum(0.0, gap)
    return float(np.sum(shortfall_kw * export_price * hours))
