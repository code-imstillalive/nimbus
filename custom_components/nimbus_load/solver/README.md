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

## Deliberately not yet built

- Layer 2 (Rolling Refinement / MPC) and Layer 3 (Safety Envelope) — this is
  Layer 1 (Daily Plan) only, the single-solve core mechanism the other two
  layers would be built on top of.
- Every mechanism from the architecture sketch's own §8 (staleness checks,
  solve-failure fallback, circuit breaker, external watchdog) — see the real
  finding above for exactly why these matter, demonstrated against real data,
  not just argued for in the abstract.
- Per-inverter/per-tower battery modeling (v1 deliberately treats the 2 real
  inverters as one aggregate).
- Any wiring into a live coordinator/entity/config_flow at all.
