"""The retrospective, HISTORY-based sibling of `extra_batteries.build_
extra_batteries()`: reconstructs each `battery_participant` subentry's own
REAL, already-elapsed dispatch from recorder history, so `compute_quality_
report()` can score the whole real storage fleet (home + EVs), not just
the hub's own single pack.

nimbus issue #1300, Phase 1 of #1298's decomposition of `solver_writer.py`'s
god-module -- the second half of Phase 1, moved alongside `extra_batteries.py`
since the two are the live-solve/history-reconstruction pair for the same
`battery_participant` subentries and share two reference constants.

`_participant_departure_deadline()` and `_widen_shared_charger_cap_to_
achieved()` move here as private helpers -- both are called from exactly
one place, `_resolve_battery_participant_history()` below, confirmed by
grep before moving (nimbus issue #603's own "measure, don't assume"
discipline). Likewise `_drop_implausible_power_samples()`, `_warn_sign_
convention_once()`, and `_warn_participant_history_is_ungated_once()`,
each with a single real caller in this file -- and their own warned-state
sets move with them, same reasoning `solver_inputs/battery_soc.py`'s own
module docstring already gives for `_HOME_BATTERY_SOC_EXCURSION_WARNED`.

`_stale_power_period_indices()` deliberately does NOT move here despite
being called from this file's own `_resolve_battery_participant_history()`
-- it has a second, real caller inside `_compute_report_for_window()`'s
own "home" battery reconstruction (Phase 2 of #1298, not yet moved), so it
stays a shared `solver_writer` utility, reached via `sw.` like every other
shared helper this module needs.

See `solver_inputs/__init__.py` for why the `solver_writer` import is
deferred and by-module; the reasoning is identical and load-bearing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np

try:
    from ..solver import elements
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import elements  # type: ignore[no-redef]

from .extra_batteries import (
    _DEFAULT_EXTRA_BATTERY_CHARGE_COST,
    _DEFAULT_EXTRA_BATTERY_DISCHARGE_COST,
)

# nimbus issue #1241: participants whose declared power-sign convention the
# recorded history contradicts. Keyed by participant name so a household with
# two EVs is told about each, and warned ONCE rather than every solve -- the
# same posture as _SOLAR_SOURCE_WARNED in solver_writer.py.
_SIGN_CONVENTION_WARNED: set[str] = set()

# nimbus issue #1247 (Mark Purcell's 26 Sep report). Participants scored with
# NO availability gate, warned once each -- same posture as the sign-convention
# set above.
_UNGATED_PARTICIPANT_WARNED: set[str] = set()

# nimbus issue #843 (Mark Purcell, option B of his own A/B/C steer,
# 2026-09-13): how far past a participant's OWN configured
# charge/discharge envelope a reconstructed sample may sit before it is
# treated as corrupt rather than real.
#
# Why 10x and not something tighter: real sensors legitimately overshoot
# a nameplate rating, and the configured figure is a household-entered
# number that may itself be conservative -- someone who types 5.0 for a
# 25 kW charger would have every genuine reading discarded by a tight
# bound, which is its own (silent, worse) bug. 10x leaves room for both.
#
# Why 10x is still tight enough to be useful: the real failure this
# guards against is a W-vs-kW mismatch, which is exactly 1000x. Mark's
# own confirmed incident was a 25 kW-configured EV reporting a single
# 1514.417 sample -- ~60x its envelope -- so a 10x bound catches it with
# two orders of magnitude to spare while never coming near plausible
# hardware overshoot. The gap between "conservative config" and "unit
# error" is three orders of magnitude wide; this sits in the middle of
# it deliberately.
_PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE: float = 10.0


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


def _warn_participant_history_is_ungated_once(name: str) -> None:
    """Say so when a participant's history is scored with no away-gate.

    #768 established the mechanism: a pack-power sensor also measures real
    PROPULSION discharge while driving -- energy that never touches the home's
    grid connection. `_resolve_battery_participant_history()` gates that out
    using `battery_participant_available_entity`, read as history for the
    scored day.

    But the gate is `if available_entity:`. A participant with none configured
    is scored UNGATED, silently: a trip's worth of discharge is priced as if
    it flowed through the grid, j_ach and EPR absorb it, and the oracle is
    compared against a discharge that was really a car leaving. Regret then
    concentrates at departure hours -- which is exactly the 11:00-13:00 shape
    #1247 reports and could not explain from its own data.

    That is the #535/#843 class: a plausible number rather than a failure.
    Nothing distinguishes "the gate ran and found the car home" from "there
    was no gate", which is the whole problem.

    REPORTS, NEVER CORRECTS -- the same line #1241 draws. There is no safe
    inference to make here: without the entity there is genuinely no evidence
    of when the car was away, and guessing from a power trace would invent the
    very fact the gate exists to supply.

    Only fires for a participant that actually has a power sensor to
    misattribute. Once per participant, at WARNING so it is visible without
    debug logging.
    """
    sw = _solver_writer()
    if name in _UNGATED_PARTICIPANT_WARNED:
        return
    _UNGATED_PARTICIPANT_WARNED.add(name)
    sw._LOGGER.warning(
        "Nimbus: battery participant '%s' has no "
        "battery_participant_available_entity configured, so its scored "
        "history is NOT gated for periods it was away. If this participant "
        "is an EV, energy used driving is measured by its pack-power sensor "
        "and will be priced as if it flowed through your grid connection -- "
        "inflating regret at departure hours and distorting EPR. Nothing has "
        "been changed. Set that entity on the participant to fix it "
        "(nimbus issues #768 / #1247). Logged once per participant.",
        name,
    )


def _warn_sign_convention_once(name: str, result: object) -> None:
    """Report a declared sign convention the history disagrees with (#1241).

    REPORTS, NEVER CORRECTS. The issue is explicit about why: "silently
    flipping a household's declared convention because a heuristic disagreed
    with it is the kind of 'fix' that becomes the next investigation." The
    precedent is nuc_state_reconcile.py's --mode reconcile deliberately
    behaving identically to --mode audit.

    Only a confident DISAGREES reaches here; the detector's own
    INSUFFICIENT_EVIDENCE is not a finding and must not produce a warning.
    """
    sw = _solver_writer()
    if name in _SIGN_CONVENTION_WARNED:
        return
    _SIGN_CONVENTION_WARNED.add(name)
    sw._LOGGER.warning(
        "Nimbus: battery participant '%s' declares a power-sign convention "
        "its own recorded history contradicts -- %s of %s windows where SoC "
        "actually moved show the opposite sign. Nothing has been changed: "
        "check this participant's 'power sensor positive means charge' "
        "setting, because a wrong sign inverts the charge/discharge split in "
        "scoring and shows up as an energy-balance discrepancy rather than as "
        "an error (nimbus issue #1241). Logged once per participant.",
        name,
        getattr(result, "windows_disagreeing", "?"),
        getattr(result, "windows_examined", "?"),
    )


def _drop_implausible_power_samples(
    power_hist: list[tuple[datetime, float]],
    *,
    power_scale: float,
    max_plausible_kw: float,
    participant_name: str,
    power_sensor: str,
) -> list[tuple[datetime, float]]:
    """nimbus issue #843 (option B): drop raw power samples whose real
    magnitude is physically impossible for this participant's own
    configured envelope, BEFORE they reach resample_history_mean().

    DISCARD, not clamp -- deliberately. Clamping a 1514 kW reading down
    to a 250 kW bound would assert a value that was never measured, and
    would still be ~10x what a 25 kW device can physically do: it turns
    an obviously-absurd number into a plausible-looking but still-wrong
    one, which is strictly worse for a figure that feeds a real cost
    calculation, because it no longer trips anyone's suspicion. Dropping
    the sample instead lets the period's mean be built from whatever
    REAL samples remain; a period left with none falls through to
    resample_history_mean()'s own `default=0.0`, which since #843's own
    v0.94.285 fix genuinely means "no measured flow" rather than
    "whatever this sensor read next". For a power FLOW signal that is
    the honest answer.

    Filtered on the RAW history rather than the resampled series, also
    deliberately: a bad sample that survives into resample_history_mean()
    has already been averaged into its period before any post-hoc check
    could see it, so a period holding one 1514 kW sample among five good
    ones emerges at ~252 kW -- contaminated, but no longer obviously
    corrupt enough for a magnitude bound to catch. Filtering upstream
    means the bad reading never enters any mean at all.

    `max_plausible_kw <= 0` means this participant has no configured
    envelope to judge against, so there is no bound and this is a
    genuine no-op -- never clamp to zero, never divide by anything.

    Logs once per call (not once per bad sample) with the count and the
    single worst offender: this is real corrupt data in a household's
    own recorder and they should know, but a sensor stuck in the wrong
    unit for an hour must not produce hundreds of WARNING lines. Same
    bounded-logging posture as _ENVELOPE_LIMIT_WARNED/_AEMO_P5MIN_LAST_
    WARNED_PERIOD elsewhere in solver_writer.py, scoped to this
    function's own once-per-scored-day call pattern.
    """
    sw = _solver_writer()
    if max_plausible_kw <= 0.0 or not power_hist:
        return power_hist
    kept: list[tuple[datetime, float]] = []
    dropped: list[tuple[datetime, float]] = []
    for t, v in power_hist:
        if abs(v * power_scale) > max_plausible_kw:
            dropped.append((t, v))
        else:
            kept.append((t, v))
    if dropped:
        worst_t, worst_v = max(dropped, key=lambda p: abs(p[1] * power_scale))
        sw._LOGGER.warning(
            "Nimbus quality: discarded %d physically implausible reading(s) "
            "from battery participant '%s' power sensor %s -- worst was "
            "%.3f (scaled: %.3f kW) at %s, beyond this participant's own "
            "configured envelope of %.3f kW (%.0fx). Most often a sensor "
            "briefly reporting a different unit than it does now (nimbus "
            "issue #843); the affected periods are reconstructed from the "
            "remaining real samples instead, or read 0 kW if a period has "
            "none left",
            len(dropped),
            participant_name,
            power_sensor,
            worst_v,
            worst_v * power_scale,
            worst_t.isoformat(),
            max_plausible_kw,
            _PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE,
        )
    return kept


def _resolve_battery_participant_history(
    *,
    day_start: datetime,
    day_end: datetime,
    grid_times: list[datetime],
    period_hours: float,
    n_periods: int,
) -> list[
    tuple[
        elements.BatteryConfig,
        np.ndarray,
        np.ndarray,
        float,
        list[tuple[datetime, float]],
    ]
]:
    """nimbus issue #768/#585 (Mark Purcell): the retrospective, HISTORY-
    based sibling of `build_extra_batteries()` in `extra_batteries.py` --
    same `battery_participant` subentries, but reconstructing each one's
    own REAL, already-elapsed [day_start, day_end) dispatch from recorder
    history, exactly the way `_compute_report_for_window()`'s own "home"
    battery reconstruction already does for the hub's single configured
    battery. This is what lets `compute_quality_report()` score the
    whole real storage fleet (home + EVs), not just the pack -- the gap
    solver_writer.py's own #585 comment already named explicitly:
    "integrating each battery_participant's own power sensor into its
    own SoC... is a substantially larger architectural piece,
    deliberately not attempted here." This function is that piece.

    Per Mark Purcell's own direct instruction (2026-09-12): reuses each
    participant's own ALREADY-CONFIGURED `battery_participant_power_
    sensor` (the same real sensor `build_extra_batteries()`'s own
    comment notes is "read live for future dashboard/monitoring wiring,
    not needed by BatteryConfig itself" for the forward solve) as the
    historic baseline -- no new sensor configuration needed, this is
    simply the first real consumer of a field that already existed.

    Returns one `(BatteryConfig, actual_charge_kw, actual_discharge_kw,
    final_soc_kwh_actual, soc_hist)` tuple per participant that has
    EVERYTHING this function needs to score it for real (capacity, SoC
    sensor, power sensor, and real non-empty history for both across the
    window) -- a participant missing any of these, or with a genuinely
    empty history for this specific day (e.g. an EV added to the wizard
    after this day already elapsed), is honestly SKIPPED for this one
    day's report, not treated as a hard failure of the whole report
    (mirrors this module's own "skip this cycle, retry later" discipline
    for the report as a whole, scoped down to one participant). Logged
    once (INFO) per (name, day) skip reason, not every solve cycle --

    `soc_hist` (nimbus issue #949, Mark Purcell's own chosen fix): the
    participant's raw, already-fetched real SoC recorder history
    (`[(datetime, pct), ...]`, same shape and 6h-lookback convention as
    the "home" battery's own `soc_hist` in `_compute_report_for_window()`
    just above), carried out rather than discarded once this function is
    done with it internally for `initial_pct`/`final_pct`. Every
    participant reaching `results.append()` below is guaranteed non-empty
    here -- the missing/empty-history skip above already excluded anyone
    who wouldn't be -- so a caller building a fleet-wide SoC comparison
    never has to re-derive an honest-absence case that was already
    resolved.
    this function only ever runs once per real calendar day being
    scored, so there is no log-spam risk to guard against the way the
    live per-solve gating above does.

    Bidirectional-flow gating (2026-09-13, Mark Purcell's own real,
    substantive catch): the pack-power sensor measures the PACK's own
    internal flow, which includes real propulsion discharge WHILE
    DRIVING -- energy that never touches the home's grid connection,
    solar, or the shared charger at all. Left ungated, that would get
    priced by evaluate_realized_cost_multi() as if it flowed into the
    household's own grid balance (a real trip's worth of discharge
    looking like a real grid export that never happened), corrupting
    j_ach/EPR the same way the #299 sign-convention bug and the #532
    shared-sensor double-count once did. Gated here using the SAME
    `battery_participant_available_entity` the live forward solve
    already uses for scheduling (#563/#779) -- its own HISTORY for this
    exact day (not live state) zeroes both `actual_charge_kw` and
    `actual_discharge_kw` for any period the car was away, so only
    genuine at-home flow is ever priced against the grid.

    RESOLVED 2026-09-14 (nimbus issue #467, Mark Purcell: "Build an
    internal gating gap please"). This function used to mask the
    RECONSTRUCTION side only -- the oracle re-solve had no equivalent
    per-period gate, because `BatteryConfig` could only express a single
    contiguous `unavailable_until_period_index` PREFIX, so a day with
    more than one separate trip (a morning school run AND a separate
    evening trip) let the oracle unrealistically assume the EV was home
    to charge/discharge during a real away window -- inflating the
    oracle's own achievable cost floor and therefore overstating that
    participant's regret.

    `BatteryConfig.unavailable_period_indices` (#467) is now a genuine
    per-period mask, and this function hands it exactly the same
    `is_home_mask` it uses to zero the reconstruction arrays. Both
    halves of the scorer now agree about where the car was, on any
    number of trips per day.

    Deliberately NOT handled here either (see regret.py's own
    `oracle_dispatch()` docstring, "Still deliberately NOT extended"): a
    participant whose ONLY power signal is a sensor SHARED with another
    participant (this household's own Sigen DC charger, serving both
    the Model 3 and Model Y) is scored using that shared reading as-is,
    honestly wrong on any day both EVs actually used it --
    disambiguating that is a real, separate, larger piece of work, not
    attempted in this pass.

    Native/in-process mode ONLY, same reasoning and same graceful `[]`
    fallback as `build_extra_batteries()` -- a standalone/cron
    deployment has no subentries at all, so `compute_quality_report()`
    always scores exactly the "home" battery there, unchanged from
    before this function existed.
    """
    sw = _solver_writer()
    if sw._NATIVE_HASS is None:
        return []
    try:
        from ..const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )

    entries = sw._NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return []

    results: list[tuple[elements.BatteryConfig, np.ndarray, np.ndarray, float]] = []
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_BATTERY_PARTICIPANT:
            continue
        data = subentry.data
        name = data.get(CONF_BATTERY_PARTICIPANT_NAME) or subentry.subentry_id
        capacity_kwh = float(data.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH) or 0.0)
        power_sensor = data.get(CONF_BATTERY_PARTICIPANT_POWER_SENSOR)
        soc_sensor = data.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR)
        if capacity_kwh <= 0.0 or not soc_sensor or not power_sensor:
            sw._LOGGER.info(
                "Nimbus quality: battery participant '%s' has no power "
                "sensor/SoC sensor/capacity configured -- excluded from "
                "this day's multi-battery score (this participant simply "
                "isn't scored, the rest of the fleet is unaffected)",
                name,
            )
            continue

        # nimbus issue #843 (option A): per-row unit scaling, scoped to
        # exactly this one fetch. See fetch_entity_power_history_kw()'s
        # own docstring for why this is a separate function and why every
        # other history read in this file deliberately keeps the cheaper
        # attribute-stripped path.
        power_hist = sw.fetch_entity_power_history_kw(power_sensor, day_start, day_end)
        soc_hist = sw.fetch_entity_history_range(
            soc_sensor, day_start - timedelta(hours=6), day_end
        )
        if not power_hist or not soc_hist:
            sw._LOGGER.info(
                "Nimbus quality: battery participant '%s' has no real "
                "history for window [%s, %s] (power=%d, soc=%d rows) -- "
                "excluded from this day's multi-battery score",
                name,
                day_start.isoformat(),
                day_end.isoformat(),
                len(power_hist),
                len(soc_hist),
            )
            continue

        try:
            # nimbus issue #843 (option A): rows arrive already
            # normalised to kW by fetch_entity_power_history_kw(), using
            # each sample's OWN recorded unit -- so there is no whole-
            # window scale left to apply here. _kw_scale_factor() itself
            # is untouched and still correct for every other caller, none
            # of which fetches per-row units.
            power_scale = 1.0
            # Same sign convention as the home battery's own
            # solver_battery_power_positive_is_charge (nimbus #299):
            # internally, positive net_kw always means DISCHARGE. A
            # participant whose sensor reports positive=charge needs the
            # same -1.0 flip before this function's own charge/discharge
            # split below can treat every reading the same way.
            sign = (
                -1.0
                if data.get(CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE)
                else 1.0
            )
            # nimbus issue #843 (option B, Mark Purcell's own steer): a
            # cheap always-on guard so no single corrupt reading can ever
            # publish a physically impossible figure again. Judged against
            # THIS participant's own configured envelope -- read here
            # rather than reusing the BatteryConfig construction further
            # down, because the filter has to run on the raw history
            # before it is resampled (see the helper's own docstring for
            # why upstream and why discard rather than clamp).
            max_plausible_kw = (
                max(
                    float(data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0),
                    float(data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0),
                )
                * _PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE
            )
            power_hist = _drop_implausible_power_samples(
                power_hist,
                power_scale=power_scale,
                max_plausible_kw=max_plausible_kw,
                participant_name=str(name),
                power_sensor=str(power_sensor),
            )
            # nimbus issue #1241: the declared sign convention is REDUNDANT
            # with this history -- over a window where SoC rose, energy went
            # in, so the sign of the mean power across it is determined rather
            # than assumed. Checked here, AFTER the implausible-sample filter
            # above, so a single #843-class corrupt reading cannot produce a
            # false accusation.
            #
            # Reports only. A wrong sign inverts the charge/discharge split
            # and surfaces as an energy-balance discrepancy rather than an
            # error, which is why it has cost real investigations -- but
            # silently flipping a household's own declaration on a heuristic
            # would be worse. Wrapped because a diagnostic must never take a
            # solve down.
            try:
                from .sign_convention import detect_power_sign_convention

                _sign_check = detect_power_sign_convention(
                    [(t, float(v)) for t, v in soc_hist],
                    [(t, float(v) * power_scale) for t, v in power_hist],
                    declared_positive_is_charge=bool(
                        data.get(CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE)
                    ),
                )
                if _sign_check.is_actionable:
                    _warn_sign_convention_once(str(name), _sign_check)
            except Exception:
                sw._LOGGER.debug(
                    "Nimbus: sign-convention check skipped for participant %s",
                    name,
                    exc_info=True,
                )
            net_kw = np.array(
                [
                    v * power_scale * sign
                    for v in sw.resample_history_mean(
                        power_hist, grid_times, period_hours
                    )
                ]
            )
            # nimbus issue #1161: refuse to credit throughput across a
            # recorder gap the resample merely held a stale reading over.
            #
            # `resample_history_mean()` carries the last sample forward
            # indefinitely -- correct for a STATE, and energy-fabricating
            # for POWER. A participant whose pack sensor stops writing
            # rows mid-window was otherwise credited its last reading
            # multiplied by every remaining hour of the day, then priced
            # against real tariffs. `load_run_state.py`'s live counter
            # has refused exactly this since it was written; the
            # retrospective scorer never inherited the guard.
            #
            # Zeroing (rather than holding) is the conservative
            # direction, and it is the one this repo already chose for
            # the live counter -- crediting nothing for time nobody
            # observed. The cost is that a real, unrecorded dispatch goes
            # uncounted, which is why the fraction is published rather
            # than adjusted silently (see `stale_history_period_indices`
            # on BatteryConfig).
            stale_period_indices = sw._stale_power_period_indices(
                power_hist, grid_times, period_hours
            )
            if stale_period_indices:
                stale_mask = np.zeros(len(net_kw), dtype=bool)
                stale_mask[list(stale_period_indices)] = True
                net_kw = np.where(stale_mask, 0.0, net_kw)
                sw._LOGGER.info(
                    "Nimbus quality: battery participant '%s' has no recorded "
                    "power for %d of %d periods (gaps longer than the "
                    "one-hour sample guard) -- those periods contribute no "
                    "throughput rather than holding the last reading",
                    name,
                    len(stale_period_indices),
                    len(grid_times),
                )
            actual_charge_kw = np.array([max(0.0, -v) for v in net_kw])
            actual_discharge_kw = np.array([max(0.0, v) for v in net_kw])

            # nimbus issue #768 (Mark Purcell, 2026-09-13, real catch):
            # the pack-power sensor also measures real propulsion
            # discharge WHILE DRIVING -- energy that never touches the
            # home's grid connection at all. Left in, a real trip would
            # get priced by evaluate_realized_cost_multi() as if it
            # exported into the household's own grid balance. Gated
            # using the SAME battery_participant_available_entity the
            # live forward solve already uses for scheduling (#563/
            # #779), read as HISTORY for this exact day rather than live
            # state -- zeroes both charge/discharge for any period the
            # car was away. See this function's own docstring for the
            # accepted multi-trip-day limitation this simple mask
            # carries (a real, separate, larger oracle-side fix, not
            # attempted here).
            # nimbus issue #467: the away-window mask computed just below
            # is now ALSO handed to the oracle via BatteryConfig, not only
            # used to zero the reconstruction arrays. None means "no
            # availability entity configured", which stays a real no-op.
            away_period_indices: frozenset[int] | None = None
            available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
            if not available_entity:
                # nimbus issue #1247: the gate below is skipped, and nothing
                # in the published report distinguishes that from "the gate
                # ran and the car was home all day". Say so once.
                _warn_participant_history_is_ungated_once(str(name))
            if available_entity:
                home_hist = sw.fetch_entity_state_history_range(
                    available_entity, day_start, day_end
                )
                # Conservative default -- no real history at all for
                # this entity means "assume away" (0.0), same "if we
                # can't confirm the car/resource is really there, don't
                # count it" posture #563 item 2 already established for
                # the live solve's own availability gate.
                # nimbus issue #843, second real instance of the same
                # defect: this default=0.0 "assume away" intent was
                # silently defeated before that fix, because the helper
                # always initialised to pts[0][1]. A car whose FIRST
                # binary_sensor row of the day read "on" therefore had
                # every period before it masked as home -- the exact
                # opposite of the conservative posture stated above.
                # Now genuinely honoured (backfill_first stays False).
                home_numeric = [(t, 1.0 if v == "on" else 0.0) for t, v in home_hist]
                is_home = np.array(
                    sw.resample_history_nearest(home_numeric, grid_times, default=0.0)
                )
                is_home_mask = is_home >= 0.5
                actual_charge_kw = np.where(is_home_mask, actual_charge_kw, 0.0)
                actual_discharge_kw = np.where(is_home_mask, actual_discharge_kw, 0.0)
                # nimbus issue #467: the SAME mask, in the shape the LP
                # takes. Previously this was computed, used to zero the
                # two reconstruction arrays above, and then thrown away --
                # so compute_quality_report()'s own oracle re-solve had no
                # idea the car had ever left, and was free to schedule
                # charge/discharge during a real away window. On a
                # multi-trip day that inflated the oracle's achievable
                # floor and therefore overstated this participant's regret.
                away_period_indices = frozenset(
                    int(i) for i in np.flatnonzero(~is_home_mask)
                )

            min_soc_pct = float(
                data.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT) or 0.0
            )
            max_soc_pct = float(
                data.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT) or 100.0
            )
            # nimbus issue #843: backfill_first=True is load-bearing for
            # exactly the EV this bug was found on. soc_hist is fetched
            # with a 6h lookback so a real preceding sample usually
            # exists -- but a car that sleeps through the whole buffer
            # (real Tesla behaviour) has none, and its SoC genuinely did
            # NOT change while parked. Backfilling its first real reading
            # is physically right; falling through to min_soc_pct would
            # newly corrupt this participant's own scored trajectory.
            initial_pct = sw.resample_history_nearest(
                soc_hist, [day_start], default=min_soc_pct, backfill_first=True
            )[0]
            final_pct = sw.resample_history_nearest(
                soc_hist,
                [day_end - timedelta(seconds=1)],
                default=initial_pct,
                backfill_first=True,
            )[0]
            # Physical-range clamp only (same reasoning as the home
            # battery's own #325/#327/#328 history -- a real installed
            # BatteryConfig must never crash this report on sensor
            # nonsense), not the home battery's own full scheduling-
            # envelope WARNING treatment -- a single participant's own
            # noisy sensor is honestly logged and skipped above already
            # if history is entirely missing; a physically-valid but
            # out-of-schedule reading (e.g. an EV parked below its own
            # configured floor) is scored as-is, same as the live solve
            # treats it as a soft preference, not a hard error.
            initial_soc_kwh = min(
                max(capacity_kwh * initial_pct / 100.0, 0.0), capacity_kwh
            )
            final_soc_kwh_actual = min(
                max(capacity_kwh * final_pct / 100.0, 0.0), capacity_kwh
            )
            min_soc_kwh = capacity_kwh * min_soc_pct / 100.0
            max_soc_kwh = capacity_kwh * max_soc_pct / 100.0
            efficiency = (
                min(
                    float(data.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT) or 95.0)
                    / 100.0,
                    0.999,
                )
                ** 0.5
            )
            # nimbus issue #1111: computed here rather than inline in the
            # constructor because it needs the achieved trajectory (to
            # widen against) as well as the config, and the widening rule
            # is the substance rather than a detail.
            _must_have_idx, _must_have_kwh = _participant_departure_deadline(
                grid_times=grid_times,
                period_hours=period_hours,
                departure_hour=data.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR),
                must_have_soc_pct=data.get(
                    CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT
                ),
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                actual_charge_kw=actual_charge_kw,
                actual_discharge_kw=actual_discharge_kw,
                efficiency=efficiency,
            )
            battery_cfg = elements.BatteryConfig(
                name=str(name),
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                max_charge_kw=float(
                    data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0
                ),
                max_discharge_kw=float(
                    data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0
                ),
                charge_efficiency=efficiency,
                discharge_efficiency=efficiency,
                # nimbus issue #1111: the departure deadline, the second
                # field this function did not carry while its live-solve
                # sibling did. Widened to what the day actually reached --
                # see _participant_departure_deadline().
                must_have_soc_by_period_index=_must_have_idx,
                must_have_soc_kwh=_must_have_kwh,
                # nimbus issue #1109: the shared-charger cap, which this
                # function did NOT carry while its live-solve sibling
                # build_extra_batteries() did. #768 lists exactly this as
                # a prerequisite -- "an oracle free to charge both EVs on
                # one 25 kW charger simultaneously ... would overstate
                # achievable value" -- and the availability half (#467,
                # just below) made the trip while this half did not.
                #
                # Left at the CONFIGURED value here and widened after the
                # loop, once every participant's achieved charge array
                # exists: the widening is a property of the group, not of
                # any one member, so it cannot be computed yet.
                shared_charger_group=(
                    data.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP) or None
                ),
                shared_charger_max_kw=(
                    float(shared_charger_max_kw_raw)
                    if (
                        shared_charger_max_kw_raw := data.get(
                            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW
                        )
                    )
                    is not None
                    else None
                ),
                # nimbus issue #467: the real per-period away-window mask
                # built above, so the oracle re-solve is gated by exactly
                # the same windows the reconstruction arrays were masked
                # by. `available` stays True -- this is a per-period gate
                # for an already-elapsed day, not a whole-horizon "this
                # car is away right now" statement.
                unavailable_period_indices=away_period_indices,
                # nimbus issue #1161: diagnostic only -- network.py
                # never reads this. Carried on the config for the same
                # reason #1098's away mask is: the quality report is
                # handed these configs already, so the fraction of the
                # window that was unobserved arrives without new
                # plumbing.
                stale_history_period_indices=(stale_period_indices or None),
                # Same reference constants build_extra_batteries() uses
                # for the live forward solve -- no wizard field for a
                # real per-participant $/kWh cost exists yet (see that
                # function's own comment for why 0.0/0.0 isn't a valid
                # no-op here).
                charge_cost=_DEFAULT_EXTRA_BATTERY_CHARGE_COST,
                discharge_cost=_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST,
                # Stripped to 0.0/None regardless by compute_quality_
                # report()'s own battery_scoring pass (this is an
                # already-elapsed day, crediting leftover SoC is a guess
                # about tomorrow this scorer has no basis for) -- set
                # honestly here rather than left at a nonzero default
                # that would only ever be silently discarded downstream.
                salvage_value=0.0,
                degradation_cost_per_kwh=float(
                    data.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH) or 0.0
                ),
            )
        except (ValueError, TypeError) as e:
            # A bad/inconsistent config for ONE participant (e.g. a
            # physically-impossible min/max SoC pair) must never take
            # down the whole day's report -- skip just this participant,
            # same "the rest of the fleet is unaffected" posture as the
            # missing-sensor branch above.
            sw._LOGGER.warning(
                "Nimbus quality: battery participant '%s' could not be "
                "scored for window [%s, %s] (%s) -- excluded from this "
                "day's multi-battery score",
                name,
                day_start.isoformat(),
                day_end.isoformat(),
                e,
            )
            continue

        results.append(
            (
                battery_cfg,
                actual_charge_kw,
                actual_discharge_kw,
                final_soc_kwh_actual,
                soc_hist,
            )
        )
    return _widen_shared_charger_cap_to_achieved(results)


def _participant_departure_deadline(
    *,
    grid_times: list,
    period_hours: list,
    departure_hour: object,
    must_have_soc_pct: object,
    capacity_kwh: float,
    initial_soc_kwh: float,
    actual_charge_kw: np.ndarray,
    actual_discharge_kw: np.ndarray,
    efficiency: float,
) -> tuple[int | None, float | None]:
    """A participant's departure deadline for the SCORED window, widened
    to what the day actually reached (nimbus issue #1111).

    `build_extra_batteries()` sets `must_have_soc_by_period_index` /
    `must_have_soc_kwh` for the live solve; its history-based sibling
    never did. `elements.py` is explicit that this is a **hard**
    constraint -- *"a real EV genuinely needs a real SoC by a real time,
    not a priced preference"* -- so an oracle without it is free to leave
    the car empty at departure and arbitrage overnight instead, banking a
    trade the household could never have accepted. That makes `j_star`
    better than achievable, `regret_dollars` overstated and EPR
    understated, exactly as #1109 did through the shared-charger cap.

    **Widened, for the same reason and by the same rule as #1109.** A
    constraint narrows the oracle, and #956 is the record of what that
    costs when the achieved trajectory falls outside the narrowed set.
    Here the failure is ordinary rather than exotic: a household that
    simply did not plug the car in, or plugged it in late, has a day that
    misses its own departure target. Binding the oracle to a target the
    real day missed would put the achieved trajectory outside the
    oracle's feasible set on precisely those days.

    So the returned requirement is `min(configured, the SoC the day
    actually reached by that period)` -- as demanding as the day itself
    and no more. On a day that met its target this is the configured
    value and nothing changes.

    Returns `(None, None)` when the pair is not configured, when only one
    half is set (matching the live path's own "treat as neither"), or
    when the departure hour does not occur in this window -- all real,
    expected cases rather than errors.
    """
    sw = _solver_writer()
    if departure_hour is None or must_have_soc_pct is None:
        return None, None
    idx: int | None = None
    for i, start in enumerate(grid_times):
        if sw._local(start).hour == int(departure_hour):
            idx = i
            break
    if idx is None:
        # The hour does not occur in this scored window. Same posture the
        # live path takes for a short horizon: a no-op, not an error.
        return None, None

    configured_kwh = capacity_kwh * float(must_have_soc_pct) / 100.0

    # The achieved trajectory's own SoC at that period, integrated the
    # same way the reconstruction elsewhere does: charge credited at
    # efficiency, discharge debited by it.
    soc = float(initial_soc_kwh)
    for t in range(min(idx, len(actual_charge_kw))):
        hours = float(period_hours[t]) if t < len(period_hours) else 0.0
        soc += float(actual_charge_kw[t]) * efficiency * hours
        soc -= float(actual_discharge_kw[t]) / efficiency * hours

    return idx, min(configured_kwh, max(0.0, soc))


def _widen_shared_charger_cap_to_achieved(
    results: list[
        tuple[
            elements.BatteryConfig,
            np.ndarray,
            np.ndarray,
            float,
            list[tuple[datetime, float]],
        ]
    ],
) -> list[
    tuple[
        elements.BatteryConfig,
        np.ndarray,
        np.ndarray,
        float,
        list[tuple[datetime, float]],
    ]
]:
    """Each shared-charger group's cap, relaxed to contain what that group
    demonstrably drew (nimbus issue #1109).

    **Why the cap is not simply passed through.** Adding it at all is the
    fix -- the scorer's oracle could charge every member of a group at its
    own `max_charge_kw` simultaneously, which the hardware cannot do, so
    `j_star` was better than achievable and regret was overstated. But a
    constraint NARROWS the oracle's feasible set, and #956 is the record of
    what that costs when the achieved trajectory falls outside it: the
    achieved side prices out cheaper than optimal, regret goes negative,
    and the comparison is invalid rather than merely imprecise.

    The household settled that in #956 -- "go with B", widen the oracle to
    contain the achieved trajectory rather than clamp the achieved
    integration to fit the oracle -- and this applies the same decision to
    the same class of problem rather than making a new one. Same shape as
    `quality_report.py`'s own `_widen_export_pin_to_achieved()` does for
    the P2P export pin.

    **Exactly as wide as the day itself, and no wider.** The widened cap is
    `max(configured, the largest simultaneous draw the group actually
    made)` -- where "draw" is charge **plus** discharge, matching the LP
    constraint's own shape exactly (nimbus issue #1140; a shared
    connector's capacity is directional-agnostic). On a well-behaved day nothing changes: real draw stays under
    the cap, the max is the configured value, and the result is
    byte-identical to passing it straight through. It only moves when the
    real day already exceeded the configured number -- a cap set to
    nameplate while the charger briefly ran above it, a meter
    disagreement, or simply a misconfiguration -- which is precisely the
    case that would otherwise reintroduce #956.

    A group of one is left alone: `shared_charger_max_kw` on a single
    participant is just a second `max_charge_kw`, and widening it would
    quietly relax a real per-battery limit.
    """
    sw = _solver_writer()
    groups: dict[str, float] = {}
    for cfg, charge_kw, discharge_kw, _final, _soc_hist in results:
        group = cfg.shared_charger_group
        if not group or cfg.shared_charger_max_kw is None:
            continue
        # Charge AND discharge, because the LP constraint this widens
        # against sums both into one ceiling (nimbus issue #1140):
        # `shared_charger_{group}_t{t}` in network.py adds
        # charge_vars[m][t] + discharge_vars[m][t] for every member, which
        # is physically right -- one connector's throughput is bounded
        # regardless of direction, so a V2H participant discharging draws
        # from the same shared capacity as one charging. Summing charge
        # alone made a discharging member invisible to the widening, so
        # the cap could stay narrower than the real combined draw and
        # reintroduce #956's negative-regret failure for that case.
        arr = np.asarray(charge_kw, dtype=np.float64) + np.asarray(
            discharge_kw, dtype=np.float64
        )
        if group in groups:
            groups[group] = groups[group] + arr  # type: ignore[assignment]
        else:
            groups[group] = arr  # type: ignore[assignment]

    members: dict[str, int] = {}
    for cfg, _c, _d, _f, _sh in results:
        if cfg.shared_charger_group:
            members[cfg.shared_charger_group] = (
                members.get(cfg.shared_charger_group, 0) + 1
            )

    widened: list[
        tuple[
            elements.BatteryConfig,
            np.ndarray,
            np.ndarray,
            float,
            list[tuple[datetime, float]],
        ]
    ] = []
    for cfg, charge_kw, discharge_kw, final, soc_hist in results:
        group = cfg.shared_charger_group
        if (
            not group
            or cfg.shared_charger_max_kw is None
            or members.get(group, 0) < 2  # a group of one is a per-battery limit
        ):
            widened.append((cfg, charge_kw, discharge_kw, final, soc_hist))
            continue
        achieved_peak = float(np.max(np.asarray(groups[group], dtype=np.float64)))
        relaxed = max(float(cfg.shared_charger_max_kw), achieved_peak)
        if relaxed > float(cfg.shared_charger_max_kw):
            sw._LOGGER.debug(
                "Nimbus quality (#1109): shared charger group %r drew %.3f kW "
                "at peak against a configured cap of %.3f kW -- widening the "
                "oracle's cap to the achieved peak so the achieved trajectory "
                "stays inside the oracle's feasible set (#956)",
                group,
                achieved_peak,
                float(cfg.shared_charger_max_kw),
            )
        widened.append(
            (
                replace(cfg, shared_charger_max_kw=relaxed),
                charge_kw,
                discharge_kw,
                final,
                soc_hist,
            )
        )
    return widened
