# tests/regression/ — data-driven invariant regression suite

## Purpose

Turn each ad-hoc IV&V observation into a standing regression test, so no future
refactor can silently violate an invariant that was previously verified in the
field. Introduced as the first concrete deliverable against repo issue
[#217](https://github.com/code-imstillalive/nimbus/issues/217).

Each test is a **one-line assertion** parametrised across every captured
"golden" install under `fixtures/`. Adding a new install is one directory
drop-in — no test modifications required — so the coverage widens as beta
testers contribute their own captures.

## Layout

```
tests/regression/
├── README.md                            # this file
├── conftest.py                          # fixture loaders + parametrisation
├── test_forecast_invariants.py          # RAW-*, PRICE-*, LP-* invariant suite
└── fixtures/
    └── <install_name>/
        ├── README.md                    # provenance, install shape, capture wall-clock
        ├── nimbus_diag.json             # /api/diagnostics/config_entry/<id>
        ├── nimbus_solver_battery_forecast.json   # optional; falls back to diag
        ├── amber_ex_feed_in.json        # optional; gates PRICE-01
        └── amber_ex_general.json        # optional; gates future PRICE-02
```

Files are ordinary JSON captured directly from `curl` against the running HA
instance's REST API — no post-processing, no scrubbing beyond what
`/api/diagnostics/...` already omits. That keeps the fixtures easy to
regenerate (`curl > fixtures/<name>/nimbus_diag.json`) and easy to diff.

**Size discipline.** A single diag is ~450 KB and one full source-sensor
state is ~15 KB. Two installs' captures fit in <1 MB, well under any
reasonable review threshold. If the suite outgrows this, the answer is to
shard by fixture-per-scenario (e.g. `blended_prices/`, `no_secondary/`,
`fixed_tariff/`) rather than to trim the JSONs themselves — the whole point
is that they are real captures.

## Invariant naming

Prefixes match the domain areas in issue #217:

| Prefix | Domain | Origin |
|:-|:-|:-|
| `RAW-*` | `_raw` diagnostic attribute conventions | #216 / #217 item 2 |
| `PRICE-*` | Price pipeline source-sensor pass-through | #216 / #217 item 1 |
| `LP-*` | LP output invariants (SoC, power, signs, energy balance) | #125 / #147-149 / #217 item 1 |

More prefixes will land as new invariants get extracted from prior IV&V
reports (v0.82, v0.85, v0.88, v0.92).

## Running the suite

```bash
pytest tests/regression/
```

Every invariant that doesn't require source-sensor comparison runs against
every fixture. Source-dependent invariants (PRICE-*) skip on installs where
the matching source-sensor JSON isn't captured — so an install using
LocalVolts or a fixed tariff doesn't trigger a spurious failure on the
Amber-side PRICE-01 test.

## Contributing a fixture from your own install

If you're one of the beta cohort (see issue #217 item 5), a new fixture is
five `curl` calls at a single wall-clock moment:

```bash
INSTALL=<slug>
mkdir -p tests/regression/fixtures/$INSTALL

# Nimbus config entry ID from /api/config/config_entries/entry
ENTRY_ID=<uuid-from-config-entries>
HA=https://your.ha.instance

# 1. Full diagnostic
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/diagnostics/config_entry/$ENTRY_ID" \
  > tests/regression/fixtures/$INSTALL/nimbus_diag.json

# 2. Dedicated solver forecast state (optional but recommended — has richer
#    attributes than the diagnostic's embedded copy on some builds)
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.nimbus_solver_battery_forecast" \
  > tests/regression/fixtures/$INSTALL/nimbus_solver_battery_forecast.json

# 3+. Source-sensor states — capture whichever apply to your install
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.amber_express_amber_feed_in_price" \
  > tests/regression/fixtures/$INSTALL/amber_ex_feed_in.json

curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.amber_express_amber_general_price" \
  > tests/regression/fixtures/$INSTALL/amber_ex_general.json
```

Then add a short `README.md` in the same directory with:
- Install shape (inverter × retailer × NEM region × battery capacity)
- Nimbus version at capture
- Wall-clock timestamp of capture
- Which invariants are expected to be exercised

The parametrisation picks up the new directory automatically on the next
`pytest tests/regression/` run.
