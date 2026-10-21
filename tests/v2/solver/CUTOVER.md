# Solver area cutover ledger (#1837)

The design (#1837, 18 Oct): an old file is deleted only when the new layer
matches or beats the old on the area's mutation score and branch coverage,
**and** each old file's checks are covered by a named `tests/v2` test whose
docstring carries the old file's issue numbers, so `git grep '#NNN'` still
finds them.

Mutation scores (fixed runner `5acb2c20`, same mutants for both sets):

| module | old | new |
|---|---|---|
| `lp.py` (seed 7078, 20 mutants) | 13/20 | 18/20 |
| `network.py::_build_plan_once` (seed 1837, 30 mutants) | 22/30 | 24/30 (#1957) |

**In scope:** the 75 old files whose only subject is `solver.lp`,
`solver.network` and `solver.elements` (605 tests). Files that also test
another module stay until that module's own area is cut over.

**How each row is checked:** read the old file in full; name the v2 test
that asserts the same thing (or write one); for a new guard, plant the bug
the old file was written for and show both sets fail.

## Checked

| old file | issues | covered by | how it was checked |
|---|---|---|---|
| `test_soft_soc_penalty_covers_salvage.py` | #328, #1837 | `test_plan_invariants::test_every_flow_and_soc_within_bounds` | the property that found this bug (salvage drawn to $0.30/kWh, SoC within bounds) |
| `test_spike_override_at_the_floor.py` | #567, #1837 | `test_plan_features::test_a_spike_near_min_soc_discharges_only_what_is_left` | draws the margin above Min SoC from 0 (at the floor) up, rates above and below what is left |
| `test_1837_tiny_coefficients_reach_highs_dropped.py` | #1837, HAEO #501/#511 | `test_solver_contracts::test_a_tiny_row_entry_is_dropped_and_changes_nothing`, `::test_a_row_whose_every_entry_is_dropped_keeps_its_bounds`, `::test_a_tiny_cost_beside_real_ones_still_takes_the_phased_solve` | planted the original bug (a raise on an entry at or below 1e-9): the old file fails 6 cases, the three new tests fail. Removing `_add_row`'s own drop, or `_dropped_contribution`'s shift, is caught by neither set: HiGHS drops the entry itself through `addRow` |
| `test_solver_infeasible_plan_array_aliasing.py` | #356 | `test_solver_contracts::test_a_plan_that_did_not_solve_shares_no_array` | planted two fields sharing one array: the new test fails |
| `test_lp_dump_retry_after_transient_failure.py` | #773, #1058 (#1074) | `test_solver_contracts::test_a_failed_dump_never_blocks_the_next_one` | planted the original bug (the phase marked before the write): old and new fail |
| `test_1761_unfundable_block_still_solves.py` | #1761, #390, #773 | `test_plan_features::test_a_short_p2p_block_costs_its_penalty_on_every_missing_kwh` (feasible, short kWh priced above any way out), `test_solver_contracts::test_a_committed_block_is_an_exact_ceiling` (never above the rate; exact when fundable) | planted export above the block (ub = export limit): old and new fail |
| `test_risk_aversion_is_solar_only.py` | household 12 Oct 2026 | `test_solver_contracts::test_the_risk_dial_leans_solar_and_never_load` | planted load leaning to its upper band: old fails 3, new fails |
| `test_solver_soft_soc_penalty_grid_invariance.py` | #338 | `test_solver_contracts::test_the_same_scenario_costs_the_same_on_any_period_length` | planted the penalty per period (x hours): old fails 3, new fails |
| `test_solver_lp_sweep_cost.py` | #494, #489 | `test_solver_contracts::test_the_cost_sweep_re_solves_each_cost_and_puts_it_back`, `::test_the_cost_sweep_refuses_without_a_basis_or_a_known_variable` | planted no restore after a sweep: old and new fail (the new one only once its export check came straight after a sweep ending on the other side of the export price; it missed it first); planted no `keep_basis` check: old and new fail |

## Still to check

The other 66 files, smallest first. Nothing is deleted until every row is
checked and the branch coverage comparison is posted on #1837.
