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

**Identity math** (satisfied to rounding):

```
value_captured + uplift_available = theoretical_maximum_yield
J_ref - J_ach + J_ach - J_star   = J_ref - J_star
```

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

## Known warnings

Tracked in [#283](https://github.com/code-imstillalive/nimbus/issues/283). Warnings only, entities are safe to use:

- **24 `state_class='measurement'` mismatches** at HA restart — 17 monetary + 5 energy + 2 semantic (see below).
- ~~**Duplicate `uplift_available` == `regret_dollars`**~~ — **fixed.** The flattened `uplift_available` child sensor was removed; `regret_dollars` is the canonical entity. `uplift_available` remains an **attribute** on `sensor.nimbus_solver_quality_report` (and on `epr.py`'s own report) for the `value_captured + uplift_available = theoretical_maximum_yield` identity below — it is no longer an entity, and this table listed one until 2026-09-15.
- **`tracking_fidelity` unit-vs-value** — reads `1.0` with unit `%`. Value is a dimensionless 0..1 ratio; recommend dropping the unit.

All three defects live in the same spec dictionaries in `sensor_flattened.py` and can ship in one follow-up PR.
