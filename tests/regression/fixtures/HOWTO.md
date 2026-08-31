# fixtures/ — contributing a fixture from your own install

This is the **tester-facing** walkthrough. If you're maintaining the invariant suite itself, read [../README.md](../README.md) instead. If you have a Nimbus install and want to add a five-minute contribution that grows the suite's coverage beyond one household, this is the file.

## Why your fixture matters

Every invariant in `tests/regression/test_forecast_invariants.py` was originally extracted from one household's real IV&V (a Sigenergy plant on Amber Express in QLD1). Each install you contribute lowers the risk that a "works on Mark's install" fix silently misbehaves on yours. The fixture files are just JSON captured from your HA's REST API — the JSON is what runs against every invariant. Nothing is compiled, no code is required, and you don't have to write a test.

Concretely, contributing one fixture means:

- Any future refactor of the price pipeline gets run against your retailer and your tariff structure before it merges.
- If your inverter uses a sign convention or a scaling nobody has hit before, the suite catches it.
- Your capture-time state becomes a permanent point of reference the next time something drifts — a "was this ever true?" test rather than an "is it true today?" one.

## What "install shape" means for coverage

The suite already covers one shape end-to-end (Sigenergy + Amber Express + QLD1 + secondary blended tariff). Any of the axes below being different from that shape is a genuine coverage gain:

| Axis | Current cover | Interesting new shapes |
|:-|:-|:-|
| **Inverter** | Sigenergy | Fronius, GoodWe, SolarEdge, Enphase, LuxPower, Growatt, DIY (Victron, PIP), any brand whose battery power sensor uses the OPPOSITE sign convention |
| **Battery** | Sigenergy stack, ~40 kWh | Powerwall, LG Chem, Pylontech, DIY LiFePO4, LFP wall units, hybrid inverters with built-in storage |
| **Retailer (primary)** | Amber Electric — Express | Amber Classic, Amber Trial, Localvolts, Powerclub, OVO, Origin Spot, Reposit-fronted retailers, any wholesale-passthrough retailer |
| **Secondary blend** | Energex 6900 ToU via `nem_pd7day` | Fixed flat tariff, EV-only tariff, controlled load, Solar Sponge, ToU with more than two blocks, tariffs with a demand charge |
| **NEM region** | QLD1 | NSW1, VIC1, SA1, TAS1, WA (SWIS if you have a Nimbus port to it) |
| **Solar forecast source** | Solcast (primary) + Energy Production Today (secondary) | Forecast.Solar, PVLib, no secondary, dual Solcast API keys, single-string vs multi-string PV |
| **Load forecast source** | Nimbus's own retrained ML forecast | Baseline persistence, a custom `template` sensor, EMHASS-supplied forecast |
| **Config-flow options** | `solver_battery_power_positive_is_charge = False` (default) | `= True` (any inverter whose sensor reports positive-on-charge — SigEnergy, several DIY setups) |

Any single non-default axis is worth capturing. Everything different is a stronger fixture.

## Five curl calls at one wall-clock moment

The capture is five HTTP GETs against your HA's REST API, all fired within a few seconds of each other so the price/state alignment is genuine. You can run this from your HA host, from a laptop on the same LAN, or from anywhere with the Nabu Casa remote URL.

### Prerequisites

- Your HA base URL (either `http://homeassistant.local:8123` on LAN, or your Nabu Casa `https://<hash>.ui.nabu.casa` remote URL).
- A long-lived access token from your HA user profile page.
- The Nimbus config-entry ID. Grab it with:

  ```bash
  curl -sH "Authorization: Bearer $HA_TOKEN" \
       "$HA/api/config/config_entries/entry" \
    | python3 -c 'import sys,json; [print(e["entry_id"], e["title"]) for e in json.load(sys.stdin) if e["domain"]=="nimbus_load"]'
  ```

  Copy the ULID or UUID that prints — the 26 or 36 characters before `nimbus_load`.

### Capture

```bash
# Fill these in for your install.
INSTALL=<short_slug>           # e.g. "smithhome_nsw1", "victron_diy_sa1" — lower_snake_case, region suffix helps
HA=<your HA base URL>
HA_TOKEN=<your long-lived token>
ENTRY_ID=<the ID from the previous step>

mkdir -p tests/regression/fixtures/$INSTALL

# 1. Full diagnostic. This is the big one — it embeds every solver config, every
#    forecast attribute, the LP plan, and the last handful of state transitions.
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/diagnostics/config_entry/$ENTRY_ID" \
  > tests/regression/fixtures/$INSTALL/nimbus_diag.json

# 2. Dedicated solver forecast state. Some builds publish richer attributes on
#    the state itself than on the diagnostic's embedded copy — cheap to grab.
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.nimbus_solver_battery_forecast" \
  > tests/regression/fixtures/$INSTALL/nimbus_solver_battery_forecast.json

# 3-5. Source-sensor states. Capture whichever match your retailer:

# Amber Electric (any plan):
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.amber_express_amber_feed_in_price" \
  > tests/regression/fixtures/$INSTALL/amber_ex_feed_in.json
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.amber_express_amber_general_price" \
  > tests/regression/fixtures/$INSTALL/amber_ex_general.json

# LocalVolts (substitute your instance's actual entity IDs):
# curl -sH "Authorization: Bearer $HA_TOKEN" \
#      "$HA/api/states/sensor.localvolts_market_price" \
#   > tests/regression/fixtures/$INSTALL/localvolts_market.json

# Fixed tariff / no secondary: skip this step entirely — the source-dependent
# invariants (PRICE-*) will skip on your fixture and the rest still run.
```

Total capture time: about 15 seconds. Total on-disk size: under 700 KB per install (a diagnostic is around 450 KB, source-sensor states are around 15-20 KB each).

## The README your fixture needs

Every fixture directory ships with a short `README.md` next to the JSON files so a maintainer scanning the fixtures can immediately tell what shape this install is. Copy [purcell_qld1/README.md](purcell_qld1/README.md) as a template and fill in your own values. The mandatory sections are:

1. **Provenance.** One sentence: whose install, which Nimbus version at capture, wall-clock timestamp.
2. **Install shape table.** The HA/Nimbus version rows, the six-to-eight axes from the coverage table above that describe your install, and the capture wall-clock.
3. **Why this fixture is useful.** Two or three bullet points naming what your install exercises that the existing fixtures don't (e.g. "opposite battery sign convention", "no secondary blend", "different NEM region").
4. **Files table.** File name, source curl endpoint, on-disk size.

You do not need to run the suite yourself or list which invariants pass or fail. The maintainer will run `pytest tests/regression/` when your PR opens, and the CI matrix reports pass/fail per fixture per invariant. If your fixture legitimately opts out of a whole invariant class (e.g. no Amber sensors → skip `PRICE-*`), drop a `SKIP_INVARIANTS.txt` in the same directory listing the prefix on its own line — see [../README.md](../README.md#per-fixture-opt-outs--skip_invariantstxt) for the shape.

## Privacy and provenance

The diagnostic export is what HA's own Diagnostics UI produces. It already redacts what HA classifies as sensitive: your access token, your Nabu Casa hash, your latitude and longitude, your device's serial numbers. It DOES contain your entity IDs, your Nimbus config values (battery capacity, SoC bounds, grid limits, retailer plan slug), and your price history — treat those as the level of detail you're comfortable putting in a public GitHub repo. If any single entity ID or config value is genuinely sensitive on your install (e.g. a project name in a `friendly_name`), rename or redact just that field before submitting.

The fixture is captured from your live install at the exact wall-clock timestamp you record in the README. That timestamp is the fixture's single source of provenance — do not edit the JSON after capture. If something in the capture looks wrong on re-inspection, discard it and re-capture rather than mutating the file. A fixture that has been hand-edited stops being a real capture, and the whole regression suite depends on the files being unaltered snapshots of real installs.

## Contributing

- Fork [code-imstillalive/nimbus](https://github.com/code-imstillalive/nimbus), branch off `main`.
- Add your `tests/regression/fixtures/<INSTALL>/` directory (JSON files + README).
- Run `pytest tests/regression/` locally if you can — if you can't, that's fine, CI will run it.
- Open a PR titled `Add regression fixture: <INSTALL> (<one-line shape>)`, e.g. `Add regression fixture: victron_diy_sa1 (Victron LFP, SA1, LocalVolts)`.

The parametrisation picks up the new directory automatically. There is no code change required to add a fixture, and no test to write. The whole idea is that adding coverage should be one directory drop-in, not a coding task.

## Getting help

- Ask on [issue #217](https://github.com/code-imstillalive/nimbus/issues/217) if you're not sure whether your install adds coverage — describe the axes and someone will answer.
- If the capture curl commands fail, the most common cause is a wrong `$ENTRY_ID` (check the config-entries endpoint) or a token without long-lived scope.
- If a source sensor you rely on has a different entity ID on your install, use whatever `sensor.*` your install actually publishes and note the mapping in your fixture's README.
