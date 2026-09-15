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

> **Corrected 2026-09-15.** This step previously told you to read
> `total_cost`, `naive_pv_only_cost` and `flat_tariff_cost` off
> `sensor.nimbus_solver_config`. All three were wrong, and had been since
> this file was written under [#217](https://github.com/code-imstillalive/nimbus/issues/217)/#219
> — the two baseline names appear nowhere in the source, in any version,
> and `sensor.nimbus_solver_config` mirrors *configuration*, not plan
> results. The step has never been runnable as written. Below is what the
> install actually publishes.

The plan cost lives on the plan sensor, not the config sensor:

```bash
curl -sH "Authorization: Bearer $HA_TOKEN" \
  "$HA/api/states/sensor.nimbus_solver_battery_forecast" \
  | jq '.attributes | {status, total_cost, total_cost_with_fixed_costs, cost_band, binding_constraint_now}'
```

Confirm:

- `status` is `optimal`. Anything else (`infeasible`, a clamped or
  fallback status) means the plan you are looking at is not the plan the
  LP wanted, and every number below it is suspect.
- `total_cost` is negative on an export-capable install with a
  time-varying tariff, or at least below what standing still would cost
  you. A positive total cost on a day with real solar and real spread is
  worth investigating.
- `binding_constraint_now` names a bound you actually configured. If it
  reports a value that is neither of a constraint's own limits, say so in
  an issue — that is the constraint reporter disagreeing with the solve,
  not a config problem.

For a real before/after against counterfactual controllers, the Quality
sub-device is the honest source: `epr`, `j_ref` (no-control reference),
`j_ach` (achieved), `j_star` (oracle), `regret_dollars`,
`value_captured` and `uplift_available`, with the identity
`value_captured + uplift_available = theoretical_maximum_yield`. See
[`docs/entities.md`](../entities.md) for the per-entity table.

Note that forecast regret specifically — "what did forecast error cost
me", "does the Forecaster beat persistence" — is **not** among these on a
HACS install; see [#919](https://github.com/code-imstillalive/nimbus/issues/919).

If a check fails, file an issue with your diagnostic JSON attached.

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
