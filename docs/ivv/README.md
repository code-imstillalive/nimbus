# IV&V — Independent Verification & Validation

## What this is

A user-facing, install-facing check that Nimbus's *outputs* match its stated
invariants on your own hardware, price feeds, and forecast sources. It is
complementary to the unit-test suite under `tests/`: unit tests verify the
code, IV&V verifies the code's *result*, in-situ, on a real install.

If you are running Nimbus and want to know "is my install healthy, and did
the last release regress anything I care about?" — this doc is for you.

The name and format come from the "new-style IV&V" proposal in repo
[issue #217](https://github.com/code-imstillalive/nimbus/issues/217). Older
one-off IV&V reports are linked from that issue for historical context.

## The three artifacts

**1. A short human-readable checklist** — the [Field checklist](#field-checklist)
below. Runs in <5 minutes with only `curl` and a browser. Confirms your
install is producing the outputs downstream tools expect. Do this after
every version bump.

**2. A data-driven regression test suite** at
[`tests/regression/`](../../tests/regression/) — parametrised across
captured "golden" install diagnostics. Every invariant is a one-line
assertion; adding a new install is one directory drop-in. Run with
`pytest tests/regression/`.

**3. Per-release IV&V reports** — long-form write-ups mirrored on issue
#217 comments. These interpret the numbers the checklist and test suite
produce, and propose new invariants to codify. Each report ends with a
short PR-ready diff for adding its new invariants to
`tests/regression/test_forecast_invariants.py`.

## Field checklist

After every Nimbus version bump, in five minutes on your own instance:

### 1 · Confirm the forecast attributes you depend on are present

```bash
HA=https://your.ha.instance
curl -sH "Authorization: Bearer $HA_TOKEN" \
  "$HA/api/states/sensor.nimbus_solver_battery_forecast" \
  | jq '.attributes.forecast[0] | keys'
```

Expect at least: `time`, `battery_kw`, `soc_pct`, `grid_import_kw`,
`grid_export_kw`, `solar_kw`, `load_kw`, `import_price`, `export_price`,
`import_price_raw`, `export_price_raw`, `hours`.

Missing anything? That's a regression — file it with `curl` output.

### 2 · Confirm `_raw` prices pass through your primary source

If you use Amber Express feed-in (issue #216 use-case):

```bash
# Compare export_price_raw with the source sensor's forecast at overlapping timestamps
python3 - <<'PY'
import json, subprocess
from datetime import datetime

def get(entity):
    return json.loads(subprocess.check_output([
        "curl", "-sH", f"Authorization: Bearer {HA_TOKEN}",
        f"{HA}/api/states/{entity}"
    ]))

nsbf = get("sensor.nimbus_solver_battery_forecast")
aex  = get("sensor.amber_express_amber_feed_in_price")

fi = {datetime.fromisoformat(f['time']): f['value']
      for f in aex['attributes']['forecast']}
mismatches = 0
for x in nsbf['attributes']['forecast']:
    t = datetime.fromisoformat(x['time'])
    if t not in fi: continue
    src, raw = fi[t], x.get('export_price_raw')
    if raw is None or abs(src - raw) > 1e-4:
        print(f"MISMATCH {t}: source={src} raw={raw}")
        mismatches += 1
print(f"{'PASS' if mismatches == 0 else 'FAIL'}: {mismatches} mismatches")
PY
```

Non-zero mismatches means the price pipeline is compressing or offsetting
your source feed. Post the count + a couple of example rows to a new issue.

### 3 · Confirm LP output invariants

Eyeball the forecast[] against your configured bounds:

- `soc_pct` should never sit outside your configured min–max SoC range
- `abs(battery_kw)` should never exceed your configured charge/discharge kW
- `grid_import_kw`, `grid_export_kw`, `solar_kw`, `load_kw` should all be ≥ 0

Fastest way — copy your forecast[] into the regression suite as a new fixture:

```bash
mkdir -p tests/regression/fixtures/<your_slug>
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/diagnostics/config_entry/$ENTRY_ID" \
  > tests/regression/fixtures/<your_slug>/nimbus_diag.json
curl -sH "Authorization: Bearer $HA_TOKEN" \
     "$HA/api/states/sensor.nimbus_solver_battery_forecast" \
  > tests/regression/fixtures/<your_slug>/nimbus_solver_battery_forecast.json

pytest tests/regression/ -v -k <your_slug>
```

The suite will tell you exactly which invariant fails, at which timestamp,
with which numbers.

### 4 · Compare plan cost vs the naive baselines

The `sensor.nimbus_solver_config` bridge sensor exposes `total_cost` and
supporting counterfactuals. Confirm:

- `total_cost < naive_pv_only_cost` (using the battery should be no worse than
  ignoring it)
- `total_cost < flat_tariff_cost` if you're on a time-varying tariff

If either isn't true, either your config is degenerate or the LP is choosing
a bad plan. File an issue with your diagnostic JSON attached.

## Extending the regression suite

If step 3 flushed out something worth codifying, add an invariant to
`tests/regression/test_forecast_invariants.py`. Naming prefixes:

| Prefix | Domain |
|:-|:-|
| `RAW-*` | `_raw` diagnostic attribute conventions |
| `PRICE-*` | Price pipeline source-sensor pass-through |
| `LP-*` | LP output invariants (SoC, power, signs, energy balance) |

Every new invariant is a one-line assertion parametrised across every
captured install — so adding one immediately widens coverage across all
contributed fixtures. See `tests/regression/README.md` for the layout.

## Contributing an IV&V report

Post it as a new comment on issue #217, or as a standalone issue with `iv-v`
in the title. Each report should include:

1. **Install shape** — inverter × retailer × NEM region × battery kWh
2. **Nimbus version** at time of capture (and the version bumped *from*)
3. **Numeric evidence** for each invariant checked (source values,
   pass/fail counts, worst-case deltas)
4. **Proposed pytest fixture** — the one-line assertion the report suggests
   adding to `tests/regression/`

The [issue #217 first-cut IV&V comment](https://github.com/code-imstillalive/nimbus/issues/217#issuecomment-5432600365)
is the reference template. Copy its structure.
