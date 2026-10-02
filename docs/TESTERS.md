# Testers

Real installs, tracked by version, so a bug report always carries its
own anchor — "it broke" is much less useful than "it broke on v0.73.0,
here's the traceback."

## Start here

**New and want to try it? Read [`setup-guide.md`](setup-guide.md).** Part 1 is the
minimum that gets Nimbus producing a real plan — five required fields — and ends
with a verification step so you can tell whether it worked. Part 2 is everything
else.

Two things worth knowing before you install, both covered there in full:

- **Nimbus publishes a battery plan; it does not command your inverter.** Wiring
  the plan to hardware is an automation you write. The one exception is a
  **Controllable Load** with a device entity set — that Nimbus *does* switch
  directly.
- **The Solver needs `highspy`**, which has prebuilt wheels for **amd64 and
  aarch64 only**. On 32-bit ARM it will not start. The Forecaster still works.

## Active

| Tester | Hardware / setup | First install | Notes |
|---|---|---|---|
| Raf ([@code-imstillalive](https://github.com/code-imstillalive)) | Reference household — 2 NUCs (keepalived HA pair), 18 circuit-breaker loads, 2 inverters, 4 battery towers, LocalVolts P2P export contract | Project origin | The only install with real live-money dependence (P2P export automation), and the Solver **does** drive real dispatch here — it graduated out of observe-only shadow mode, so a bug on this install can cost real money. Readiness history lives in the "Nimbus → HAEO Replacement Readiness Checklist" in the sibling `116KAT-HA-AI` repo. (This row said the Solver had "never had operational control here" until 2026-09-15; [#749](https://github.com/code-imstillalive/nimbus/issues/749) corrected that claim across the docs on 09-11 by searching for the words "shadow mode", which this differently-worded copy of it escaped.) |
| Mark Purcell ([@purcell-lab](https://github.com/purcell-lab)) | Independent hardware, own Home Assistant install (Sigen inverter/battery — different manufacturer from the reference household, a real, valuable cross-hardware test) | 2026-08-22 | First genuine external install. Found real gaps within hours both times a new version shipped: [#79](https://github.com/code-imstillalive/nimbus/issues/79)/HACS private-repo auth, then the two v0.73.0-era regressions ([#82](https://github.com/code-imstillalive/nimbus/issues/82), thread-safety crash — root-caused precisely from his own traceback, fixed same day as v0.73.1). Also the author of PR #77 (SensorEntity migration), PR #54, PR #81 (README refresh), and issue #36 (this DevOps proposal) — a genuine contributor, not just a tester. |

## What to capture in a bug report

Per issue #36's own proposal — carries the report's own version anchor
so a fix can be verified against the exact same conditions:

- **Nimbus version** — `custom_components/nimbus_load/manifest.json`'s
  `version` field (or the HACS-shown version).
- **Home Assistant Core version.**
- **Install method** — HACS (native in-process Solver) or a standalone
  cron script (see `docs/real-world-integration/`). The `nimbus_solver_app`
  Supervisor add-on was removed in **v0.94.85**
  ([#357](https://github.com/code-imstillalive/nimbus/issues/357)) — if you're
  reporting against an install older than that, note the exact version too.
  (This line said "v1.0.0" until 2026-09-15; no such version has shipped.)
- **Real hardware/integration** feeding the Solver's Battery/Grid/Solar
  sensors (inverter brand, price-sensor source) — genuinely different
  hardware has already surfaced real gaps (see Mark's row above).
- **What's actually broken** — the specific entity/sensor, its real
  state, and (if available) the relevant Home Assistant log lines. A
  full traceback, when one exists, is the single most useful thing a
  report can include — see #82 for how precisely a good traceback can
  pin a root cause.

### The fastest way to capture most of that

Developer Tools → **Template**, paste this, and paste the output into the issue.
It carries the version anchor and the Solver's real state in one go:

```jinja
Nimbus:      {{ state_attr('sensor.nimbus_solver_solve_seconds','nimbus_version') }}
Config:      {{ states('sensor.nimbus_solver_config') }}
LP status:   {{ states('sensor.nimbus_solver_lp_status') }}
Solve time:  {{ states('sensor.nimbus_solver_solve_seconds') }} s
Periods:     {{ states('sensor.nimbus_solver_n_periods') }}
Capacity:    {{ states('number.nimbus_solver_battery_capacity_kwh') }} kWh
SoC now:     {{ states('sensor.nimbus_solver_current_soc_pct') }} %
Load now:    {{ states('sensor.nimbus_solver_current_load_kw') }} kW
Why:         {{ states('sensor.nimbus_solver_binding_constraint_now') }}
Health:      {{ states('sensor.nimbus_health_report') }} errors
Load source: {{ state_attr('sensor.nimbus_household_load_total_forecast','load_forecast_source_used') }}
```

Two lines in that block answer the most common false alarms before anyone
investigates: **`Capacity`** reading `0.1` means the live `number.*` entities were
never set, so the plan is meaningless rather than wrong; and **`Load source`** names
which load-forecast field actually won, which settles the silent-override trap
documented in the README.

Also attach `sensor.nimbus_health_report`'s own `recent_errors` attribute — it holds
up to 20 recent errors with detail, so you rarely need the raw log file.

## Adding yourself

Genuinely installed and running Nimbus somewhere? Open a PR adding a
row above, or ask in an issue and it'll get added. No formal
alpha/beta/stable channel exists yet (see issue #36's own step 5) —
this list is currently just "who to credit and who to ask" when
triaging a report, not a gated program.
