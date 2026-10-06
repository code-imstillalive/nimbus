"""Controllable-load inputs: the wizard's own fields resolved into the LP's
`AdequacyLoadConfig` / `SheddableLoadConfig` / `ThermalLoadConfig` objects
(nimbus issue #1302, spec 003 — Phase 3 of #1298).

Moved here verbatim from `solver_writer.py`. The eight functions are one
cluster: `build_controllable_loads()` is the entry point every solve calls, and
the seven helpers exist only to serve it — the window resolvers, the
done-condition evaluator, the live-number tuning resolver, and the persisted
`LoadRunState` sampler.

## Why the bodies reach `solver_writer` through a deferred accessor

`_solver_writer()` below imports the module **late and by MODULE**, never by
name. A module-scope `from ..solver_writer import X` binds `X` at import time,
which silently defeats `patch.object(solver_writer, "X", ...)` — the test's
replacement lands on the `solver_writer` attribute while this module keeps
pointing at the original object. The suite relies on exactly that mechanism, so
the seam is load-bearing rather than stylistic. Same shape as
`solver_inputs/controllable_load_history.py` and `solver_reports/*`.

**The rule is "does anything patch it on the `solver_writer` module object", not
"is it a pure function".** Two names here would look safe to import directly and
are not:

- **The injected `hass`** used to be `_NATIVE_HASS`, *rebound* by
  `set_native_hass()`, so an import would have frozen it at import time. Since
  nimbus #1437 it is `solver_shared.NATIVE.hass`: the holder's identity never
  changes, so it is read directly and needs no seam.
- **`resolve_controllable_load_power_sensor` moves into this module and is still
  reached as `sw.resolve_controllable_load_power_sensor`.**
  `test_768_controllable_load_delivery_reconstruction.py` patches it as
  `solver_writer.resolve_controllable_load_power_sensor`, and spec 003 keeps
  that target unchanged. A bare intra-module call would make the spy invisible —
  which is precisely how `compute_quality_report` cost four test failures in
  Phase 2c.

`_DONE_CONDITION_WARNED` and `_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED` are sets
mutated in place and never rebound, so `sw.X.add(...)` is exactly equivalent to
the original `X.add(...)`; they stay in `solver_writer` because other code there
reads them.

## `_LOGGER` is `solver_shared._LOGGER`, never `sw._LOGGER`

`tests/test_callers_mode_counts_only_real_references.py` asserts **zero**
`sw._LOGGER`-shaped references outside `solver_writer.py` — Phase 2a's own
success condition. `solver_writer._LOGGER` is an identity alias of
`solver_shared._LOGGER` (spec 001), so the five test files that patch
`solver_writer._LOGGER` are unaffected by using the `solver_shared` form here.

**Regeneration is not idempotent with that fix.** If any of these bodies is ever
regenerated from a shifted `solver_writer.py` rather than hand-edited, the
regenerated text carries bare `_LOGGER` again, and a naive `_LOGGER` →
`sw._LOGGER` pass turns it into the form the gate rejects. Grep for
`sw._LOGGER` in this file specifically after any regeneration, not merely for the
presence of a `_LOGGER` reference.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

try:
    from .. import household_modes, solver_shared
    from ..done_condition import parse_done_when as _parse_done_when
    from ..solver import elements
    from ..solver_shared import safe_num
except ImportError:  # pragma: no cover - standalone/cron path
    import household_modes  # type: ignore[no-redef]
    import solver_shared  # type: ignore[no-redef]
    from done_condition import (
        parse_done_when as _parse_done_when,  # type: ignore[no-redef]
    )
    from solver import elements  # type: ignore[no-redef]
    from solver_shared import safe_num  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by name).

    See this module's own docstring for why every cross-module read goes through
    here rather than through a module-scope `from ..solver_writer import X`.
    """
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer  # type: ignore[no-redef]
    return solver_writer


def _resolve_hour_to_period_index(
    grid_times: list[datetime], now: datetime, hour: float, *, is_deadline: bool
) -> int:
    """nimbus issue #486: a controllable-load wizard field is a plain
    24hr-decimal "hour of day" (e.g. 6.0 = 6am) -- AdequacyLoadConfig
    needs a real PERIOD INDEX into this cycle's own tiered grid instead.
    Resolves "the next real occurrence of this hour from `now`" against
    grid_times (build_tiered_grid()'s own real, boundary-snapped period
    start times) -- e.g. asked for 6.0 at 22:00 today resolves to 6am
    TOMORROW, not a nonsensical negative offset into the past.

    is_deadline=True (CONF_DEFERRABLE_DEADLINE_HOUR): returns the LAST
    period index whose own start time is still <= the target instant --
    the period containing the actual deadline moment, matching
    AdequacyLoadConfig's own "deadline_period is inclusive, cumulative
    energy through this period must reach target_kwh" contract.
    is_deadline=False (CONF_DEFERRABLE_EARLIEST_HOUR): returns the FIRST
    period index whose own start time is >= the target instant -- the
    first period this load is allowed to draw any power at all.

    Clamped to [0, len(grid_times)-1] -- a target more than 96h out (the
    grid's own real horizon) still resolves to a real, usable index
    rather than an out-of-range one build_plan() would reject.
    """
    sw = _solver_writer()
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        hours=hour
    )
    if target < now:
        target += timedelta(days=1)
    return sw._period_index_for_instant(grid_times, target, is_deadline=is_deadline)


def _earliest_period_for_same_day_window(
    *,
    now: datetime,
    earliest_hour: float | None,
    deadline_hour: float | None,
    earliest_period: int,
    deadline_period: int,
) -> int:
    """`earliest_period`, corrected for a same-day window that is already
    in progress (nimbus issue #582, Mark Purcell).

    `_resolve_hour_to_period_index()` resolves "the next occurrence from
    now" for each hour INDEPENDENTLY. Once `now` is past today's
    `earliest_hour`, earliest rolls forward to TOMORROW even though
    today's window is still open -- correct for a genuine overnight
    window (earliest=22, deadline=6, `now` between midnight and 6am),
    wrong for a same-day window once the day has started (earliest=6,
    deadline=16, `now`=06:01 -- Mark's real repro on the first live
    morning of #534). The load was then dropped for its ENTIRE active
    window, the opposite of intended.

    **Extracted 2026-09-18 from four identical copies** (nimbus #485).
    Two sat in `build_controllable_loads()` and two in
    `apply_commanded_state_guard()`, and the 09-09 worklog already
    recorded the risk as realised once:

        While rebasing onto #582, found and fixed a real inconsistency:
        apply_commanded_state_guard()'s own duplicated period-index
        resolution didn't inherit #582's same-day fix ... flagged the
        drift risk between the two call sites as a candidate for a
        future shared-helper refactor.

    That fix was itself applied by duplicating, and the copies grew to
    four. **Checked before extracting rather than assumed: all four were
    byte-identical in logic**, differing only in `ruff format` line
    wrapping at different indentation depths. So this consolidation is
    prophylactic -- it fixes no live divergence, it removes the room for
    the next one, which has already happened once and was caught by a
    rebase rather than by any test.

    It also makes the question #485 asks -- whether deadline/earliest
    hours should be moded per household mode -- a much smaller one:
    moding multiplies the distinct hour pairs flowing through this
    logic, and "is the one helper right" is answerable in a way "are the
    five copies still in agreement" is not.

    Returns `earliest_period` unchanged whenever either hour is unset, or
    the periods are already correctly ordered, or the window is a genuine
    overnight one.

    **A fifth site shares the predicate and is deliberately NOT folded in
    here.** `_build_daily_adequacy_windows()` evaluates the same
    `earliest_today <= now <= deadline_today` test, but inside a
    per-day loop and as one arm of a conditional that also handles
    `now > deadline_today` (skip today entirely), `today_done`, and an
    already-met target. It is a superset rather than a copy: its
    `earliest_period` comes from `_period_index_for_instant()` on a
    day-offset instant, not from a pre-resolved value, so calling this
    would mean reshaping it rather than substituting it. Noted here
    because "four copies became one" is only true of the four that were
    genuinely identical, and someone grepping the predicate will find a
    fifth.
    """
    if earliest_hour is None or deadline_hour is None:
        return earliest_period
    if deadline_period >= earliest_period:
        return earliest_period
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest_today = midnight + timedelta(hours=float(earliest_hour))
    deadline_today = midnight + timedelta(hours=float(deadline_hour))
    if earliest_today <= now <= deadline_today:
        # The window opened earlier today and is still active, so the
        # load may draw RIGHT NOW -- period 0, not tomorrow.
        return 0
    return earliest_period


def _sample_load_run_state(
    hub_entry_id: str,
    subentry_id: str,
    power_sensor: str,
    now: datetime,
    day_key: str,
    import_price_now: float | None = None,
):
    """nimbus issue #479: reads one Controllable Load's real power sensor
    and folds a single solve-tick sample into its persisted LoadRunState
    (custom_components/nimbus_load/load_run_state.py). `import_price_now`
    (nimbus issue #591) is this cycle's own live blended import price,
    passed straight through to apply_power_sample() so it can accumulate
    the load's real actual-cost-today alongside delivered_today_kwh --
    optional/None is a genuine no-op, not an error. Best-effort and
    silent on any failure (sensor unavailable, Store I/O error, HA not
    fully started) -- this bookkeeping isn't consumed by build_plan() at
    all yet (see #479's own scope note), so it must never be able to take
    the actual solve cycle down. Native mode only, same reasoning as this
    function's own caller.

    nimbus issue #626: returns the freshly-persisted LoadRunState (or
    None on any failure/no-op path above) so build_controllable_loads()
    can read this cycle's own just-updated delivered_today_kwh without a
    second store round-trip -- this function already has the freshest
    possible sample for this solve tick, taken moments before its own
    caller resolves target_kwh.
    """
    sw = _solver_writer()
    if solver_shared.NATIVE.hass is None:
        # Not reachable from build_controllable_loads() (guarded at its
        # own entry), but this function has no other caller today either
        # -- a defensive, cheap-to-keep guard rather than an assumption.
        return None
    try:
        from homeassistant.helpers.storage import Store as _Store

        state_obj = solver_shared.NATIVE.hass.states.get(power_sensor)
        if state_obj is None or state_obj.state in (None, "unknown", "unavailable"):
            return None
        # nimbus issue #535 (Mark Purcell, real household finding): this
        # used to treat state_obj.state as already being kW, with no
        # check against what the sensor itself declares -- the same
        # real class of bug _kw_scale_factor() (this file, near
        # compute_daily_quality_report()) was already found and fixed
        # for the solar/load/battery quality-report sensors. A real
        # power/CT-clamp sensor reporting Watts (Mark's own case: a
        # 4.6W standby reading on a heat-pump HWS) was silently read as
        # 4.6 kW -- currently_on permanently true, delivered_today_kwh
        # ~1000x too large. Same fix, read directly off the already-
        # fetched state_obj's own attributes rather than a second
        # ha_get() round-trip (native mode only here, unlike
        # _kw_scale_factor()'s own REST-shaped caller).
        unit = state_obj.attributes.get("unit_of_measurement")
        scale = solver_shared.power_scale_to_kw(unit)  # nimbus #1570
        if scale != 1.0 and power_sensor not in sw._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED:
            sw._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED.add(power_sensor)
            solver_shared._LOGGER.info(
                "Nimbus: controllable load power sensor %s reports Watts "
                "(unit_of_measurement=%r) -- scaling by %.3f to kW for "
                "run-state sampling (logged once per entity)",
                power_sensor,
                unit,
                scale,
            )
        power_kw = float(state_obj.state) * scale

        try:
            from .. import load_run_state
            from ..const import DOMAIN
        except ImportError:
            import load_run_state
            from const import DOMAIN

        async def _update() -> load_run_state.LoadRunState:
            store = load_run_state.LoadRunStateStore(
                store=_Store(
                    solver_shared.NATIVE.hass,
                    1,
                    f"{DOMAIN}_{hub_entry_id}_load_run_state",
                )
            )
            prev = await store.async_read(subentry_id)
            new = load_run_state.apply_power_sample(
                prev,
                now=now,
                day_key=day_key,
                power_kw=power_kw,
                import_price_now=import_price_now,
            )
            await store.async_write(subentry_id, new)
            return new

        import asyncio as _asyncio

        future = _asyncio.run_coroutine_threadsafe(
            _update(), solver_shared.NATIVE.hass.loop
        )
        return future.result(timeout=10)
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        solver_shared._LOGGER.debug(
            "Nimbus: controllable load run-state sample failed for %s (%s)",
            subentry_id,
            power_sensor,
            exc_info=True,
        )
        return None


def _evaluate_done_condition(done_entity: str, done_when: str | None) -> bool | None:
    """nimbus issue #480: reads done_entity's real live state and decides
    whether a deferrable load counts as DONE. Returns True/False, or
    None for "can't tell right now" (entity missing/unavailable/unknown,
    or a genuinely malformed done_when) -- the caller's own fail-open
    contract (#480's acceptance: "a done-sensor going unavailable is
    ignored... same discipline as #313/#314") treats None as "not done,
    keep the normal schedule", never as an error.

    done_when=None means done_entity is treated as a binary_sensor --
    its own "on" state alone is the done condition, the same convention
    a plain HA automation trigger would use. Any other domain (a numeric
    tank-temperature sensor, say) needs done_when to say what "done"
    means for that reading -- except water_heater/climate (#534), whose
    own state is a mode string: those read current_temperature instead,
    and an unset done_when falls back to the entity's own temperature
    (setpoint) attribute rather than the binary_sensor "on" convention.
    """
    sw = _solver_writer()
    if solver_shared.NATIVE.hass is None:
        return None
    state_obj = solver_shared.NATIVE.hass.states.get(done_entity)
    if state_obj is None or state_obj.state in (None, "unknown", "unavailable"):
        return None
    domain = done_entity.split(".", 1)[0]
    if domain in sw._ATTRIBUTE_DONE_DOMAINS:
        current = state_obj.attributes.get("current_temperature")
        if current is None:
            return None
        if done_when is None:
            target = state_obj.attributes.get("temperature")
            if target is None:
                return None
            try:
                return float(current) >= float(target)
            except (ValueError, TypeError):
                return None
        try:
            op_fn, threshold = _parse_done_when(done_when)
            return bool(op_fn(float(current), threshold))
        except (ValueError, TypeError):
            condition_key = (done_entity, done_when, str(current))
            if condition_key not in sw._DONE_CONDITION_WARNED:
                sw._DONE_CONDITION_WARNED.add(condition_key)
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load done_entity %s / done_when %r "
                    "could not be evaluated (current_temperature %r) -- "
                    "treating as not done until this changes (logged once "
                    "per condition, not every solve)",
                    done_entity,
                    done_when,
                    current,
                )
            return None
    if done_when is None:
        return state_obj.state == "on"
    try:
        op_fn, threshold = _parse_done_when(done_when)
        return bool(op_fn(float(state_obj.state), threshold))
    except (ValueError, TypeError):
        condition_key = (done_entity, done_when, state_obj.state)
        if condition_key not in sw._DONE_CONDITION_WARNED:
            sw._DONE_CONDITION_WARNED.add(condition_key)
            solver_shared._LOGGER.warning(
                "Nimbus: controllable load done_entity %s / done_when %r could "
                "not be evaluated (state %r) -- treating as not done until this "
                "changes (logged once per condition, not every solve)",
                done_entity,
                done_when,
                state_obj.state,
            )
        return None


def _build_daily_adequacy_windows(
    grid_times: list[datetime],
    now: datetime,
    earliest_hour: float,
    deadline_hour: float,
    target_kwh: float,
    *,
    today_delivered_kwh: float,
    today_done: bool,
) -> list:
    """nimbus issue #612: builds one elements.AdequacyWindow per real
    calendar-day occurrence of [earliest_hour, deadline_hour] that fits
    (even partially) within grid_times' own horizon -- so a deferrable
    load owes its own target_kwh FRESH every day, not just once wherever
    the single "next occurrence from now" window happens to land (the
    bug: `plan_forecast` reading 0.0 for every day past the first).

    Only ever called for the same-day window shape (deadline_hour >=
    earliest_hour) -- see this function's own caller for why a genuine
    overnight window falls through to the pre-#612 single-window path
    instead.

    Day 0 (today) gets the #582 same-day-in-progress treatment (earliest
    resolves to "right now" if the window already opened) plus the
    #626/#480 treatment (today_delivered_kwh reduces its own target;
    today_done, or today's window having already fully closed for the
    day, drops it from the list entirely). Day 1 onward always get the
    FULL, unreduced target_kwh -- "delivered today"/"done" only ever
    speak to today's own run, never a future day's.
    """
    sw = _solver_writer()
    try:
        from .. import load_run_state
        from ..solver import elements
    except ImportError:
        import load_run_state
        from solver import elements

    windows: list = []
    if not grid_times:
        return windows
    last_grid_time = grid_times[-1]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = 0
    while True:
        earliest_today = midnight + timedelta(days=day, hours=earliest_hour)
        if earliest_today > last_grid_time:
            break
        deadline_today = midnight + timedelta(days=day, hours=deadline_hour)
        if day == 0:
            day_target_kwh = load_run_state.remaining_kwh(
                target_kwh=target_kwh, delivered_today_kwh=today_delivered_kwh
            )
            if now > deadline_today or today_done or day_target_kwh <= 0.0:
                day += 1
                continue
            earliest_period = (
                0
                if earliest_today <= now <= deadline_today
                else sw._period_index_for_instant(
                    grid_times, earliest_today, is_deadline=False
                )
            )
        else:
            earliest_period = sw._period_index_for_instant(
                grid_times, earliest_today, is_deadline=False
            )
            day_target_kwh = target_kwh
        deadline_period = sw._period_index_for_instant(
            grid_times, deadline_today, is_deadline=True
        )
        if deadline_period >= earliest_period:
            windows.append(
                elements.AdequacyWindow(
                    earliest_period=earliest_period,
                    deadline_period=deadline_period,
                    target_kwh=day_target_kwh,
                )
            )
        day += 1
    return windows


def _resolve_controllable_load_tuning(data: dict, subentry) -> dict:
    """nimbus issue #645: overlays each of the 7 real live-editable
    tuning fields (number.nimbus_<load>_<key>) on top of `data`'s own
    wizard-saved value, so every EXISTING read site in build_
    controllable_loads()/apply_commanded_state_guard() (a plain `data.
    get(CONF_DEFERRABLE_...)`) picks up the live value automatically,
    with zero further changes needed at each individual call site.

    A field whose live entity doesn't exist yet (a load created before
    this change, or a genuinely fresh install before the number platform
    has finished setup) or reads unknown/unavailable/non-numeric falls
    straight back to `data`'s own existing value -- the wizard value
    stays a REAL fallback, never silently dropped. Native mode only
    (returns `data` unchanged when `NATIVE.hass` is None), same
    reasoning as build_controllable_loads() itself: ConfigSubentries
    have no standalone/cron equivalent to read a live entity from
    either. Also returns `data` unchanged if `subentry` has no `title`
    (a real ConfigSubentry always does; a genuinely malformed/unusual
    object here fails open to the pre-#645 behaviour rather than
    crashing this whole load's own solve over a slug it can't compute).
    """
    sw = _solver_writer()
    if solver_shared.NATIVE.hass is None:
        return data
    title = getattr(subentry, "title", None)
    if not title:
        return data
    slug = sw._slug_for_controllable_load_entity_id(title)
    resolved = dict(data)
    for key in sw._CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS:
        state = solver_shared.NATIVE.hass.states.get(f"number.nimbus_{slug}_{key}")
        if state is None or state.state in (None, "unknown", "unavailable"):
            continue
        try:
            resolved[key] = float(state.state)
        except (TypeError, ValueError):
            continue
    # nimbus issue #485: the household-mode preset lands LAST, on
    # top of the live number.* overlay above -- so it scales the
    # value actually in force, never a stale wizard entry the
    # household has already tuned past. `home` and an unset mode
    # are the identity transform.
    mode_state = solver_shared.NATIVE.hass.states.get("select.nimbus_household_mode")
    mode = None
    if mode_state is not None and mode_state.state not in (
        None,
        "unknown",
        "unavailable",
    ):
        mode = mode_state.state
    resolved, _mode_applied = household_modes.apply_to_load_config(resolved, mode)
    if _mode_applied:
        # DEBUG, not INFO, deliberately: this fires once per load per
        # solve, so a six-load install in `away` would emit six lines a
        # minute at INFO -- exactly the noise #757 and #773 each had to
        # clean up after shipping. The solver-lever half logs at INFO
        # because it fires once per solve, not once per load.
        #
        # Worth having at all because "which of my levers did `away`
        # actually move?" is the first question a household asks when a
        # mode does not do what they expected, and without this the
        # per-load half is invisible: the moded value lives only inside
        # the solve and is never published anywhere.
        solver_shared._LOGGER.debug(
            "Nimbus #485: household mode %r moved %d lever(s) on %r: %s",
            mode,
            len(_mode_applied),
            title,
            _mode_applied,
        )
    return resolved


def resolve_controllable_load_power_sensor(data: dict) -> str | None:
    """`controllable_load_power_sensor` if configured, otherwise the one
    auto-discovered from the load's own device (nimbus issue #768).

    An explicit setting always wins -- discovery only fills a gap, it
    never overrides a household's own stated answer.
    """
    sw = _solver_writer()
    # Same dual-mode deferred const import every other function in this
    # file uses -- solver_writer is loaded both as part of the real
    # package and as a bare top-level module (tests/_solver_path.py, the
    # standalone/cron deployment), and these names are not bound at
    # module scope here.
    try:
        from ..const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (  # type: ignore[no-redef]
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
        )

    explicit = data.get(CONF_CONTROLLABLE_LOAD_POWER_SENSOR)
    if explicit:
        return explicit
    return sw._discover_power_sensor_for_device(
        data.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY) or ""
    )


def build_controllable_loads(
    now: datetime,
    grid_times: list[datetime],
    n_periods: int,
    import_price_arr: list[float] | None = None,
) -> tuple[list, list, list]:
    """nimbus issue #486: builds SheddableLoadConfig/AdequacyLoadConfig
    lists from this hub's own `controllable_load` subentries, for
    build_plan()'s own sheddable_loads=/adequacy_loads= arguments --
    before this function existed, both were always empty (hardcoded),
    and neither LP class (despite existing since #229/#16) had ever
    actually run on a live install.

    nimbus issue #774: also builds ThermalLoadConfig entries for
    kind=thermal subentries, returned as a third list. `heating_rate_
    c_per_kwh`/`idle_decay_c_per_hour` come from (in order): an explicit
    CONF_THERMAL_HEATING_RATE_C_PER_KWH/CONF_THERMAL_IDLE_DECAY_C_PER_HOUR
    override, else this subentry's own ALREADY-PERSISTED LoadRunState
    (`run_state_sample.thermal_heating_rate_c_per_kwh`/`_idle_decay_c_
    per_hour`, keyed by subentry_id -- so a load migrated from
    kind=deferrable keeps whatever it already learned, since that
    learning lives in the run-state store, not the kind), else
    thermal_forecast's own module-level defaults. Real, honest scope
    limit (not silently hidden): this function is SYNCHRONOUS (native
    ConfigSubentry access only), and a fresh recorder-history relearn is
    genuinely async (see apply_commanded_state_guard()'s own deferrable-
    kind relearning block, which bridges into it via `_async_fetch_
    thermal_history()`) -- a thermal load that has NEVER been kind=
    deferrable has no persisted learned rate to read yet and stays on
    config-override/module-default until a future issue extends that
    same async relearning trigger to kind=thermal loads too.

    Native/in-process mode ONLY (returns ([], [], []) unconditionally when
    NATIVE.hass is None, i.e. the standalone/cron deployment) --
    ConfigSubentries are a real HA config_entries object, not something
    exposed over this module's own plain-REST ha_get()/ha_post_state()
    seam the standalone path uses, and Mark's own #486 spec doesn't ask
    for a standalone controllable-load config path either. Imported
    locally (not at module top) so this module's own standalone-mode
    import path (see this file's own top-of-file try/except) never has
    to resolve `.const` at all -- it's only ever needed here, and only
    ever reached once NATIVE.hass is already known to be set.
    """
    sw = _solver_writer()
    if solver_shared.NATIVE.hass is None:
        return [], [], []
    # Same relative-then-absolute fallback as this file's own top-of-file
    # import block -- solver_writer.py can be imported either as part of
    # the real `custom_components.nimbus_load` package (native mode,
    # relative import resolves) or as a bare top-level module (this
    # project's own stub-based test harness, and the standalone/cron
    # deployment -- no parent package, relative import raises
    # ImportError). _NATIVE_HASS being non-None only ever happens via
    # native mode's own set_native_hass() in real deployment, but a test
    # mocking that module-level global directly (as this function's own
    # tests do, to exercise this path without the full HA test harness)
    # hits the bare-module case, so both must actually work.
    try:
        from .. import done_condition, load_run_state, thermal_forecast
        from ..const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_DONE_ENTITY,
            CONF_DEFERRABLE_DONE_WHEN,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_KWH_PER_DAY,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_DEFERRABLE_TARGET_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_NOMINAL_KW,
            CONF_SHEDDABLE_SHED_COST,
            CONF_THERMAL_COMFORT_FLOOR_C,
            CONF_THERMAL_COMFORT_FLOOR_COST,
            CONF_THERMAL_DEADLINE_HOUR,
            CONF_THERMAL_EARLIEST_HOUR,
            CONF_THERMAL_HEATING_RATE_C_PER_KWH,
            CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
            CONF_THERMAL_MAX_POWER_KW,
            CONF_THERMAL_TARGET_TEMPERATURE_C,
            CONF_THERMAL_TEMPERATURE_ENTITY,
            CONTROLLABLE_LOAD_KIND_DEFERRABLE,
            CONTROLLABLE_LOAD_KIND_SHEDDABLE,
            CONTROLLABLE_LOAD_KIND_THERMAL,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )
    except ImportError:
        import done_condition
        import load_run_state
        import thermal_forecast
        from const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_DONE_ENTITY,
            CONF_DEFERRABLE_DONE_WHEN,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_KWH_PER_DAY,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_DEFERRABLE_TARGET_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_NOMINAL_KW,
            CONF_SHEDDABLE_SHED_COST,
            CONF_THERMAL_COMFORT_FLOOR_C,
            CONF_THERMAL_COMFORT_FLOOR_COST,
            CONF_THERMAL_DEADLINE_HOUR,
            CONF_THERMAL_EARLIEST_HOUR,
            CONF_THERMAL_HEATING_RATE_C_PER_KWH,
            CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
            CONF_THERMAL_MAX_POWER_KW,
            CONF_THERMAL_TARGET_TEMPERATURE_C,
            CONF_THERMAL_TEMPERATURE_ENTITY,
            CONTROLLABLE_LOAD_KIND_DEFERRABLE,
            CONTROLLABLE_LOAD_KIND_SHEDDABLE,
            CONTROLLABLE_LOAD_KIND_THERMAL,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )

    sheddable_loads: list = []
    adequacy_loads: list = []
    thermal_loads: list = []
    entries = solver_shared.NATIVE.hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return [], [], []
    # nimbus issue #757 (temporary diagnostic, remove once root-caused):
    # a live check found build_extra_batteries()'s own identical
    # `_NATIVE_HASS.config_entries.async_entries(DOMAIN)` call returning
    # a STALE `entries[0]` (34 subentries, missing every controllable_
    # load and battery_participant, real subentry_ids that don't match
    # a fresh `ha_get_integration` read of the SAME entry_id at the same
    # moment) -- while THIS function, using the exact same call, was
    # simultaneously dispatching real controllable loads correctly on
    # the same devhub install. Logging the same entry-count/entry_id/
    # title/subentry-count triple here too, so the next real occurrence
    # settles directly whether these two call sites are ever actually
    # seeing DIFFERENT `entries[0]` objects (which would mean something
    # environmental, not a bug in either function's own logic) or the
    # same one (which would mean build_extra_batteries()'s own filter
    # loop, not the entries lookup itself, is where subentries are
    # actually being lost).
    solver_shared._LOGGER.debug(
        "Nimbus #757 diag: build_controllable_loads() async_entries(DOMAIN) "
        "returned %d entr%s: %s",
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
    run_state_day_key = now.strftime("%Y-%m-%d")
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONTROLLABLE_LOAD:
            continue
        # nimbus issue #645: overlays the 7 live-editable tuning fields
        # on top of the wizard-saved data -- every read below is
        # unchanged, it now just sees the live value when one exists.
        data = _resolve_controllable_load_tuning(subentry.data, subentry)
        name = data.get(CONF_CONTROLLABLE_LOAD_NAME) or subentry.subentry_id
        kind = data.get(CONF_CONTROLLABLE_LOAD_KIND)
        # nimbus issue #768: falls back to the single device_class=power
        # sensor on this load's own device when the field is unset.
        power_sensor = sw.resolve_controllable_load_power_sensor(data)
        run_state_sample = None
        if power_sensor:
            # nimbus issue #479: every configured load's own currently_on/
            # on_since/off_since/delivered_today_kwh gets sampled here,
            # regardless of kind -- #484's chatter-guard needs this for
            # any Controllable Load, not just a future quota kind. See
            # _sample_load_run_state's own docstring for why this never
            # raises into the rest of this function. `entries[0].entry_id`
            # is only ever read here (not unconditionally above), so a
            # fake/test hass object with no real entry_id -- like this
            # file's own tests use for the sheddable/deferrable cases,
            # neither of which sets power_sensor -- never has to carry
            # one just to exercise the rest of this function.
            #
            # nimbus issue #626: the return value (this cycle's own
            # freshest delivered_today_kwh) is kept for the deferrable
            # branch below, which needs it to stop scheduling the full
            # target_kwh on top of what's already been delivered today.
            run_state_sample = _sample_load_run_state(
                entries[0].entry_id,
                subentry.subentry_id,
                power_sensor,
                now,
                run_state_day_key,
                import_price_now=(
                    float(import_price_arr[0])
                    if import_price_arr is not None and len(import_price_arr) > 0
                    else None
                ),
            )
        if kind == CONTROLLABLE_LOAD_KIND_SHEDDABLE:
            nominal_kw = float(data.get(CONF_SHEDDABLE_NOMINAL_KW) or 0.0)
            if nominal_kw <= 0.0:
                # Real caller mistake (wizard submitted with the one
                # field this kind actually needs left blank) -- skip
                # rather than let elements.SheddableLoadConfig's own
                # >0 validation crash the whole solve cycle over one
                # misconfigured subentry.
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (sheddable) has no "
                    "nominal_kw configured -- skipping this cycle",
                    name,
                )
                continue
            sheddable_loads.append(
                elements.SheddableLoadConfig(
                    name=name,
                    forecast_kw=np.full(n_periods, nominal_kw),
                    shed_cost=float(
                        data.get(CONF_SHEDDABLE_SHED_COST) or elements.DEFAULT_SHED_COST
                    ),
                    min_fraction=float(data.get(CONF_SHEDDABLE_MIN_FRACTION) or 0.0),
                    subentry_id=subentry.subentry_id,
                )
            )
        elif kind == CONTROLLABLE_LOAD_KIND_DEFERRABLE:
            max_power_kw = float(data.get(CONF_DEFERRABLE_MAX_POWER_KW) or 0.0)
            target_kwh_config = float(data.get(CONF_DEFERRABLE_TARGET_KWH) or 0.0)
            if max_power_kw <= 0.0 or target_kwh_config <= 0.0:
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (deferrable) is missing "
                    "max_power_kw/target_kwh -- skipping this cycle",
                    name,
                )
                continue
            # nimbus issue #626 (Mark Purcell, real repro: 1.48 of 2.0 kWh
            # already delivered at 13:05, plan still scheduled 1.99 kWh
            # more): this used to pass the raw configured target_kwh
            # straight to AdequacyLoadConfig on every solve, with nothing
            # anywhere reducing it by what run_state_sample (just taken,
            # above) already shows was delivered today --
            # `load_run_state.remaining_kwh()` existed and was unit-
            # tested since #479 but was never actually called from here.
            # Only trust run_state_sample's own delivered_today_kwh when
            # its day_key matches THIS cycle's day -- apply_power_sample()
            # already rolls delivered_today_kwh back to 0.0 on a genuine
            # day change, so a mismatch here only means the sample call
            # above failed/no-op'd (see its own docstring), and 0.0
            # (today's config-target behaviour, unchanged) is the correct
            # fail-open default rather than guessing. Only ever speaks to
            # TODAY's own run -- see _build_daily_adequacy_windows() for
            # why a future day's window always gets the full,
            # config-configured target_kwh regardless of this value.
            delivered_today_kwh = (
                run_state_sample.delivered_today_kwh
                if run_state_sample is not None
                and run_state_sample.day_key == run_state_day_key
                else 0.0
            )
            earliest_hour = data.get(CONF_DEFERRABLE_EARLIEST_HOUR)
            deadline_hour = data.get(CONF_DEFERRABLE_DEADLINE_HOUR)
            # nimbus issue #480: a done_entity that currently reports DONE
            # means TODAY's real requirement is already satisfied -- fail-
            # open (None/malformed/unavailable) is treated as NOT done,
            # same as _evaluate_done_condition()'s own contract; only ever
            # speaks to today, same reasoning as delivered_today_kwh above.
            # nimbus issue #809: the wizard no longer asks for a separate
            # Done entity -- it defaults to this same load's own device_
            # entity (the common real case, and Mark's own actual
            # household config before #809: both fields pointed at the
            # identical water_heater.wwk302). An explicit override is
            # still honoured for a household whose done condition
            # genuinely lives on a different entity than the one being
            # commanded.
            done_entity = data.get(CONF_DEFERRABLE_DONE_ENTITY) or data.get(
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
            )
            today_done = bool(
                done_entity
                and _evaluate_done_condition(
                    done_entity, data.get(CONF_DEFERRABLE_DONE_WHEN)
                )
            )
            value_per_kwh = data.get(CONF_DEFERRABLE_VALUE_PER_KWH)
            # nimbus issue #482: a configured entity's CURRENT numeric
            # state overrides the static field above for this solve --
            # same "live override, static field as the safe_num()
            # fallback" pattern as build_extra_batteries()'s own
            # charge_limit_entity handling for CONF_BATTERY_PARTICIPANT_
            # CHARGE_LIMIT_ENTITY. A household can point this at a
            # template sensor (hashprice x hashrate / power) or a plain
            # input_number ("willing to pay") and change it freely
            # between solves with no config reload.
            value_per_kwh_entity = data.get(CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY)
            if value_per_kwh_entity:
                value_per_kwh = safe_num(
                    value_per_kwh_entity,
                    float(value_per_kwh) if value_per_kwh is not None else 0.0,
                )
            max_kwh_per_day = data.get(CONF_DEFERRABLE_MAX_KWH_PER_DAY)
            # nimbus issue #769 (household decision 2026-09-15):
            # "do not defer this load unless it saves more than $X",
            # per load. 0.0/unset is a complete no-op.
            min_deferral_saving = data.get(CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS)
            shortfall_price = float(
                data.get(CONF_DEFERRABLE_SHORTFALL_PRICE)
                or elements.DEFAULT_ADEQUACY_SHORTFALL_PRICE
            )

            # nimbus issue #612 (Mark Purcell, real repro: plan_forecast
            # is 0.0 for 10-13 Sep because a load's target only ever
            # applied to whichever single day the "next occurrence from
            # now" window happened to land on): a load with BOTH
            # earliest/deadline hours set, in the ordinary same-day shape
            # (deadline_hour >= earliest_hour), owes its own fresh
            # target_kwh EVERY calendar day within the horizon, not just
            # once. A genuine overnight window (deadline_hour <
            # earliest_hour, e.g. earliest=22/deadline=6) is a different,
            # not-yet-validated recurring shape -- falls through to the
            # single-window path below unchanged, same as a load missing
            # either hour entirely.
            if (
                earliest_hour is not None
                and deadline_hour is not None
                and float(deadline_hour) >= float(earliest_hour)
            ):
                windows = _build_daily_adequacy_windows(
                    grid_times,
                    now,
                    float(earliest_hour),
                    float(deadline_hour),
                    target_kwh_config,
                    today_delivered_kwh=delivered_today_kwh,
                    today_done=today_done,
                )
                if not windows:
                    solver_shared._LOGGER.info(
                        "Nimbus: controllable load '%s' (deferrable) has no "
                        "real window left in this cycle's own horizon -- "
                        "skipping (today's own target already met/done, and "
                        "no future day's window fits inside the horizon)",
                        name,
                    )
                    continue
                # nimbus issue #712/#713 (Mark Purcell, real live finding:
                # two consecutive nights of uncontrolled compressor cut-in
                # on the WWK302/#534 heat pump -- the deferrable model's
                # kWh-target/deadline framing has no representation of the
                # device's own physical thermal floor, so the LP is free
                # to wait for a cheaper period even when doing so lets the
                # tank fall past its floor and self-trigger, uncontrolled,
                # at whatever price happens to be live). #713's own text
                # proposed "pull the earliest allowed start forward" --
                # traced the actual LP constraint (network.py's adequacy
                # window sum) and that lever alone would not have changed
                # Mark's real repro: his window's earliest_period (06:00)
                # was already before the projected floor crossing (07:30),
                # the LP simply preferred the cheaper 08:00 WITHIN that
                # already-permissive window. The lever that actually forces
                # delivery before a real deadline is the window's own
                # DEADLINE, not its earliest bound -- tightening the
                # NEAREST window's deadline_period down to the projected
                # crossing period is what genuinely compels the LP to
                # schedule real heating before the tank breaches its floor,
                # rather than merely widening a bound the LP wasn't
                # constrained by in the first place.
                #
                # Deliberately only the NEAREST window (windows[0]) --
                # naive_floor_crossing_period() assumes ZERO further
                # heating from `now` onward, which is only a trustworthy
                # assumption up to whichever window real heating might
                # first occur in; a later window's own eventual deadline is
                # left untouched, same as PR #719's own floor_crossing_
                # forecast_* fields never claimed to predict past the first
                # crossing either.
                #
                # Known, honest simplification (not silently hidden): the
                # decay rate used here is the project's own documented
                # DEFAULT_IDLE_DECAY_C_PER_HOUR fallback, not this load's
                # own LEARNED rate (LoadRunState's thermal_idle_decay_c_
                # per_hour) -- the learned rate lives in the async run-state
                # store, and this function is synchronous (native
                # ConfigSubentry access only, see this function's own top
                # docstring), so threading the learned rate through here
                # would need a real async refactor of this function's own
                # call chain. Worth a follow-up once that's justified on
                # its own; the default fallback is the same constant this
                # project already trusts for a load with no learned rate
                # yet, not an invented number.
                if done_entity and done_entity.split(".", 1)[0] in (
                    done_condition.ATTRIBUTE_DONE_DOMAINS
                ):
                    live_temperature = done_condition.read_current_temperature(
                        solver_shared.NATIVE.hass, done_entity
                    )
                    done_state_obj = solver_shared.NATIVE.hass.states.get(done_entity)
                    min_temp = (
                        done_state_obj.attributes.get("min_temp")
                        if done_state_obj is not None
                        else None
                    )
                    floor_temperature = thermal_forecast.resolve_floor_temperature(
                        min_temp,
                        data.get(CONF_DEFERRABLE_DONE_WHEN),
                        done_condition.parse_done_when,
                    )
                    crossing_period = thermal_forecast.naive_floor_crossing_period(
                        grid_times,
                        now,
                        live_temperature,
                        thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR,
                        floor_temperature,
                    )
                    first_window = windows[0]
                    if (
                        crossing_period is not None
                        and crossing_period < first_window.deadline_period
                    ):
                        new_deadline_period = max(
                            first_window.earliest_period, crossing_period
                        )
                        solver_shared._LOGGER.warning(
                            "Nimbus: controllable load '%s' (deferrable) is "
                            "projected to cross its own physical floor (%.1f) "
                            "at period %d assuming no further heating -- "
                            "tightening this window's own deadline from "
                            "period %d to %d to force delivery before the "
                            "device self-triggers outside the solved plan "
                            "(nimbus issue #712/#713)",
                            name,
                            floor_temperature,
                            crossing_period,
                            first_window.deadline_period,
                            new_deadline_period,
                        )
                        windows = [
                            elements.AdequacyWindow(
                                earliest_period=first_window.earliest_period,
                                deadline_period=new_deadline_period,
                                target_kwh=first_window.target_kwh,
                            ),
                            *windows[1:],
                        ]
                first = windows[0]
                adequacy_loads.append(
                    elements.AdequacyLoadConfig(
                        name=name,
                        max_power_kw=max_power_kw,
                        # Required legacy fields, unused for LP construction
                        # once `windows` is set (see AdequacyLoadConfig's own
                        # docstring) -- populated from the first real window
                        # so they still describe something true rather than
                        # an arbitrary placeholder.
                        target_kwh=first.target_kwh,
                        deadline_period=first.deadline_period,
                        earliest_period=first.earliest_period,
                        shortfall_price=shortfall_price,
                        value_per_kwh=float(value_per_kwh)
                        if value_per_kwh is not None
                        else None,
                        max_kwh_per_day=float(max_kwh_per_day)
                        if max_kwh_per_day is not None
                        else None,
                        min_deferral_saving_dollars=float(min_deferral_saving)
                        if min_deferral_saving is not None
                        else 0.0,
                        subentry_id=subentry.subentry_id,
                        windows=tuple(windows),
                    )
                )
                continue

            # ---- Single-window path: a load missing one/both hours, or
            # a genuine overnight window -- unchanged from before #612.
            target_kwh = load_run_state.remaining_kwh(
                target_kwh=target_kwh_config, delivered_today_kwh=delivered_today_kwh
            )
            if target_kwh <= 0.0:
                solver_shared._LOGGER.info(
                    "Nimbus: controllable load '%s' (deferrable) already "
                    "delivered %.3f kWh today, meeting its %.3f kWh target "
                    "-- releasing the remainder of this window's schedule",
                    name,
                    delivered_today_kwh,
                    target_kwh_config,
                )
                continue
            earliest_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(earliest_hour), is_deadline=False
                )
                if earliest_hour is not None
                else 0
            )
            deadline_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(deadline_hour), is_deadline=True
                )
                if deadline_hour is not None
                else n_periods - 1
            )
            # nimbus issue #582 (Mark Purcell, first live morning of #534):
            # _resolve_hour_to_period_index() always resolves "the next
            # occurrence from now" independently for each hour -- once
            # `now` is past today's earliest_hour, earliest rolls forward
            # to TOMORROW even though today's window (earliest through
            # deadline) is still open and in progress right now. Correct
            # for the genuine overnight case (earliest=22, deadline=6,
            # `now` between midnight and 6am) but wrong for a same-day
            # window once the day has started (earliest=6, deadline=16,
            # `now`=06:01 -- exactly Mark's real repro). Detect the
            # same-day-in-progress case directly, before the ordering
            # check below: today's own (unrolled) earliest and deadline
            # instants both fall on today, and `now` sits between them --
            # if so the window opened earlier today and is still active,
            # so earliest_period is simply "right now" (0), not tomorrow.
            # nimbus issue #582, extracted to one helper by #485 --
            # this logic previously existed as four identical copies.
            earliest_period = _earliest_period_for_same_day_window(
                now=now,
                earliest_hour=earliest_hour,
                deadline_hour=deadline_hour,
                earliest_period=earliest_period,
                deadline_period=deadline_period,
            )
            if deadline_period < earliest_period:
                # A real, live-possible edge: e.g. earliest=22.0 (10pm),
                # deadline=6.0 (6am) both resolve relative to `now` (see
                # _resolve_hour_to_period_index's own docstring), and if
                # `now` is already past today's 6am but before 10pm,
                # earliest resolves to tonight while deadline resolves to
                # tomorrow's 6am -- fine. But if `now` is itself between
                # midnight and 6am, both can resolve to the same day in
                # the wrong order. Rather than construct an
                # AdequacyLoadConfig that fails its own __post_init__
                # ordering check (a real crash), skip this cycle with a
                # clear reason -- the next cycle's own `now` will very
                # likely resolve this correctly on its own.
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (deferrable) resolved "
                    "deadline_period (%d) before earliest_period (%d) for "
                    "this cycle's own 'now' -- skipping until the window "
                    "resolves normally",
                    name,
                    deadline_period,
                    earliest_period,
                )
                continue
            # nimbus issue #480: a done_entity that currently reports DONE
            # means this window's real requirement is already satisfied --
            # skip this cycle entirely rather than let the LP keep buying
            # energy this load no longer needs (the issue's own worked
            # example: HWS scheduled for 3h, reaches setpoint after 2h,
            # "the third hour is still bought"). Fail-open on anything
            # else (no done_entity configured, entity unavailable, a
            # malformed done_when) -- _evaluate_done_condition() only
            # ever returns True when it's genuinely confident.
            if today_done:
                solver_shared._LOGGER.info(
                    "Nimbus: controllable load '%s' (deferrable) reports "
                    "done via %s -- releasing the remainder of this "
                    "window's schedule",
                    name,
                    done_entity,
                )
                continue
            adequacy_loads.append(
                elements.AdequacyLoadConfig(
                    name=name,
                    max_power_kw=max_power_kw,
                    target_kwh=target_kwh,
                    deadline_period=deadline_period,
                    earliest_period=earliest_period,
                    shortfall_price=shortfall_price,
                    value_per_kwh=float(value_per_kwh)
                    if value_per_kwh is not None
                    else None,
                    max_kwh_per_day=float(max_kwh_per_day)
                    if max_kwh_per_day is not None
                    else None,
                    min_deferral_saving_dollars=float(min_deferral_saving)
                    if min_deferral_saving is not None
                    else 0.0,
                    subentry_id=subentry.subentry_id,
                )
            )
        elif kind == CONTROLLABLE_LOAD_KIND_THERMAL:
            max_power_kw = float(data.get(CONF_THERMAL_MAX_POWER_KW) or 0.0)
            target_temperature_c = data.get(CONF_THERMAL_TARGET_TEMPERATURE_C)
            # nimbus issue #809: the wizard no longer asks for a separate
            # temperature entity -- kind=thermal only ever has one real
            # device.py entity worth pointing at anyway (the same
            # water_heater/climate this load is commanded through), so
            # this defaults to the load's own device_entity. An explicit
            # override is still honoured for the rare household whose
            # temperature reading and commanded device are genuinely
            # different entities.
            temperature_entity = data.get(CONF_THERMAL_TEMPERATURE_ENTITY) or data.get(
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
            )
            if (
                max_power_kw <= 0.0
                or target_temperature_c is None
                or not temperature_entity
            ):
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) is missing "
                    "max_power_kw/target_temperature_c/temperature_entity -- "
                    "skipping this cycle",
                    name,
                )
                continue
            live_temperature = done_condition.read_current_temperature(
                solver_shared.NATIVE.hass, temperature_entity
            )
            if live_temperature is None:
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) has no live "
                    "temperature reading from %s (unavailable, or not a "
                    "water_heater/climate entity) -- skipping this cycle",
                    name,
                    temperature_entity,
                )
                continue
            earliest_hour = data.get(CONF_THERMAL_EARLIEST_HOUR)
            deadline_hour = data.get(CONF_THERMAL_DEADLINE_HOUR)
            earliest_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(earliest_hour), is_deadline=False
                )
                if earliest_hour is not None
                else 0
            )
            deadline_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(deadline_hour), is_deadline=True
                )
                if deadline_hour is not None
                else n_periods - 1
            )
            # nimbus issue #582's own same-day-in-progress fix, duplicated
            # here rather than shared -- same accepted drift-risk tradeoff
            # already flagged for apply_commanded_state_guard()'s own
            # duplicate of this exact block (2026-09-09 worklog).
            # nimbus issue #582, extracted to one helper by #485 --
            # this logic previously existed as four identical copies.
            earliest_period = _earliest_period_for_same_day_window(
                now=now,
                earliest_hour=earliest_hour,
                deadline_hour=deadline_hour,
                earliest_period=earliest_period,
                deadline_period=deadline_period,
            )
            if deadline_period < earliest_period:
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) resolved "
                    "deadline_period (%d) before earliest_period (%d) for "
                    "this cycle's own 'now' -- skipping until the window "
                    "resolves normally",
                    name,
                    deadline_period,
                    earliest_period,
                )
                continue
            # nimbus issue #774: heating_rate_c_per_kwh/idle_decay_c_per_hour
            # -- explicit override, else this subentry's own already-
            # persisted LoadRunState (see this function's own docstring for
            # the full "why not a fresh relearn here" reasoning), else
            # thermal_forecast's own module-level defaults.
            heating_rate_override = data.get(CONF_THERMAL_HEATING_RATE_C_PER_KWH)
            idle_decay_override = data.get(CONF_THERMAL_IDLE_DECAY_C_PER_HOUR)
            # nimbus issue #940: the origin is recorded on the SAME
            # branch that picks the value, so it cannot describe a
            # different branch than the one taken. This is the only site
            # that resolves this precedence; everything downstream echoes
            # what is decided here.
            if heating_rate_override is not None:
                heating_rate_c_per_kwh = float(heating_rate_override)
                heating_rate_origin = "override"
            elif (
                run_state_sample is not None
                and run_state_sample.thermal_heating_rate_c_per_kwh is not None
            ):
                heating_rate_c_per_kwh = run_state_sample.thermal_heating_rate_c_per_kwh
                heating_rate_origin = "learned"
            else:
                heating_rate_c_per_kwh = thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
                heating_rate_origin = "fallback"
            if idle_decay_override is not None:
                idle_decay_c_per_hour = float(idle_decay_override)
                idle_decay_origin = "override"
            elif (
                run_state_sample is not None
                and run_state_sample.thermal_idle_decay_c_per_hour is not None
            ):
                idle_decay_c_per_hour = run_state_sample.thermal_idle_decay_c_per_hour
                idle_decay_origin = "learned"
            else:
                idle_decay_c_per_hour = thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR
                idle_decay_origin = "fallback"
            comfort_floor_c = data.get(CONF_THERMAL_COMFORT_FLOOR_C)
            comfort_floor_cost = data.get(CONF_THERMAL_COMFORT_FLOOR_COST)
            thermal_loads.append(
                elements.ThermalLoadConfig(
                    name=name,
                    max_power_kw=max_power_kw,
                    initial_temperature_c=live_temperature,
                    target_temperature_c=float(target_temperature_c),
                    earliest_period=earliest_period,
                    deadline_period=deadline_period,
                    heating_rate_c_per_kwh=heating_rate_c_per_kwh,
                    idle_decay_c_per_hour=idle_decay_c_per_hour,
                    # nimbus issue #940
                    heating_rate_origin=heating_rate_origin,
                    idle_decay_origin=idle_decay_origin,
                    comfort_floor_c=(
                        float(comfort_floor_c) if comfort_floor_c is not None else None
                    ),
                    comfort_floor_cost=(
                        float(comfort_floor_cost)
                        if comfort_floor_cost is not None
                        else 0.0
                    ),
                    subentry_id=subentry.subentry_id,
                )
            )
    return sheddable_loads, adequacy_loads, thermal_loads
