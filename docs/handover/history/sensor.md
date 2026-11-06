# sensor.py: comment history

Moved out of `custom_components/nimbus_load/sensor.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim. Each block's rule, reason and issue number stay in the code as a short comment; the narrative is kept here, keyed by the code line it sat above. Line numbers are the file's at `main` before the move.

## Line 1228: above `from . import solver_runtime`

```text
    # Deferred import (same reasoning as solver_runtime.py's own
    # _ensure_ready(): solver_writer imports the pure-Python `solver`
    # and `ml` packages via a bare `from solver import ...` at module
    # top, which resolves against sys.path -- fine at real HA runtime
    # where solver_runtime.py sets that up before ever touching this
    # code path, but importing it at THIS module's top would drag that
    # requirement into every unit test that only wants to exercise the
    # NimbusForecastSensor / NimbusSolverConfigSensor / NimbusTopology-
    # ConfigSensor classes above. Moving it inside async_setup_entry --
    # which is only ever called by real HA -- keeps the test surface
    # unchanged and matches the pattern solver_runtime.py already uses).
    #
    # Real bug fixed here (issue #89, Mark Purcell, 2026-08-23): this is
    # genuinely the FIRST place solver_writer.py can get imported in the
    # whole process on a fresh start -- BEFORE solver_runtime.py's own
    # _ensure_ready() ever runs (that's only reached later, from the
    # periodic solve loop). Without the env vars set first, solver_
    # writer.py's own module-level sys.path.insert() fell back to this
    # HOUSEHOLD's own hardcoded NUC path -- wrong on every other install,
    # crashing with ModuleNotFoundError: No module named 'ml'/'solver' on
    # Mark's real HACS install, every single restart. set_default_env_
    # vars() is a cheap, pure os.environ.setdefault()/hass.config.path()
    # helper -- no disk I/O, no solver_writer import of its own -- safe
    # to call directly here on the event loop, unlike the actual
    # solver_writer import/solve cycle (which genuinely does need the
    # worker-thread treatment _ensure_ready()'s own callers already give
    # it, for unrelated reasons -- see that function's own docstring).
```

## Line 1258: above `import importlib`

```text
    # nimbus issue #349 (Mark Purcell, 2026-09-03 codebase review): this
    # bare `from . import solver_writer` runs solver_writer.py's own
    # module-level body -- a synchronous file open (TOKEN_PATH), a
    # sys.path mutation, and `import numpy`/`import highspy` (a native
    # extension load) -- ON THE EVENT LOOP, the first time it happens in
    # the process (confirmed by this function's own comment above,
    # predating this fix). solver_runtime.py's own _ensure_ready() already
    # got the equivalent fix for ITS OWN first-import call site (see that
    # function's docstring, and the "Real bug fixed here" comment above --
    # this is the one remaining call site with the same shape).
    # hass.async_add_import_executor_job() is HA core's own real,
    # documented API for exactly this: run a plain, synchronous
    # importlib.import_module() call on the dedicated import executor
    # instead of the event loop. A later import of an already-loaded
    # module is a trivial sys.modules cache hit either way -- this only
    # matters for the genuine first import, but costs nothing on every
    # subsequent one.
```

## Line 1391: above `_entity_registry = er.async_get(hass)`

```text
    # Diagnostic only, no behaviour change (2026-09-08, real live incident:
    # "Platform nimbus_load does not generate unique IDs ... ignoring
    # sensor.nimbus_solver_quality_report" fired on this household's own
    # NUC1 during a restart, self-recovered within ~3 minutes with no
    # lasting damage). Root cause NOT found -- confirmed
    # NimbusSolverQualityReportSensor is constructed in exactly ONE place
    # in this whole file (right above), so this file's own code cannot be
    # duplicating the entity itself; the collision has to be happening at
    # HA-core's own EntityPlatform/registry layer, which isn't inspectable
    # without direct filesystem access to .storage/core.entity_registry --
    # not available for NUC1 (see this repo's own CLAUDE.md deploy section
    # and the reference household's own access rules). This block
    # logs, right before the add_entities call that's the exact point the
    # real collision fires at, whether HA already has a conflicting state
    # or registry row for any entity in this batch -- if this ever
    # recurs, THIS log line (not another restart-and-guess) is what
    # actually answers "was something already there before we tried to
    # add it, and what was it." Same "diagnostic logging kept in place
    # for next time" pattern already used in __init__.py's own #312
    # investigation (see that file's own comment above async_forward_
    # entry_setups). Cheap: only real work happens when a conflict is
    # actually found, which is the abnormal case this exists to catch.
    #
    # nimbus issue #570 (Mark Purcell, found within hours of this landing
    # on his own install): the original version below fired 25 WARNINGs
    # on EVERY ordinary config-entry reload (an options-flow save, or any
    # subentry add/remove), not just the real collision it was written
    # for -- `hass.states.get(candidate.entity_id) is not None` is true
    # on every reload for every entity, because the unload a few hundred
    # milliseconds earlier leaves the OLD entity's state behind as
    # `unavailable, restored=True` -- the normal, expected state of any
    # entity between an unload and its re-add, not evidence of a
    # duplicate registration. Confirmed live: on Mark's install, two cold
    # restarts produced zero warnings (by the time the sensor platform
    # sets up post-restart, no leftover state exists yet), but three
    # ordinary reloads produced 25 warnings each. Real collision evidence
    # looks different: either NO restored flag on the leftover state (a
    # genuine live ghost, not a normal unload artifact), or the registry
    # already mapping this unique_id to a DIFFERENT entity_id than the
    # one about to be added. Tightened to exactly that -- see Mark's own
    # suggested condition on #570.
```

## Line 1562: above `self._signal_role = subentry.data.get(CONF_SIGNAL_ROLE, SIGNAL_ROLE_OTHER)`

```text
        # Explicit role (2026-08-23, see const.py's CONF_SIGNAL_ROLE for
        # the full "why not inferred from naming" reasoning) -- exposed
        # as a live attribute the same way subentry_type already is, so
        # the topology dashboard card can auto-discover "which power
        # signal is Grid/Battery/Solar" directly from hass.states, zero
        # config file needed. Meaningless-but-harmless on a load
        # subentry (never has this field set, defaults to "other").
        # Read BEFORE the device_class/unit block below, which now
        # depends on it (2026-09-03 fix, see const.py's SIGNAL_ROLE_
        # TEMPERATURE/SIGNAL_ROLE_HUMIDITY comment for the real bug this
        # closes: a household was told to add a Temperature/Humidity
        # signal via SIGNAL_ROLE_OTHER, which forced it through kW/POWER
        # semantics -- a real, live "unconvertible unit" warning every
        # coordinator cycle, and a temperature forecast entity literally
        # labelled in kilowatts).
```

## Line 1579: above `if self._signal_role == SIGNAL_ROLE_TEMPERATURE:`

```text
        # nimbus issue #263 (Mark Purcell) -- belt-and-braces instance-level
        # assignment, NOT the actual fix for the reported "unit has changed"
        # repair. Verified directly: Python already resolves the class-scope
        # _attr_* declarations above via normal attribute lookup on every
        # `self.native_unit_of_measurement` read, from the very first state
        # write onward -- there is no timing window where these three
        # class-level values fail to resolve correctly. The real fix for
        # the reported symptom is _remediate_forecast_lts_unit (called from
        # async_setup_entry below), which corrects an already-stale, empty
        # long-term-statistics metadata row left over from before this
        # unit was ever declared. This instance-level copy is kept only as
        # cheap, harmless defensiveness against a future refactor (e.g. a
        # subclass computing its own unit dynamically) that might rely on
        # `self._attr_*` rather than the class attribute.
        #
        # 2026-09-03: now genuinely role-dependent, not just a defensive
        # copy of the class default -- Temperature/Humidity roles get
        # their own real device_class/unit instead of being forced
        # through POWER/kW (see this method's own comment above).
```

## Line 1641: above `self._consecutive_unavailable_source_ticks = 0`

```text
        # nimbus issue #740 (Mark Purcell, live finding, real household):
        # the Silver fix above checks the source sensor's LIVE state at
        # the exact instant of each coordinator tick's write -- a single
        # momentary unavailable/unknown reading (a real, ordinary Zigbee/
        # Modbus blip, not a genuine disconnection) was enough to flip
        # `available` False for that entire ~UPDATE_INTERVAL_MINUTES-long
        # tick, which HA's own entity base class then uses to publish
        # NO extra_state_attributes at all -- including `forecast`, even
        # though the coordinator's own already-computed forecast for this
        # cycle is perfectly good and doesn't depend on the source sensor
        # being live at read time. Confirmed live: recurred 10 times
        # across ~9 hours of otherwise-normal overnight operation, each
        # time knocking sensor.nimbus_offer_curve/quality_report to
        # "unknown" for a full tick because solver_writer.py's own read
        # landed in exactly that window. Debounced below: only a source
        # reading that stays unavailable/unknown for
        # _SOURCE_UNAVAILABLE_DEBOUNCE_TICKS CONSECUTIVE ticks (~4
        # minutes at the current 2-minute interval) now flips this
        # entity unavailable -- long enough that an ordinary transient
        # blip (which self-heals within the same tick or the next one)
        # never trips it, short enough to still catch a genuinely
        # disconnected source well within the "confidently stale
        # forever" failure mode the original Silver fix closed.
```

## Line 3118: above `battery_towers.append(`

```text
                # Unlike Power Source (power_source_name) and PV String
                # (pv_string_label), Battery Tower has no free-text name
                # field of its own in _BATTERY_TOWER_KEYS -- its only real
                # identity is subentry.title, auto-derived at creation
                # time from the SoC sensor's own friendly_name (see
                # flows/battery_tower_subentry.py's _derive_title, e.g.
                # "Battery Tower 2 SoC" -> "Battery Tower 2"). Found and
                # fixed 2026-08-23 after a real live migration: without
                # this, the frontend has nothing but subentry_id to label
                # a tower with, and fell back to a synthetic per-inverter
                # position index ("Tower 1"/"Tower 2") that silently
                # discarded the real physical tower number (e.g. towers
                # 2 & 4 both landing on the SAME inverter both displayed
                # as "Tower 1"/"Tower 2", losing which was actually which).
```

## Line 3274: above `_unrecorded_attributes = frozenset(`

```text
    # Confirmed live on devhub (2026-09-01): this sensor was hitting HA's
    # 16384-byte recorder attribute-size limit on nearly every cycle
    # (62 warnings in one 30-minute window) -- the same class of bug
    # NimbusForecastSensor's own _unrecorded_attributes fix (issue #99)
    # addressed for the `forecast` array, just never applied here. This
    # class's own extra_state_attributes() below returns up to 20
    # recent_errors + 20 recent_warnings (each a full log message) plus a
    # subentry_status entry for every forecastable subentry (18+ on the
    # reference household) -- real, current diagnostic state, not
    # something worth keeping in long-term statistics. never_trained
    # stays recorded: small, cheap, and genuinely useful in history.
    #
    # nimbus issue #362 (Mark Purcell): the original reasoning above for
    # ALSO excluding generated_at ("small and cheap either way") missed
    # the actual bug -- size was never the problem, CHURN was. This
    # entity polls every 30s with no should_poll=False, and
    # generated_at is recomputed fresh on every single read regardless
    # of whether anything else changed. Since it wasn't excluded, the
    # attribute dict differed on every poll even when recent_errors/
    # recent_warnings/subentry_status were byte-for-byte identical --
    # firing state_changed and making the recorder write a full,
    # non-dedupable states + state_attributes row every 30s (~2880/day)
    # for no real reason. Excluding it here means a poll with no actual
    # change is finally a genuine no-op for the recorder.
```

## Line 3705: above `_attr_should_poll = False`

```text
    # nimbus issue #302: NOT a temporary diagnostic flag despite an
    # earlier comment here saying so -- confirmed permanent, real,
    # independently-justified hygiene by #302's own closed conclusion.
    # This is a pure push entity (see class docstring) with no
    # update()/async_update() method at all; leaving should_poll at its
    # default True meant HA's own default entity-platform scan interval
    # (15s on a real installed HA core) polled it anyway, force-
    # refreshing a push-only entity that never asked to be polled. Real,
    # observed effect on the reference household's own NUC1: sensor.
    # nimbus_solver_quality_report flapped between its real value and a
    # bare "unknown" (a freshly-constructed, never-updated instance's own
    # class-level attrs only) every ~30s. nimbus issue #362 (Mark
    # Purcell, codebase review) correctly flagged the stale "DIAG:
    # temporary" wording as misleading -- the fix itself was always
    # meant to stay, this is that correction.
```

## Line 3806: above `self._last_updated: float | None = None`

```text
        # Silver `entity-unavailable` (2026-08-23): the sibling
        # NimbusForecastSensor already went through this exact fix
        # (2026-08-22) for a different data path -- these two push
        # sensors were the one place it was never applied, and they're
        # exactly the shape of bug that rule exists for: a plain
        # SensorEntity with no coordinator at all, no `available`
        # override, so a Solver that stops solving (highspy import
        # failure, an unhandled exception, the periodic timer somehow
        # getting cancelled) leaves this entity confidently reporting
        # its LAST successful plan forever, with nothing on screen
        # distinguishing that from a fresh, genuinely-current one.
        # `_last_updated` (monotonic, not wall-clock -- only elapsed
        # time matters here, and monotonic sidesteps any clock-skew/DST
        # edge case) is stamped on every real push; `available` below
        # compares against it.
```

## Line 3822: above `self._was_available: bool = True`

```text
        # Real bug found live (issue #302, 2026-08-31): this used to
        # start at None, with _async_recheck_availability() treating a
        # None baseline as "first tick, nothing to compare against yet"
        # and skipping the write unconditionally. But `available` (see
        # its own property below) is ALREADY, definitionally, True for a
        # freshly-constructed instance (both of its own early-return
        # conditions -- self._state is None, self._last_updated is None
        # -- are true at this exact point in __init__). Starting the
        # baseline at None instead of that already-known True value
        # created a real gap: if enough time has already elapsed BEFORE
        # the very first recheck tick ever fires (confirmed live and in
        # a CI regression test: a test that pushes a value then jumps
        # time forward past _STALE_AFTER_SECONDS in one step, before any
        # recheck tick has run), that first tick already observes
        # available=False -- but the None-baseline branch swallows it as
        # "just establishing a baseline," and the entity never
        # transitions to `unavailable` at all. True is the correct,
        # already-known initial value, not a sentinel -- removes the
        # gap entirely rather than special-casing around it.
```

## Line 3943: above `_LOGGER.debug(`

```text
        # #85 diagnostic (2026-08-23, not yet root-caused): Mark's own
        # empirical trace shows a real solve's rich write getting
        # clobbered to state=None/4-attrs ~2s later, on a v0.73.2
        # install where #83's fix is confirmed present and where the
        # recheck's own _LOGGER.info/.warning calls (unconditional on
        # every write path in that function -- see its own docstring)
        # NEVER fire. That rules out _async_recheck_availability as the
        # source under this code. If the clobber is a call to THIS
        # method with a genuinely empty/None payload, this line proves
        # it directly -- if it's something else entirely (a raw
        # states.async_set() bypassing this method, a second, distinct
        # entity instance never wired through this dispatch table at
        # all), this line's silence during the clobber is itself the
        # proof. DEBUG-level, additive only -- safe to ship without
        # knowing the answer yet.
```
