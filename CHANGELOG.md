# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versioned from `custom_components/nimbus_load/manifest.json` — the Nimbus Solver add-on (`nimbus_solver_app`) that used to share this version line was removed in v0.94.85 (see [#357](https://github.com/code-imstillalive/nimbus/issues/357)).

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [Unreleased]

## [0.104.0] - 2026-11-13

**Device setup finds a Sigenergy system, and Nimbus runs on HiGHS 1.16.**

- **A Sigenergy system fills its own devices in Device setup** (#2523, #2526, for #2514, reviewed by Mark Purcell). With the `sigen` integration installed, **Configure → Device setup → Review what Nimbus finds** proposes the battery's power and charge level, the solar power, the plant's Total Load as the Whole house, and the grid's import and export power, found by the integration's own keys, so a renamed entity is still found. The battery's power reads positive while charging on that key, and the proposal records it. The plant has no single net grid sensor, so the net is worked out as import minus export and no template helper is needed; an Energy Dashboard net grid power, where you have one, is kept instead. A sensor the Energy Dashboard already names differently becomes a question, never overridden. Two Sigenergy plants propose nothing rather than guess. Nothing changes until you review and apply the proposal.
- **HiGHS 1.16 is allowed** (#2522, evaluated in #2520 with Mark Purcell). The solver library may now be 1.15.1 up to, not including, 1.17. On 1.16 plans cost the same; only ties between equally good plans can break differently.
- Internal: the support matrix lists equipment profiles, and its test checks them (#2514). Tests 963 → 964.
- Install shapes (#2394): #2523 and #2526 touch only a Sigenergy install; they were built from keys read live on Mark Purcell's install and are unseen on any install until he or Justin reviews a proposal. #2522 touches every install; devhub ran HiGHS 1.16 against 1.15.1 in #2520.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at d15ab1fa** (`release/0.104.0`, installed untagged through Home Assistant's marketplace and restarted at 15:17 AEST, 13 Nov; read through 15:20).
  - 123 of the 127 Nimbus entities carrying a version read 0.104.0; the other four are the daily reports (0.103.0).
  - The same 388 Nimbus entity ids as a read at 15:16 before the install, none unavailable, no name changed; plan `optimal` on the first solve after the restart (15:19); all 62 Solver numbers present with the same values as at 15:16.
  - #2522: Home Assistant installed HiGHS 1.16.0 on the restart, and that first solve ran on it.
  - Help's What's New shows 0.104.0. No Repairs at all. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (two P5MIN/AEMO price diagnostics, an implausible meter row), none from this release.
  - Not seen live: the Sigenergy profile (#2523, #2526), since devhub has no Sigenergy system. It is covered by the tests in its PRs, built from keys read live on Mark Purcell's install.
- Consumer check: no setting, entity id or name is moved or renamed. A Sigenergy install sees a new proposal in Device setup, applied only when reviewed. Any install may move to HiGHS 1.16 on its next restart.

## [0.103.0] - 2026-11-11

**Fewer false Repairs, the Regret card on your own midnight, and a temperature forecast that survives a reload.**

- **An entity that reads `unknown` on purpose is not a Repair** (#2502). 0.102.0's Repair for a chosen entity that has gone quiet also named one reading `unknown` by design while its integration still reported it: a retailer's spot rate on the unmatched share is empty while import is fully matched, for hours, every evening. An `unknown` reported within the last hour now counts as an answer. `unavailable`, a missing entity and an `unknown` that has stopped being reported still raise it.
- **The Regret card scores each day from Home Assistant's own midnight** (#2506). It asked for `<day>T00:00:00+10:00`, Brisbane's offset, so every install in another zone, or on daylight saving, was scored on Brisbane's midnight. It now sends local midnight, which Nimbus reads in Home Assistant's time zone. Brisbane installs see no change.
- **A temperature forecast that worked survives a reload** (#2443, for #2442, reviewed by Mark Purcell). The last good forecast is kept per weather entity across a settings save or reload, and a first empty fetch is asked again before it warns, so one reload no longer logs four "yielded 0 forecast entries" warnings at once.
- Internal: every module, all 137 files, is in the blocking strict type check, and the quality scale's `strict-typing` is done, the last rule left (#2477–#2503 under #2392, reviewed by Mark Purcell). A test runs the device import over three install shapes, the test box, the reference household and Mark Purcell's install (#2504, #2394). Tests 961 → 963.
- Install shapes (#2394): #2502 touches every shape (any chosen entity); its false Repair was seen live on the reference household, and the change is unseen elsewhere until installed. #2506 touches every shape with a Regret card; the reference household is in Brisbane, so a zone difference is unseen live on any of the three. #2443 touches any install with a temperature forecast; its warnings were seen live on Mark Purcell's install, and the fix is unseen there until he installs it.
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 0974108c** (`release/0.103.0`, installed untagged through Home Assistant's marketplace and restarted at 06:34 AEST, 11 Nov; read through 06:36).
  - 123 of the 127 Nimbus entities carrying a version read 0.103.0; the other four are the daily reports (0.102.0).
  - The same 388 Nimbus entity ids as a read at 06:22 before the install, none unavailable, no name changed; plan `optimal` on the first solve after the restart (06:35); all 62 Solver numbers present with the same values as at 06:22.
  - #2506: the served `nimbus-regret-card.js` carries no `+10:00`; `compute_quality_report` called as the card now calls it (`2026-11-10T00:00:00` to `2026-11-11T00:00:00`, no offset) answered in 5 s with the window 14:00 UTC to 14:00 UTC, local midnight to midnight.
  - Help's What's New shows 0.103.0. No Nimbus Repairs. Log: no Nimbus ERROR; the four Nimbus WARNING lines are the test box's known ones (the flex-telemetry clamp, the test EV's unreachable departure target and its SoC below floor, an implausible meter row), none from this release.
  - Not seen live: #2502's skipped Repair (no chosen entity on devhub reads `unknown` for an hour) and #2443's reload path (no temperature forecast went empty). Both are covered by the tests in their PRs; #2502's case was seen on the reference household on 10 Nov.
- Consumer check: no setting, entity id or name is moved or renamed. A chosen entity reading `unknown` while still reported no longer raises the Repair. Regret days outside Brisbane's zone, or on daylight saving, move to the install's own midnight.

## [0.102.0] - 2026-11-10

**A Repair names an entity you chose that has gone quiet, and entity names and icons come from translations.**

- **A Repair for a chosen entity that stays unavailable** (#2437, for #2436, reviewed by Mark Purcell). When an entity picked in setup reads `unavailable` or `unknown`, or is missing, for an hour, **Settings → Repairs** names it and every setting that uses it, and clears itself once it reports again. The hour counts from Home Assistant's start, so a restart never raises it. An availability entity is left out, because `unavailable` can be its real answer.
- **Entity names come from translations** (#2411, quality scale `entity-translations`). All 241 names move into `strings.json`, word for word; names and entity ids are unchanged.
- **Icons come from translations** (#2412, quality scale `icon-translations`). Each action has an icon; the three entities that set one in code show the same icon as before.
- Internal: every part of Nimbus with code is now at least 95% covered by tests, 97.9% overall (#2413–#2479 under #2393; the count goes from 956 to 961, mostly as cases added to existing tests), and the quality scale's `test-coverage` is done (#2476).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 68ebe8a5** (`release/0.102.0`, installed untagged through Home Assistant's marketplace and restarted at 07:43 AEST, 10 Nov; read through 07:47).
  - 123 of the 127 Nimbus entities carrying a version read 0.102.0; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable; plan `optimal` on the first solve after the restart (07:45); all 62 Solver numbers present with the same values as a read taken at 07:31 before the install.
  - Names and ids unchanged by #2411: the same 388 Nimbus entity ids as before, and none of their names differs from the 07:31 read.
  - Icons by #2412: Home Assistant serves `mdi:list-status`, `mdi:cash-sync` and `mdi:file-tree` for the three entities from `icons.json`, and an icon for each Nimbus action.
  - Help's What's New shows 0.102.0. No Repairs at all. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (forecast residual drift, the flex-telemetry clamp, the test EV's unreachable departure target, an implausible meter row, the P5MIN diagnostic, an empty temperature forecast), none from this release.
  - Not seen live: the new Repair (#2437) raising, since no chosen entity on devhub has been unavailable for an hour. It is covered by the tests in its PR.
- Consumer check: no setting, entity id or name is moved or renamed; three icons now come from `icons.json` and look the same. A new Repair kind, `configured_entity_unavailable`, can appear in Settings → Repairs.

## [0.101.0] - 2026-11-08

**Setup refuses an entity that does not exist, errors read in plain words, and new installs start with fewer entities switched on.**

- **The setup wizard refuses an entity that does not exist** (#2404, reviewed by Mark Purcell). A mistyped entity id is refused on the form with "entity not found", and nothing is created. An entity that exists but is not reporting yet (unavailable or unknown) is still accepted, with a log warning, and revoking control of a device always goes through.
- **Diagnostic entities start disabled on new installs** (#2403, reviewed by Mark Purcell). The 86 diagnostic entities start switched off, except the 29 that Nimbus's own tabs and cards show. Existing installs keep every entity as it is; Home Assistant applies this only when an entity is first created.
- **Every error a household can see is translated** (#2401): the messages from actions and setup read in plain words instead of internal codes.
- **The Set controllable load action** is described like the other actions (#2400).
- Docs: what Nimbus provides, what people use it for, how often it updates, and its known limitations (#2396–#2399).
- Internal: a read-only replay that compares staleness weightings for the price blend (#2405, for #1786, reviewed by Mark Purcell); the test cap counts a parametrised test once (#2406).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at e19f0c28** (`release/0.101.0`, installed untagged through Home Assistant's marketplace and restarted at 18:51 AEST, 8 Nov; read through 18:54).
  - 123 of the 127 Nimbus entities carrying a version read 0.101.0; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable; plan `optimal` (solved 18:53); all 62 Solver numbers kept their values over the restart.
  - Help's What's New shows 0.101.0. No Repairs at all. Log: no Nimbus ERROR.
  - Existing install unchanged by #2403: the same 388 Nimbus entities are present and none was disabled, as Home Assistant applies "disabled by default" only to entities created for the first time.
  - Not seen live: the wizard refusing a mistyped entity (#2404). It is covered by the real Home Assistant test on CI (three cases), not exercised on devhub.
- Consumer check: no setting, entity or card is moved or renamed. New installs get the 57 diagnostic entities Nimbus's own tabs do not show switched off; existing installs keep every entity as it is. Error messages change wording only.

## [0.100.1] - 2026-11-08

**Internal only: the Solver's code is reorganised and more of it is type-checked. Plans are unchanged.**

- **The Solver's main program is split up** (#2289 item 6, reviewed by Mark Purcell). `solver_writer.py` goes from 8,786 lines at 0.100.0 to 4,231, the rest now in modules by job (inputs, prices, P2P, reports, publishing), and its main program is nine named steps in order: settings, inputs, price sources, price blend, limits, price arrays, battery, solve and publish (#2349, #2351–#2353, #2358, #2359, #2361, #2362, #2364, #2365, #2368, #2371, #2374–#2381). Every move is word for word; the regression scenarios show the same plans.
- **Strict type checking** now covers every part of Nimbus that does not use Home Assistant itself, 60 modules in all (#2289 item 9: #2370, #2384–#2387, reviewed by Mark Purcell).
- **Changelog**: releases before 0.95.0 move word for word into `docs/changelog/` (#2383).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 1260be25** (`release/0.100.1`, installed untagged through Home Assistant's marketplace and restarted at 11:02 AEST, 8 Nov; read through 11:06).
  - 123 of the 127 Nimbus entities carrying a version read 0.100.1; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable. Plan `optimal` on the first solve after the restart (11:03) and on the next two (11:04, 11:05), all run by the nine-phase `main()`.
  - All 62 Solver numbers present, with the same values as a read taken at 09:38 before the install.
  - Help's What's New shows 0.100.1. No Repairs at all. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (solver phase timing, flex-telemetry clamp, an implausible meter row, an empty temperature forecast), none from this release.
  - Plans unchanged: checked by the 22 regression scenarios on CI for every part, not by comparing live plans, which move with prices from one solve to the next.
- Consumer check: nothing a household sees changes. No setting, entity, attribute or card is added, moved or renamed; the one visible difference is the What's New line.

## [0.100.0] - 2026-11-07

**A second whole-house meter can say so, and the Solver runs only inside Home Assistant.**

- **Power signal role "Whole house"** (#2350, #2299, reviewed by Mark Purcell). Some inverters report the whole house twice, such as a Sigenergy "Total Load" beside "Consumed Power". Give the second one the role **Whole house** (Reconfigure → Role) and the Power Flow card no longer draws it again as a circuit; the Forecast card charts it with the loads. Display only: forecasts and plans are unchanged.
- **The standalone Solver script is retired** (#2333). `solver_writer.py` no longer runs from cron or the Supervisor; Nimbus's own integration has run the Solver for every install for some time. If you still run the old standalone script, see the README.
- Internal (all reviewed by Mark Purcell): the Solver reads and writes only through Home Assistant, and the REST transport and its token handling are gone (#2337, #2341); the regression tests run every scenario the way installs do; the first two steps of splitting the Solver's largest file (#2346, #2347).
- Devhub validation: **confirmed live on devhub (HA 2026.11.0) at 83616636** (`release/0.100.0`, installed untagged through Home Assistant's marketplace and restarted at 15:19 AEST, 7 Nov; read through 15:23).
  - 123 of the 127 Nimbus entities carrying a version read 0.100.0; the other four are the daily reports.
  - None of the 388 Nimbus entities unavailable; plan `optimal` (solved 15:20); all 62 Solver numbers kept their values over the restart.
  - Help's What's New shows 0.100.0. No Nimbus Repairs. Log: no Nimbus ERROR.
  - The power signal form offers **Whole house** (`whole_house`): a new-signal form was opened, its role options read, and cancelled, so nothing was created. The served Forecast card carries the Whole house routing.
  - Not seen live: the duplicate bubble disappearing, because devhub has no second whole-house meter. It's covered by the card's source test. Cottonwood (Mark) will see it after setting the role.
  - The native-only Solver ran its first solve and those since without error on the same restart.
- Consumer check: no setting or entity is moved or renamed. A new optional role value, `whole_house`, is added. The standalone `solver_writer.py` entry is retired; Home Assistant installs never used it.

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
