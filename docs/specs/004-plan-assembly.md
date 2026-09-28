# Spec 004: `solver_plan.py` — plan assembly (cfg → elements.\*Config → build_plan)

Status: implemented (#1303, Phase 4). Approved at #1380; **see
"Implementation findings" at the end of this file before trusting the
Interfaces, Façade decision or Acceptance sections — five of their claims were
measured wrong, and three requirements from this spec's own review never
reached this file.**
Plan: docs/architecture/tech-debt-plan.md; phase: #1303 (Phase 4)
Code cited at: `4bec705`

## Responsibility

Phase 4 (#1303) moves the plan-assembly glue currently inlined in `main()` —
the code that takes the outputs of every `solver_inputs/*` fetcher plus `cfg`
and builds the `elements.*Config` objects `network.build_plan()` consumes,
then calls it — into a new module, `solver_plan.py`.

Unlike Phases 1–3, this is not a relocation of an existing named function:
`main()` has no `assemble_plan()` to grep for. The scope is a specific,
contiguous span of `main()`'s own body: `solver_writer.py:13647`–`:13890`
(244 lines) — from the first `elements.BatteryConfig(...)` construction
through the debug log line immediately after `network.build_plan()` returns.
`main()` is currently 886 lines (`:13146`–end of file, `14031`; the plan's
own #1303 body cites 841 lines at a `d5b044b`-era measurement — cited fresh
here, not trusted).

`#1303`'s own issue names this "medium-high risk": *"the seam every other
input... feeds into. It's the first phase where a mistake could produce a
plan that's subtly wrong... without any single test catching it in
isolation."* This spec's own measurement confirms why: unlike Phases 1–3's
targets, this span has no discrete, already-separable boundary — it's the
convergence point of five upstream input sources plus a dozen independent
`cfg` reads.

## Prior art

Not applicable, same as specs 000–003 and the plan's own header — pure
internal module organisation. `ControllableLoadConfig`, `BatteryConfig`,
`GridConfig`, `SolarConfig`, `LoadConfig` and their assembly into a call to
`build_plan()` were each separately checked against EMHASS/HAEO when
originally designed; this spec moves the existing, already-justified
assembly code verbatim, it introduces no new mechanism.

## A real prior finding this spec must not re-litigate by accident

`solver_writer.py:13618`–`13626` (immediately before the span this spec
targets) contains a comment from #735 stage 5 (2026-09-15), the last time
anyone measured this exact code for extraction:

> *"The element construction immediately below is DELIBERATELY not extracted
> with it [the SoC-envelope slice, 5 inputs/4 outputs, already in
> `solver_inputs/battery_soc.py`]: the same measurement puts it at 27 inputs,
> which would be a worse call site than the inline code."*

That is this spec's own target. #1303 was filed without a citation to this
comment or the #735 worklog entry, so it's worth being explicit: **Phase 4 is
not a fresh green-field decision, it is a re-opening of a call #735 already
made and documented in the code it's about to move.** This spec re-measures
the same span with today's tooling (below) rather than trusting either the
13-day-old "27" figure or assuming #1303's framing supersedes it, and finds
the two independent measurements converge — see Measured cost.

## Measured cost

AST-based free-variable analysis of `solver_writer.py:13647`–`13890` (244
lines): every name the block reads that is (a) assigned earlier in `main()`
— i.e. a genuine call-site input, not a re-derivable local — and (b) not a
module-level function/class/constant/import.

```
30 real inputs, current main() (4bec705):
  capacity_kwh, cfg, charge_cost, charge_discharge_efficiency,
  discharge_cost_arr, export_bonus_price, export_limit_kw, export_price,
  export_price_lower, grid_times, import_limit_kw, import_price,
  import_price_upper, initial_soc_kwh, load_kw, load_lower_kw, load_upper_kw,
  max_charge_kw, max_discharge_kw, max_soc_kwh_val, min_soc_kwh_val,
  n_periods, now, p2p_recent_volume_kwh, period_hours_arr, salvage_value,
  solar_kw, solar_lower_kw, solar_upper_kw, spike_override_kw
```

Four of these (`initial_soc_kwh`, `min_soc_kwh_val`, `max_soc_kwh_val`,
`charge_discharge_efficiency`) are `main()`'s own unpacked fields of a single
`SocEnvelope` object (`solver_inputs/battery_soc.py`'s `resolve_soc_envelope()`
return value, held as `_soc_envelope` at `:13627`) — `main()` unpacks it into
four scalars immediately (`:13634`–`13637`) rather than keeping the object.
Two of the four (`initial_soc_kwh`, `charge_discharge_efficiency`) are read
again later, past this span, by the `publish_plan()` call (Phase 5's own
territory) — confirmed by grep, not assumed — so `_soc_envelope` itself must
stay alive in `main()` regardless of what this spec does. Passing the object
itself into the new function instead of the four unpacked scalars is a
zero-risk simplification (same values, one name instead of four) that costs
nothing at the Phase-5 call site. **With that one substitution, the span
needs 27 named inputs, not 30** — converging, independently, on the same
figure #735 stage 5 measured 13 days ago. That convergence is worth taking
as a cross-check that both measurements are counting the same thing
correctly, not as evidence either one saves work here.

`load_kw`/`load_lower_kw`/`load_upper_kw` are similarly three of twelve
fields `main()` unpacks from a `LoadArrays` object (`:13220`–`13232`) —
**not** bundled back for this spec, because the other nine fields
(`summed_18_now_kw`, `load_forecast_source_used`, etc.) are consumed by
unrelated parts of `main()` (diagnostics, `publish_plan()`), so passing the
whole `LoadArrays` object would hand this span nine inputs it doesn't use,
trading one honest-but-wide signature for a differently-wide one. Same
reasoning applies to `PriceArrays` (`_prices` at `:13312`) for
`export_bonus_price`/`p2p_recent_volume_kwh` — most of `PriceArrays`'s own
fields are pre-blend values `main()` itself further transforms (into
`import_price`/`export_price`/`import_price_upper`/`export_price_lower`)
*before* this span starts, so the bundle doesn't even contain the values
this span actually reads. Re-plumbing either of those is a real, separate,
larger question this spec does not attempt — see Non-goals.

`solar_kw`/`solar_lower_kw`/`solar_upper_kw` have no bundle to reduce to at
all: `build_solar_arrays()` (`solver_inputs/solar.py:213`) returns a plain
3-tuple, unlike `build_load_arrays()`/`build_price_arrays()`/
`resolve_soc_envelope()`, which all return a `@dataclass(frozen=True)`
result. Introducing a `SolarArrays` dataclass to match the other three would
be a genuine, independent quality improvement (making the four input
functions consistent), but is `solver_inputs/solar.py`'s own module to
change, not this spec's — noted under Non-goals rather than folded in here.

`python3 tests/analyse_module_dependencies.py --functions
"build_controllable_loads,resolve_price_spike_override,fetch_p2p_fixed_export_kw,midnight_boundary_period_indices,terminal_value_breakpoints_for,load_previous_plan,RISK_AVERSION"
--callers` (real, current `main`):

```
production callers outside the module : 4
monkeypatch sites                     : 0
test files touching any target        : 8

name                              prod callers                              lines
build_controllable_loads          0  (3 test files)                         Phase 3's own target (spec 003), not moved here
resolve_price_spike_override      0  (1 test file)                          69   [:4442]
fetch_p2p_fixed_export_kw         3  [solver_inputs/prices.py,               already in solver_shared.py (Phase 2a) --
                                       solver_reports/counterfactual.py,      not this module's concern
                                       solver_reports/quality.py]
midnight_boundary_period_indices  0  (0 test files)                         30   [:1870]
terminal_value_breakpoints_for    1  [solver_reports/counterfactual.py]     27   [:1902]
load_previous_plan                0  (1 test file)                         77   [:5444]
RISK_AVERSION                     0  (0 test files)                         module constant

A compatibility facade IS load-bearing for: fetch_p2p_fixed_export_kw,
terminal_value_breakpoints_for.
```

`fetch_p2p_fixed_export_kw` already lives in `solver_shared.py` (spec 001) —
this span calls it, doesn't own it; no facade decision for this spec at all,
just a downward import. `terminal_value_breakpoints_for` still lives in
`solver_writer.py` with one external caller reaching it via the `sw.`
deferred seam (`solver_reports/counterfactual.py`) — see Façade decision for
what moving it means. `resolve_price_spike_override`'s own call
(`:13644`–`13646`) sits three lines *before* this spec's own span boundary —
checked, not assumed, before drawing the line there: `price_spike_active`
(the call's second return value) is read again at `:13984` inside
`publish_plan()`'s own argument list, past this span, so the call itself
stays in `main()`; only its first return value, `spike_override_kw`, crosses
into this span as input #16 above (already counted).

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| `BatteryConfig`/`GridConfig`/`SolarConfig`/`LoadConfig`/`PeriodGrid` construction from live inputs | `main()` `:13647`–`13725` | every golden scenario — this span has no gating branch and no early `return` anywhere in `main()` before it (checked directly: zero `return` statements in `main():13146`–`13647`), so every scenario that reaches a successful solve executes it unconditionally |
| Fleet assembly (`home` + `battery_participant`s), `fleet_capacity_kwh` (#569) | `:13804`–`13841` | same — plus `tests/golden/scenarios_native.py`'s multi-battery scenarios specifically |
| `network.build_plan()` invocation with every solve-tuning dial (`risk_aversion`, `proximal_weight`, `smoothness_weight`, `battery_charge_earliness_budget_kw`, `offer_curve_enabled`, `flex_signals_enabled`, `calibrated_objective_enabled`) | `:13755`–`13885` | `tests/test_solver_writer_smoothness_and_proximal_weight_wiring.py`, `tests/test_p2p_bonus_is_gated_to_committed_periods.py`, `tests/test_p2p_bonus_pricing_reaches_the_oracle.py` — all patch `solver_writer.network.build_plan`/`elements.GridConfig` directly (see Façade decision: these patches survive this move unmodified) |
| Infeasible-plan handling | `:13866` onward | `tests/test_main_infeasible_plan_status_line.py` (drives real `main()`, patches `solver_writer.network.build_plan` with `_fake_infeasible_build_plan`) |
| `#757` diagnostic logging (`all_batteries` after merge, `plan.batteries` after solve) | `:13820`–`13824`, `:13886`–`13890` | referenced by name in `tests/test_773_dump_location_note.py`'s own docstring per the 2026-09-27 IV&V pass; no dedicated assertion found — noted, not invented |

Spec 000's own table measured `main()` at 94.4% golden-master coverage
overall (not 100%); the gap is elsewhere in `main()`, not in this span —
confirmed by the zero-early-return check above, which means this specific
244-line block is exercised by every scenario that reaches a real solve,
i.e. effectively all of them.

## Interfaces

One new function, `solver_plan.py` — layer 3 in the plan's own layer map
(`solver_plan.py`, `solver_publish.py`, `solver_dispatch/`, above
`solver_inputs`/`solver_reports`, below the `solver_writer.py` façade).
Imports downward only: `solver.elements`, `solver.network`,
`solver_inputs.battery_soc.SocEnvelope` (type only — the object itself is
constructed by the caller and passed in), `solver_inputs.extra_batteries`,
and, once Phase 3 lands, `solver_inputs.controllable_loads` (until then,
`solver_writer.build_controllable_loads` via facade — see Migration).

```python
# solver_plan.py — layer 3

def assemble_and_solve_plan(
    cfg: dict,
    *,
    now: datetime,
    grid_times: list[datetime],
    period_hours_arr: list[float],
    n_periods: int,
    soc_envelope: SocEnvelope,          # was 4 unpacked scalars -- see Measured cost
    capacity_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    charge_cost: float,
    discharge_cost_arr: list[float],
    salvage_value: float,
    spike_override_kw: float,
    import_price: list[float],
    export_price: list[float],
    import_limit_kw: float,
    export_limit_kw: float,
    export_bonus_price: list[float],
    p2p_recent_volume_kwh: float,
    import_price_upper: list[float] | None,
    export_price_lower: list[float] | None,
    solar_kw: list[float],
    solar_lower_kw: list[float],
    solar_upper_kw: list[float],
    load_kw: list[float],
    load_lower_kw: list[float],
    load_upper_kw: list[float],
) -> network.Plan: ...
```

27 named parameters — the "worse call site" #735 stage 5 flagged, accepted
here for the reason given there: this is the true, measured contract, and no
consolidation exists that doesn't either (a) hand the callee fields it
doesn't use (`LoadArrays`/`PriceArrays`, whose other fields belong to
unrelated parts of `main()`) or (b) invent a new bundling type for values
that have no other reason to travel together (grid/solar/load arrays are
independent signals, not one entity's fields the way a battery's SoC bounds
are). **This is the one structural call this spec asks #1298/the reviewer
to confirm before implementation** — same posture spec 001 took for the
solver_shared.py shape: the alternative (a purpose-built
`PlanAssemblyInputs` dataclass wrapping all 27) is a real option, discussed
below, not silently rejected.

**Alternative considered, not recommended:** a single `PlanAssemblyInputs`
dataclass grouping all 27 fields, constructed once in `main()` and passed as
one object. This does reduce the *call site* to one argument, but doesn't
reduce the real coupling — the new dataclass would need all 27 fields
regardless, `main()` still has to gather all 27 values from five different
sources to construct it, and now there are two things to keep in sync (the
dataclass's own field list and `main()`'s construction of it) instead of
one (the function signature). It also invents a type with no reuse: nothing
else in the codebase would ever construct a `PlanAssemblyInputs` for any
other reason, unlike `SocEnvelope`/`LoadArrays`/`PriceArrays`, which are
each a real sub-system's own natural output shape. Recommendation: keep the
flat 27-parameter signature (all keyword-only, per the signature above, so
call-site argument order can't silently drift) — but this spec explicitly
does not treat that as settled, per the plan's own precedent of deferring
this exact class of decision.

Internally, `assemble_and_solve_plan()` performs, in order (bodies moved
verbatim from `main()`, zero logic change): constructs `periods =
elements.PeriodGrid(...)`; calls `build_controllable_loads(now, grid_times,
n_periods, import_price)` (facade today, `solver_inputs.controllable_loads`
once Phase 3 lands); constructs `battery = elements.BatteryConfig(...)`;
calls `extra_batteries_inputs.build_extra_batteries(periods)` and merges
into `all_batteries`; computes `fleet_capacity_kwh`; constructs
`grid`/`solar`/`loads` configs; resolves the seven solve-tuning `cfg` reads
(`risk_aversion`, `import_price_risk_aversion`, `export_price_risk_aversion`,
`proximal_weight`, `smoothness_weight`,
`battery_charge_earliness_budget_kw`, `offer_curve_enabled`,
`flex_signals_enabled`, `calibrated_objective_enabled`); calls
`load_previous_plan()` and constructs `solve_options`; calls
`network.build_plan(...)`; logs the `#757` diagnostic; returns `plan`.
`main()`'s own remaining body starts from `plan = solver_plan.
assemble_and_solve_plan(cfg, now=now, ...)` and continues unchanged into the
tariff-attribution/`apply_commanded_state_guard()`/`publish_plan()`
sequence (Phase 6/5's own territory, untouched by this spec).

## Façade decision

- **`elements.*`, `network.build_plan`** — no facade question at all. These
  are shared `solver/` package modules every layer already imports; a test
  patching `solver_writer.network.build_plan` (confirmed: `patch.object(
  solver_writer.network, "build_plan", ...)` in
  `tests/test_main_infeasible_plan_status_line.py` and
  `tests/test_solver_writer_smoothness_and_proximal_weight_wiring.py`) is
  mutating the attribute on the shared `network` module object itself, not
  on a `solver_writer`-owned reference to it — `solver_plan.py` importing
  and calling the same `network.build_plan` sees the identical patched
  object regardless of which module made the call. Checked directly against
  the two test files' own patch syntax, not assumed from the general
  pattern.
- **`terminal_value_breakpoints_for`** (27 lines, `:1902`) — moves with this
  spec's own span (it exists only to compute `BatteryConfig`'s
  `terminal_value_breakpoints` field, tightly coupled to the construction
  this spec relocates). Keeps a facade re-export in `solver_writer.py` for
  its one external caller, `solver_reports/counterfactual.py`, which reaches
  it via the `sw.` seam.
- **`midnight_boundary_period_indices`** (30 lines, `:1870`) — moves with
  this spec's own span for the same reason (feeds
  `terminal_value_breakpoints`'s own construction, `:13685`–`13687`). Zero
  external callers, zero monkeypatch sites — no facade needed.
- **`RISK_AVERSION`** (module constant) — moves with this spec; zero
  external references, no facade needed.
- **`resolve_price_spike_override`, `load_previous_plan`,
  `build_controllable_loads`** — **not moved by this spec.** The first two
  because their call sites sit just outside this span's own boundary (see
  Measured cost) and neither is exclusively this span's concern
  (`load_previous_plan()`'s own `PLAN_STATE_PATH` is state-persistence,
  arguably Phase 7's `ha_bridge` territory more than plan-assembly's);
  `build_controllable_loads` because it's Phase 3's own target (spec 003),
  already fully speced — this spec calls it via whatever facade spec 003
  leaves in `solver_writer.py` until Phase 3 actually lands (see Migration
  step 5 and Non-goals).

## Invariants

- `assemble_and_solve_plan()`'s own internal calls to `network.build_plan`,
  `elements.*Config` construction, `build_extra_batteries`,
  `build_controllable_loads` are byte-for-byte the same call shape as
  `main()`'s current inline code — no argument reordered, no default
  changed, no field renamed.
- `solver_writer.terminal_value_breakpoints_for is solver_plan.
  terminal_value_breakpoints_for` — a real test, not a claim (mirrors every
  prior spec's own facade-identity convention).
- The two tests patching `solver_writer.network.build_plan` directly
  (`test_main_infeasible_plan_status_line.py`,
  `test_solver_writer_smoothness_and_proximal_weight_wiring.py`) pass
  unmodified — their own patch target is the shared `network` module, not
  anything this spec moves.
- `import-linter`'s `nimbus-layers` contract gains `solver_plan` as a new
  layer-3 entry, importing only from layer 1/2 (`solver`, `solver_inputs`)
  plus its one late-bound facade read (`_NATIVE_HASS`-style deferred import
  for `build_controllable_loads`, until Phase 3 lands — see Migration).

## Migration

1. Create `solver_plan.py` with `assemble_and_solve_plan()`,
   `terminal_value_breakpoints_for`, `midnight_boundary_period_indices`, and
   `RISK_AVERSION`, bodies/values copied verbatim from
   `solver_writer.py:1870`, `:1902`, and the constant's own current
   definition line, plus the `:13647`–`13890` span reshaped into the
   function body above (parameter list first, then the moved statements
   unchanged apart from replacing `_soc_envelope.<field>` reads with the
   parameter name `soc_envelope.<field>` directly, removing the four
   now-redundant unpacking lines this spec's own Measured Cost section
   argued for).
2. In `main()`, delete the moved span, replace with: keep
   `_soc_envelope = battery_soc_inputs.resolve_soc_envelope(...)` and its
   own two still-needed-later unpacked fields
   (`initial_soc_kwh`/`charge_discharge_efficiency`, for `publish_plan()`'s
   own later call) exactly as today; call `plan = solver_plan.
   assemble_and_solve_plan(cfg, now=now, grid_times=grid_times, ...,
   soc_envelope=_soc_envelope, ...)`.
3. In `solver_writer.py`, add
   `from .solver_plan import terminal_value_breakpoints_for` to the
   existing facade-import block (delete the moved body); `main()`'s own
   remaining code has no reference to `midnight_boundary_period_indices`,
   `RISK_AVERSION`, or `assemble_and_solve_plan` outside the one call site
   in step 2, so no further facade re-export is required for those three.
4. Full-plan golden-output check: run the golden master
   (`tests/test_golden_master.py`) — every scenario's `Plan` object must be
   snapshot-identical, byte-for-byte, pre/post move. This is the real gate
   for this spec, per #1303's own "Method" step 1 and this being the first
   phase where a subtle wrongness could hide from any narrower test.
5. **The `build_controllable_loads` call inside `assemble_and_solve_plan()`
   is a real cross-spec dependency, stated plainly rather than glossed
   over:** if spec 003/Phase 3 has not yet merged when this spec
   implements, `assemble_and_solve_plan()` calls `build_controllable_loads`
   via the existing `solver_writer` facade (the function still lives there
   until Phase 3 moves it) — a downward-then-facade call, not a new
   late-import pattern, since `solver_plan.py` would need
   `from . import solver_writer` deferred inside the function body the same
   way `solver_inputs/*.py` already does for `_NATIVE_HASS`. If Phase 3 has
   already merged, `solver_plan.py` imports
   `solver_inputs.controllable_loads.build_controllable_loads` directly, no
   facade needed. State which case applies at implementation time; don't
   assume.
6. Run the full suite, `ruff`, the Part C gates (`size_ratchet.py`,
   `coverage_compare.py` with a `--moved-code` mapping to `solver_plan.py`,
   `noop_patches.py` over the two `network.build_plan`-patching test files
   plus `test_p2p_bonus_is_gated_to_committed_periods.py`/
   `test_p2p_bonus_pricing_reaches_the_oracle.py`, `assertions_unchanged.py`).
7. Update `nimbus-layers`: add `solver_plan` as its own layer-3 entry; if
   step 5's facade case applies, add one `ignore_imports` exception for it
   (15th total, current count 14 per spec 003's own Invariants section,
   itself unmerged — re-count fresh against whatever `pyproject.toml` holds
   at implementation time, per every prior spec's own standing caution
   about this number moving under concurrent phases).

## Non-goals

- Resolving whether `LoadArrays`/`PriceArrays` should themselves be
  restructured so more of their fields are used together, or whether
  `build_solar_arrays()` should gain a `SolarArrays` dataclass to match the
  other three input functions' own convention. Real, independent
  improvements to `solver_inputs/*.py`, not this spec's call.
- The `PlanAssemblyInputs`-bundling alternative discussed under Interfaces
  — named as a real option, not adopted, pending reviewer decision.
- Moving `resolve_price_spike_override`, `load_previous_plan`, or
  `build_controllable_loads` — the first two sit outside this span's own
  boundary and belong to other concerns (spike detection feeding both this
  span and `publish_plan()`; persisted plan state, arguably Phase 7's); the
  third is Phase 3's own target.
- The tariff-attribution computation (`_flow_decomp_for_tariff`,
  `compute_tariff_attributed_cost`) immediately after this span in `main()`
  — reads `plan` (this spec's own output) but exists specifically to feed
  `apply_commanded_state_guard()`'s timing requirement (Phase 6), per that
  code's own comment; not plan assembly.
- Splitting `assemble_and_solve_plan()` itself into smaller functions once
  moved — it will be a single ~250-line function, over the 60-line ratchet
  threshold, same accepted shape as `build_controllable_loads` in spec 003.
  Decomposing the assembly logic further is a future spec's call, not this
  one's.

## Deploy timing

Not devhub-gated by the plan's own gate table (scoped to Phases 5–6). Pure
internal move: `network.build_plan`'s own call shape, arguments and
defaults are unchanged; every existing monkeypatch site (confirmed above)
resolves identically; the golden master is the real behavioural proof.
"Devhub validation: not claimed — a pure internal refactor with nothing for
a live install to exercise differently than before."

## Acceptance

- [ ] Golden master identical for every scenario (`tests/test_golden_master.py`).
- [ ] Full suite passes; the two `network.build_plan`-patching tests and the
      two P2P-bonus tests pass unmodified.
- [ ] `noop_patches.py` reports zero regressions across those four files.
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [x] mypy: CI's strict `solver/`+`ml/` gate unchanged (6 pre-existing, both
      before and after). The advisory whole-package job moves 207 → 211; see
      finding 6 for the exact breakdown and why it is not suppressed.
- [x] `nimbus-layers`: `solver_plan` promoted from the optional `(solver_plan)`
      entry to a required layer-3 entry. **Zero** new `ignore_imports` —
      contract KEPT at 14 ignored imports, unchanged. Phase 3 had landed, and
      `previous_plan` being a parameter removes the only other upward reach.
- [x] `size_ratchet.py`: `solver_writer.py`'s over-60-line count is
      **UNCHANGED**, not down one — see finding 5. `solver_plan.py` introduces
      exactly one new over-60-line function (`assemble_and_solve_plan`, 294
      lines), documented and accepted per Non-goals.
- [x] `solver_writer.terminal_value_breakpoints_for is solver_plan.
      terminal_value_breakpoints_for` — a real test
      (`test_1303_plan_assembly_extraction.py`).
- [x] The reviewer explicitly confirmed the flat keyword-only signature on
      #1380 ("keep the flat keyword-only signature") over the
      `PlanAssemblyInputs` alternative. The *return* shape was never reviewed
      and is a deviation — see finding 1.

### Added by the #1380 review, and missing from this file until 2026-09-28

The review's verdict was *"approve the shape, add the mutation check to
Acceptance, add the `solver_shared._LOGGER` invariant, and note the
Phase-3-first ordering."* The spec merged without any of the three. Recording
them here as the requirements they always were:

- [x] **Mutation check.** *"Swap one pair within each of the four groups, one at
      a time, and confirm the golden master fails each time. If any swap passes,
      the golden scenarios do not discriminate that input and the extraction is
      not actually covered where it matters."* Keyword-only arguments stop the
      call site's *order* from drifting; they do nothing about a
      **transposition** between two same-shaped arguments, and 18 of the inputs
      sit in such a group. Implemented as
      `test_1303_plan_assembly_extraction.py::TestPlanAssemblyMutationCheck`
      against `assemble_and_solve_plan()` directly rather than by mutating the
      golden master — same property, seconds instead of four golden runs, and
      the failure message names the pair. **Result: 13 transpositions probed, 13
      caught, 0 undiscriminated** — see finding 7 for the two distinct
      mechanisms, which are not what the review assumed.
- [x] **`solver_shared._LOGGER`, never `sw._LOGGER`.** The review, repeating its
      own point on #1370: qualify the moved span's `_LOGGER` as `sw._LOGGER` and
      `test_callers_mode_counts_only_real_references.py:178` fails, because it
      asserts ZERO real `sw._LOGGER` references outside `solver_writer.py`. Both
      of the span's `_LOGGER` uses are `solver_shared._LOGGER`; that file passes
      unmodified, and the form is pinned by a test.
- [x] **Phase 4 must not merge before Phase 3**, or this span's
      `build_controllable_loads` call site changes under it mid-flight. Phase 3
      (#1397, #1399) merged first, so Migration step 5's "already landed" case
      applies: a direct `solver_inputs.controllable_loads` import, no facade.
- [x] **State that this phase rests on behavioural equivalence, not identity.**
      The review: *"a verifier reading specs 002 and 004 side by side will
      otherwise assume the same strength of guarantee is available, and it is
      not."* Stated in `solver_plan.py`'s own module docstring and in the test
      file's.

## Rollback

Revert the single commit. `solver_plan.py` is new and unreferenced outside
`main()`'s own one call site and `solver_writer.py`'s facade re-export of
`terminal_value_breakpoints_for`. No state file format changes, no
migration to undo.

---

## Implementation findings (2026-09-28)

Measured against `main()` at `b181403` (12,853 lines) with AST free-variable
analysis, not carried over from this spec's own `4bec705`-era figures. **The
input measurement reproduced exactly** — the same 30 names, the same 244-line
span, still zero `return` statements in `main()` before it. Three independent
measurements now agree on that set (#735 stage 5's "27", this spec's "30 → 27",
and this one). Phase 3's move did not change it, because the four values
`build_controllable_loads` takes were already inputs.

Everything below is where this spec was wrong.

**1. The return type. `-> network.Plan` is seven outputs short.** The rest of
`main()` reads **eight** names the span binds: `plan`, `grid`, `all_batteries`,
`fleet_capacity_kwh`, `solve_started`, `risk_aversion`,
`import_price_risk_aversion`, `export_price_risk_aversion` — and all but `plan`
feed `publish_plan()`'s own argument list. Checked which failure mode this
produces, because it decides how dangerous the omission was: **none of the eight
is also bound before the span**, so all eight fail loudly with `NameError` at the
first post-span read rather than silently using a stale value. Resolved with a
`@dataclass(frozen=True) PlanAssembly`, matching what `resolve_soc_envelope()`,
`build_load_arrays()` and `build_price_arrays()` already return. **This is a
deviation from what #1380 approved** — the review confirmed the *input* shape and
never examined the outputs.

*A near-miss worth recording:* a careless analyser reports **ten** outputs. `b`
looks span-bound and post-span-read, but the read is
`{b.name: b.capacity_kwh for b in all_batteries}` — a dict comprehension, which
has its own scope in Python 3. Respecting comprehension scope is the difference.

**2. `load_previous_plan()`'s call site is INSIDE the span, not outside it.** The
Façade decision says `resolve_price_spike_override` and `load_previous_plan` are
both excluded "because their call sites sit just outside this span's own
boundary". True of the first (`:12466`, three lines before the span) and false of
the second: `previous_plan = load_previous_plan()` is at `:12561`, inside.

Since `load_previous_plan` stays in `solver_writer.py` (layer 4), calling it from
`solver_plan.py` (layer 3) would be an upward dependency needing an
`ignore_imports` exception. Resolved by making `previous_plan` a **28th
parameter**: `main()` calls `load_previous_plan()` and passes the result. Verified
observationally identical before doing it — everything the hoisted call now
crosses is pure computation (`elements.*Config` construction,
`terminal_value_breakpoints_for`, `midnight_boundary_period_indices`, and
`fetch_p2p_fixed_export_kw`, which was read start-to-finish to confirm it does no
`ha_get`, no `open()`, no HTTP), and the only write to `PLAN_STATE_PATH` is
`save_plan_state()`, which runs later inside `publish_plan()`. One test file
references `load_previous_plan` and it calls the function directly, never through
`main()`.

**The payoff is bigger than avoiding one lint exception: `solver_plan.py` is the
only module #1298 has extracted with NO `_solver_writer()` seam at all.** Nothing
in it reaches back up, so there is no deferred-import trap to get wrong.

**3. `_local` moves with the span, and this spec never mentions it.**
`midnight_boundary_period_indices` calls `_local()`, which lives in
`solver_shared.py:157` — downward, so a plain import, and the no-upward-seam
property survives. Found by ruff F821 after the first build, because this spec's
free-variable analysis covered the *span* and not the two *helper functions* it
also moves. `terminal_value_breakpoints_for` and the span itself have no free
names at all.

**4. `import_limit_kw` and `export_limit_kw` are NOT scalars.** The review groups
them under "4 scalar kW limits". `resolve_envelope_limit_kw()` returns
`list[float]` and `GridConfig.import_limit_kw` is declared
`float | NDArray[np.float64]`, because #493's dynamic operating envelopes made
them per-period arrays — which this spec's own Non-goals section half-remembers.
Annotating them `float` type-checks against nothing real; mypy caught it
immediately. `discharge_cost_arr` is likewise an `NDArray[np.float64]`, not a
`list[float]` — `solver_writer.py`'s own two existing functions annotate it that
way at `:1471` and `:1599`.

**5. `solver_writer.py`'s over-60-line count does NOT drop by one.** Acceptance
asserted it would. Measured: **47 before, 47 after** (scoped runs of
`size_ratchet.py custom_components/nimbus_load/solver_writer.py`). The span was
never its own function, so nothing over 60 lines left the file; `main()` shrank
but is still far over the threshold, and the two moved helpers are 30 and 27
lines. The acceptance item contradicted its own parenthetical, which already said
the span "doesn't count against the ratchet today".

**6. mypy: the strict gate is unchanged; the advisory job moves 207 → 211.**
CI's enforced gate is
`mypy custom_components/nimbus_load/solver custom_components/nimbus_load/ml
--ignore-missing-imports --follow-imports=silent` — **6 findings before, 6 after,
both in `solver/quality_report.py`, neither touching this phase.** (Dropping
`--follow-imports=silent` reports 633/646 instead; CI's own comment explains that
a bare directory path pulls the whole package in via `__init__.py` and produces
"the misleadingly large finding count". Worth knowing before quoting a number.)

The advisory whole-package job goes 207 → 211. Of the eight findings that look
new, **three are the same findings relocated** with the code from
`solver_writer.py` to `solver_plan.py`, and one pre-existing finding is
eliminated. The five genuinely new ones are all one thing: `LoadArrays` and
`PriceArrays` declare **every field as `object`**, so `main()`'s `load_kw`,
`load_lower_kw`, `load_upper_kw`, `export_bonus_price` and
`p2p_recent_volume_kwh` are `object` locals, and passing them into honestly-typed
parameters is an error that could not exist while the code was inline — there was
no typed boundary to cross, and `np.array(x)` accepts `object` happily.

That is upstream looseness becoming **visible**, not looseness introduced here.
Annotating the five parameters `object` would make the count go down while hiding
exactly the information the mutation check depends on, so it is deliberately not
done. **Tightening those two dataclasses' field annotations is a real, separable
follow-up** — and note it is *not* what this spec's Non-goals exclude, which is
restructuring them so more fields travel together.

**7. The mutation check found something better than the review assumed.** All 13
transpositions probed are caught, but by two different mechanisms, and the
difference is worth having:

| group | pairs probed | how a swap is caught |
|---|---:|---|
| load arrays, solar arrays | 6 | **impossible, not merely detected** — `solver/elements.py:1231`'s shared band validation (`lower_kw <= forecast_kw <= upper_kw at every period`) raises `ValueError` at construction |
| price arrays, scalar kW, scalars | 7 | a genuinely different plan: different dispatch, different total cost |

So six of the eighteen same-shaped inputs the review worried about cannot produce
a wrong plan at all — a pre-existing invariant this phase inherits. The two are
asserted separately, so if either mechanism ever weakens the failure says which.

Every swap is preceded by an assertion that the two fixture values actually
**differ**, and the fixture sets both price-risk-aversion dials non-zero on
purpose: with them at 0.0 an `import_price`/`import_price_upper` swap is a true
no-op and the check would pass vacuously.
