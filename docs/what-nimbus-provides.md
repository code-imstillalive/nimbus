# What Nimbus provides

Everything Nimbus adds to Home Assistant: its entities, by kind, and its
actions (services). Entity ids below are examples from a real install; yours
follow the names you give your loads and devices.

## Entities

Nimbus adds five kinds of entity.

### Sensors

| What | Example | More |
|---|---|---|
| A forecast for each load or power signal you add, with its upper and lower band | `sensor.nimbus_<your source>_forecast` | [Dashboard tabs](tabs.md) (Forecaster) |
| The battery plan: power, state of charge, prices and costs for the next 96 hours | `sensor.nimbus_solver_battery_forecast` | [Going live](going-live.md) |
| Whether the last solve succeeded, and when | `sensor.nimbus_solver_lp_status` | [Troubleshooting](troubleshooting.md) |
| Status and health of the whole install, including What's New | `sensor.nimbus_status` | [Dashboard tabs](tabs.md) (Help) |
| The daily reports: quality score, backtest, counterfactual and flex | `sensor.nimbus_solver_quality_report`, `sensor.nimbus_efficiency_backtest`, `sensor.nimbus_counterfactual_soc`, `sensor.nimbus_flex_report` | [Entities](entities.md) |
| Flex telemetry for the last 5-minute interval | `sensor.nimbus_flex_telemetry` | [Entities](entities.md) |
| What Nimbus's own dispatcher would send, while dry run is on | `sensor.nimbus_solver_dispatch_dry_run` | [Every setting](configuration-reference.md) |

### Numbers, switches and times

The Solver's settings: battery limits, prices and fees, P2P blocks, reserve,
risk and the rest. Numbers hold values (`number.nimbus_solver_*`), switches turn
features on and off (for example `switch.nimbus_solve_on_price_change`), and
time entities hold times of day (for example
`time.nimbus_solver_battery_reserve_start_hour`). They sit on the Solver tab and
can be changed there or from an automation.

Every one is listed, with its range and default, in
[Every setting](configuration-reference.md).

### Selects

| Entity | Chooses |
|---|---|
| `select.nimbus_household_mode` | The household mode: home, away, guests or economy |
| `select.nimbus_solver_load_forecast_source_policy` | The load forecast the Solver plans with: off (the learned forecast), measure (learned, compared with a simple repeat of past days), blend, or persistence (the repeat alone) |
| `select.nimbus_pricing_source` | Your price provider: LocalVolts v2, Amber Express, Amber Electric or Manual |
| `select.nimbus_device_setup_mode` | Device setup: legacy, shadow or active |

## Actions

Call these from **Developer tools → Actions** or an automation. Those that
return data show it there.

| Action | Does |
|---|---|
| `nimbus_load.solve_now` | Runs one Solver cycle now, instead of waiting for the next |
| `nimbus_load.retrain` | Retrains one or more load models now, instead of at the daily retrain |
| `nimbus_load.compute_quality_report` | Scores a past window against perfect hindsight, on demand |
| `nimbus_load.rescore_history` | Re-scores the last few days and corrects the quality history |
| `nimbus_load.get_dashboard` | Returns the Nimbus dashboard, filled in for your install, to paste in |
| `nimbus_load.export_decision_inputs` | Returns what the Solver decided on in a past window |
| `nimbus_load.get_stochastic_summary` | Returns the latest stochastic plan summary |
| `nimbus_load.replay_price_offsets` | Replays the price consolidation's learned offsets over the stored price history |
| `nimbus_load.flex_telemetry_record` | Returns the current flex telemetry record |
| `nimbus_load.device_contract_discover` | Finds devices and price sources from your Energy Dashboard and providers |
| `nimbus_load.device_contract_preview` | Shows what moving to device setup would change, without changing anything |
| `nimbus_load.device_contract_confirm` | Saves what you accepted from discovery |
| `nimbus_load.device_contract_set_mode` | Switches device setup between legacy, shadow and active |

Each action's fields are described in Developer tools when you pick it.
