# fixtures/purcell_qld1_v0.94.6

## Provenance

Same install as [`purcell_qld1/`](../purcell_qld1/), captured immediately
after bumping to Nimbus v0.94.6 — the release that contains Raf's fix for
[issue #220 (settled prices must not be blended)](https://github.com/code-imstillalive/nimbus/issues/220).

Held alongside `purcell_qld1/` (v0.94.4) so the SET-* invariants have a
before-and-after pair on the same physical install.

## Install shape

Unchanged from `purcell_qld1/README.md`. Reproduced here for offline use:

| Field | Value |
|:-|:-|
| HA version | 2026.8.3 (Home Assistant OS) |
| Nimbus version | 0.94.6 |
| NEM region | QLD1 |
| Retailer (primary) | Amber Electric — Express plan |
| Retailer (secondary blend) | Energex 6900 residential ToU (`nem_pd7day`) |
| Battery | Sigenergy 40.0 kWh |
| SoC bounds | 5% – 100% |
| Battery power | 21.0 kW charge / 24.0 kW discharge |
| Grid limits | 30.0 kW import / 30.0 kW export |
| Solar forecast | Solcast (primary) + Energy Production Today (secondary) |
| Load forecast | `nimbus_sigen_plant_total_load_power_forecast` |
| Capture wall-clock | 2026-08-27T05:06:00 UTC (2026-08-27 15:06 AEST) |

## Why this install is a useful golden

Adds a *second* golden on the same install, differing only in the Nimbus
version. The important properties:

- **Post-#220-fix state**: the SET-* invariants (new in this PR) PASS here.
  The same invariants applied to a pre-#220-fix capture would FAIL — this
  fixture is the "positive" side of the SET regression.
- **Doubles coverage of every pre-existing invariant** (RAW-*, PRICE-*, LP-*)
  automatically via pytest parametrisation. Directory drop-in — no test
  changes.
- **Captured at a settlement boundary with a negative export price**
  (Amber feed-in `state = −0.0010336`, i.e. Amber paying you not to
  export). This is the realistic "if the blend hides this you export into
  a curtailment window" scenario that motivated #220.

## Invariants exercised

At time of contribution, this fixture exercises:

| Invariant | Verdict |
|:-|:-:|
| RAW-01 (both `_raw` attributes present) | PASS |
| RAW-02 (`load_kw` / `solar_kw` present) | PASS |
| PRICE-01 (export_price_raw matches Amber Ex feed-in) | PASS |
| SET-01a (current-block import matches source `state`) | PASS |
| SET-01b (current-block export matches source `state`) | PASS |
| LP-01 (SoC bounds respected) | PASS |
| LP-02 (battery kW within limits) | PASS |
| LP-03 (sign conventions) | PASS |
| LP-04 (energy balance closes) | SKIP (`battery_kw_after_efficiency` not published) |

## Note on capture timing

This diagnostic was captured at 15:05:00 AEST — the very first solve after
HA restarted onto v0.94.6. A subsequent solve two minutes later reported
`total_cost = +$187.80`; this fixture happens to have captured a transient
`total_cost = −$47.39` because the load-forecast sensor was still populating
after the retrain task (see v0.94.6 IV&V report). This does not affect any
of the invariants tested here — every SET-*, PRICE-*, LP-*, RAW-* assertion
holds because they concern shape and identity, not the absolute plan cost.

## Files

| File | Source | Size |
|:-|:-|-:|
| `nimbus_diag.json` | `GET /api/diagnostics/config_entry/<id>` | ~460 KB |
| `nimbus_solver_battery_forecast.json` | `GET /api/states/sensor.nimbus_solver_battery_forecast` | ~115 KB |
| `amber_ex_feed_in.json` | `GET /api/states/sensor.amber_express_amber_feed_in_price` | ~1 KB |
| `amber_ex_general.json` | `GET /api/states/sensor.amber_express_amber_general_price` | ~1 KB |
