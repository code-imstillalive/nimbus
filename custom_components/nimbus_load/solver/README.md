# Nimbus Solver — draft, observation-only

**Status: draft, 2026-08-15. Not wired into anything. Not imported by `__init__.py`,
`coordinator.py`, or `config_flow.py`. Registers zero HA entities, writes nothing
to Modbus/any switch/number entity, never touches a live restart.** This is
intentional and load-bearing, not an oversight — see the architecture sketch's
own explicit scope: *"we will not automate it to control anything... just to
see how it behaves."*

## What's here

- `lp.py` — a small, general-purpose LP solver, pure numpy (two-phase simplex,
  dense tableau, Bland's rule for anti-cycling). No scipy/PuLP/highspy — this
  integration has no C compiler / no confirmed compiled-wheel availability
  inside the HA container it deploys into (the same reasoning `ml/gbrt.py`'s
  own from-scratch GBRT already established for the Forecaster).
- `elements.py` — typed configs for this household's real topology: `Grid`,
  `Battery` (one aggregate, v1 simplification — the 2 real inverters/4 towers
  are not modeled separately yet), `Solar`, `Load`, `SheddableLoad`. Sheddable
  loads are modeled from day one (see the architecture sketch's own §1a) even
  though this household has zero configured today.
- `network.py` — `build_plan()`, the pure function that assembles an LP from
  element configs + a time horizon, solves it, and returns a `Plan`. No I/O.
- `modes.py` — translates a solved `Plan` into this project's own real,
  documented Sungrow mode language (Self-Consume / VPP Discharge / VPP Charge
  / VPP Stop, same 0.05kW dispatch threshold this project's real battery
  automations already use) — purely so a human can compare the solver's shadow
  plan against what the real inverter is actually doing, the same comparison
  shape `monitor_haeo.py` already proved useful for.
- `rolling.py` — Layer 2 (Rolling Refinement): `run_rolling_refinement()`,
  a receding-horizon re-solve loop calling `build_plan()` repeatedly with
  `previous_plan` threaded through automatically, so the three stability
  mechanisms below actually have something to stabilize against. Own SoC
  continuity (with clamping) and solve-failure fallback — see its own
  module docstring for the full design.

## A real, significant LP bug found and fixed (2026-08-16)

While testing `rolling.py` against a real battery scenario, found that
`lp.py`'s two-phase simplex was silently returning a WRONG (non-optimal,
not even reported as infeasible) answer whenever a variable had a
nonzero finite lower bound and the true optimum required it near its own
real upper bound. Root cause: the shifted-variable upper-bound row was
built as `ub - lb` and then shifted a SECOND time by `_expand_row()`'s
own logic, silently capping the variable's real usable ceiling at
`ub - 2*lb` (in shifted terms) — i.e. `ub - lb` in original terms,
instead of the correct `ub`.

This is invisible for any variable with `lb=0` (double-subtracting zero
changes nothing) — which is every single variable in this whole package
except battery `soc` (`min_soc_kwh` is always > 0 by `BatteryConfig`'s
own validation). Every prior test in `test_lp_correctness.py` and every
prior scenario in `test_network_synthetic.py`/`test_stability_mechanisms.py`
happened to either use `lb=0`/`lb=-inf` variables only, or never actually
needed `soc` to approach its true ceiling closely enough for the bug to
flip a loose-threshold assertion — so this went completely undetected
through this project's entire prior solver history until a
high-`min_soc`-relative-to-range scenario (this household's own real
5%-of-capacity min_soc is comparatively small; the bug needed a bigger
min_soc to become numerically obvious) surfaced it directly.

One-line fix: pass the real, unshifted `ub` into the upper-bound row
(not `ub - lb`), letting `_expand_row()`'s own shift-adjustment do the
subtraction exactly once, same as every other row already does. New
permanent regression test in `test_lp_correctness.py` (#8) targets this
exact shape directly. Full `test_lp_correctness.py`/
`test_network_synthetic.py`/`test_stability_mechanisms.py` suites all
re-verified passing after the fix (numeric outputs unaffected at their
existing assertion tolerances — the bug's effect was always there, just
never large enough to flip an existing loose-threshold check).

## Structural degeneracy guards (real, not advisory)

Both `BatteryConfig` and `GridConfig` reject certain configurations *at
construction time*, raising `DegenerateConfigError` — not a warning, not a
clamp, a hard refusal to build the config at all:

- Battery `charge_cost + discharge_cost` below `MIN_CHARGE_DISCHARGE_COST_SPREAD`
  (0.01 $/kWh) — this is the exact configuration shape that produced HAEO's
  own documented, real wash-trade degeneracy (a 100%-efficient, zero-friction
  battery is mathematically a free arbitrage loop, and a linear solver will
  genuinely find and exploit it).
- Battery `charge_efficiency`/`discharge_efficiency` exactly 1.0 (100%) —
  rejected outright, not just discouraged; real efficiency is *also* a natural
  degeneracy guard independent of the cost floor.
- Grid `import_price <= export_price` (within `MIN_GRID_COST_SPREAD`) for any
  period — the same class of degeneracy one hop over (free simultaneous
  import+export).

See the architecture sketch's own §3 for the full reasoning.

## Real bugs found and fixed while building this, tonight

1. **LP solver sign bug**: the Phase-1 infeasibility check read the tableau's
   own `-(objective)` cell directly, without re-negating — every prior test
   happened to have a true Phase-1 objective of exactly 0 (genuinely feasible),
   where the sign doesn't matter, so this went undetected until a
   dedicated genuinely-infeasible test case caught it returning a wrong
   "optimal" answer instead of correctly reporting infeasible.
2. **Efficiency validation bug**: used `<= 1.0` (allows exactly 100%) instead
   of `< 1.0` (rejects it) — directly contradicted the design's own explicit
   decision. Caught by a dedicated test, not discovered later.

Both are documented in-line at the fix site, same convention as the rest of
this project's own commit-message/code-comment history.

## Running the tests

Same pattern as the Forecaster's own local test harness (see this repo's own
`CLAUDE.md`, "Testing" section) — pure numpy + stdlib, zero HA dependency, so
these run directly with a plain Python interpreter, no HA container needed:

```
test_lp_correctness.py        -- 8 tests, hand-verifiable LP answers (a
                                  textbook 2-variable LP, an exact battery
                                  arbitrage case, infeasible/unbounded detection)
test_network_synthetic.py     -- 14 tests, hand-derivable scenarios one layer
                                  up (price arbitrage, solar+load balance,
                                  sheddable-load shortfall handling, degeneracy
                                  guards actually firing)
test_real_household_data.py   -- end-to-end run against REAL solar/load
                                  forecasts and REAL HAEO price data (never
                                  the Battery/Grid power-signal forecasts,
                                  which represent what the EXISTING system
                                  already decided — using those as input would
                                  be circular, not an independent test)
```

## A real finding from tonight's real-data run — and a correction to how it was first described

**Corrected 2026-08-15, same night: the first write-up of this finding wrongly
implied something happened to the real battery tonight. Nothing did. Read
this version, not the impression left by the original PR description.**

`test_real_household_data.py` pulled one genuinely real thing: HAEO's actual
live price *forecast*, snapshotted at 17:44 AEST tonight, which projected a
jump from $0.33 to **$1.45/kWh** starting the very next minute and holding
through roughly 18:30 (this project's own already-documented "HAEO briefly
publishes an intermediate plan spike, then revises down" phenomenon — this
snapshot alone doesn't establish whether that forecast spike was ever acted on
or corrected before it arrived).

Everything else in that test run was **synthetic and hypothetical**:
the battery's starting SoC (`initial_soc_kwh=CAPACITY_KWH * 0.5`, a plain
made-up 50% picked for the test script, not a read of the real battery) and
the resulting "discharged hard, drained ~36kWh" outcome — that was this
untested draft solver's own offline computation, run completely outside HA,
writing nowhere, with zero connection to what the real P2P automation was
doing to the real battery at the same moment. Confirmed directly afterward:
real SoC was **90%** the whole time, `p2p_battery_sell_5pm_midnight` was `on`
and running its own normal, price-independent, fixed-setpoint logic
(`target_kw + house_load` — it doesn't even read import price). The two are
unrelated, and describing them side by side without saying that plainly was a
real mistake, not a nuance — it read as "the real battery did this," which
never happened.

**What the finding actually, honestly shows**: this draft's math correctly
reacts to whatever number it's given, including a still-unsettled forecast
value that may or may not reflect what actually happens 15 minutes later. That
is a real, useful thing to know about a solver with no input-staleness
awareness yet (§8 of the architecture sketch) — but it is a statement about
the code's own behavior on a hypothetical input, not an observation about
tonight's real dispatch. Do not read this section, or a clean test pass in
general, as anything about what actually happened to the real system tonight.

Separately, real live price data also tripped the grid degeneracy guard for
37 of 285 real periods (import price at or below export price) — the test
script works around this by nudging the price for those periods rather than
silently hiding it, and flags it loudly in its own output. Whether this
reflects a genuine, expected market condition (a real negative-spread period
is not physically exploitable by a household meter regardless) or a data-
staleness artifact worth investigating on its own is an open question, not
resolved tonight — the guard may be calibrated correctly for the battery case
and too strict for the grid case; worth a closer look before this solver goes
any further.

## Stability mechanisms (2026-08-16)

Everything above is Layer 1 — correct, but not yet STABLE across repeated
re-solves. Direct user requirement: *"i do not want mistakes... i do not
want dumb algorithm - i want it to be clever and responsive but smart wise
naturally adaptive not chaotic."* Three mechanisms, all in `network.py`
(full design rationale in its own module docstring — not duplicated here):

1. **Plan stability / proximal regularization** (`previous_plan`,
   `proximal_weight`) — a soft L1 penalty (the standard LP linearization of
   an absolute-value cost) on how far each of the 4 real dispatch variables
   deviates from what the previous solve planned for the SAME real moment.
   Breaks genuine economic ties toward continuity instead of an arbitrary
   simplex vertex, without ever distorting a real price/cost-driven decision
   (deliberately tiny relative to any real economic signal).
2. **Rate limiting** (`max_rate_kw`) — a hard cap, not a cost, applied to
   EVERY dispatch variable independently: period 0 bounded against the
   aligned previous-plan value (protects the real dispatch transition at the
   moment of command), and every consecutive pair of periods within the new
   plan itself bounded the same way (protects against a chaotic-looking plan
   even before it's ever dispatched).
3. **Confidence-aware dispatch** (`risk_aversion`, plus new `lower_kw`/
   `upper_kw` fields on `LoadConfig`/`SheddableLoadConfig`/`SolarConfig`) —
   blends a point forecast toward its own pessimistic confidence bound
   (loads lean toward MORE demand, solar leans toward LESS supply),
   proportional to both the real band width and `risk_aversion`. A load/
   solar with no band at all is completely unaffected regardless of
   `risk_aversion`'s value.

**Real alignment gotcha, worth internalizing before touching this again**:
mechanisms 1 and 2 both require matching a new solve's periods against a
previous solve's periods by REAL WALL-CLOCK TIME (`PeriodGrid.start`/
`period_starts`), never by array index — a rolling re-solve's new grid does
not start at the same instant as the previous one, so index-based alignment
would silently compare the wrong periods the moment the two grids diverge
(every re-solve after the first). `PeriodGrid.start` defaults to `None`
(no calendar anchor at all) — every mechanism above is a correctly-skipped
no-op in that case, not an error, so every pre-existing test/caller
continues to work completely unchanged.

**All three are OFF by default** — calling `build_plan()` with none of
these new parameters is byte-for-byte the same bare single-solve LP as
before this section existed. Confirmed directly: the full pre-existing
`test_lp_correctness.py`/`test_network_synthetic.py` suite passes
unmodified against the current `network.py`.

New test file: `test_stability_mechanisms.py` (23 checks) — hand-derivable
scenarios for all three mechanisms, same convention as the other two test
files, including a genuine real-economic-tie scenario (proximal) and a
genuine hard-tradeoff scenario (rate limiting) constructed so the expected
numeric answer can be verified by hand, not just asserted to "look
reasonable."

## Layer 2 — Rolling Refinement (2026-08-16)

`rolling.py`'s `run_rolling_refinement()` is the receding-horizon loop
that actually exercises the three stability mechanisms above — without
it, `previous_plan` never gets threaded from one solve to the next at
all. Standard MPC "solve, act, observe, re-solve" pattern: only period 0
of each individual re-solve is ever treated as the real dispatch
decision (`RollingTick.dispatched_*`); everything the solve planned
beyond period 0 is provisional and gets re-planned fresh next tick.

Verified two ways:
1. **Mechanics** (`test_rolling_refinement.py`, small synthetic
   scenarios): SoC genuinely continues tick-to-tick rather than resetting
   to whatever the input provider statically returns; a SoC carried
   forward outside a tick's own (possibly-shifted) `min_soc`/`max_soc`
   window clamps correctly instead of crashing (a real, legitimate case —
   this project's own real household already runs automations that shift
   min_soc/max_soc on a schedule during the day); an infeasible tick
   freezes the last known-good dispatch rather than crashing or inventing
   a value, and `previous_plan` correctly stays anchored to the last REAL
   optimal solve, not the failed one's own zero-filled placeholder.
2. **The actual point, against REAL data**: ran a real solar/load/price
   forecast (same "genuinely independent inputs, never the Battery/Grid
   power-signal forecasts" discipline as the Layer-1 real-data test)
   through 40 re-solves spanning today's real 5pm P2P price step, once
   with the stability mechanisms off and once on. Total dispatch
   variation (sum of |consecutive tick-to-tick deltas|) was measurably
   lower with the mechanisms on, at a bounded, non-catastrophic real
   economic cost — the actual, concrete demonstration of what this whole
   stability layer buys in a realistic repeated-solve scenario, not just
   an argument for why it should.

## Deliberately not yet built

- Layer 3 (Safety Envelope).
- Every mechanism from the architecture sketch's own §8 (staleness checks,
  solve-failure fallback beyond the simple freeze-last-dispatch already in
  `rolling.py`, circuit breaker, external watchdog) — see the real finding
  in the earlier real-data section for exactly why these matter,
  demonstrated against real data, not just argued for in the abstract.
- Per-inverter/per-tower battery modeling (v1 deliberately treats the 2 real
  inverters as one aggregate).
- Any wiring into a live coordinator/entity/config_flow at all.
