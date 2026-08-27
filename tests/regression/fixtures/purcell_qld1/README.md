# fixtures/purcell_qld1

## Provenance

Captured directly from Mark Purcell's HA instance immediately after bumping to
Nimbus v0.94.4. Same install and same capture that produced the
[issue #216 fix confirmation](https://github.com/code-imstillalive/nimbus/issues/216#issuecomment-5432600274).

## Install shape

| Field | Value |
|:-|:-|
| HA version | 2026.8.3 (Home Assistant OS) |
| Nimbus version | 0.94.4 |
| NEM region | QLD1 |
| Retailer (primary) | Amber Electric — Express plan |
| Retailer (secondary blend) | Energex 6900 residential ToU (`nem_pd7day`) |
| Battery | Sigenergy 40.0 kWh |
| SoC bounds | 5% – 100% |
| Battery power | 21.0 kW charge / 24.0 kW discharge |
| Grid limits | 30.0 kW import / 30.0 kW export |
| Solar forecast | Solcast (primary) + Energy Production Today (secondary) |
| Load forecast | `nimbus_sigen_plant_total_load_power_forecast` |
| Capture wall-clock | 2026-08-26T23:57:18 UTC (2026-08-27 09:57 AEST) |

## Why this install is a useful golden

- **Both `_sensor_2` slots populated** on import AND export — exercises the
  coverage-aware blending path that was the site of #216.
- **Real Amber Express data** available at capture time as an independent
  external truth source for PRICE-01.
- **v0.94.4 with the #216 fix** — this fixture will fail RAW-01 and
  PRICE-01 if the fix is ever regressed.

## Invariants exercised

At time of contribution, this fixture exercises:

| Invariant | Verdict |
|:-|:-:|
| RAW-01 (both `_raw` attributes present) | PASS |
| RAW-02 (`load_kw` / `solar_kw` present) | PASS |
| PRICE-01 (export_price_raw matches Amber Ex feed-in) | PASS (41/41 aligned pts, max diff 0.0 c/kWh) |
| LP-01 (SoC bounds respected) | PASS |
| LP-02 (battery kW within limits) | PASS |
| LP-03 (sign conventions) | PASS |
| LP-04 (energy balance closes) | SKIP (`battery_kw_after_efficiency` not published) |

## Files

| File | Source | Size |
|:-|:-|-:|
| `nimbus_diag.json` | `GET /api/diagnostics/config_entry/<id>` | ~460 KB |
| `nimbus_solver_battery_forecast.json` | `GET /api/states/sensor.nimbus_solver_battery_forecast` | ~115 KB |
| `amber_ex_feed_in.json` | `GET /api/states/sensor.amber_express_amber_feed_in_price` | ~17 KB |
| `amber_ex_general.json` | `GET /api/states/sensor.amber_express_amber_general_price` | ~22 KB |

Total: ~616 KB.
