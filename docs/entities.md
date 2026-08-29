# Entities reference: Quality, Backtest, Counterfactual

Companion to `README.md` "What Nimbus publishes". Covers the three sub-devices introduced in Family-A (v0.94.24) and the flattened child sensors they expose. For per-load, whole-house rollup, and solver-plan entities, see the README.

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
| `sensor.nimbus_quality_uplift_available` | AUD | `J_ach - J_star`. Uplift still on the table vs oracle. |
| `sensor.nimbus_quality_theoretical_maximum_yield` | AUD | `J_ref - J_star`. Total spread between do-nothing and oracle. |
| `sensor.nimbus_quality_regret_dollars` | AUD | `J_ach - J_star`. Canonical name for regret. |
| `sensor.nimbus_quality_tracking_fidelity` | (dimensionless 0..1) | Plan-vs-actual tracking ratio. 1.0 = perfect. |
| `sensor.nimbus_quality_tracking_cost` | AUD | Cost added by deviation between planned and actual dispatch. |

**Identity math** (satisfied to rounding):

```
value_captured + uplift_available = theoretical_maximum_yield
J_ref - J_ach + J_ach - J_star   = J_ref - J_star
```

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

## Sub-device structure

Each family is registered by three artefacts in `custom_components/nimbus_load/sensor_flattened.py`:

- Spec dictionary: `FLATTENED_ATTRS_QUALITY`, `FLATTENED_ATTRS_BACKTEST`, `FLATTENED_ATTRS_COUNTERFACTUAL`
- Factory: `create_flattened_entities_quality/backtest/counterfactual`
- Dispatch handler: `dispatch_to_flattened_quality/backtest/counterfactual`

Children inherit `_FlattenedAttributeSensorSubDevice`, which sets a DeviceInfo whose `via_device` points at the hub `entry.entry_id`.

## Known warnings

Tracked in [#283](https://github.com/code-imstillalive/nimbus/issues/283). Warnings only, entities are safe to use:

- **24 `state_class='measurement'` mismatches** at HA restart — 17 monetary + 5 energy + 2 semantic (see below).
- **Duplicate `uplift_available` == `regret_dollars`** — both publish `J_ach - J_star`. Recommend keeping `regret_dollars` (canonical) and dropping `uplift_available`, or re-deriving it as a percentage of TMY.
- **`tracking_fidelity` unit-vs-value** — reads `1.0` with unit `%`. Value is a dimensionless 0..1 ratio; recommend dropping the unit.

All three defects live in the same spec dictionaries in `sensor_flattened.py` and can ship in one follow-up PR.
