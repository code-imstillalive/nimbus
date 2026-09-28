# Spec 000: Golden master and refactor gates

Status: proposed. Part A and Part D implemented in this PR; Parts B and C
specified, not built.
Plan: docs/architecture/tech-debt-plan.md, step "Spec 000"
Code cited at: `d5b044b`

## Responsibility

Build the evidence every later extraction under
[#1298](https://github.com/code-imstillalive/nimbus/issues/1298) is accepted
against, before any of them moves a line: a golden master of one complete
`solver_writer.main()` cycle (Part A), a way to record live inputs for it
(Part B), the gate tooling (Part C), and real NEM market inputs, including
price spikes, from NEMWEB (Part D). No file under `custom_components/`
changes.

## Current behaviour this must preserve

All of it, because nothing moves. This spec defines what "behaviour" means for
every later spec: the record Part A writes.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| States posted to HA | `publish_plan` (solver_writer.py:12305), called from `main` (16657) | golden record `cycles[].posted` |
| Services called | `ha_call_service*` | `cycles[].service_calls` |
| Entities and history read | `ha_get`, `fetch_entity_history_range` | `cycles[].requests` |
| WARNING and above logged | module loggers | `cycles[].warnings` |
| State files written | `save_plan_state` (5785) and the other `NIMBUS_SOLVER_*_PATH` files | `files` |

## Part A: golden master

### Layout

```
tests/golden/
  fake_ha.py              FakeHA: urlopen replacement serving states, services, history
  child.py                runs main() in-process under freezegun, writes the record
  harness.py              run_isolated(): one fresh interpreter per scenario
  scenarios.py            Scenario, register(), names()
  scenarios_synthetic.py  Part A scenarios
  scenarios_nemweb.py     Part D scenarios
  nemweb/<folder>/        Part D inputs (NEMWEB files, manifest, nem_pd7day sensor)
  snapshots/<name>.json        uncompressed since #1359, so a real change is a readable diff
tests/test_golden_master.py      exact comparison
tests/test_golden_nonvacuity.py  each scenario reaches its path
scripts/golden_nemweb.py         Part D generator (not run by the suite)
```

### How a scenario runs

`main()` talks to Home Assistant through `urllib.request.urlopen`. `FakeHA`
replaces it and serves `GET /api/states/<id>`, `POST /api/states/<id>`,
`POST /api/services/<domain>/<service>` (with `?return_response` answered
from the scenario or refused), and `GET /api/history/period/...`. An entity
the scenario does not list answers 404, as a missing entity does live. Any
other endpoint, or any other host, raises: a refactor that starts reading
something new fails loudly instead of reading nothing.

Each scenario runs in its own interpreter (`python -m golden.child`), because
`solver_writer` holds module state and the suite has a known order dependence
(see Found while building it). The child gets:

- `TZ=UTC`, `NIMBUS_SOLVER_TIMEZONE=Australia/Brisbane`;
- every state file path (`NIMBUS_SOLVER_PLAN_STATE_PATH`, `_LOCK_PATH`,
  `_LOAD_ERROR_NOTIFIED_PATH`, `_SOLAR_DELIVERY_RATIO_PATH`) inside a fresh
  temporary directory, so nothing leaks between runs or machines;
- `PYTHONHASHSEED=0`, one BLAS thread, the Haswell OpenBLAS kernel and numpy's
  X86_V4 features off. These are the pins nem_pd7day needed for bit-exact
  `lstsq` across CI and development machines. For `guardrail_baseline` they
  make no difference locally; they are there so a numerical path does not
  make the gate flaky between machines.

`main()` runs under `freeze_time(instant, tick=False)`. A multi-cycle
scenario advances the clock by `cycle_minutes` per cycle over the same state
directory.

### What a record holds

For each cycle: every state posted (entity id, state, all attributes), every
service call, every request path, every WARNING or higher log record. After
the last cycle: every file in the state directory, parsed if JSON. The Python
version is recorded and not compared. When a comparison fails on a Python
other than the one the snapshots were recorded on (3.14, CI's), the failure
says so first. A state file that is not UTF-8 is recorded as its size and
SHA-256, so it is still compared.

### Canonical form

Keys sorted, floats round-tripped through `repr`, JSON gzipped with `mtime=0`.
Excluded key: `solve_seconds`, the solve's wall-clock duration and the only
such field in the package. Nothing else is excluded; `generated_at` and
every timestamp are stable under the frozen clock (three consecutive runs gave identical records).

### Scenarios (synthetic tier)

| Scenario | Purpose | Non-vacuity check |
|---|---|---|
| `guardrail_baseline` | the #363 fixture of `test_main_golden_output_guardrail.py`, now recorded whole | state -5.0, `total_cost` 26.9874, 202 periods, as that test asserts |
| `guardrail_two_cycles` | cycle 2 reads the `plan_state.json` cycle 1 wrote (proximal term) | `plan_state.json` written; cycle 2's `shadow_price` differs from a fresh run at the same instant (0.2999 against 0.3) |

More synthetic scenarios are added by the spec that needs them, before its
code moves (Migration).

### Coverage today

Measured by running every scenario's child under coverage: 1,035 of 3,215
statements in `solver_writer.py` (32%). 81 of 151 top-level functions are
entered; 38 of the 67 over 60 lines. Per function, for the largest:

| Function | Lines | Covered |
|---|---:|---:|
| `main` | 842 | 94.4% |
| `publish_plan` | 1,051 | 73.5% |
| `publish_daily_quality_report` | 393 | 30.0% |
| `compute_efficiency_backtest_report` | 242 | 12.8% |
| `_compute_report_for_window` | 1,280 | 5.7% |
| `build_controllable_loads` | 736 | 2.4% |
| `apply_commanded_state_guard` | 1,300 | 1.1% |

So today the golden master protects Phase 4 and Phase 5 well, Phase 2 poorly
and Phases 3 and 6 not at all. Each of those phases' specs adds the scenarios
that reach its functions (see the native gap below) and quotes the coverage
before it starts.

### The native gap — closed by #1335

`build_controllable_loads`, `apply_commanded_state_guard` and `_update_all`
return early when `_NATIVE_HASS is None`, which is the standalone path this
harness drives, so no scenario written for this spec could reach them.

`tests/golden/fake_native.py` (nimbus issue #1335) is the driver: the
in-process counterpart of `fake_ha.FakeHA`, fake enough for
`set_native_hass()` to be given a real-shaped `hass` inside a scenario's own
child process. `tests/golden/scenarios_native.py` adds the two scenarios that
use it. Re-measured the same way as the table above:

| Function | Covered, this spec | Covered, with the native scenarios |
|---|---:|---:|
| `build_controllable_loads` | 2.4% | 46.5% |
| `apply_commanded_state_guard` | 1.1% | 56.2% |

`kind=thermal` is still uncovered, deliberately — see #1335 and the
`scenarios_native` docstring.

### Update rule

`GOLDEN_UPDATE=1 python -m pytest tests/test_golden_master.py -p no:homeassistant`
rewrites the snapshots. Only a PR that intends a behaviour change may do so,
and it says why in its description. A refactor PR never does.

Since #1359 the rule is also *enforceable* rather than only stated. The
snapshots are plain `.json` and the regeneration is content-addressed, so a
run that records no changed value leaves `git status` clean and a real change
stands alone in the diff. `tests/test_golden_snapshot_format.py` pins the
canonical bytes, so a hand-edited or CRLF-converted snapshot fails there
rather than surfacing as a mystery diff in an unrelated PR.

They were `.json.gz` until then, and `_dump()` pinned `mtime=0` — but nothing
can pin zlib's own output, which differs between builds, so regenerating on a
different interpreter than the one that recorded a snapshot rewrote every
file's bytes with not one recorded value changed. Measured over 7 successive
one-value revisions of all 14 snapshots, text costs a larger working tree
(7.19 MB against 0.25 MB) and a **smaller** repository (0.25 MB of `.git`
against 0.92 MB), because text deltas across revisions and a gzip stream does
not. The `nemweb/` fixtures stay compressed: those are recorded inputs, never
read as a diff.

## Part B: recorded inputs (specified, not built)

### Why

Synthetic scenarios cover what someone thought of. The live install covers
what actually happens, including configurations no fixture has.

### Interface

`scripts/golden_record.py <ha_url> <scenario>`: reads the `requests` list of
an existing snapshot to learn which entities and history windows `main()`
reads, fetches exactly those from a live Home Assistant with a token from
the environment, strips identifying attributes (no address, NMI, account or
device serial; the de-identification list lives in the script), and writes a
`scenarios_recorded/<scenario>.json.gz` the harness serves like any other.

### Replay for the live gate

Record at four instants over the 24 hours after a Phase 5 or 6 deploy, replay
each through the previous and the new release, and require identical records.
This replaces comparing live values, which differ for market reasons.

## Part D: real market inputs

### Why

Nimbus's value is decided at price spikes, and synthetic prices do not have
the shape real ones do: price-cap forecasts days out that never arrive,
short-lead forecasts, and the occasional real spike that nothing forecast.
Part D puts the real NEM market of July to September 2026 in front of `main()`
through the same Home Assistant entities a household uses.

### What Nimbus reads, and where each comes from

Nimbus never reads NEMWEB itself.

| Entity (config key) | Built from |
|---|---|
| Regional spot forecast (`solver_regional_spot_forecast_sensor`), a `sensor.nem_pd7day_<region>_nem_spot_price_forecast` | the folder's PD7DAY run, through nem_pd7day's own parsers and golden harness |
| Regional spot price and its history (`solver_regional_spot_current_price_sensor`) | the region's 5-minute TradingIS prices before the frozen instant, 14 days (fewer where the harvest starts later) |
| Retail import and export prices and their history | TradingIS spot plus a fixed $0.12/kWh margin for import, spot for export |
| Retail price forecast array (`solver_price_forecast_array_sensor`) | the first hour only, from the PD7DAY raw price, so every later period reaches the LP through `fetch_aemo_forecast` (5238) and `compute_5min_offset` (5296), the path spikes take in production |

The household (40 kWh battery, 5 kW, no solar, a fixed load profile) is
synthetic. Only the market is real. The retail margin and array are
simplifications and are named as such: a retailer's real tariff and forecast
are not public data.

### The nem_pd7day sensor, generated not imitated

`scripts/golden_nemweb.py forecast <nem_pd7day checkout> <folder> <region> <now>`
runs purcell-lab/nem_pd7day's `PD7DayClient.fetch_all` on the committed
PD7DAY file, its STPASA parser on the committed STPASA file, the committed
market notices, and its golden harness (`tests/golden/harness.py` there) to
build the entities, then writes the forecast sensor's state and attributes in
two variants:

- `passthrough`: an empty calibration store, so `calibrated` equals the raw
  PD7DAY price. What a new install publishes, and the case in which every
  price-cap forecast reaches Nimbus whole.
- `fitted`: nem_pd7day's synthetic observation seed, fitted by its real
  calibration engine. The fit is synthetic; the inputs are real.

Every file records the nem_pd7day commit (`e789e6a`, branch
`claude/spec-000-nemweb`, [purcell-lab/nem_pd7day#186](https://github.com/purcell-lab/nem_pd7day/pull/186),
not yet merged) and the SHA-256 of each input it read. Forecast mode is
`days_1_7` for every region, because Nimbus plans from now; the live
reference install runs SA1, NSW1, QLD1 and VIC1 in `days_2_7`, which this does
not reproduce.

`scripts/golden_nemweb.py history <archive> <folder> <region> <now>` writes
the TradingIS rows, unchanged from NEMWEB, for 14 days before to 8 days after
`<now>`.

### Harvest and provenance

The files come from nem_pd7day's harvest (`scripts/nemweb_harvest.py`, its
spec 000 Part D): 182 PD7DAY runs, 1,367 STPASA runs, ten weekly TradingIS
archives (19 July to 19 September 2026) and the market notices. Each folder's
`manifest.jsonl` gives, per file, the NEMWEB URL, the zip SHA-256 and size.
`tests/test_golden_nonvacuity.py` checks every input hash the generated
sensor names against the committed file.

The five folders take 1.8 MB. The STPASA file (about 100 kB each) is not read
by Nimbus; it is kept so the sensor can be regenerated from the folder alone.

### Scenarios

Frozen at the first interval of the PD7DAY run, 07:31 NEM time (UTC+10), each
in both calibration variants:

| Scenario stem | Region | PD7DAY run | Passthrough: periods at or above $5/kWh import | Real outcome |
|---|---|---|---:|---|
| `qld_nemweb_short_lead_spike` | QLD1 | 2026-08-05 07:09 | 23 (peak $20.33) | none arrived |
| `nsw_nemweb_days27_endeavour` | NSW1 | 2026-08-05 07:09 | 47 (peak $23.32) | none arrived |
| `vic_nemweb_spike_interconnectors` | VIC1 | 2026-07-30 07:08 | 25 (peak $23.32) | none arrived |
| `sa_nemweb_cap_plateau` | SA1 | 2026-07-30 07:08 | 35 (peak $23.32) | none arrived |
| `sa_nemweb_spike_arrived` | SA1 | 2026-07-30 17:40 | 23 (peak $23.32) | $4,981/MWh at 02:30 on 31 July |

`sa_nemweb_spike_arrived` is frozen at 02:31 on 31 July, inside the only
5-minute interval above $1,000/MWh in the harvest window (SA1, $4,981/MWh
ending 02:35; the other is $3,844/MWh ending 02:55). The PD7DAY run before it
forecast neither. It arms the spike override
(`solver_price_spike_threshold` 1.0, `_override_armed` true,
`_discharge_kw` 5.0; `resolve_price_spike_override`, 4704).

### Invariants (tests/test_golden_nonvacuity.py)

- Passthrough: some PD7DAY spike reaches `import_price` at $5/kWh or more, and
  the battery discharges into at least one.
- Fitted: no period more than 30 minutes out has `import_price` at $5/kWh or
  more. The calibration removes them before Nimbus sees them.
- `sa_nemweb_spike_arrived`: `price_spike_active` is true in both variants, the
  first period is priced at the real spike and the battery discharges 5 kW.
- Every other passthrough scenario: `price_spike_active` is false. A forecast
  spike alone does not arm the override, because it reads `import_price[0]`
  only.

These record what Nimbus does, not what it should do. The passthrough plans
expect to earn between $537 and $1,587 over the horizon from spikes that did
not arrive; that is the input nem_pd7day's calibration exists to correct, and
the golden master pins it so a refactor cannot change it silently.

## Part C: gate tooling (specified, not built)

| Tool | Behaviour |
|---|---|
| `tests/gates/coverage_compare.py` | runs the golden scenarios and the suite under coverage on the base and head commits; fails if any line executed on base is unexecuted on head, mapped through the spec's moved-code list |
| `tests/gates/size_ratchet.py` | AST: fails on a new function over 60 lines or a rise in the count over 60 (67 at `d5b044b`) |
| `tests/gates/assertions_unchanged.py` | fails if an `assert` in an existing test changed, except lines the spec lists |
| `tests/gates/noop_patches.py` | for each `patch`/`monkeypatch.setattr` site, records whether the patched attribute is read during the test; fails when one that was read is no longer read (the #1316 failure mode) |
| import-linter contracts | the layer map of the plan, section 2, with today's violations recorded as exceptions |
| mypy ratchet | the advisory `typecheck` job's count, which may not rise |

## Found while building it

- **`test_main_golden_output_guardrail.py` passes once per machine.** It
  points `PLAN_STATE_PATH` at a fixed `/tmp/nonexistent_plan_state_golden_test.json`
  (test file line 384), which `main()` then writes. A second run reads that
  plan back through the proximal term, and `forecast[1].shadow_price` becomes
  0.2999 instead of 0.3. CI starts clean, so it passes there. Verified by
  deleting the file (pass) and rerunning (fail). The harness above gives every
  run its own state directory. Filed as #1331: the test should use `tmp_path`.
- **Order dependence.** Running `test_solver_writer_controllable_loads.py`
  before `test_commanded_state_guard_reports_its_own_failure.py` gives 35
  failures. Tracked in #1329: the file is imported twice, under two module
  names, and the second copy's run-state store replaces the first's.
- **`tests/test_gates_coverage_compare.py` needs `-p no:homeassistant` when
  run by hand, same as `test_main_golden_output_guardrail.py` above.**
  Without it, the file errors out during fixture setup with
  `pytest_socket.SocketBlockedError` -- the `homeassistant` pytest11 plugin
  (registered via entry_points, loaded for every invocation in the process,
  same mechanism `DEFAULT_SUITE_ARGS` in `coverage_compare.py` itself works
  around) opens an event-loop self-pipe socket, which `pytest-socket`
  blocks. CI's stub-suite job already passes this flag, so it is only a
  trap when the file is invoked directly. Also worth knowing before running
  it: the full file takes roughly 15 minutes locally (two full worktree
  checkouts, each running the golden scenarios and the whole stub suite
  under coverage), not a quick smoke check.
- **nimbus #1354: the gate reported PASS on a base side that measured ZERO
  covered lines.** "Every line covered at base is covered at head" is
  vacuously true over an empty set, so a run that measured nothing read
  identically, in both wording and exit code, to a run that measured
  everything and found no regression. `run()` now refuses to report PASS
  when the base side's covered-line count is 0, returning a third, distinct
  exit code instead of either 0 or 1. On this repo the zero-coverage case
  was itself caused by a second, Windows-only bug found in the same issue:
  the worktree root is built from `tempfile.gettempdir()`, which can return
  an 8.3 short path (`C:\Users\RAF_LO~1\...`) while coverage.py
  canonicalises the files it measures via `os.path.realpath` (the long
  form, `C:\Users\Raf_local\...`) -- no measured line ever matched a
  looked-up one. Fixed by canonicalising the worktree root once,
  immediately after creating it, via `Path(os.path.realpath(tmp))`, before
  it is handed to coverage or compared against anything.

## Migration

1. This PR: Part A harness, synthetic scenarios, Part D inputs and scenarios,
   the snapshots, the non-vacuity tests, this spec, the plan and the template.
2. #1310, against `0d0d553`, using Part A where it reaches the fleet code.
3. Part C tools, one PR each, each proven on a deliberately bad commit.
4. The native-mode driver, before #1302.
5. Phase specs, each adding the scenarios that reach its functions before any
   code moves.

## Non-goals

- Any change under `custom_components/`.
- Fixing the two defects above, or any behaviour the Part D scenarios pin.
- Solar in Part D. Solar forecasts from real irradiance data are a separate
  input and a separate spec.

## Acceptance

- [ ] `tests/test_golden_master.py` passes on CI's Python.
- [ ] `tests/test_golden_nonvacuity.py` passes.
- [ ] Three consecutive local runs give identical records.
- [ ] Full suite passes; no existing test edited.
- [ ] No file under `custom_components/` changed.
- [ ] Every Part D input hash matches its committed file.

## Rollback

Revert the PR. Nothing reads the new files outside the tests.
