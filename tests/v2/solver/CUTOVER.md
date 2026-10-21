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

**In scope:** the old files whose only subject is `solver.lp` and
`solver.network` (they may build their inputs from `solver.elements`): 69
files. Files that also test another module stay until that module's own
area is cut over.

**Out of scope, for `elements.py`'s own cutover** (the scores above do not
vouch for `elements.py`): `test_elements_battery_config_validation.py`,
`test_elements_adequacy_windows_validation.py`,
`test_elements_thermal_load_config_validation.py`.

**Out of scope, not solver tests at all** (they import neither `lp` nor
`network`; an earlier scan admitted them on text, not imports):
`test_settlement_capture_timing.py` (the standalone writer script's timing),
`test_docs_writer_function_set_drift.py` and
`test_1411_coverage_compare_matches_requalified_lines.py` (repo gates).

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
| `test_solver_lp_lex_tie_slack.py` | #773 | `test_solver_contracts::test_the_lex_tie_slack_is_the_solver_tolerance_never_a_price`; behaviour: `test_lp_paths::test_the_lexicographic_solve_reaches_the_true_lexicographic_optimum` | planted the slack at 1e-3 (a price): old fails, the new contract and four v2 behaviour tests fail; planted it at 0 (cuts off its own optimum): old and the new contract fail, no behaviour test can (float drift of that size needs a production-sized model) |
| `test_solver_lp_status_and_time_limit.py` | #356 | `test_solver_contracts::test_an_unfinished_solve_is_an_error_never_infeasible`, `::test_a_proved_outcome_keeps_its_own_name`, `::test_every_solve_carries_the_time_limit` | planted every unfinished status reported as `infeasible`: old fails, the new status test fails; planted no time limit: old and the new time-limit test fail |
| `test_solver_lp_sweep_cost_ranging.py` | #676 | `test_solver_contracts::test_the_ranging_sweep_reports_each_steps_exact_interval`, `::test_the_ranging_sweep_refuses_without_a_basis_or_a_known_variable`; `test_lp_properties::test_every_ranging_sweep_step_holds_inside_its_own_interval` | planted up reading the down interval, no restore, and out-var reading in-var: old and new fail on each. The new property's basis-change names differ from the old example's (solar at or below demand there is export in, import out), measured, not assumed |
| `test_1480_plan_reports_lp_only_tiebreak_costs.py` | #1480, #731, #692 | **split, not deleted.** `TestTheLossTiebreakIsReported`: `test_plan_features::test_the_loss_tie_break_costs_what_its_rate_says`, `::test_charge_earliness_costs_what_its_rate_says` (now drawing the budget, zero included). `TestTheScorerCountsEveryLpOnlyTerm` tests the quality report's wiring and stays for the quality area | planted earliness ignoring its budget argument: old and new fail (v2 missed it before the budget was drawn) |
| `test_1576_period0_anchors_to_dispatched.py` | #1576, #635, #1417 | `test_solver_contracts::test_period_0_holds_what_was_dispatched`, `::test_only_a_recent_period_0_anchors_to_the_dispatched_value` | planted plain containment for period 0 (the original bug): old and new fail; planted period 0 always anchored to old period 0 (stale too): old and new fail |
| `test_lp_pin_binaries_integrality_diagnostic.py` | #773, #616 | `test_solver_contracts::test_pinning_fixes_each_binary_at_its_rounded_value`; `test_lp_paths::test_pinning_warns_only_beyond_the_integrality_tolerance`, `::test_pinning_no_binaries_warns_of_nothing` | planted every column relaxed: old and new fail; planted the warning skipping the pin: old and new fail |
| `test_solver_lp_pin_resolve_tolerance.py` | #773 | `test_solver_contracts::test_highs_accepts_a_mip_point_looser_than_an_lp_one`, `::test_the_pin_resolve_widens_the_lp_tolerance_for_its_block_only`, `::test_a_solver_refusing_option_calls_never_breaks_the_solve` | planted narrowing as well as widening: old and new fail; planted no restore: old and new fail |
| `test_solver_export_bonus_tiebreak.py` | household 20 Aug 2026 (#355, #732) | `test_solver_contracts::test_a_flat_export_bonus_is_claimed_in_one_block_at_the_end`; `test_plan_features::test_the_export_bonus_is_claimed_on_real_export_within_its_cap`, `::test_the_export_bonus_cap_resets_at_each_calendar_midnight` | planted no nudge, and the nudge reversed (earliest-preferred), in the per-day branch: old and new fail; planted an economic-size nudge (1e-2): old fails, two v2 bonus tests fail. The no-timestamp fallback branch's nudge is checked by neither set |
| `test_solver_adequacy_marginal_cost.py` | #483, #476, #482, #613 | `test_solver_contracts::test_an_adequacy_load_costs_its_energy_at_the_marginal_price` (and `test_network_contracts::...profit...`, #1958) | planted the cost summed without hours, and the dual not divided by hours: the new property fails on both, the old file on neither (it uses hourly periods only) |
| `test_adequacy_placement_vs_shadow_price.py` | #769, #662 | `test_solver_contracts::test_narrowing_an_adequacy_window_never_makes_the_plan_cheaper` | a demonstration (an unused cheaper period is not a missed saving), not a regression pin: both it and the new property check that the solve is optimal over its window. No bug in this code would make a narrower window cheaper short of a solver defect, so there is no plant to show; the property generalises the one fixture to generated sites |

## Still to check

The other 50 files, smallest first. Nothing is deleted until every row is
checked and the branch coverage comparison is posted on #1837.
