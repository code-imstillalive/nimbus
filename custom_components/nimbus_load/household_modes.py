"""Household-mode presets: `select.nimbus_household_mode` applied to the
levers that already exist.

nimbus issue #485. Mark Purcell's own steer, which reversed two earlier
proposals and is the whole design constraint here:

    "This shouldn't result in dozens of new entities or wizard
    configuration items... These modes should have preset values for the
    existing levers, not generate new levers."

So there are **no new entities and no new wizard fields**. `select.
nimbus_household_mode` (already shipped, v0.94.298) is the only control,
and everything below is a table in code applied at solve time.

## Relative, never absolute

Every preset is a **multiplier or delta on the household's own configured
value**, never a number this module invents. A household that has spent
weeks tuning `deferrable_target_kwh` to 4.0 keeps that as its baseline;
`guests` asks for 30% more of *their* number, not 5.2 kWh of Nimbus's.

That is a correctness property, not a style preference: an absolute would
silently discard real tuning the moment someone switched modes, and would
be wrong by a different amount on every install.

## `home` is the identity transform

`home` has no entry in either table, so it applies nothing. The mode
select defaults to `home`, which means **nothing changes on any install
until someone deliberately switches** — this issue's own second
acceptance criterion ("a mode with no override for a field behaves
byte-identically to `default`"), satisfied structurally rather than by
matching numbers.

An unknown mode string, or `None` from an install whose select entity has
not come up yet, is also identity. Failing open is right: a mode nobody
recognises must never silently reshape a real dispatch.

## Two seams, because one is not enough

The obvious reading of "one solve-time resolution step" does not work,
and the gap is structural:

    _resolve_controllable_load_tuning()   2 call sites -- a real chokepoint
    solver_battery_min_soc_percent        read at 4 sites via _cfg_num(cfg, ...)
    solver_degradation_cost_per_kwh       read at 3 sites via _cfg_num(cfg, ...)

The battery/solver levers are not funnelled through any resolver — each
site reads `cfg` directly at the point of use. A preset applied only at
the load chokepoint would reach the load levers and silently miss every
battery one. So `apply_to_solver_config()` runs once on `cfg` itself,
immediately after it is built and before any consumer reads it.

`cfg` is a copy fetched *from* `sensor.nimbus_solver_config`, so a moded
value lives only inside that one solve and does not change what the
sensor reports. An INFO log names every lever a mode moved, per solve,
which is the honest record of what happened.

## The table is the part most open to change

The mechanism is settled; these specific factors are a starting point
drawn from this issue's own words ("away for a week: no HWS deadline,
pool pump at a maintenance quota... guests: bigger HWS target"). They are
deliberately modest, and they are one dict — changing them needs no
mechanism change at all.

Two levers are deliberately **absent** where an obvious candidate exists:

- **The SoC floor** (`solver_battery_min_soc_percent`) is untouched in
  every mode. How much reserve a house keeps while empty is a real
  safety and money judgement about that household, not something a
  preset table should assume.
- **Deadline/earliest hours** are untouched. They are clock values with
  wrap-around behaviour (#582 was a real bug in exactly that arithmetic),
  and "no HWS deadline while away" is expressed here as a much lower
  `shortfall_price` instead — the same intent, without touching hour
  maths that has already bitten once.

## The thermal band -- nimbus issue #1314's decision, recorded here

#1314 asked whether a mode may change a SCHEDULE lever (deadline,
thermal band, `enabled`) rather than only a quantity or a value. The
answer taken, and confirmed by @purcell-lab on that issue, is
**option 2 for the band, option 3 for deadline/allowed/enabled**:

- **The band moves; the constraint stays hard.** `network.py` enforces
  `T[deadline_period] >= target_temperature_c` unconditionally, and
  nothing here makes that conditional on a `select`. Only the *operand*
  moves. #774's five-incident guarantee is therefore untouched -- which
  is the whole difference between "soften the bound" and "suspend the
  constraint".
- **`deadline` / `allowed` stay out.** `build_controllable_loads()`
  `continue`s -- drops the load out of the plan entirely for that cycle,
  with a warning and nothing else -- whenever a resolved
  `deadline_period` lands before its `earliest_period`. A preset that
  moved an hour could therefore silently remove a real load from a real
  plan, which is a structurally different failure from scaling a price.
  A household that genuinely wants a mode to move a window writes the
  underlying `number.*` entity from its own automation, where HA's own
  logbook records who did it and when.
- **`enabled` is not a decision at all.** No controllable load has an
  `enabled` field; there is nothing to preset. #485's spec named one
  before the schema existed.
- **`quota_kwh_per_day` likewise.** `CONTROLLABLE_LOAD_KIND_QUOTA` is
  reserved in `const.py` and not selectable, so the quota kind has no
  configured lever for a preset to reach.

### Why the band gets its OWN table

#1314's option 1 had one property worth keeping even though its
mechanism was not taken: *"so the diff between 'scale a number' and
'change a constraint' stays visible at the table, not buried in a
float."* `THERMAL_BAND_PRESETS` is that -- same `_apply()` machinery as
the other two tables, separate name, so a reader can see at a glance
which entries move a constraint boundary.

It also has to be separate for a hard mechanical reason.
`ThermalLoadConfig.__post_init__` raises `ValueError` when
`comfort_floor_c > target_temperature_c` or
`target_temperature_c > max_temperature_c`, and
`build_controllable_loads()` constructs it with no `try` around the
call -- so a band preset that broke either ordering would abort the
WHOLE solve, not just that one load. `_repair_thermal_band()` runs
immediately after the band table and re-establishes both invariants by
clamping, and it runs **only when the band table actually moved
something**, so `home` stays the identity transform rather than gaining
a repair pass it never needed.

### The band deltas are degrees, not factors

A multiplier on a Celsius temperature has no physical meaning -- 0.9 x
55 C is not "10% less heat", it is an arbitrary 5.5 C. So every band
entry is a `DELTA` in degrees on the household's own configured
setpoint. Still relative, same correctness property as the rest of this
module.

**`economy` moves the comfort floor and never the target.** A cost mode
must not quietly deliver a colder tank at the deadline; it may decline
to pay for a mid-day reheat. That asymmetry is deliberate and is pinned
by a test.

**One thing to know before changing the `away` delta:** on a hot-water
tank a setback has a real legionella dimension, and this module cannot
tell a domestic tank from a pool. -5 C off a household's own 60 C
setpoint is a 55 C tank, deliberately chosen to stay inside the range a
real install already runs; a deeper setback is a decision for a
household, not for this table.
"""

from __future__ import annotations

MULTIPLY = "multiply"
DELTA = "delta"

# The two thermal BOUND keys, named rather than repeated: `_repair_
# thermal_band()` and the tests both have to agree with the table on
# exactly which keys are the band.
THERMAL_TARGET_KEY = "thermal_target_temperature_c"
THERMAL_COMFORT_FLOOR_KEY = "thermal_comfort_floor_c"

# Mirrors `solver.elements.ThermalLoadConfig.max_temperature_c`'s own
# default. Duplicated rather than imported, deliberately: this module is
# imported from BOTH the native package path and the flat standalone/cron
# path (see solver_writer.py's own dual `from . import household_modes` /
# `import household_modes`), and it currently has zero imports of its
# own, which is worth keeping. A test asserts the two numbers agree, so
# drift fails CI rather than surfacing as a ValueError mid-solve.
THERMAL_MAX_TEMPERATURE_C = 99.0

# Levers read from `cfg` (the Solver settings bridge). Applied once, to
# `cfg` itself, so every `_cfg_num(cfg, ...)` site picks the value up.
SOLVER_PRESETS: dict[str, dict[str, tuple[str, float]]] = {
    # Nobody home: the battery is the only thing with a job, so let it
    # value its own longevity more than a marginal arbitrage cent.
    "away": {
        "solver_degradation_cost_per_kwh": (MULTIPLY, 1.5),
    },
    # Cost first, comfort second: same posture, stronger.
    "economy": {
        "solver_degradation_cost_per_kwh": (MULTIPLY, 2.0),
    },
}

# Levers read from a Controllable Load's own resolved config. Applied at
# `_resolve_controllable_load_tuning()`, after the live `number.*`
# override has already won over the wizard value — so a preset scales
# whatever the household is actually running, not a stale wizard entry.
LOAD_PRESETS: dict[str, dict[str, tuple[str, float]]] = {
    "away": {
        # "pool pump at a maintenance quota" -- half the daily energy.
        "deferrable_target_kwh": (MULTIPLY, 0.5),
        # Mark Purcell, 2026-09-15, for the pool heater specifically:
        # "turn on when the marginal cost is less than 5c/kWh" at home,
        # 2c away, 8c with guests. `value_per_kwh` IS that rule -- a
        # price-gated load runs exactly where the switchboard's own
        # shadow price is at or below this value (#482). Expressed
        # relative so a household that decides 6c is its real baseline
        # gets 2.4c/9.6c rather than having its own number overridden.
        "deferrable_value_per_kwh": (MULTIPLY, 0.4),
        # "no HWS deadline" -- expressed as a much weaker penalty for
        # missing the target rather than by moving the deadline hour.
        "deferrable_shortfall_price": (MULTIPLY, 0.25),
        # Fewer starts when nobody benefits from the result.
        "controllable_load_max_activations_per_day": (DELTA, -1.0),
        # nimbus issue #1314: the PRICE of a mid-day reheat, paired with
        # the band setback in THERMAL_BAND_PRESETS below. Nobody is home
        # to want a warm tank at 2pm, so the reheat is worth far less --
        # but it stays priced rather than switched off, because a soft
        # priced action is exactly what the LP can trade away safely.
        "thermal_comfort_floor_cost": (MULTIPLY, 0.25),
    },
    "guests": {
        # "bigger HWS target, earlier deadline" -- the target half.
        "deferrable_target_kwh": (MULTIPLY, 1.3),
        # 5c baseline -> 8c, per the same steer as `away` above.
        "deferrable_value_per_kwh": (MULTIPLY, 1.6),
        # ...and the deadline half, as urgency rather than clock maths.
        "deferrable_shortfall_price": (MULTIPLY, 1.5),
        # A house full of guests should never run cold at 3pm -- worth
        # paying more for a mid-day reheat, per Mark Purcell's own table
        # on #485 ("comfort_floor_c/comfort_floor_cost active, and worth
        # paying more for").
        "thermal_comfort_floor_cost": (MULTIPLY, 1.5),
    },
    "economy": {
        "deferrable_shortfall_price": (MULTIPLY, 0.75),
        # Less willing to pay for a mid-day reheat. The DEADLINE target
        # is untouched in this mode -- see the band table below.
        "thermal_comfort_floor_cost": (MULTIPLY, 0.5),
    },
}

# Thermal BOUND presets -- nimbus issue #1314, option 2. Separate table
# because these entries move a constraint boundary rather than scale a
# cost or a quantity, and because the band has an ordering invariant the
# generic path cannot enforce. See this module's own docstring, "The
# thermal band".
#
# Deltas in DEGREES, never factors: a multiplier on a Celsius setpoint is
# physically meaningless.
THERMAL_BAND_PRESETS: dict[str, dict[str, tuple[str, float]]] = {
    # #485's own criterion 1: "away ... drops the pool heater band". The
    # whole band translates down by one setback, so its WIDTH is
    # preserved and the floor cannot cross the target.
    "away": {
        THERMAL_TARGET_KEY: (DELTA, -5.0),
        THERMAL_COMFORT_FLOOR_KEY: (DELTA, -5.0),
    },
    # "Guests: bigger HWS target" -- the band translates up together, for
    # the same reason.
    "guests": {
        THERMAL_TARGET_KEY: (DELTA, 2.0),
        THERMAL_COMFORT_FLOOR_KEY: (DELTA, 2.0),
    },
    # Deliberately the FLOOR only. A cost mode may decline to pay for a
    # mid-day reheat; it must not quietly deliver a colder tank at the
    # deadline the household actually asked for.
    "economy": {
        THERMAL_COMFORT_FLOOR_KEY: (DELTA, -3.0),
    },
}


def _apply(
    values: dict, presets: dict[str, dict[str, tuple[str, float]]], mode: str | None
) -> tuple[dict, dict[str, float]]:
    """Return `(new_values, applied)` — `applied` maps each changed key to
    its new value, for diagnostics.

    Never mutates the input. Returns the SAME dict object when nothing
    applies, so an identity transform is free and provably a no-op.
    """
    table = presets.get(mode or "")
    if not table:
        return values, {}

    out = dict(values)
    applied: dict[str, float] = {}
    for key, (op, operand) in table.items():
        if key not in out:
            # A lever this install does not configure. Skipping is right:
            # inventing a base to scale would be exactly the absolute
            # value this module exists to avoid.
            continue
        try:
            base = float(out[key])
        except (TypeError, ValueError):
            # A non-numeric value (an entity id in an `_entity` variant,
            # say). Leave it alone rather than crash a real solve.
            continue
        new = base * operand if op == MULTIPLY else base + operand
        # A lever is never pushed below zero by a preset -- every key in
        # these tables is a price, an energy target, a count or a
        # temperature setpoint, none of which has a meaningful negative.
        # (A 0 C setpoint is a valid `ThermalLoadConfig`; a negative one
        # is not, and a DELTA on a small baseline is how you get there by
        # accident.)
        new = max(0.0, new)
        if new != base:
            out[key] = new
            applied[key] = new
    return out, applied


def apply_to_solver_config(
    cfg: dict, mode: str | None
) -> tuple[dict, dict[str, float]]:
    """The `cfg` seam. Call once in `main()`, immediately after
    `fetch_solver_config()` and before anything reads `cfg`."""
    return _apply(cfg, SOLVER_PRESETS, mode)


def _as_float(value: object) -> float | None:
    """`float(value)` or `None` -- never raises. An unset optional field
    reads `None` and several config keys hold entity ids."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _repair_thermal_band(values: dict) -> tuple[dict, dict[str, float]]:
    """Re-establish the two orderings `ThermalLoadConfig.__post_init__`
    enforces, by clamping rather than by raising.

    `build_controllable_loads()` constructs `ThermalLoadConfig` with no
    `try` around the call, so a band preset that pushed
    `comfort_floor_c` above `target_temperature_c`, or the target above
    the physical ceiling, would raise out of that function and abort the
    ENTIRE solve -- every load and the battery with it, over one
    temperature. Clamping is the only acceptable behaviour here.

    Copy-on-write, and returns the same object when nothing needed
    repairing, matching `_apply()`'s own contract.
    """
    out = values
    repaired: dict[str, float] = {}

    target = _as_float(out.get(THERMAL_TARGET_KEY))
    if target is not None and target > THERMAL_MAX_TEMPERATURE_C:
        out = dict(out) if out is values else out
        target = THERMAL_MAX_TEMPERATURE_C
        out[THERMAL_TARGET_KEY] = target
        repaired[THERMAL_TARGET_KEY] = target

    floor = _as_float(out.get(THERMAL_COMFORT_FLOOR_KEY))
    if floor is not None and target is not None and floor > target:
        out = dict(out) if out is values else out
        out[THERMAL_COMFORT_FLOOR_KEY] = target
        repaired[THERMAL_COMFORT_FLOOR_KEY] = target

    return out, repaired


def apply_to_load_config(data: dict, mode: str | None) -> tuple[dict, dict[str, float]]:
    """The per-load seam. Call at the end of
    `_resolve_controllable_load_tuning()`, after the live `number.*`
    overlay, so the preset scales the value actually in force.

    Two tables, in order: the quantity/value levers, then the thermal
    BOUND levers (nimbus issue #1314). The band's ordering repair runs
    only when the band table actually moved something -- so `home`, an
    unset mode and an unrecognised mode all still return the caller's
    very own dict object, unrepaired and untouched.
    """
    out, applied = _apply(data, LOAD_PRESETS, mode)
    out, band_applied = _apply(out, THERMAL_BAND_PRESETS, mode)
    if band_applied:
        out, repaired = _repair_thermal_band(out)
        # The repaired value is the one that reaches the LP, so it is the
        # one worth logging. `repaired` therefore overwrites rather than
        # being reported alongside the pre-clamp figure.
        band_applied = {**band_applied, **repaired}
        applied = {**applied, **band_applied}
    return out, applied
