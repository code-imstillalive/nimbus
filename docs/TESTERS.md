# Testers

Real installs, tracked by version, so a bug report always carries its
own anchor — "it broke" is much less useful than "it broke on v0.73.0,
here's the traceback."

## Active

| Tester | Hardware / setup | First install | Notes |
|---|---|---|---|
| Raf ([@code-imstillalive](https://github.com/code-imstillalive)) | Reference household — 2 NUCs (keepalived HA pair), 18 circuit-breaker loads, 2 inverters, 4 battery towers, LocalVolts P2P export contract | Project origin | The only install with real live-money dependence (P2P export automation) — the Solver itself has never had operational control here, deliberately, see the "Nimbus → HAEO Replacement Readiness Checklist" in the sibling `116KAT-HA-AI` repo. |
| Mark Purcell ([@purcell-lab](https://github.com/purcell-lab)) | Independent hardware, own Home Assistant install (Sigen inverter/battery — different manufacturer from the reference household, a real, valuable cross-hardware test) | 2026-08-22 | First genuine external install. Found real gaps within hours both times a new version shipped: [#79](https://github.com/code-imstillalive/nimbus/issues/79)/HACS private-repo auth, then the two v0.73.0-era regressions ([#82](https://github.com/code-imstillalive/nimbus/issues/82), thread-safety crash — root-caused precisely from his own traceback, fixed same day as v0.73.1). Also the author of PR #77 (SensorEntity migration), PR #54, PR #81 (README refresh), and issue #36 (this DevOps proposal) — a genuine contributor, not just a tester. |

## What to capture in a bug report

Per issue #36's own proposal — carries the report's own version anchor
so a fix can be verified against the exact same conditions:

- **Nimbus version** — `custom_components/nimbus_load/manifest.json`'s
  `version` field (or the HACS-shown version).
- **Home Assistant Core version.**
- **Install method** — HACS (native in-process Solver) or a standalone
  cron script (see `docs/real-world-integration/`). The `nimbus_solver_app`
  Supervisor add-on was removed in v1.0.0 — if you're reporting against an
  install older than that, note the exact version too.
- **Real hardware/integration** feeding the Solver's Battery/Grid/Solar
  sensors (inverter brand, price-sensor source) — genuinely different
  hardware has already surfaced real gaps (see Mark's row above).
- **What's actually broken** — the specific entity/sensor, its real
  state, and (if available) the relevant Home Assistant log lines. A
  full traceback, when one exists, is the single most useful thing a
  report can include — see #82 for how precisely a good traceback can
  pin a root cause.

## Adding yourself

Genuinely installed and running Nimbus somewhere? Open a PR adding a
row above, or ask in an issue and it'll get added. No formal
alpha/beta/stable channel exists yet (see issue #36's own step 5) —
this list is currently just "who to credit and who to ask" when
triaging a report, not a gated program.
