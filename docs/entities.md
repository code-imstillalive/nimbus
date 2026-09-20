# Entities reference: Quality, Backtest, Counterfactual, Flex

Companion to `README.md` "What Nimbus publishes". Covers the sub-devices introduced in Family-A (v0.94.24, Quality/Backtest/Counterfactual) and Flex (v0.94.282-284, nimbus issue #496) and the flattened child sensors they expose. For per-load, whole-house rollup, and solver-plan entities, see the README.

Each sub-device is a HA device parented to the hub via `via_device`. The parent entity retains its full attribute payload for backwards compatibility; per-attribute flattened children are additive and let you graph, template, and script against first-class sensors without unpacking `state_attr`.

## Nimbus Quality

Publishes the Efficiency Performance Ratio (EPR) and the cost decomposition behind it. Recomputed every solve tick.

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.nimbus_solver_quality_report` | % | Legacy parent. Full attribute dict; state equals `nimbus_quality_epr`. |
| `sensor.nimbus_quality_epr` | % | Efficiency Performance Ratio, the headline. Positive = capturing value vs baseline. |
| `sensor.nimbus_quality_j_ref` | AUD | Reference 24h cost: what "do nothing" would have cost. |
| `sensor.nimbus_quality_j_ach` | AUD | Achieved 24h cost: what Nimbus's plan actually cost. |
| `sensor.nimbus_quality_j_star` | AUD | Oracle 24h cost: what a perfect-foresight plan would have cost. |
| `sensor.nimbus_quality_value_captured` | AUD | `J_ref - J_ach`. Positive = Nimbus beats do-nothing. |
| `sensor.nimbus_quality_theoretical_maximum_yield` | AUD | `J_ref - J_star`. Total spread between do-nothing and oracle. |
| `sensor.nimbus_quality_regret_dollars` | AUD | `J_ach - J_star`. Canonical name for regret. |
| `sensor.nimbus_quality_tracking_fidelity` | (dimensionless 0..1) | Plan-vs-actual tracking ratio. 1.0 = perfect. |
| `sensor.nimbus_quality_tracking_cost` | AUD | Cost added by deviation between planned and actual dispatch. |
| `sensor.nimbus_quality_achieved_energy_in_kwh` | kWh | Energy charged over the scored window. **Home battery only** — see the scope note below. |
| `sensor.nimbus_quality_achieved_energy_out_kwh` | kWh | Energy discharged over the scored window. **Home battery only** — see the scope note below. |
| `sensor.nimbus_quality_fleet_achieved_energy_in_kwh` | kWh | Energy charged across the **whole fleet** (home battery + every `battery_participant`). |
| `sensor.nimbus_quality_fleet_achieved_energy_out_kwh` | kWh | Energy discharged across the **whole fleet**. |

**Scope note — which figures are fleet-wide and which are not** (nimbus issue [#858](https://github.com/code-imstillalive/nimbus/issues/858)):

Everything else on this device — EPR, `j_ach`, `regret_dollars`, `value_captured`, `uplift_available`, and the SoC-discrepancy fields — is scored across the **whole fleet**: the home battery plus every configured `battery_participant` (EVs, additional packs).

The two `achieved_energy_*` sensors are the exception: they are **home-battery-only**. That is deliberate, not an oversight — their original purpose ([#532](https://github.com/code-imstillalive/nimbus/issues/532)) is letting you compare the energy that moved through your configured `solver_battery_power_sensor` against your configured `solver_battery_capacity_kwh`, to tell a recorder history gap from a sensor that covers more physical storage than the capacity figure describes. That comparison is inherently a home-battery question.

They were added when the scorer was effectively single-battery, so "home" and "fleet" were the same number. Once `battery_participant` support landed they silently diverged. Rather than redefine a field households already diagnose with, both scopes are now published side by side. **On an install with no participants configured the two pairs are equal by construction**, so nothing changed for single-battery installs.

The parent `sensor.nimbus_solver_quality_report` additionally carries `achieved_energy_by_battery`, a per-battery `{in_kwh, out_kwh}` breakdown keyed by battery name — useful on a fleet install for attributing throughput (or a bad reading) to a specific participant without a manual recorder pull. It is a dict, so it has no flattened child sensor.

**`energy_decomposition` — what the controller did differently** (nimbus issue [#1149](https://github.com/code-imstillalive/nimbus/issues/1149)):

The same parent sensor also carries `energy_decomposition`, four rows — `reference`, `achieved`, `oracle`, `achieved_minus_oracle` — each with `charge_kwh`, `discharge_kwh`, `grid_import_kwh`, `grid_export_kwh`. Also a dict, so also no flattened child.

`hourly_regret` answers *which hour cost money*. It cannot answer *what did the controller do differently*, and when the difference is one decision spread over several hours it actively obscures it — `hourly_regret_breakdown()`'s own docstring already warns the buckets can be large in both directions and cancel out.

A real day (19 Sep 2026, reference household): hourly regret ran −$6.17 at 11:00 against +$11.46 across 12:00–15:00 — a gross positive 3.1× the $3.65 net, with the single largest-magnitude hour being a *negative* one. Ranking the hours points at 14:00. The decomposition says it in one line instead: achieved charged 108.1 kWh against the oracle's 83.1 and imported 60.0 against 39.8, while evening discharge differed by only ~3.5 kWh. One over-charge during the flat-price midday window — not five separate hourly findings.

Before this, only the achieved side was derivable and only partly: the two `fleet_achieved_energy_*` figures above publish achieved charge and discharge, but there was no oracle counterpart and no grid figure for either trajectory, so the delta that explains a day could not be computed from this sensor at all. The achieved row reconciles with those two fields by construction (same arrays, same hours) rather than being a second, independently-derived answer.

Sign conventions match the hourly rows: `grid_import_kwh`/`grid_export_kwh` split one signed grid flow, and `charge_kwh`/`discharge_kwh` are summed per battery as magnitudes rather than taken from the fleet net — on a fleet where one battery charges while another discharges, netting is right for a grid identity and wrong for "how much energy moved". The `reference` row is the idle-battery baseline, so its battery figures are 0.0 by construction while its grid figures show what the house alone would have drawn.

**Identity math** (satisfied to rounding):

```
value_captured + uplift_available = theoretical_maximum_yield
J_ref - J_ach + J_ach - J_star   = J_ref - J_star
```

**Reliability: when the report does not stand behind its own EPR** (nimbus issues [#533](https://github.com/code-imstillalive/nimbus/issues/533), [#956](https://github.com/code-imstillalive/nimbus/issues/956), [#1089](https://github.com/code-imstillalive/nimbus/issues/1089), [#1162](https://github.com/code-imstillalive/nimbus/issues/1162)):

EPR is a ratio of reconstructed quantities, and the reconstruction can be wrong. Three independent signals gate it, and **a definite "no" from any one wins**:

| attribute | meaning |
| --- | --- |
| `epr_reliable` | `true` / `false` / `null`. `null` means the SoC half could not be computed at all — genuinely unknown, not fine. |
| `epr_reason` | Why, when it is not `true`. See values below. |
| `epr_denominator_reason` | #1089's: EPR's denominator is not a positive quantity, so the ratio is not a percentage of anything. Kept as its own field rather than folded into `epr_reason`, because a day can hit both and a reader needs to see both. |
| `soc_discrepancy_reliable` / `soc_discrepancy_reason` | Whether the reconstructed SoC trajectory agrees with the real SoC sensor, and why not. `disagreement` means it exceeded `number.nimbus_solver_soc_discrepancy_max_threshold_pct` or the mean equivalent; `out_of_range` means the reconstruction left `[0, 100]`. |
| `regret_reliable` | `false` when `regret_dollars < 0` — achieved priced out cheaper than perfect foresight, which is not a result but proof the comparison was invalid. |

`epr_reason` values:

| value | meaning |
| --- | --- |
| `null` | Nothing wrong (when `epr_reliable` is `true`). |
| `achieved_soc_unreliable:<verdict>` | The SoC comparison failed; `<verdict>` carries `soc_discrepancy_reason`'s own word so the two fields cannot disagree about one finding. |
| `achieved_soc_unverifiable` | The SoC comparison could not be made — no real SoC history. Distinct from the above on purpose: "we checked and it disagrees" is not "we could not check". |
| `oracle_beaten` | Negative regret with the achieved trajectory inside the LP's SoC envelope. |
| `oracle_beaten_achieved_outside_lp_soc_bounds` | Negative regret with the trajectory outside it — sensor or unit trouble rather than a modelling gap. |

**The invariant**: `epr_reliable` not being `true` implies at least one of `epr_reason` / `epr_denominator_reason` is non-null. Before #1162 the SoC half could fire with every one of them reading `null`, so a household was told not to trust the number and given nothing to act on.

**`j_star_path_delta` and `regret_path_delta_share` — how much of the regret is a pricing disagreement** (nimbus issues [#1081](https://github.com/code-imstillalive/nimbus/issues/1081), [#1162](https://github.com/code-imstillalive/nimbus/issues/1162)):

`j_star` is the oracle LP's own objective; `j_star_evaluator` is that same plan repriced through the path `j_ach` takes. They differ because an LP objective carries terms an evaluator does not (soft-SoC penalties, slack, the bonus as a chosen variable). EPR and regret are computed from the **evaluator** figure, so the two are comparable.

`j_star_path_delta` is their difference, and it is exactly the amount the regret moved by that choice:

```
regret_evaluator - regret_raw
  = (j_ach - j_star_evaluator) - (j_ach - j_star)
  = j_star - j_star_evaluator
  = j_star_path_delta
```

`regret_path_delta_share` publishes that as a fraction of the published regret, clamped to `[0, 1]`, magnitudes on both sides (regret can be negative for an unrelated reason), and `0.0` when the paths agree. It is scale-free so one threshold reads the same on a $3 day and a $30 one.

Why it matters: on the reference household's 19 Sep, regret against the raw objective is $1.02 and the published regret is $3.65 — **72% of the headline is the two paths disagreeing about the oracle's own plan**, not the household having dispatched differently. On the dev install the same day reads 90%. A household reading "$3.65 of regret" without this would go looking for a dispatch mistake that is mostly not there.

**`measured_usable_capacity_kwh` and `configured_usable_capacity_kwh` — is the configured pack size right?** (nimbus issue [#1172](https://github.com/code-imstillalive/nimbus/issues/1172)):

| attribute | meaning |
| --- | --- |
| `measured_usable_capacity_kwh` | This pack's usable capacity as the day's own data measures it: energy charged over the day's largest monotonic real-SoC rise, divided by that rise. `null` when no rise is large enough for the division to mean anything, which on a shallow-cycling install is most days. |
| `configured_usable_capacity_kwh` | What the solver is using — `solver_battery_capacity_kwh` **already derated** by `solver_battery_soh_percent`. The derate is applied here so the pair is directly comparable without deriving it. |

Measured from a monotonic rise rather than the energy balance, deliberately: within one scored window the balance's `measured` is a **net** swing and cannot be attributed to either direction, which is why `battery_energy_balance()` does not solve for capacity. A monotonic charge phase has no discharge to confound it.

Why the pair rather than the measurement alone: `soc_discrepancy_reason` says `disagreement` and cannot say **which** disagreement. A reconstruction drifting because the configured capacity is wrong, and one drifting because a fleet blend compares different things ([#949](https://github.com/code-imstillalive/nimbus/issues/949)), produce the identical word.

On the reference household the configured figure reads 119.8 kWh (122.2 nameplate × 98% SoH) against ~110 kWh measured across four consecutive near-full sweeps — 112.6 / 109.9 / 109.7 / 107.7. An overstated capacity makes the same energy move the modelled SoC **less**, so the reconstruction under-rises through the charge phase and carries that deficit into the evening as a near-constant offset. Neither figure changes the configured value: that re-prices every future dispatch decision and stays a household decision.

## Attribute history: the parent sensors store none

**`state_attr()` works live, but returns nothing for any past timestamp** on the large parent sensors (`sensor.nimbus_solver_battery_forecast`, `sensor.nimbus_health_report`). If you are writing a template, automation, or chart that looks back in time, read the flattened child sensors instead — they are ordinary sensors with ordinary history and long-term statistics.

Why (nimbus issue [#849](https://github.com/code-imstillalive/nimbus/issues/849)): Home Assistant's recorder refuses to store attributes for a state whose attribute payload exceeds 16 KB, and it evaluates that size against the **full** payload *before* `_unrecorded_attributes` filtering removes the big arrays. On the reference household `sensor.nimbus_solver_battery_forecast` measures ~307 KB in total (`forecast` 280 KB, `batteries` 24 KB), so although only ~3 KB would actually be written, the whole attribute set is dropped — every historical state records `attributes: {}`.

This is why issues [#59](https://github.com/code-imstillalive/nimbus/issues/59) / [#99](https://github.com/code-imstillalive/nimbus/issues/99) / [#625](https://github.com/code-imstillalive/nimbus/issues/625) each closed as fixed while the recorder warnings kept firing: adding `_unrecorded_attributes` correctly stopped the bulk data being *written*, but never brought the *measured* payload under the gate.

**What this does and doesn't cost you.** Every numeric and short-string field that is worth tracking over time now has its own child sensor with real history — including `sensor.nimbus_solver_lp_status` and `sensor.nimbus_solver_binding_constraint_now`, so "was the solver infeasible overnight" and "what was binding at 03:00" are both answerable. What has no history, and cannot have: the dict-valued attributes (`solve_diagnostics`, `load_forecast_warnings`, `cost_band_24h`) and `load_forecast_source_used`, which measures ~762 characters against Home Assistant's own 255-character limit on a sensor state. Those are live-only by nature.

## Nimbus Backtest

Publishes the results of the offline reference-benchmark harness (`tests/run_reference_benchmark.py`, added in v0.94.24).

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.nimbus_efficiency_backtest` | (ratio) | Legacy parent. Headline efficiency vs reference. |
| `sensor.nimbus_backtest_configured_efficiency_percent` | % | Configured round-trip efficiency used in the backtest. |
| `sensor.nimbus_backtest_best_candidate_cost` | AUD | Lowest 24h cost across candidate parameter sweeps. |
| `sensor.nimbus_backtest_worst_candidate_cost` | AUD | Highest 24h cost across candidate parameter sweeps. |

Negative cost = net export revenue.

## Nimbus Counterfactual

Publishes what Nimbus's plan would have produced against what the plant actually did. Useful for detecting plant-side deviation from plan (charger overrides, capacity clamps, sign errors).

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.nimbus_counterfactual_soc` | % | Legacy parent. Same as `nimbus_only_soc_close_pct`. |
| `sensor.nimbus_counterfactual_real_soc_anchor_pct` | % | SoC at the start of the counterfactual window (typically 24h ago). |
| `sensor.nimbus_counterfactual_real_soc_close_pct` | % | SoC actually reached by close. |
| `sensor.nimbus_counterfactual_nimbus_only_soc_close_pct` | % | SoC Nimbus's plan would have closed at. |

Delta between real and nimbus-only close = plant-side deviation from plan.

## Nimbus Flex

Publishes what the LP's own HiGHS ranging already knows about headroom, plus a daily flex report -- both from nimbus issue #496 (Signals 7/7 of #489), Mark Purcell-authorized 2026-09-09. Two families on the same sub-device, shipped separately: live period-0 signals (v0.94.282/283) and the daily report (v0.94.284).

Live signals are gated behind `switch.nimbus_solver_flex_signals_enabled` (off by default -- ranging adds real solve overhead, see that switch's own const.py comment). All-`unknown` when the switch is off; that's expected, not a bug.

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.nimbus_flex_signals` | kW | Legacy parent. State mirrors `flex_available_up_kw`; attributes also carry `battery_signals`/`load_signals` (per-participant JSON lists, variable length). |
| `sensor.nimbus_flex_flex_available_up_kw` | kW | Upward flexibility available right now (plan-consistent, from ranging). |
| `sensor.nimbus_flex_flex_available_down_kw` | kW | Downward flexibility available right now. |
| `sensor.nimbus_flex_grid_import_headroom_kw` | kW | How much more import the current period could absorb at the same marginal price. |
| `sensor.nimbus_flex_grid_import_headroom_kwh` | kWh | Same, as energy over the period. |
| `sensor.nimbus_flex_grid_export_headroom_kw` | kW | How much more export the current period could absorb at the same marginal price. |
| `sensor.nimbus_flex_grid_export_headroom_kwh` | kWh | Same, as energy over the period. |
| `sensor.nimbus_flex_forced_import_cost` | AUD/kWh | Reduced cost of forcing one more kWh of import right now. |
| `sensor.nimbus_flex_forced_export_cost` | AUD/kWh | Reduced cost of forcing one more kWh of export right now. |
| `sensor.nimbus_flex_load_headroom_up_kwh` | kWh | Switchboard-level headroom to absorb more load at the same λ(t), from `power_balance_t` row ranging. |
| `sensor.nimbus_flex_load_headroom_down_kwh` | kWh | Switchboard-level headroom to shed load at the same λ(t). |

The daily report runs once per day for "yesterday" (same convention as `compute_daily_quality_report()`), independent of whether the live-signals switch is currently on.

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.nimbus_flex_report` | kW | Legacy parent. Attributes also carry `latest_date` and `price_response_curve` (a variable-length list of `{price_band_low, price_band_high, mean_net_import_kw, n_samples}`, not flattened -- same reasoning as `battery_signals`/`load_signals` above). |
| `sensor.nimbus_flex_offered_up_kwh` | kWh | `Σ flex_available_up_kw·dt` for the scored day. `null` (not `0`) on a day the live-signals switch was off -- there's genuinely no offered data to report, not zero flex. |
| `sensor.nimbus_flex_offered_down_kwh` | kWh | Same, downward. |
| `sensor.nimbus_flex_realised_up_kwh` | kWh | What the day's real measured battery charge/discharge actually moved, upward direction. |
| `sensor.nimbus_flex_realised_down_kwh` | kWh | Same, downward direction. |
| `sensor.nimbus_flex_envelope_curtailment_kwh` | kWh | `Σ max(0, solar - load - export_limit)·dt` for the day, using the real configured `solver_grid_max_export_kw` (never a hardcoded default). |

## Sub-device structure

Each family is registered by three artefacts in `custom_components/nimbus_load/sensor_flattened.py`:

- Spec dictionary: `FLATTENED_ATTRS_QUALITY`, `FLATTENED_ATTRS_BACKTEST`, `FLATTENED_ATTRS_COUNTERFACTUAL`, `FLATTENED_ATTRS_FLEX`, `FLATTENED_ATTRS_FLEX_REPORT`
- Factory: `create_flattened_entities_quality/backtest/counterfactual/flex/flex_report`
- Dispatch handler: `dispatch_to_flattened_quality/backtest/counterfactual/flex/flex_report`

Flex and Flex Report are a genuine sibling pairing -- both use `entity_id_prefix="nimbus_flex"` and the SAME `device_identifier=(DOMAIN, f"{entry.entry_id}_flex")`, i.e. one "Nimbus Flex" device with two families of children, not two devices.

Children inherit `_FlattenedAttributeSensorSubDevice`, which sets a DeviceInfo whose `via_device` points at the hub `entry.entry_id`.

## Resolved warnings (#283)

[#283](https://github.com/code-imstillalive/nimbus/issues/283) tracked three defects in this family. **All three are fixed**, and that issue closed on 2026-08-29. Until 2026-09-15 this section still described them as live — telling a reader to expect warnings at HA restart that no longer fire, and framing shipped work as a pending follow-up PR.

- **`state_class='measurement'` mismatches at HA restart** — fixed. No spec in `sensor_flattened.py` pairs a `MONETARY` or `ENERGY` device class with a state class any more: 81 of the 107 specs set `device_class=None` explicitly, each with a comment citing this issue, and the only device classes left are `POWER` (19), `BATTERY` (4) and `DURATION` (3) — all of which HA accepts alongside `MEASUREMENT`. The three `MONETARY` sensors in `sensor.py` deliberately carry no `state_class` at all.
- **Duplicate `uplift_available` == `regret_dollars`** — fixed. The flattened child was removed; `regret_dollars` is the canonical entity. `uplift_available` remains an **attribute** on `sensor.nimbus_solver_quality_report` (and on `epr.py`'s own report) for the `value_captured + uplift_available = theoretical_maximum_yield` identity above. This file's own table listed it as a real entity until 2026-09-15.
- **`tracking_fidelity` unit-vs-value** — fixed. Its spec's `unit_of_measurement` is `None`; the value is the dimensionless 0..1 ratio the table above already describes.

Kept as a record rather than deleted, because the shape is worth remembering: a spec table that is easy to get subtly wrong, producing warnings that only surface at restart. `tests/test_entities_doc_references_exist.py` now checks this file's entity names **and units** against the real specs, so the table cannot drift from them again silently.
