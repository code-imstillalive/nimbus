"""Act on the measured verdict: when persistence is beating the ML load
forecaster, use persistence.

nimbus issue #937, item 4 -- the last of that issue's own four "what would
move this forward" actions, and the only one still unaddressed:

    **Consider whether persistence deserves to be a real fallback.** If a
    household's own data shows persistence winning consistently, the honest
    product answer might be to use it, or to blend. That is a bigger
    conversation and not a proposal yet.

The measurement it rests on is the issue's own headline, on the reference
household's real data: **naive persistence beat the ML load forecaster on 11
of 14 scored days, mean -$0.71/day**, worst day -$3.42. Sustained, roughly
-$260/year against doing nothing smarter than "tomorrow looks like today."

Every other item on that list is closed or is waiting on time rather than on
code. Item 1 ("more days") needed the day-ahead decomposition published on the
native path, which stages 1-3 shipped (#1287/#1288/#1307). Item 2 (level vs
shape) is `forecast_regret.py`'s own three-way split. Item 3 (does the model's
own validation agree) was measured to death in that thread: selection validates
recursive error at 4 hours and the forecast is used out to 48, the winner
demonstrably flips to naive on at least one real circuit, and on half the
reference household's circuits no recursive metric exists at all so selection
falls back to single-step error.

**What none of that does is change what the solver consumes.** Fourteen
measured days said the forecaster was costing money and the forecaster kept
being used, because nothing connected the measurement to the decision. This
module is that connection.

## Why this is a pure module with no default action

It decides nothing on its own and it changes nothing by default. The policy
argument's default is `POLICY_OFF`, and with `POLICY_OFF` every input is
ignored and the answer is always "use the ML forecast, weight 0.0" -- so an
install that never touches the new entity is byte-identical to today. That is
not timidity: the reference household runs real battery dispatch on this code
path, and the evidence for acting is 14 days from one install, which the issue
itself flags as "a small sample... One household. The forecaster trains
per-install, so this may not generalise at all."

The four policies exist because the issue names two different product answers
("use it, or to blend") and because there is an honest third position between
measuring and acting:

- `off`         -- the default. No effect, whatever the data says.
- `measure`     -- compute and publish the recommendation, keep using the ML
                   forecast. The state this issue has effectively been in for
                   two weeks, except now it is deliberate and visible.
- `blend`       -- weight persistence by how often it actually won.
- `persistence` -- when the trailing record says persistence wins, use
                   persistence outright.

## The blend weight is the evidence, not a tuning knob

`blend` sets the persistence weight to the **fraction of trailing scored days
persistence won**. On the issue's own 11-of-14 that is 0.786. It is deliberately
not a configurable dial: a dial invites picking the number that produces a
liked answer, and there is no measurement behind any particular value. The
fraction of days won is a quantity the data already states, it is bounded in
[0, 1] by construction, and it can only reach 1.0 if persistence won every
single day in the window.

## What this module deliberately does NOT do

- **Does not read anything.** The trailing record comes in as a plain mapping
  of ISO date to that day's `nimbus_value_add_dollars`, exactly as the quality
  report's own history rows carry it. No Home Assistant imports, no recorder,
  no I/O -- the same contract `forecast_regret.py` and `forecast_snapshot.py`
  hold, so the tests load this by path and prove it rather than asserting it.
- **Does not decide what "persistence" means.** The caller supplies the
  persistence array, and the caller is the one place that already knows which
  entity counts as the real household load (`solver_whole_house_cross_check_
  sensor`, the same sensor the day-ahead scorer reads).
- **Does not touch the solar forecast.** #937 is about the load forecaster.
  `forecast_regret.py`'s own `solar_error_dollars` exists precisely because
  nobody has checked whether solar is the dominant term, and acting on solar
  before that is answered would be acting on an assumption.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

__all__ = [
    "DEFAULT_LOSS_THRESHOLD_DOLLARS",
    "DEFAULT_MIN_SCORED_DAYS",
    "DEFAULT_TRAILING_DAYS",
    "POLICIES",
    "POLICY_BLEND",
    "POLICY_MEASURE",
    "POLICY_OFF",
    "POLICY_PERSISTENCE",
    "SOURCE_BLEND",
    "SOURCE_ML",
    "SOURCE_PERSISTENCE",
    "ForecastSourceDecision",
    "blend_load_forecast",
    "select_forecast_source",
]

POLICY_OFF = "off"
POLICY_MEASURE = "measure"
POLICY_BLEND = "blend"
POLICY_PERSISTENCE = "persistence"

#: Declared in the order they escalate, so a reader can see that `off` is the
#: floor and `persistence` the ceiling. The select entity offers exactly these.
POLICIES: tuple[str, ...] = (
    POLICY_OFF,
    POLICY_MEASURE,
    POLICY_BLEND,
    POLICY_PERSISTENCE,
)

SOURCE_ML = "ml"
SOURCE_BLEND = "blend"
SOURCE_PERSISTENCE = "persistence"

#: The window #937's own figure was computed over. Not a coincidence and not a
#: round number picked for neatness -- it is the sample size the issue reports,
#: so a household reading "persistence won 11 of 14" on their own dashboard is
#: reading the same shape of statement the issue makes.
DEFAULT_TRAILING_DAYS = 14

#: How many scored days must actually be present in that window before any
#: policy other than `off`/`measure` is allowed to act.
#:
#: Seven, i.e. half the window, and the reason is the one caveat #937 states
#: about its own data rather than an arbitrary floor: the day-ahead
#: decomposition is only produced on days that had a forecast snapshot and
#: usable previous-day history, so rows are legitimately sparse after a
#: restart or a recorder gap. A mean over two rows is not a verdict about a
#: forecaster, it is a verdict about two days, and the issue's own worst day
#: (-$3.42) is nearly five times its mean -- a thin sample can be dominated
#: by one outlier in either direction.
DEFAULT_MIN_SCORED_DAYS = 7

#: How much mean daily loss the record has to show before persistence is used.
#:
#: Ten cents a day. Chosen against the measured spread rather than as a round
#: number: #937's own per-day series has three days inside +-$0.10
#: (09-05 -0.0709, and the nowcast comparison's 09-15 -0.0358 / 09-16 +0.0680),
#: and the day-ahead losses it is meant to catch are an order of magnitude
#: larger -- -$0.71 mean, -$3.42 worst. So this screens out a forecaster that
#: is merely indistinguishable from persistence, which is not a reason to
#: change what a live household dispatches on, while leaving a factor of seven
#: of headroom to the signal that motivated the issue.
DEFAULT_LOSS_THRESHOLD_DOLLARS = 0.10

# Reason codes. Every decision carries one, including every "no change"
# decision -- a diagnostic that goes quiet without saying which gate closed is
# the defect `epr_reason` (#1162) and `forecast_regret_reason` (#1307) both
# exist to fix, and this module has five distinct ways of answering "ml".
REASON_POLICY_OFF = "policy_off"
REASON_POLICY_UNRECOGNISED = "policy_unrecognised"
REASON_NO_TRAILING_RECORD = "no_trailing_record"
REASON_INSUFFICIENT_SCORED_DAYS = "insufficient_scored_days"
REASON_FORECAST_NOT_LOSING = "forecast_not_losing"
REASON_PERSISTENCE_FAVOURED_MEASURE_ONLY = "persistence_favoured_measure_only"
REASON_PERSISTENCE_FAVOURED_BLEND = "persistence_favoured_blend"
REASON_PERSISTENCE_FAVOURED_FULL = "persistence_favoured_full"


@dataclass(frozen=True)
class ForecastSourceDecision:
    """What to feed the LP for load, and the evidence behind it.

    Every field is published, including on the `off` path. A household whose
    policy is `measure` gets the full recommendation with
    `persistence_weight == 0.0` -- which is the whole point of that policy,
    and is only readable because the evidence travels with the decision
    rather than being recomputed by whoever wants to see it.
    """

    source: str
    """`ml`, `blend` or `persistence` -- derived from `persistence_weight`,
    never set independently, so the label and the number cannot disagree."""

    persistence_weight: float
    """0.0 uses the ML forecast unchanged; 1.0 uses persistence outright.
    **Always exactly 0.0 under `POLICY_OFF` and `POLICY_MEASURE`**, whatever
    the record says -- that invariant is what makes this safe to ship into a
    live dispatch path, and it is pinned by its own test."""

    policy: str
    """The policy that was asked for, echoed back. A caller that passed an
    unrecognised value sees it here alongside `REASON_POLICY_UNRECOGNISED`
    rather than having it silently normalised to `off`."""

    reason: str
    """Which gate decided this. Never None."""

    days_scored: int
    """Scored days found inside the trailing window."""

    days_persistence_won: int
    """Of those, how many had `nimbus_value_add_dollars < 0`. Exactly the
    count #937's headline reports ("naive persistence beat Nimbus's forecast:
    11")."""

    mean_value_add_dollars: float | None
    """Mean `nimbus_value_add_dollars` over the window, or None when the
    window held no scored day. Negative means persistence was cheaper."""


def _window_values(
    value_add_by_day: Mapping[str, float],
    *,
    today: date,
    trailing_days: int,
) -> list[float]:
    """The scored values inside the trailing calendar window.

    **A calendar window, not "the most recent N rows"**, and the distinction
    matters. Rows are legitimately sparse -- the day-ahead decomposition is
    written only on days that had a snapshot and usable previous-day history --
    so "most recent 14 rows" can silently reach back two months and present a
    stale verdict as a current one. A calendar window cannot: it thins the
    sample instead, and `days_scored` then says so out loud and the
    `DEFAULT_MIN_SCORED_DAYS` gate refuses to act on it.

    Malformed keys and non-finite values are skipped rather than raising. This
    mapping comes off a published attribute that any program can have written
    (the standalone cron writer does, and did for the era #937's own figures
    come from), so one bad row must not cost the whole decision -- the same
    "defensive about shape rather than trusting it" posture the quality
    report's own history rebuild takes.
    """
    if trailing_days <= 0:
        return []
    values: list[float] = []
    for key, raw in value_add_by_day.items():
        if not isinstance(key, str):
            continue
        try:
            day = date.fromisoformat(key)
        except (TypeError, ValueError):
            continue
        # Scored days are elapsed days, so `today` itself is included only if
        # something has already written a row for it; anything in the future
        # is a clock or timezone problem and is not evidence.
        if day > today:
            continue
        if (today - day).days >= trailing_days:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        values.append(value)
    return values


def select_forecast_source(
    *,
    value_add_by_day: Mapping[str, float],
    today: date,
    policy: str = POLICY_OFF,
    trailing_days: int = DEFAULT_TRAILING_DAYS,
    min_scored_days: int = DEFAULT_MIN_SCORED_DAYS,
    loss_threshold_dollars: float = DEFAULT_LOSS_THRESHOLD_DOLLARS,
) -> ForecastSourceDecision:
    """Decide what the LP's load input should be built from.

    `value_add_by_day` maps an ISO local date to that day's
    `nimbus_value_add_dollars` (`j_persistence - j_forecast`), so **negative
    means persistence was cheaper** -- the sign convention
    `forecast_regret.py` publishes and #937 reports.

    The gates, in order, each of which short-circuits to the ML forecast:

    1. policy `off` (the default) or unrecognised -- no evidence is even read.
    2. no scored day in the trailing window.
    3. fewer than `min_scored_days` in it.
    4. mean value-add at or above `-loss_threshold_dollars`, i.e. the
       forecaster is winning or is indistinguishable from persistence.

    Only past all four does the policy get to act, and `measure` still does
    not: it returns weight 0.0 with its own reason, so the recommendation is
    published without changing a single dispatch.
    """
    if policy == POLICY_OFF:
        # Deliberately BEFORE reading the record. Not an optimisation -- it is
        # the statement that the default configuration's answer does not
        # depend on the data at all, which is what makes "off is byte-
        # identical to today" a property rather than a hope.
        return ForecastSourceDecision(
            source=SOURCE_ML,
            persistence_weight=0.0,
            policy=policy,
            reason=REASON_POLICY_OFF,
            days_scored=0,
            days_persistence_won=0,
            mean_value_add_dollars=None,
        )
    if policy not in POLICIES:
        return ForecastSourceDecision(
            source=SOURCE_ML,
            persistence_weight=0.0,
            policy=policy,
            reason=REASON_POLICY_UNRECOGNISED,
            days_scored=0,
            days_persistence_won=0,
            mean_value_add_dollars=None,
        )

    values = _window_values(value_add_by_day, today=today, trailing_days=trailing_days)
    days_scored = len(values)
    days_won = sum(1 for v in values if v < 0.0)
    mean = (sum(values) / days_scored) if days_scored else None

    def _no_change(reason: str) -> ForecastSourceDecision:
        return ForecastSourceDecision(
            source=SOURCE_ML,
            persistence_weight=0.0,
            policy=policy,
            reason=reason,
            days_scored=days_scored,
            days_persistence_won=days_won,
            mean_value_add_dollars=mean,
        )

    if days_scored == 0:
        return _no_change(REASON_NO_TRAILING_RECORD)
    if days_scored < min_scored_days:
        return _no_change(REASON_INSUFFICIENT_SCORED_DAYS)
    assert mean is not None  # days_scored > 0
    if mean >= -abs(loss_threshold_dollars):
        return _no_change(REASON_FORECAST_NOT_LOSING)
    if policy == POLICY_MEASURE:
        return _no_change(REASON_PERSISTENCE_FAVOURED_MEASURE_ONLY)

    if policy == POLICY_BLEND:
        # The evidence, not a dial -- see this module's own docstring.
        weight = days_won / days_scored
        reason = REASON_PERSISTENCE_FAVOURED_BLEND
    else:  # POLICY_PERSISTENCE
        weight = 1.0
        reason = REASON_PERSISTENCE_FAVOURED_FULL

    if weight <= 0.0:
        # Unreachable on the arithmetic above (a negative mean requires at
        # least one negative day, so `days_won >= 1`), kept because the label
        # must be derived from the weight rather than from the branch: if a
        # future policy ever produces 0.0 here, it has to read as `ml`.
        return _no_change(reason)
    return ForecastSourceDecision(
        source=SOURCE_PERSISTENCE if weight >= 1.0 else SOURCE_BLEND,
        persistence_weight=weight,
        policy=policy,
        reason=reason,
        days_scored=days_scored,
        days_persistence_won=days_won,
        mean_value_add_dollars=mean,
    )


def blend_load_forecast(
    ml_kw: Sequence[float],
    persistence_kw: Sequence[float],
    weight: float,
) -> list[float]:
    """`(1 - weight) * ml + weight * persistence`, elementwise.

    Returns a plain list so the caller's own in-place period-0 live anchor
    (`load_kw[0] = live_load_kw`) still works on the result -- that anchor is
    a measured value, not a forecast, and must survive whatever this returns.

    A `weight` of exactly 0.0 returns the ML values unchanged, including their
    own float identity, so the no-change path introduces no arithmetic at all.

    Raises `ValueError` on a length mismatch rather than truncating to the
    shorter array. A silently short load array is the shape that costs real
    money here: the periods past the end would be whatever the LP's own
    padding does, on an input the household believes is a forecast.
    """
    if not 0.0 <= weight <= 1.0:
        msg = f"persistence weight must be in [0, 1], got {weight!r}"
        raise ValueError(msg)
    if weight == 0.0:
        return list(ml_kw)
    if len(ml_kw) != len(persistence_kw):
        msg = (
            "load forecast blend needs equal-length arrays, got "
            f"{len(ml_kw)} ML and {len(persistence_kw)} persistence periods"
        )
        raise ValueError(msg)
    if weight == 1.0:
        return list(persistence_kw)
    return [
        (1.0 - weight) * float(m) + weight * float(p)
        for m, p in zip(ml_kw, persistence_kw, strict=True)
    ]
