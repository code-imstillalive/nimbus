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
"""

from __future__ import annotations

MULTIPLY = "multiply"
DELTA = "delta"

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
    },
    "guests": {
        # "bigger HWS target, earlier deadline" -- the target half.
        "deferrable_target_kwh": (MULTIPLY, 1.3),
        # 5c baseline -> 8c, per the same steer as `away` above.
        "deferrable_value_per_kwh": (MULTIPLY, 1.6),
        # ...and the deadline half, as urgency rather than clock maths.
        "deferrable_shortfall_price": (MULTIPLY, 1.5),
    },
    "economy": {
        "deferrable_shortfall_price": (MULTIPLY, 0.75),
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
        # these tables is a price, an energy target or a count, none of
        # which has a meaningful negative.
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


def apply_to_load_config(data: dict, mode: str | None) -> tuple[dict, dict[str, float]]:
    """The per-load seam. Call at the end of
    `_resolve_controllable_load_tuning()`, after the live `number.*`
    overlay, so the preset scales the value actually in force."""
    return _apply(data, LOAD_PRESETS, mode)
