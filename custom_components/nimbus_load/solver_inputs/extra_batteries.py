"""Additional `BatteryConfig` entries from this hub's own `battery_participant`
subentries -- nimbus issue #563's own config surface for #467 stage 1's
multi-battery solver support.

nimbus issue #1300, Phase 1 of #1298's decomposition of `solver_writer.py`'s
god-module. Measured before moving, same discipline as every #735 stage
before it: `build_extra_batteries()` is a pure read-and-construct function
(no dispatch, no publish) with real, already-extensive test coverage from
its own history (#563, #601, #757, #1067) -- the lowest-risk of the seven
phases #1298 lists, and the one picked to prove the methodology first.

Native/in-process mode ONLY -- `ConfigSubentries` aren't exposed over
`solver_writer.py`'s own plain-REST seam, and the standalone/cron
deployment has no wizard to configure a battery_participant from anyway.
Zero subentries (the default, and every install before #563) returns
`[]`, byte-identical to pre-#563 single-battery behaviour.

`_IMMOBILE_PARTICIPANT_WARNED` and `_BATTERY_PARTICIPANT_WARNED` move here
with the only function that ever reads or writes them -- same reasoning
`solver_inputs/battery_soc.py`'s own module docstring already gives for
`_HOME_BATTERY_SOC_EXCURSION_WARNED`: neither had any business living
three thousand lines away from its only reader.

`_DEFAULT_EXTRA_BATTERY_CHARGE_COST`/`_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST`
are defined here (this function is their original/primary definer) and
imported by `solver_inputs/battery_participants.py`'s own history-based
sibling, which needs the identical reference floor for the same reason.

See `solver_inputs/__init__.py` for why the `solver_writer` import is
deferred and by-module; the reasoning is identical and load-bearing here
too -- 30+ tests patch `solver_writer._NATIVE_HASS`/`solver_writer.safe_num`
as module attributes, and a module-scope `from ..solver_writer import X`
would resolve those names at import time and silently defeat every patch.
"""

from __future__ import annotations

from datetime import timedelta

try:
    from ..solver import elements
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import elements  # type: ignore[no-redef]

# nimbus issue #563 item 3: no wizard field exists yet for a battery
# participant's own charge/discharge $/kWh cost, but BatteryConfig.
# __post_init__ structurally requires the two to sum to at least
# elements.MIN_CHARGE_DISCHARGE_COST_SPREAD (0.01 $/kWh, the HAEO
# wash-trade-degeneracy guard). These are deliberately small (0.005 +
# 0.01 = 0.015, clearing the floor with a real margin) so they never
# meaningfully distort dispatch decisions -- a placeholder that clears a
# structural validation floor, not a real household-specific cost.
_DEFAULT_EXTRA_BATTERY_CHARGE_COST: float = 0.005
_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST: float = 0.01

# nimbus issue #779 (Mark Purcell, confirmed decision): a battery
# participant reading "away" right now is only bounded evidence for the
# NEAR term -- see BatteryConfig.unavailable_until_period_index's own
# docstring for the full mechanism this backs. Explicitly a fixed
# constant, not a wizard field: Mark's own ask was "the next hour," and
# he confirmed it's the right value as-is, not a placeholder to tune
# against real commute patterns first (detailed availability -- knowing
# WHEN a car actually returns -- stays out of scope, waiting on real
# calendar/trip-window integration, #467 item 3).
_BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS: float = 1.0

# nimbus issue #563 item 2: log-once-per-participant dedup for the
# "departure_hour/must_have_soc_by_departure_percent set alone" partial-
# config warning below, and (a second, independent condition keyed the
# same way) the live SoC-excursion warning -- same log-once-per-condition
# discipline this file's own siblings use.
_BATTERY_PARTICIPANT_WARNED: set[str] = set()

# nimbus issue #1067: a battery participant that can neither charge nor
# discharge (both max_charge_kw/max_discharge_kw at 0, a reachable
# wizard state) is warned once per subentry, not every solve.
_IMMOBILE_PARTICIPANT_WARNED: set[str] = set()


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


def build_extra_batteries(periods: elements.PeriodGrid | None = None) -> list:
    """nimbus issue #563: the config surface for #467 stage 1's own
    `batteries: list[BatteryConfig]` solver support. Builds ADDITIONAL
    BatteryConfig entries from this hub's own `battery_participant`
    subentries -- the household's existing single hub-level battery
    (built separately in main()/compute_nimbus_only_soc_counterfactual(),
    always name="home") is never touched or replaced by this function;
    every caller does `batteries=[home_battery, *build_extra_batteries(periods)]`.

    Zero subentries (the default, and every install before #563) returns
    [] -- a real no-op, `batteries` stays exactly `[home_battery]`,
    byte-identical to v0.94.177's own single-battery behaviour. This is
    the explicit "upgrade is a no-op" requirement from #563's own issue
    body.

    Native/in-process mode ONLY -- same reasoning as build_controllable_
    loads() (ConfigSubentries aren't exposed over the plain-REST seam,
    and the standalone/cron deployment doesn't have a wizard to
    configure these from anyway).

    `periods` (2026-09-08, nimbus issue #563 items 2/3, following up on
    PR #566's own explicit deferral): THIS solve's own PeriodGrid, needed
    only to resolve a configured departure_hour into a real period index
    for the departure-deadline mechanism below (see BatteryConfig.must_
    have_soc_by_period_index's own docstring for why network.py itself
    stays grid-agnostic and only ever receives an already-resolved
    index). Optional and defaults to None so every existing test/caller
    that predates #563 items 2/3 keeps working unchanged; a real caller
    always passes the real periods (see main()'s own call site).

    Availability gating (item 2, the binary_sensor half): reads
    available_entity's CURRENT state once per solve -- 'on' (or no
    entity configured at all) means available, anything else ('off',
    missing, unavailable/unknown) means not available for this WHOLE
    solve. See BatteryConfig.available's own docstring for why this is
    a whole-horizon snapshot, not a mid-horizon prediction of when an
    away EV will return -- there is no real forecast for that, and
    fabricating one would be worse than the honest "re-evaluate fresh
    every 5-minute solve cycle" answer.

    Availability gating (item 2, the departure-deadline half): resolves
    departure_hour to the FIRST period in THIS horizon whose own real
    wall-clock start hour matches (via periods.period_starts) -- a
    departure hour with no matching period in this particular solve's
    horizon (a short manual solve, or the hour has already passed today
    with no later occurrence in range) is a real, expected no-op, not an
    error. Both departure_hour and must_have_soc_by_departure_percent
    must be configured together to do anything -- either one alone is
    treated as neither set (logged once, not every solve).

    **The deadline is ONE-SHOT, not daily** (nimbus issue #467, stated
    because the field name suggests otherwise). On the real 96-hour
    horizon a departure hour occurs four times and the loop below breaks
    on the first, so the constraint guarantees SoC for the NEXT
    departure and says nothing about the three after it.

    That is acceptable rather than a defect because Nimbus is
    receding-horizon (rolling.py): every cycle re-solves and re-resolves,
    so the near deadline is always the one being enforced, and the
    unconstrained far end of the plan is never committed -- the next
    solve replaces it. It stops being acceptable the moment a real
    calendar event is expressible ("next Tuesday 07:15", #467 stage 3),
    at which point recurring-vs-one-shot becomes a genuine modelling
    choice rather than an artefact of hour-of-day being the only
    vocabulary available. Pinned by test_the_deadline_is_ONE_SHOT_
    across_a_real_multi_day_horizon so that decision gets made rather
    than inherited.

    Shared-charger group (item 3): shared_charger_group/shared_charger_
    max_kw pass straight through to BatteryConfig -- the actual LP
    constraint (grouping participants by name, taking the minimum
    declared ceiling) lives entirely in network.py's own build_plan(),
    this function only carries the two raw config values across.
    """
    sw = _solver_writer()
    if sw._NATIVE_HASS is None:
        return []
    try:
        from ..const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
            DEFAULT_PARTICIPANT_KWH_PER_100KM,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
            DEFAULT_PARTICIPANT_KWH_PER_100KM,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )

    batteries: list = []
    entries = sw._NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return []
    seen_names: set[str] = set()
    # nimbus issue #757 (temporary diagnostic, remove once root-caused):
    # a battery_participant subentry confirmed correctly stored (verified
    # directly via ha_get_integration's subentry introspection) was never
    # appearing in the solved battery list, with zero log output from any
    # of this loop's own existing warning branches -- meaning either this
    # loop never reaches the subentry, or it's being skipped by something
    # this unconditional trace hasn't been asked to explain yet. Logs
    # every subentry_type this loop actually sees, every cycle, until the
    # real mechanism is found.
    #
    # 2026-09-13 follow-up: a fresh devhub read found `entries[0].
    # subentries` reporting a stable set of 34 subentries (real
    # subentry_ids like "01KZYY...", no battery_participant present at
    # all) that does NOT match `ha_get_integration`'s own live read of
    # this exact entry_id moments later (38 subentries, real ids like
    # "01M14NJ...", battery_participant "Test EV" present) -- same
    # circuit TITLES appear under completely different subentry_ids in
    # the two reads. Two real, testable hypotheses this alone can't
    # distinguish between: (a) `async_entries(DOMAIN)` is genuinely
    # returning more than one config entry for this domain and
    # `entries[0]` is silently picking a stale/orphaned one instead of
    # the real, currently-configured entry the household actually sees;
    # or (b) something rarer (a stale Python object reference to the
    # "same" entry_id surviving past a point where HA replaced it).
    # Logging every entry this call actually returns -- not just
    # entries[0] -- to settle this directly on the next real occurrence,
    # rather than re-deriving it from static reading a third time.
    sw._LOGGER.debug(
        "Nimbus #757 diag: async_entries(DOMAIN) returned %d entr%s: %s",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        [
            (
                getattr(e, "entry_id", None),
                getattr(e, "title", None),
                getattr(getattr(e, "state", None), "value", None),
                len(e.subentries),
            )
            for e in entries
        ],
    )
    sw._LOGGER.debug(
        "Nimbus #757 diag: build_extra_batteries scanning %d subentries: %s",
        len(entries[0].subentries),
        [
            (s.subentry_id, s.subentry_type, s.data.get(CONF_BATTERY_PARTICIPANT_NAME))
            for s in entries[0].subentries.values()
        ],
    )
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_BATTERY_PARTICIPANT:
            continue
        data = subentry.data
        name = data.get(CONF_BATTERY_PARTICIPANT_NAME) or subentry.subentry_id
        sw._LOGGER.debug(
            "Nimbus #757 diag: found battery_participant subentry name=%r data_keys=%s",
            name,
            sorted(data.keys()),
        )
        if name in seen_names or name == "home":
            # "home" is reserved for the hub's own single battery (see
            # this function's own docstring) -- a household typing it
            # in here by accident would otherwise silently collide with
            # it across LP variable naming and cross-solve stability.
            # A duplicate name between two subentries is the same real
            # risk. Both skipped with a loud warning rather than crashing
            # the whole solve cycle over one misconfigured subentry --
            # same "skip, don't crash" discipline build_controllable_
            # loads() above already uses.
            sw._LOGGER.warning(
                "Nimbus: battery participant subentry name '%s' is reserved "
                "or duplicated -- skipping this cycle. Every battery "
                "participant needs its own real name, and 'home' is "
                "reserved for the hub's own single battery.",
                name,
            )
            continue
        seen_names.add(name)
        capacity_kwh = float(data.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH) or 0.0)
        max_charge_kw = float(data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0)
        max_discharge_kw = float(
            data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0
        )
        soc_sensor = data.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR)
        if capacity_kwh <= 0.0 or not soc_sensor:
            sw._LOGGER.warning(
                "Nimbus: battery participant '%s' is missing capacity_kwh "
                "or its SoC sensor -- skipping this cycle",
                name,
            )
            continue
        # nimbus issue #1067: a participant that can neither charge nor
        # discharge passes every check above -- capacity is positive,
        # the SoC sensor is present -- and joins the fleet as a battery
        # the LP can never move. Nothing said so.
        #
        # Reachable without touching the schema: the wizard requires
        # both fields, but `0` is a valid entry for a kW selector, and
        # both read sites fall back to `0.0` when absent.
        #
        # It is not inert, which is why it is worth a line. `battery_
        # oracle` still hands this participant its full SoC envelope, so
        # the scorer's oracle models a fleet member it can never
        # dispatch -- moving `j_star`, and therefore EPR and regret. Same
        # shape as the phantom recorded on #768, reached by a different
        # route.
        #
        # Deliberately a WARNING and NOT a `continue`. Excluding it is
        # probably the right end state, but that changes what a live
        # install solves, and the honest first step is to make the
        # silent case loud so a household can see it and decide. #1067
        # carries the argument for the stronger version.
        if (
            max_charge_kw <= 0.0
            and max_discharge_kw <= 0.0
            and subentry.subentry_id not in _IMMOBILE_PARTICIPANT_WARNED
        ):
            _IMMOBILE_PARTICIPANT_WARNED.add(subentry.subentry_id)
            sw._LOGGER.warning(
                "Nimbus #1067: battery participant %r has both "
                "max_charge_kw and max_discharge_kw at 0 -- it is in "
                "the fleet but the solver can never move it, and the "
                "scorer's oracle still receives its full SoC "
                "envelope, which shifts j_star/EPR/regret. Set real "
                "power limits, or remove the participant if it is "
                "not meant to be dispatched.",
                name,
            )
        min_soc_pct = float(data.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT) or 0.0)
        max_soc_pct = float(data.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT) or 100.0)
        # #563 item 4: a live number entity's CURRENT value, when
        # configured, overrides the wizard's own static max_soc_percent
        # for this solve -- see const.py's own comment on this field.
        charge_limit_entity = data.get(CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY)
        if charge_limit_entity:
            max_soc_pct = sw.safe_num(charge_limit_entity, max_soc_pct)
        min_soc_kwh = capacity_kwh * min_soc_pct / 100.0
        max_soc_kwh = capacity_kwh * max_soc_pct / 100.0
        # Same honest, no-crash-on-a-glitch-reading discipline as the
        # home battery's own live SoC read in main() -- see that call
        # site's own comment for the real "27+-crashes-per-window"
        # incident this pattern fixes. safe_num() itself already
        # degrades gracefully (WARN + fallback) on a non-numeric state.
        initial_soc_pct = sw.safe_num(soc_sensor, min_soc_pct)
        initial_soc_kwh = capacity_kwh * initial_soc_pct / 100.0
        initial_soc_kwh = min(max(initial_soc_kwh, 0.0), capacity_kwh)
        # nimbus issue #563 item 2 (2026-09-08): availability gating.
        # No entity configured -- always available, byte-identical to
        # every scenario before this field existed. An entity that's
        # missing/unavailable/unknown is treated the same as 'off' (not
        # available) -- the conservative reading for a live safety-
        # relevant gate: if we can't confirm the car/resource is really
        # there, don't plan to dispatch it. Moved ahead of the SoC-
        # excursion warning below (nimbus issue #601) so that warning can
        # know whether this participant is even reachable this cycle.
        available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
        available = True
        if available_entity:
            state_obj = sw._NATIVE_HASS.states.get(available_entity)
            available = state_obj is not None and state_obj.state == "on"
        # nimbus issue #779: a currently-away participant is only gated
        # for the next _BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS, not
        # this whole solve's horizon -- resolved to a real period index
        # the same way departure_hour is resolved further down (first
        # period whose own start is >= now + the exclusion window).
        # `periods` being unavailable (an older/manual caller with no
        # period context) falls back to the pre-#779 whole-horizon gate
        # -- same graceful-degradation posture #563 items 2/3 already
        # established for this same function.
        unavailable_until_period_index: int | None = None
        if not available and periods is not None:
            period_starts = periods.period_starts
            if period_starts:
                cutoff = period_starts[0] + timedelta(
                    hours=_BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS
                )
                unavailable_until_period_index = sum(
                    1 for start in period_starts if start < cutoff
                )
        # Parity fix (2026-09-08, Mark Purcell's own live-tested #563
        # review): the home battery's own live SoC read in main() logs a
        # WARNING when it sits outside its configured [min, max] --
        # "if this repeats every period the real battery is stuck
        # outside its own configured range... investigate rather than
        # lower the floor." This path previously recovered silently (an
        # EV arriving above its own charge limit landing in the soft
        # `overfill` slack with nothing logged) -- same real signal,
        # same real reason to surface it, now the same warning.
        #
        # nimbus issue #601 (Mark Purcell, real finding: 35 WARNING lines
        # in 51 minutes for one parked EV recovering slowly on solar --
        # every solve, including the 2-3 extra price-change-triggered
        # solves per 5-minute slot, re-logged the identical line). Now
        # WARNING only on the FIRST cycle a participant is found outside
        # its own range, DEBUG on every cycle it stays outside (still
        # visible if you go looking, never spamming the real log), and
        # one INFO "recovered" the cycle it returns inside -- same
        # per-condition warn-once discipline _BATTERY_PARTICIPANT_WARNED
        # already uses for the partial departure-deadline config just
        # below. A participant that's `available=False` (gated off, e.g.
        # away from home) is skipped entirely: the LP cannot schedule its
        # recovery and the household cannot act on it either, so there is
        # nothing actionable to log.
        _soc_excursion_key = f"{name}:soc_excursion"
        if not (min_soc_kwh <= initial_soc_kwh <= max_soc_kwh):
            if not available:
                pass
            elif _soc_excursion_key not in _BATTERY_PARTICIPANT_WARNED:
                _BATTERY_PARTICIPANT_WARNED.add(_soc_excursion_key)
                sw._LOGGER.warning(
                    "Nimbus: battery participant '%s' live SoC %.2f%% is "
                    "outside its own configured floor/ceiling [%.2f%%, "
                    "%.2f%%] -- the LP is scheduling real recovery this "
                    "cycle rather than having this state clamped away. If "
                    "this repeats every period, investigate rather than "
                    "adjust the floor/ceiling. (Logged once per excursion; "
                    "further cycles are DEBUG until it recovers.)",
                    name,
                    initial_soc_pct,
                    min_soc_pct,
                    max_soc_pct,
                )
            else:
                sw._LOGGER.debug(
                    "Nimbus: battery participant '%s' live SoC %.2f%% "
                    "still outside its own configured floor/ceiling "
                    "[%.2f%%, %.2f%%] this cycle.",
                    name,
                    initial_soc_pct,
                    min_soc_pct,
                    max_soc_pct,
                )
        elif _soc_excursion_key in _BATTERY_PARTICIPANT_WARNED:
            _BATTERY_PARTICIPANT_WARNED.discard(_soc_excursion_key)
            sw._LOGGER.info(
                "Nimbus: battery participant '%s' live SoC %.2f%% has "
                "recovered back inside its own configured floor/ceiling "
                "[%.2f%%, %.2f%%].",
                name,
                initial_soc_pct,
                min_soc_pct,
                max_soc_pct,
            )
        # nimbus issue #563 item 2, the departure-deadline half. Both
        # fields must be set together to do anything -- either one alone
        # is treated as neither set (a real, expected partial config,
        # logged once so it's visible without being noisy every solve).
        departure_hour = data.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR)
        must_have_soc_pct = data.get(
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT
        )
        must_have_soc_by_period_index: int | None = None
        must_have_soc_kwh: float | None = None
        if (departure_hour is not None) != (must_have_soc_pct is not None):
            _partial_key = f"{name}:departure_deadline_partial"
            if _partial_key not in _BATTERY_PARTICIPANT_WARNED:
                _BATTERY_PARTICIPANT_WARNED.add(_partial_key)
                sw._LOGGER.warning(
                    "Nimbus: battery participant '%s' has only one of "
                    "departure_hour/must_have_soc_by_departure_percent set -- "
                    "both are required together, treating as neither set "
                    "this cycle (logged once, not every solve).",
                    name,
                )
        elif departure_hour is not None and periods is not None:
            period_starts = periods.period_starts
            if period_starts is not None:
                for idx, start in enumerate(period_starts):
                    if sw._local(start).hour == int(departure_hour):
                        must_have_soc_by_period_index = idx
                        must_have_soc_kwh = (
                            capacity_kwh * float(must_have_soc_pct) / 100.0
                        )
                        break
                # No matching period in THIS horizon -- a real, expected
                # no-op (short manual solve, or the hour already passed
                # today with no later occurrence in range), not an error.
        # nimbus issue #467 item 4: a CALENDAR-driven departure OVERRIDES the
        # fixed hour/percent pair resolved above, when it actually resolves a
        # trip in this horizon.
        #
        # Precedence rather than replacement, and the order matters. The fixed
        # pair answers "this car leaves at 07:00 and should hold 60%"; a calendar
        # answers when it ACTUALLY leaves and how far it is going, and sizes the
        # requirement from real distance. When the calendar has something to say
        # it is strictly the better information, so it wins. When it does not --
        # an empty calendar, a trip beyond this horizon, an event with no
        # distance in its text, an unavailable calendar entity -- the fixed pair
        # stands, unchanged, which is what keeps a household that configured
        # only the fixed pair completely unaffected.
        #
        # Deliberately AFTER the pair above rather than instead of it: the
        # partial-config warning that block emits is still the right thing to say
        # about a half-configured fixed pair, independent of any calendar.
        trip_calendar = data.get(CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY)
        if trip_calendar and periods is not None and periods.period_starts:
            from .calendar_trips import resolve_trip_deadline

            period_starts = list(periods.period_starts)
            # The horizon's real end: the last period's start plus its own real
            # duration, not a flat assumption -- this solver's grid is tiered and
            # the final period is an hour wide while the first is minutes.
            horizon_end = period_starts[-1] + timedelta(
                hours=float(periods.hours[-1]) if len(periods.hours) else 0.0
            )
            trips = sw.fetch_calendar_trips(
                str(trip_calendar), period_starts[0], horizon_end
            )
            # The odometer is NOT read here, and the field is configurable but
            # deliberately unused for now. An odometer reports TOTAL lifetime
            # distance, not distance travelled into the current trip, and
            # converting one to the other needs a reading taken at the moment of
            # departure -- which nothing stores yet. Reading it and subtracting
            # anything would be arithmetic on two different quantities.
            #
            # Not reading it means the FULL trip distance is always required,
            # which errs towards a fuller pack rather than a stranded car. The
            # resolution layer already accepts `already_driven_km_by_start` and
            # is tested for it, so the honest gap is a stored departure reading,
            # not the mechanism.
            resolved = resolve_trip_deadline(
                trips,
                period_starts,
                kwh_per_100km=float(
                    data.get(CONF_BATTERY_PARTICIPANT_KWH_PER_100KM)
                    or DEFAULT_PARTICIPANT_KWH_PER_100KM
                ),
                horizon_end=horizon_end,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
            )
            if resolved is not None:
                must_have_soc_by_period_index, must_have_soc_kwh = resolved
        # nimbus issue #563 item 3: passed straight through to
        # BatteryConfig -- the actual LP constraint lives in network.py.
        shared_charger_group = (
            data.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP) or None
        )
        shared_charger_max_kw_raw = data.get(
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW
        )
        shared_charger_max_kw = (
            float(shared_charger_max_kw_raw)
            if shared_charger_max_kw_raw is not None
            else None
        )
        efficiency = (
            min(
                float(data.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT) or 95.0)
                / 100.0,
                0.999,
            )
            ** 0.5
        )
        # power_positive_is_charge (data key CONF_BATTERY_PARTICIPANT_
        # POWER_POSITIVE_IS_CHARGE) is read live for future dashboard/
        # monitoring wiring, not needed by BatteryConfig itself -- the
        # LP's own charge[t]/discharge[t] are always two separate
        # nonnegative variables (see BatteryConfig's own class
        # docstring), never one signed reading.
        salvage_value = float(data.get(CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE) or 0.0)
        degradation_cost_per_kwh = float(
            data.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH) or 0.0
        )
        sw._LOGGER.debug(
            "Nimbus #757 diag: about to append BatteryConfig for %r "
            "(capacity_kwh=%s, max_charge_kw=%s, max_discharge_kw=%s, "
            "min_soc_kwh=%s, max_soc_kwh=%s, initial_soc_kwh=%s)",
            name,
            capacity_kwh,
            max_charge_kw,
            max_discharge_kw,
            min_soc_kwh,
            max_soc_kwh,
            initial_soc_kwh,
        )
        batteries.append(
            elements.BatteryConfig(
                name=name,
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                charge_efficiency=efficiency,
                discharge_efficiency=efficiency,
                # No wizard field for a real charge/discharge $/kWh cost
                # per participant yet -- BatteryConfig.__post_init__
                # structurally requires charge_cost + discharge_cost to
                # clear elements.MIN_CHARGE_DISCHARGE_COST_SPREAD (the
                # HAEO wash-trade-degeneracy guard, see that constant's
                # own docstring), so a bare 0.0/0.0 is NOT a valid no-op
                # here the way it is for degradation_cost_per_kwh just
                # below. These two small reference constants clear that
                # floor with margin while staying a genuinely small,
                # non-distorting economic signal -- see their own
                # module-level comment.
                charge_cost=_DEFAULT_EXTRA_BATTERY_CHARGE_COST,
                discharge_cost=_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST,
                salvage_value=salvage_value,
                degradation_cost_per_kwh=degradation_cost_per_kwh,
                available=available,
                unavailable_until_period_index=unavailable_until_period_index,
                must_have_soc_by_period_index=must_have_soc_by_period_index,
                must_have_soc_kwh=must_have_soc_kwh,
                shared_charger_group=shared_charger_group,
                shared_charger_max_kw=shared_charger_max_kw,
            )
        )
    sw._LOGGER.debug(
        "Nimbus #757 diag: build_extra_batteries returning %d battery config(s): %s",
        len(batteries),
        [b.name for b in batteries],
    )
    return batteries
