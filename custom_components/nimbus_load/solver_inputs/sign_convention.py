"""Check a declared battery power-sign convention against recorded history.

nimbus issue #1241, split out of #1131 which deferred exactly this:

    **Detect it.** The sign is checkable against recorded history: over a
    window where SoC rose, the net power sign is known. That is real work and
    wants its own issue; noting it only because #388 established the principle
    for the home battery and the same evidence exists for a participant.

## The defect this detects

`solver_battery_power_positive_is_charge` (home battery) and
`battery_participant_power_positive_is_charge` (participant) both ask the
installer to *declare* a sign convention for a power sensor. Getting either
wrong does not error — it inverts the charge/discharge split in the scorer's
own history reconstruction. That is the #535 / #843 class: a convention error
producing plausible-looking numbers rather than a failure.

Worse, the resulting energy imbalance trips #1073 / #1098's guards, so the
symptom presents as one of the *known confounds* rather than as a
configuration error. Both of those cost real investigations.

The two defaults are also opposite (#1131), so a household that accepts what
both forms offer gets one convention for its home battery and the other for
its EV.

## Why it is checkable at all

The declaration is **redundant with recorded history**, and that redundancy is
the test. Over any window where the pack's SoC rose, energy went in; where it
fell, energy came out. So the sign of the mean power across that same window
is determined, not assumed.

Both series are already required and already fetched by the scorer, so this
needs no new configuration — only a comparison of two things the code has.

## Deliberately conservative, in four specific ways

1. **Only windows where SoC actually moved** beyond a noise threshold count. A
   flat pack contributes nothing, and a pack sitting at 100% in absorption can
   draw real power with no SoC movement at all.
2. **A clear majority must agree** before concluding anything. One window can
   be confounded by a recorder gap (#1161) or a wake transient (#843).
3. **It reports. It never corrects.** Silently flipping a household's declared
   convention because a heuristic disagreed is the kind of "fix" that becomes
   the next investigation. The precedent is `nuc_state_reconcile.py`'s
   `--mode reconcile` deliberately behaving identically to `--mode audit`.
4. **`INSUFFICIENT_EVIDENCE` is a real answer**, returned whenever the windows
   are too few or too mixed — never silently treated as agreement.

## Distinguishing a wrong sign from a measurement-plane offset

#1231 is a genuine ~5-8% offset in both directions, and it also shows up as an
energy-balance discrepancy. The two are distinguishable and the issue asked for
that distinction explicitly, because reporting only "these disagree" would have
lengthened #1231 rather than shortened it:

* a **wrong sign** inverts the split — charge and discharge swap;
* a **plane offset** scales both in the same direction.

So a detector that finds the SoC direction and the power sign disagreeing in
*every* usable window is seeing a sign error; one that finds them agreeing
while magnitudes are off is seeing something else, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import pairwise

__all__ = [
    "SignConventionResult",
    "SignVerdict",
    "detect_power_sign_convention",
]


class SignVerdict(str, Enum):
    """What the recorded history says about the declared convention."""

    AGREES = "agrees"
    DISAGREES = "disagrees"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class SignConventionResult:
    """The verdict plus the evidence behind it.

    The counts are part of the result on purpose: a household told "your sign
    looks wrong" deserves to see how many windows said so, and a 9-of-10 is a
    different claim from a 3-of-4.
    """

    verdict: SignVerdict
    windows_examined: int = 0
    windows_agreeing: int = 0
    windows_disagreeing: int = 0
    reason: str | None = None
    #: Mean |power| across usable windows, for the caller that wants to tell a
    #: sign error apart from a magnitude one (#1231). Never used to decide the
    #: verdict itself.
    mean_abs_power_kw: float | None = None
    examined_window_starts: tuple[datetime, ...] = field(default_factory=tuple)

    @property
    def is_actionable(self) -> bool:
        """True only for a confident DISAGREES. Callers should not warn on
        INSUFFICIENT_EVIDENCE -- "we could not tell" is not a finding."""
        return self.verdict is SignVerdict.DISAGREES


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def detect_power_sign_convention(
    soc_series: list[tuple[datetime, float]],
    power_series: list[tuple[datetime, float]],
    *,
    declared_positive_is_charge: bool,
    min_soc_move_pct: float = 1.0,
    min_power_kw: float = 0.05,
    min_windows: int = 4,
    majority: float = 0.75,
) -> SignConventionResult:
    """Compare a declared sign convention against what the history implies.

    `soc_series` and `power_series` are `(timestamp, value)` pairs as the
    scorer already fetches them. SoC is a percentage; power is kW **as the
    sensor reports it**, before any sign flip -- the whole point is to judge
    the raw reading.

    A "window" is one consecutive pair of SoC samples. For each, the SoC
    direction is known; the mean of the power readings falling inside it is
    compared against it. Windows where SoC barely moved, or where power was
    essentially zero, are skipped rather than counted as evidence.

    `majority` is the fraction of usable windows that must agree before a
    verdict is returned. Below it, the answer is INSUFFICIENT_EVIDENCE -- a
    genuinely mixed result means something else is wrong (a gap, a transient,
    two packs summed) and is not a licence to guess.
    """
    if len(soc_series) < 2 or not power_series:
        return SignConventionResult(
            verdict=SignVerdict.INSUFFICIENT_EVIDENCE,
            reason="not enough recorded history to compare",
        )

    soc = sorted(soc_series, key=lambda p: p[0])
    power = sorted(power_series, key=lambda p: p[0])

    agreeing = 0
    disagreeing = 0
    starts: list[datetime] = []
    abs_powers: list[float] = []

    for (t0, soc0), (t1, soc1) in pairwise(soc):
        soc_delta = soc1 - soc0
        if abs(soc_delta) < min_soc_move_pct:
            continue  # guard 1: a flat pack is not evidence
        window = [v for ts, v in power if t0 <= ts < t1]
        if not window:
            continue
        mean_power = _mean(window)
        if abs(mean_power) < min_power_kw:
            continue  # near-zero net power says nothing about direction

        # What the RAW sensor sign would have to be, given the declaration,
        # for this window to be consistent: if positive means charge, then a
        # rising SoC implies positive power.
        soc_rose = soc_delta > 0
        power_positive = mean_power > 0
        implied_positive_is_charge = soc_rose == power_positive

        if implied_positive_is_charge == declared_positive_is_charge:
            agreeing += 1
        else:
            disagreeing += 1
        starts.append(t0)
        abs_powers.append(abs(mean_power))

    examined = agreeing + disagreeing
    if examined < min_windows:
        return SignConventionResult(
            verdict=SignVerdict.INSUFFICIENT_EVIDENCE,
            windows_examined=examined,
            windows_agreeing=agreeing,
            windows_disagreeing=disagreeing,
            reason=(
                f"only {examined} usable window(s) in this history, "
                f"{min_windows} needed -- a pack that barely moved, or a "
                f"recorder gap, is not evidence either way"
            ),
            mean_abs_power_kw=_mean(abs_powers) or None,
            examined_window_starts=tuple(starts),
        )

    if disagreeing / examined >= majority:
        return SignConventionResult(
            verdict=SignVerdict.DISAGREES,
            windows_examined=examined,
            windows_agreeing=agreeing,
            windows_disagreeing=disagreeing,
            reason=(
                f"{disagreeing} of {examined} windows where SoC moved have the "
                f"opposite power sign to the declared convention"
            ),
            mean_abs_power_kw=_mean(abs_powers) or None,
            examined_window_starts=tuple(starts),
        )

    if agreeing / examined >= majority:
        return SignConventionResult(
            verdict=SignVerdict.AGREES,
            windows_examined=examined,
            windows_agreeing=agreeing,
            windows_disagreeing=disagreeing,
            mean_abs_power_kw=_mean(abs_powers) or None,
            examined_window_starts=tuple(starts),
        )

    return SignConventionResult(
        verdict=SignVerdict.INSUFFICIENT_EVIDENCE,
        windows_examined=examined,
        windows_agreeing=agreeing,
        windows_disagreeing=disagreeing,
        reason=(
            f"history is genuinely mixed ({agreeing} agree, {disagreeing} "
            f"disagree) -- that is not a sign error, it is a sign that "
            f"something else is wrong with one of the two series"
        ),
        mean_abs_power_kw=_mean(abs_powers) or None,
        examined_window_starts=tuple(starts),
    )
