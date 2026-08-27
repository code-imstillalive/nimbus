# fixtures/purcell_qld1_v0.94.6_midblock

## Provenance

Same install as [`purcell_qld1/`](../purcell_qld1/) and
[`purcell_qld1_v0.94.6/`](../purcell_qld1_v0.94.6/) — Mark Purcell's QLD1
install, Nimbus v0.94.6. Captured deliberately **mid-block** at 17:36 AEST on
2026-08-27 to exercise SET-02 (multi-row identity within the current NEM
settlement block; #220 reopen).

The existing `purcell_qld1_v0.94.6/` fixture was captured at 15:05:00 AEST —
a NEM block boundary — so it contains only one in-block forecast row
(`forecast[0]`) and does not exercise the multi-row case. This fixture,
captured at 17:36 with block [17:35, 17:40) containing forecast rows
[17:36, 17:37, 17:38, 17:39], is the reproducer for the partial regression
found in the field after PR #225 was already up.

## Install shape

Unchanged from `purcell_qld1_v0.94.6/README.md`.

| Field | Value |
|:-|:-|
| HA version | 2026.8.3 (Home Assistant OS) |
| Nimbus version | 0.94.6 |
| NEM region | QLD1 |
| Retailer (primary) | Amber Electric — Express plan |
| Retailer (secondary blend) | Energex 6900 residential ToU (`nem_pd7day`) |
| Battery | Sigenergy 40.0 kWh |
| Capture wall-clock | 2026-08-27T07:36:00 UTC (2026-08-27 17:36 AEST) |
| Capture position | mid-block (NEM block [17:35, 17:40), forecast[0] at 17:36) |

## Why this install is a useful golden

- **Multi-row in-block coverage**: block [17:35, 17:40) contains four
  Nimbus forecast rows at 17:36, 17:37, 17:38, 17:39 (the align-to-now
  60 s rows). Fixtures captured on a boundary can't exercise this.
- **Demonstrates the SET-02 regression cleanly**: rows 1-3 have
  `import_price = 0.4042` vs `import_price_raw = 0.4024` = source `state`,
  and `export_price = 0.1296` vs `export_price_raw = 0.1448` = source
  `state`. The `_raw` fields hold the correct identity value; the effective
  `import_price` / `export_price` (what the LP consumes) are blended.
  The v0.94.5/v0.94.6 row-0-only fix does not extend to these rows.
- **Doubles coverage of every pre-existing invariant** (RAW-*, PRICE-*,
  LP-*, SET-01a, SET-01b) via pytest parametrisation.

## Invariants exercised

| Invariant | Verdict |
|:-|:-:|
| RAW-01 (both `_raw` attributes present) | PASS |
| RAW-02 (`load_kw` / `solar_kw` present) | PASS |
| PRICE-01 (export_price_raw matches Amber Ex feed-in) | PASS |
| SET-01a (forecast[0] import matches source `state`) | PASS |
| SET-01b (forecast[0] export matches source `state`) | PASS |
| **SET-02a** (all in-block rows import identity) | **currently SKIP** (see below) |
| **SET-02b** (all in-block rows export identity) | **currently SKIP** (see below) |
| LP-01 (SoC bounds respected) | PASS |
| LP-02 (battery kW within limits) | PASS |
| LP-03 (sign conventions) | PASS |
| LP-04 (energy balance closes) | SKIP (`battery_kw_after_efficiency` not published) |

## SET-02 currently opted out via `SKIP_INVARIANTS.txt`

This fixture is captured on **v0.94.6, which pre-dates the SET-02 fix**
(the multi-row extension of #220 landed in v0.94.5 for row 0 only; the
row 1..N extension is the follow-up in the reopened #220 discussion).

The `SET_02` prefix is listed in this fixture's `SKIP_INVARIANTS.txt` so
the suite stays green until v0.94.7 ships with the follow-up fix. On the
day the fix lands, this fixture should either:

  (a) have `SET_02` removed from `SKIP_INVARIANTS.txt` (if the fixture is
      still representative of the fixed pipeline), or

  (b) be replaced with a fresh capture from the same install on the fixed
      Nimbus version (preferred — matches the pattern used for
      `purcell_qld1` → `purcell_qld1_v0.94.6`).

Until then, this fixture serves as the **negative** golden that documents
what the bug looks like: `pytest -v` will show SET-02 as `SKIPPED` here,
with the skip reason pointing at this README.

## Files

| File | Source | Size |
|:-|:-|-:|
| `nimbus_diag.json` | `GET /api/diagnostics/config_entry/<id>` | ~450 KB |
| `nimbus_solver_battery_forecast.json` | `GET /api/states/sensor.nimbus_solver_battery_forecast` | ~115 KB |
| `amber_ex_feed_in.json` | `GET /api/states/sensor.amber_express_amber_feed_in_price` | ~23 KB |
| `amber_ex_general.json` | `GET /api/states/sensor.amber_express_amber_general_price` | ~29 KB |
| `SKIP_INVARIANTS.txt` | opt-out list until v0.94.7 lands | small |
