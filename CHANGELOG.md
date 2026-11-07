# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versioned from `custom_components/nimbus_load/manifest.json` — the Nimbus Solver add-on (`nimbus_solver_app`) that used to share this version line was removed in v0.94.85 (see [#357](https://github.com/code-imstillalive/nimbus/issues/357)).

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [Unreleased]

## [0.100.0] - 2026-11-07

**A second whole-house meter can say so, and the Solver runs only inside Home Assistant.**

- **Power signal role "Whole house"** (#2350, #2299, reviewed by Mark Purcell). Some inverters report the whole house twice, such as a Sigenergy "Total Load" beside "Consumed Power". Give the second one the role **Whole house** (Reconfigure → Role) and the Power Flow card no longer draws it again as a circuit; the Forecast card charts it with the loads. Display only: forecasts and plans are unchanged.
- **The standalone Solver script is retired** (#2333). `solver_writer.py` no longer runs from cron or the Supervisor; Nimbus's own integration has run the Solver for every install for some time. If you still run the old standalone script, see the README.
- Internal (all reviewed by Mark Purcell): the Solver reads and writes only through Home Assistant, and the REST transport and its token handling are gone (#2337, #2341); the regression tests run every scenario the way installs do; the first two steps of splitting the Solver's largest file (#2346, #2347).

## [0.99.0] - 2026-11-07

**Topology: a circuit's own devices are easier to read; Power Flow says when it is waiting for a plan.**

- **Topology** (#2338, #2343, #2344): a circuit with several devices under it now has one line down to them, with one arrow, and each device has its own arrow into its tile, indented further (64 px) and, while drawing power, in a deeper shade of purple than its circuit. The arrows no longer pile on top of each other.
- **Power Flow says "Waiting for the plan"** (#2340) for the minute after Nimbus reloads, such as after saving a circuit's name, instead of keeping the last plan's "On plan" and "Solar xx% of forecast" up with no Buy/Sell price.
- **Power Flow: no line through Grid** (#2342). With two or more inverters, Grid now sits a little below their row, so power going from one inverter to the other no longer runs through the Grid bubble on top of Grid's own lines. The Buy/Sell price stays above the row.
- Internal: long incident notes moved out of five more modules' code comments into history files (#2331, #2332, #2334–#2336).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 7d67af49** (`release/0.99.0`, installed untagged through Home Assistant's marketplace; the checks below were read at 12:43 on `eba97443`, which differs only by the sub-device colour, and devhub was restarted onto `7d67af49` at 12:50).
  - 123 of the 127 Nimbus entities carrying a version read 0.99.0; the other four are the daily reports.
  - Re-read on `7d67af49` at 12:58: the same 123 at 0.99.0, none of the 388 unavailable, plan `optimal` (solved 12:58), all 62 Solver numbers present.
  - None of the 388 Nimbus entities unavailable; plan `optimal` (solved 12:43); all 62 Solver numbers kept their values over the restart.
  - Help's What's New shows 0.99.0. No Nimbus Repairs. Log: no ERROR.
  - The cards are served with every change: the Power Flow card carries "Waiting for the plan" and Grid's lower row; the Topology card carries the 64 px indent and the deeper purple (`#6a2486`, served at 12:51).
  - The plan sensor had no forecast for 53 s after the restart, and the plan returned at 12:28:51. That is the window "Waiting for the plan" covers, the same ~50 s measured on a reload at 11:27.
  - On screen, checked by the household on devhub: Power Flow with Grid below the inverter row and no line through it; Topology with sub-devices indented further and, after a second, deeper shade (#2344), in a purple they said reads distinctly ("thats better"). "Waiting for the plan" itself was not watched on screen; the 53 s window was measured on the plan sensor.
- Consumer check: no setting or entity is moved or renamed; the changes are to the Power Flow and Topology cards' drawing only.

## [0.98.0] - 2026-11-07

**"Part of another circuit" is now recorded in the device contract.**

- **Device setup** (#2323, #2293, reviewed by Mark Purcell). A Load or plain Power Signal set as "part of" a circuit is recorded in the device contract as that circuit's sub-device, and it's counted once, inside its circuit. A plain ("Other") power signal on its own sensor is now a circuit in the contract. One on a sensor Nimbus already reads, such as the battery or grid meter, stays as it was. With device setup in `active`, setting "part of" shows **Settings → Repairs → Keep my change** once, which records it. Display only: forecasts and plans are unchanged.
- Internal: the regression tests now also run every scenario in the mode real installs use (#2324–#2326), and long incident notes moved out of two modules' code comments into a history file (#2327, #2328).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at a3823f28** (`release/0.98.0`, installed untagged through Home Assistant's marketplace and restarted at 09:19 AEST, 7 Nov; read through 09:22).
  - 123 of the 127 Nimbus entities carrying a version read 0.98.0; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable; plan `optimal` (solved 09:21); all 62 Solver numbers kept their values over the restart.
  - Help's What's New shows 0.98.0.
  - The device contract (`active`) stayed at revision 4, holding the test pairing's `part_of` that "Keep my change" recorded on the PR build (#2323). The preview is clean, and there are no Nimbus Repairs.
  - The Repair-and-keep path itself was exercised on the PR build: one `device_contract_drift`, kept, revision 3 → 4.
  - Not seen live: a plain signal on its own sensor becoming a circuit, because the test box's plain signals are all on hub sensors. It's covered by the import test.
  - Log: no ERROR.
- Consumer check: no setting or entity is moved or renamed. A Load's or plain Power Signal's `part_of` is now an owned key in device setup, so in `active` a change to it shows as drift until kept. Forecasts and plans are unchanged.

## [0.97.0] - 2026-11-07

**A circuit's own devices, shown under it; Regret cards that recover after a restart.**

- **Part of another circuit** (#2310, #2293). A Load or a plain Power Signal can say it is part of another circuit, such as a pool pump wired on Circuit 1 or a fridge on the kitchen circuit (Add or Reconfigure → *Part of another circuit*).
  - **Power Flow** lists it under the circuit's bubble with its power, and the rest as "other", instead of as a bubble of its own.
  - **Topology** shows its tile indented under the circuit's.
  - One level only: a circuit with devices under it can't itself be part of another. Removing the circuit unlinks them.
  - When the Energy Dashboard's "Upstream device" says where a meter sits, the question names it. That's a hint only; nothing is chosen for you.
  - Display only: forecasts and plans are unchanged.
- **Regret cards recover after a restart** (#2320). A Home Assistant restart while the cards were loading left them on "Couldn't score yesterday: … not found" until the page was reloaded. An error that clears once Nimbus is back up is now tried again after 15 s, 30 s and 60 s. The 2- and 3-days-ago cards name their own date in an error.
- **Solver settings saved through the API keep their advanced fields** (#2311). A save that left a whole section out cleared it; it now keeps what is stored. Saves made in the UI were never affected.
- **A smaller database** (#2317). The plan sensor no longer records two attributes it recomputes every solve, about half of each recorded row. What you see is unchanged.
- **Topology's "nothing set up yet" banner** is readable on a light theme (#2318).
- Internal: strict type checks now cover 39 more modules (#2313–#2316), and a design note covers splitting the Solver's largest file (#2312).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at dc486a3e** (`release/0.97.0`, installed untagged through Home Assistant's marketplace and restarted at 06:22 AEST, 7 Nov; read through 06:25).
  - 123 of the 127 Nimbus entities carrying a version read 0.97.0; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable; plan `optimal`, with `solved_at` 06:23; all 62 Solver numbers kept their values over the restart.
  - Cards are served at `?v=0.97.0-<digest>`.
  - The test pairing (a lighting load set as part of the COMMS circuit) carried `part_of_source_sensor`. Power Flow listed it under the COMMS bubble with "other", on screen. Topology and the Reconfigure form were checked on screen on the PR build (#2310).
  - The Regret cards recovering after a restart was checked live on the PR build (#2320): all three scored about 90 s after a restart, with no reload.
  - Help's What's New shows 0.97.0.
  - Not seen live: the Energy Dashboard hint (the test box sets no Upstream device), the wizard save fix (an API-only path, covered by its CI test) and the Topology banner on a light theme (the test box's Topology has data, so no banner).
  - Log: no ERROR.
- Consumer check: no setting or entity is moved or renamed. Loads and Power Signals gain an optional `part_of` field, and forecast sensors gain a `part_of_source_sensor` attribute. The plan sensor stops recording `solar_quantile_calibration` and `cost_breakdown_by_day`, which are still live on the state. Plans are unchanged.

## [0.96.0] - 2026-11-06

**Fewer needless re-sends and notices, and the plan's status says when it last solved.**

- **A device already set as commanded isn't re-sent** (#2298, #2297). Nimbus judges whether a controllable load follows its plan from the load's power sensor. A water heater set to the commanded mode, or a switch already on, that wasn't drawing power (its thermostat satisfied, say) was re-sent up to 20 times a day, and then reported as if something else were writing to it. Nimbus now checks the device's own setting first. If it already matches, nothing is re-sent, and the log says once a day that the device is set as commanded but isn't drawing power. An unreadable setting, or a failed last command, re-sends as before.
- **The LocalVolts notice is asked once** (#2307). With LocalVolts v2 installed, Nimbus said at every start that LocalVolts price fields were empty. It now asks once, again only if the set of empty fields changes, and not at all when your import or export price comes from another retailer.
- **`solved_at` on Solver LP Status** (#2305, #2295). Home Assistant moves no timestamp when a state doesn't change, so a plan that stayed `optimal` looked frozen. `sensor.nimbus_solver_lp_status` now carries the time of the latest solve, live only (not recorded).
- **Power Flow: no house total drawn twice** (#2306, #2299). The whole-house cross-check sensor and every further household load forecast's source are no longer drawn as circuits, as the load forecast's own source already wasn't.
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at bfbf6fc1** (`release/0.96.0`, installed untagged through Home Assistant's marketplace and restarted at 19:11 AEST, 6 Nov; read through 19:14). 123 of the 127 Nimbus entities carrying a version read 0.96.0; the other four are the daily reports. None of the 388 Nimbus entities unavailable; plan `optimal`; all 62 Solver numbers kept their values over the restart. `sensor.nimbus_solver_lp_status` carried `solved_at` 19:12:00, then 19:13:00 on the next solve, while its state stayed `optimal` and `last_changed` stayed at the restart. Not seen live: the re-send change, because the test box's controllable loads had no device set as commanded and not drawing power during the read; the LocalVolts notice, because the test box's LocalVolts readings come from copies, not the LocalVolts v2 integration; the Power Flow change, because the test box's cross-check sensor is the load forecast's own source. Each is covered by its CI test. Log: no Nimbus ERROR. The Nimbus WARNING lines are the test box's known ones: AEMO forecast disagreement, a flex-telemetry clamp, two skipped ticks and a deterministic-too-slow stochastic fallback in the first minutes after the restart, an implausible meter row and an empty temperature forecast. None comes from this release.
- Consumer check: no setting or entity is added, moved or renamed. `sensor.nimbus_solver_lp_status` gains a `solved_at` attribute, not recorded. A controllable load already set as commanded is no longer re-sent, so it can show fewer re-sends; that is the intended change. The LocalVolts notice is shown once, using a new per-entry store (`nimbus_load.asked_<entry_id>`). Plans are unchanged.

## [0.95.0] - 2026-11-06

**A new Power Flow layout with room for every circuit, and a reminder when your dashboard has no "Check for updates" button.**

The first release under the new numbering (#2289): a release you would notice moves the middle number.

- **Power Flow layout** (#2294). Grid sits on the inverters' row between them, House in the centre below, and your circuits fan out underneath House, widening up to a half circle as you add them (up to twelve), with room left for the devices inside a circuit to come. An EV is a darker green and sits beside House; up to four EVs fit clear of the circuits.
- **"Check for updates" reminder** (#2300). Tabs pasted before 0.94.507 have no Nimbus Tabs card, so no *Check for updates* button, and Nimbus never edits your dashboard to add it. Settings → Repairs now says how to add it in four clicks when a dashboard shows Nimbus cards but none has the Tabs card. It only reads your dashboards, and clears itself once the card is on one.
- **Cards reload when they change** (#2301). A card's web address now changes whenever its file does, so a browser can't keep showing an old card after an update.
- Internal: the arithmetic for a meter inside another meter (a pool pump inside a circuit) is in place for the Power Flow and Topology cards to use (#2296); nothing reads it yet, and plans are unchanged. Shipped code no longer points readers at a private repository (#2292).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 482865e8** (`release/0.95.0`, installed untagged through Home Assistant's marketplace and restarted at 17:32 AEST, 6 Nov; read through 17:36). 123 of the 127 Nimbus entities carrying a version read 0.95.0; the other four are the daily reports. None of the 388 Nimbus entities unavailable; plan `optimal` on the first solve; all 62 Solver numbers kept their values over the restart. Every card is served at `?v=0.95.0-<digest>` and the browser loaded those addresses. The Power Flow tab was opened on screen: Grid on the inverters' row between them, House in the centre below, the test EV beside it, six circuits fanned below House, nothing overlapping. Help's What's New shows 0.95.0. The "Check for updates" Repair is correctly absent, since the test box's dashboard has the Tabs card; its raised case was not seen live, because showing it would mean removing the card from the household's dashboard, and it is covered by the CI test instead. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (solver phase timing, flex-telemetry clamp, an implausible meter row, an empty temperature forecast), none from this release.
- Consumer check: no setting, entity or attribute is added, moved or renamed. A new Repair (`tabs_card_missing`) can appear; it reads dashboards and writes none. Card addresses gain a file digest, which a dashboard does not reference, so nothing needs changing. The Power Flow card's layout changes on the standard tab with no config change. Plans are unchanged.

## Earlier releases

Older entries are kept word for word in `docs/changelog/`, newest first:

- [0.94.450 to 0.94.521](docs/changelog/0.94.450-0.94.521.md) (72 releases)
- [0.94.400 to 0.94.449](docs/changelog/0.94.400-0.94.449.md) (49 releases)
- [0.94.300 to 0.94.399](docs/changelog/0.94.300-0.94.399.md) (99 releases)
- [0.94.0 to 0.94.299](docs/changelog/0.94.0-0.94.299.md) (298 releases)
- [0.64.0 to 0.93.0](docs/changelog/0.64.0-0.93.0.md) (38 releases)

---

## How to write a changelog entry

Sections use these labels (in order): **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**. Omit any that don't apply.

One line per user-visible change, present tense, human phrasing (not the commit subject verbatim). Link the relevant PR (`[#NN]`) or, when there is no PR, the commit hash (`[abcdef1]`). If an issue drove the change, prefer linking the issue over the commit.

New PRs add entries under `## [Unreleased]`. When a release PR lands, that PR renames `## [Unreleased]` to the new `## [X.Y.Z] — YYYY-MM-DD` heading and creates a fresh empty `## [Unreleased]` above it. See `.github/PULL_REQUEST_TEMPLATE.md` for the release-PR checklist.
