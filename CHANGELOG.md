# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versioned from `custom_components/nimbus_load/manifest.json` — the Nimbus Solver add-on (`nimbus_solver_app`) that used to share this version line was removed in v0.94.85 (see [#357](https://github.com/code-imstillalive/nimbus/issues/357)).

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [Unreleased]

## [0.115.0] - 2026-11-26

**A zero-export offset for sell-power inverters, and safer cards.**

- **New battery dispatch blueprint input: Zero-export offset** (#2626, 0-1 kW, default 0). In *Grid export* (sell power) mode, a discharge that only covers the house sends 0 kW, and some inverters then import a little (a tester's imported about 0.22 kW; 0.25 kW gave a small export instead). Set the offset and it is added while the plan exports 0.5 kW or less; above that the export bias applies. Battery power mode and charging are unchanged. In the going-live guide. **Re-import the battery dispatch blueprint** to use it.
- **Cards:** the dispatch card's source labels and the forecast card's chart error message are now escaped before they reach the page (#2628). An injection test rendered all six cards with real plan data and every text value replaced by a script: no outside text (entity names, units, card settings) ran; these two only ever carried Nimbus's own words or a chart library's message.
- Internal: tests 1004 → 1007.
- Install shapes (#2394): the offset is opt-in (default 0) on every shape; seen on a tester's GoodWe in sell-power mode. Reference household and Mark Purcell's install: Battery power mode, no change. The card change has no visible effect on any install.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 6c0324ca** (`release/0.115.0`, installed untagged through Home Assistant's marketplace by branch at 07:42 AEST, 26 Nov, restarted; read through 07:45).
  - 125 of the 129 Nimbus entities carrying a version read 0.115.0; the other four are the daily reports (0.114.0). No Nimbus entity unavailable; plan `optimal` (solved 07:44). All 63 Solver numbers present and unchanged from a read just before the restart. No Nimbus Repair; no Nimbus error in the system log.
  - Served cards: `/nimbus_load/nimbus-dispatch-card-v4.js` escapes the source labels and `/nimbus_load/nimbus-forecast-card.js` writes a chart error as text (both read from the served files).
  - `sensor.nimbus_status` What's new leads with 0.115.0.
  - The Zero-export offset is a blueprint input (the blueprint is imported from the repository, not installed with the integration): covered by `test_a_zero_export_offset_stops_the_import_at_a_zero_sell_power`; not driven on the test box, which has no sell-power inverter.
- Consumer check: no setting, entity id or name changes in the integration. The battery dispatch blueprint gains one input, *Zero-export offset* (default 0: no behaviour change until set); re-import the blueprint to use it.

## [0.114.0] - 2026-11-25

**Export bias: a steady top-up while selling, on the Solver tab's P2P card.**

- **New setting: Solver tab → P2P → Export bias** (#2621, #2623; `number.nimbus_solver_p2p_export_bias_kw`, 0-3 kW, default 0). Some inverters deliver a steady amount less at the grid meter than they are asked for: a household's 14 kW block exported 13.6 kW, its 10.5 kW block 10.2 kW (measured with the panels dark: 0.40 and 0.33 kW short). Set the gap here and the battery dispatch blueprint adds it to the battery while the plan exports to the grid; charging, and discharging only to cover the house, are unchanged. **It reads no meter, so it cannot swing** (an earlier draft that trimmed against the meter each cycle was dropped on Mark Purcell's review). Also in Help → Start here (step 7) and the going-live guide. The blueprint also has its own *Export bias* input, which takes priority when set. **Re-import the battery dispatch blueprint** to use it.
- Internal: tests 999 → 1004.
- Install shapes (#2394): opt-in (default 0) on every shape. Reference household: its own dispatch automation reads the same setting (its config repo). Unseen on Mark Purcell's install and the test box (default 0, no change).
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 817d3963** (`release/0.114.0`, installed untagged through Home Assistant's marketplace by branch at 07:41 AEST, 25 Nov, restarted; read through 07:43).
  - 125 of the 129 Nimbus entities carrying a version read 0.114.0; the other four are the daily reports (0.113.0). No Nimbus entity unavailable; plan `optimal` (solved 07:42). 63 Solver numbers: the 62 from before the restart unchanged, plus the new `number.nimbus_solver_p2p_export_bias_kw` ("P2P Export Bias", 0-3 kW, state 0.0), published on `sensor.nimbus_solver_config`.
  - `sensor.nimbus_status` What's new leads with 0.114.0.
  - The stored Solver tab on the test box is unchanged by an upgrade, as designed (Nimbus writes no dashboard): the new P2P row reaches a dashboard through Help → *Check for updates* → *Add updated tabs beside mine*. Not pressed here by Claude (dashboards are the household's); the row's presence in the tab template is covered by `test_p2p_export_bias_setting.py`.
- Consumer check: one new setting, `number.nimbus_solver_p2p_export_bias_kw` (default 0: no behaviour change until set). The battery dispatch blueprint reads it; re-import the blueprint. Help's Start here gains step 7. No other entity id, name or behaviour changes.

## [0.113.0] - 2026-11-24

**A P2P settlement history that stops advancing is reported.**

- **New Repair: "your P2P settlement history has stopped advancing"** (#2618). A tester's retailer stopped publishing settled meter data for four days. Nimbus kept planning each night's P2P volume and match share from the last settled days it had, and the daily report had no settled figures, with nothing to say so. When the **P2P settlement history** sensor's newest settled day is more than 3 days old (it normally runs one day behind), a Repair names the sensor and the day and says what is affected. Nothing needs changing in Nimbus; it clears itself once the retailer catches up.
- Internal: tests 997 → 999.
- Install shapes (#2394): touches installs with a P2P settlement history sensor (LocalVolts v2). Seen on a tester's install (diagnostics: four days behind). Reference household: one day behind, no Repair. Mark Purcell's install: no settlement history sensor, unchanged.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at d396d429** (`release/0.113.0`, installed untagged through Home Assistant's marketplace by branch at 12:17 AEST, 24 Nov, restarted; read through 12:20).
  - 125 of the 129 Nimbus entities carrying a version read 0.113.0; the other four are the daily reports (0.111.0). No Nimbus entity unavailable; plan `optimal` (solved 12:19); all 62 Solver numbers present and unchanged from a read just before the restart.
  - The test box's settlement history sensor's newest settled day is 23 Nov (one day behind): no Repair, as expected. The Repair itself was not raised live here (that needs a settlement history more than 3 days behind); its finding and wording are covered by the tests (`test_settlement_history_stale.py`, `test_setup_health.py`); the condition was seen in a tester's diagnostics.
  - `sensor.nimbus_status` What's new leads with 0.113.0.
- Consumer check: no setting, entity id or name changes; one new Repair (`p2p_settlement_history_stale`). Plans are unchanged.

## [0.112.0] - 2026-11-24

**Prices past your retailer's forecast keep your retail markup, and a regional price sensor that stops updating is reported.**

- **Fix: days past your retailer's own price forecast were priced at bare wholesale, with spikes at a few fixed times** (#2615). Nimbus prices the days your retailer's forecast does not reach as the regional wholesale forecast plus your retail markup, learned per 5-minute slot of the day from **Source 2, AEMO: regional price now**. A tester's sensor there had stopped updating: it recorded a value only a few times a day, so the markup was learned in just those slots, as almost the whole retail price (spikes at about 03:30, 11:00 and 13:30 every day), and every other slot had none, so buy and sell were both bare wholesale and overnight grid power looked about half its real cost. The same happened, without the spikes, to a sensor the recorder keeps no history for. Now:
  - a regional price sensor that recorded a value in under half of the last five days' 5-minute intervals, or none, teaches no markup;
  - a new **Repair**, "the regional spot price sensor has stopped updating", names the sensor and says what to pick instead; it clears itself once the sensor updates again;
  - a slot with no learned markup takes the average markup, and with no markup learned at all, prices past the forecast follow wholesale's daily shape at the gap to your last real retail price, never bare wholesale.
- **Check your setting:** **Source 2, AEMO: regional price now** should be a 5-minute *current* price that changes every 5 minutes (for AEMO NEM Data, *Current 5min Period Price* for your region), not a forecast sensor. The markup relearns within about three days of a change.
- Internal: tests 993 → 997. The four rich-site golden scenarios change past the feed (buy and sell kept apart instead of both bare wholesale); the other 18 are unchanged.
- Install shapes (#2394): touches every install with a price forecast array and a regional sensor (the price path past the feed). Seen on a tester's install (stuck regional sensor, diagnostics). Reference household: its regional "now" field pointed at a forecast sensor the recorder does not keep; changed by the household to the 5-minute current price on 24 Nov, after which the markup appeared at once. Mark Purcell's install (Amber, same-poll wholesale): unseen; a slot with no markup now takes the average instead of zero.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 57623f20** (`release/0.112.0`, installed untagged through Home Assistant's marketplace by branch at 08:37 AEST, 24 Nov, restarted; read through 08:40).
  - 125 of the 129 Nimbus entities carrying a version read 0.112.0; the other four are the daily reports (0.111.0). No Nimbus entity unavailable; plan `optimal` (solved 08:39); all 62 Solver numbers present and unchanged from a read just before the restart.
  - The fix, on the test box's price path (a LocalVolts forecast array and a live 5-minute regional sensor): past the array's last row (08:30, 25 Nov) all 144 rows keep buy and sell apart with the learned markup (for example 21:00, 25 Nov: buy 0.1908, sell 0.1055); none at bare wholesale.
  - Repairs: none for Nimbus, as expected with a live regional sensor. The new Repair was not raised live here (that needs the test box's Solver settings pointed at a stuck sensor); its finding and its wording are covered by the tests (`test_stuck_regional_spot.py`, `test_setup_health.py`). On a tester's install the condition was seen in diagnostics.
  - `sensor.nimbus_status` What's new leads with 0.112.0. Log: no Nimbus error; warnings present are known and not from this release (the test car's departure target, two implausible test-meter rows).
- Consumer check: no setting, entity id or name changes. One new Repair (`regional_spot_not_updating`). Prices past the retailer's forecast keep the retail markup where none was learned for a slot, so plans for days 2 to 4 can change on installs whose regional "now" sensor was stuck or unrecorded.

## [0.111.0] - 2026-11-23

**Nimbus is in the HACS default list. Welcome, and thank you for trying it.**

Nimbus is maturing: 1.0 is planned for around January 2027. It drives real battery and grid dispatch on live homes every day, but expect a new release most weeks and some settings to move before 1.0. Start with the [setup guide](https://code-imstillalive.github.io/nimbus/setup-guide/); if anything is wrong or unclear, [open an issue](https://github.com/code-imstillalive/nimbus/issues/new/choose) with a diagnostics download.

This release:

- **Install straight from HACS** (#2606). Search **Nimbus** in HACS; no custom repository needed. Accepted into the HACS default list on 23 Nov 2026 after Frenck's review (hacs/default#12099).
- **Battery dispatch starts on the minute at a price change** (#2597, blueprint). A tester's discharge for a 17:00 price rise started at 17:01:45, two days running. When the clock reaches a minute where a price (import, export or P2P rate) changes, the dispatch follows the plan's row for that minute instead of waiting for the next plan. Between price changes nothing changes.
- **Sell-power inverters: Grid export setpoint and a correction %** (#2599, #2601, #2603, blueprint). Two new, optional settings in the battery dispatch blueprint: **Discharge setpoint** (*Battery power*, the default, or *Grid export* for an inverter driven by an export target, which covers the house load and its own losses) and **Discharge setpoint correction** (default 100 %; for example 104.5 % for an inverter that delivers about 4.5 % short). A tester's 15 kW P2P export arrived as about 14.7 kW. Both default to today's behaviour, so no existing automation changes. Actions also get `export_kw`, the planned grid export. The [going-live guide](https://code-imstillalive.github.io/nimbus/going-live/) says which to use.
- **Fix: a small disk access on Home Assistant's event loop at startup** (#2605, from the HACS review). Finding the temp folder writes and deletes a probe file the first time; it now runs off the event loop.
- **Better first-run help.** The README and docs say where Nimbus stands; Home Assistant's *Documentation* link for Nimbus opens the docs site; a bug-report form asks for the versions and diagnostics we need; and the setup guide covers cards that show "Custom element doesn't exist" (#2513).
- Blueprints are imported from GitHub: **re-import the battery dispatch blueprint** to get the dispatch changes above.
- Internal: tests 988 → 993.
- Install shapes (#2394): the blueprint settings are opt-in, defaults unchanged on every shape; seen on a tester's install (sell-power inverter, P2P blocks). The startup fix touches every shape (setup path only). Reference household and Mark Purcell's install: defaults, no change expected.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at cd622faf** (`release/0.111.0`, installed untagged through Home Assistant's marketplace by branch at 19:14 AEST, 23 Nov, restarted; read through 19:17).
  - 125 of the 129 Nimbus entities carrying a version read 0.111.0; the other four are the daily reports (0.110.0). No Nimbus entity unavailable; plan `optimal` (solved 19:16); all 62 Solver numbers present and unchanged from a read just before the install; no Repairs.
  - The startup fix: no "blocking call" warning in the log after the restart; no Nimbus error. Warnings present are known and not from this release (the test car's low live SoC, two implausible test-meter rows, one skipped tick while the first solve ran).
  - `sensor.nimbus_status` What's new leads with the 0.111.0 entries.
  - The battery dispatch blueprint from this commit saved to devhub through the blueprint websocket: Home Assistant accepted it, shows *Discharge setpoint* (Battery power default / Grid export) and *Discharge setpoint correction* (default 100), and the test dispatch automation using it stayed loaded (`on`). The dispatch behaviour itself is covered by the blueprint tests; live sell-power export is the tester's evening of 23 Nov.
- Consumer check: no setting, entity id or name changes in the integration. The battery dispatch blueprint gains two optional inputs whose defaults keep every existing automation's behaviour; re-importing it is needed for the on-the-minute start. Home Assistant's Documentation link for Nimbus now opens the docs site.

## [0.110.0] - 2026-11-22

**A car charging inside the house is counted once: the house battery no longer holds while the car charges.**

- **Fix: a car charging inside the whole-house meter was counted twice** (#2593). The plan's current period takes its house load from the live whole-house meter, which already includes a car charging in the house; the car's charge was then planned again as the Battery Participant's own battery. A tester's plan (13.0 kW on the meter, 11 kW of it the car) saw no spare solar, held the house battery and exported at -0.1c, and with his grid import limit the house battery's charge rate stepped up and down every minute. The current period now takes what the participants are charging right now off the meter, from each participant's own power sensor (a required participant field). Charging only; an unreadable sensor changes nothing. The meter's own reading is still published as `load_whole_house_live_now_kw`.
- Internal: tests 987 → 988.
- Install shapes (#2394): touches installs with a Battery Participant whose charging is inside the whole-house meter (a tester's car; the test box's Test EV). The reference household has no participant: no change. Mark Purcell's install has two cars as participants; unseen there.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 217bffc2** (`release/0.110.0`, installed untagged through Home Assistant's marketplace by commit at 13:31 AEST, 22 Nov, restarted; read through 13:35).
  - 125 of the 129 Nimbus entities carrying a version read 0.110.0; the other four are the daily reports (0.109.0). No Nimbus entity unavailable; plan `optimal`; all 62 Solver numbers present and unchanged from a read just before the restart; no Repairs.
  - The fix, on the test box's Test EV (its power sensor set to 11 kW charging through its own MQTT topic for two minutes): on this release the whole-house reading was 2.62 kW, so period 0's house load went to the floor, 0.0, not negative (13:33 and 13:34 solves); on the same fix code at 13:06-13:07, 13.12 and 13.08 kW became 2.12 and 2.08. With the car at 0 kW period 0 equals the meter. `load_whole_house_live_now_kw` kept the meter's own reading throughout. Test EV set back to 0 kW afterwards.
  - Log: no Nimbus error; two forecaster drift notices and the known implausible meter rows, neither from this release.
- Consumer check: no setting, entity id or name changes. The plan's current-period house load leaves out what Battery Participants are charging now, so on an install with a car charging inside the house the house battery is planned against the real spare solar. Installs without a participant see no change.

## [0.109.0] - 2026-11-21

**With a car added, the plan tables show the house battery, with House / EV / All chips; a charger can follow its car's plan; dispatch rides through a Nimbus reload.**

- **House battery by default, with House / EV / All chips** (#2585; household, 21 Nov: "I like that chips change. Perfect. But for all tables." and "House. EV. All"). With a car or second battery as a Battery Participant, the plan's battery power and SoC are every battery together, so the plan table read about 75 % when the house battery was planned at 99 %. The Control Panel plan table and the Forecaster's battery and SoC lines now show the house battery, with chips for **House**, **EV** and **All** (with several participants, each chip carries its own name). The Power Flow card and the Solver tab (Whole Plan, Daily Summary, Energy Plan sums) show the house battery. No new columns; a house with no participant sees no chips and no change.
- **EV charger dispatch blueprint** (#2583, reviewed by Mark Purcell). A charger follows its car's own plan (`sensor.nimbus_solver_battery_N_kw`): starts, sets the current in amps for your supply, and stops. One automation per charger: a charger shared by several cars takes each car's plugged-in sensor and follows the car that is connected. Below the charger's minimum current while charging, it holds the minimum for a few minutes before stopping. Arm switch, fallback and stale-plan checks as the battery blueprint. Blueprints are imported from `main`, so it has been usable since 21 Nov.
- **Dispatch rides through a Nimbus reload** (#2588, fix). Saving any Nimbus setting reloads Nimbus and its plan is unavailable for a few seconds; a minute tick in that gap ran the fallback (a tester's inverter went to its own mode for a minute mid-export). Both blueprints now keep the last command while the plan has only just gone missing, for up to *Ride through a Nimbus reload* (2 minutes). Re-import the blueprint to get it.
- Internal: tests 980 → 987.
- Install shapes (#2394): #2585 touches installs with Battery Participants (the test box's Test EV; a tester's car); the reference household has no participant and sees no change. #2583 and #2588 touch blueprint users (testers); the reference household and Mark Purcell's install drive their batteries and cars with their own automations, so unseen there.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at d4b78d0d** (`release/0.109.0`, installed untagged through Home Assistant's marketplace by commit at 22:38 AEST, 21 Nov, restarted; read through 22:41).
  - 125 of the 129 Nimbus entities carrying a version read 0.109.0; the other four are the daily reports (0.106.1). No Nimbus entity unavailable; plan `optimal`; all 62 Solver numbers present and unchanged from a read just before the restart; no Repairs.
  - The cards devhub serves carry the change: the Control Panel and Forecaster cards read `flex_vehicle_participants` and number the cars. The served Control Panel card's own chip code, run against devhub's live plan and Solver settings (Test EV, a car by its settings): chips **House / EV / All**, House selected; the house row read 24.73 kW against `home_battery_kw` 24.72 while the plan's fleet figure was 12.73 (the Test EV charging). Not opened on screen: the test box's web UI needs the household's sign-in. EV 1 / EV 2 numbering with two cars is covered by `test_the_dispatch_card_shows_the_house_battery_and_offers_chips`.
  - Log: no new warning from this release (its only Python changes are help text and the Solver tab's templates). Present and not from it: the test box's Test EV reads 5 % SoC, below its own 20 % floor, all day, so the plan schedules recovery and the flex telemetry clamps a shadow price; one skipped Solver tick just after the restart; the known implausible meter rows.
  - The two blueprints (#2583, #2588) were road-tested on the test box before they merged (21 Nov 15:16-15:21 and 18:44-18:50; see #2583 and #2588); a blueprint is imported from `main`, not installed with this release.
- Consumer check: no setting, entity id or name is moved or renamed. The plan sensor and every Nimbus entity publish exactly as in 0.108.0; the change is in the cards and the Solver tab's tables, which show the house battery when a participant exists, and in the two blueprints, which users re-import. Installs with no participant see no change.

## [0.108.0] - 2026-11-21

**With a car or second battery as a participant, the house inverter follows the home battery's own plan.**

- **The dispatch blueprint drives the house inverter with the home battery's own share** (#2575, for #2573, reviewed by Mark Purcell). With an EV or second battery added as a Battery Participant, the plan sensor's state is every battery together, and the blueprint sent that to the house inverter: a car charging 7 kW from the grid made the house battery charge 7 kW too. The plan now publishes `home_battery_kw` (the home battery's own planned power now, same sign; equal to the state with no participant) and the blueprint follows it. The hold, solar-charge and spare-solar signals are worked out from the home battery alone. The state itself is unchanged.
- **Each battery's planned power on its own sensor**: `sensor.nimbus_solver_battery_N_kw` (positive = discharge), beside `sensor.nimbus_solver_battery_N_soc_pct`, on sites with participants. A charger automation can follow its car's.
- **Dispatch arm switch help text** matches what the card does when it is blank (#2580).
- Internal: tests 977 → 980.
- Install shapes (#2394): #2575 touches installs with Battery Participants (an EV or a second battery), the dispatch blueprint's users among them; a tester's EV install prompted it; Mark Purcell's install has two cars as participants but drives them with its own automation; single-battery installs see only the new attribute.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 534ecc84** (`release/0.108.0`, installed untagged through Home Assistant's marketplace by commit and restarted at 12:24 AEST, 21 Nov; read through 12:26).
  - 125 of the 129 Nimbus entities carrying a version read 0.108.0; the other four are the daily reports (0.106.1). No Nimbus entity unavailable; plan `optimal`; all 62 Solver numbers present.
  - The test box has a Test EV participant: `home_battery_kw` and the two new sensors are published (`sensor.nimbus_solver_battery_0_kw` -40.0 "home", `sensor.nimbus_solver_battery_1_kw` 0.0 "Test EV"). The car was idle in the read, so home equalled the state; the car-charging case is covered by `test_the_home_battery_is_the_fleet_less_its_participants` and the regenerated `rich_site_native_with_ev` snapshot.
  - Road test asked for by Mark Purcell's review: an automation from the current blueprint (actions writing only to text helpers; the test box has no inverter), armed with the test box's own helpers. Following the plan it took CHG at 40 kW from `home_battery_kw`; the manual mode select gave DIS, HOLD, SC and CHG in turn, back to the plan on Automatic; disarming ran the fallback.
- Consumer check: no setting, entity id or name is moved or renamed. The plan sensor's state is unchanged; it gains the attribute `home_battery_kw`. Sites with Battery Participants gain `sensor.nimbus_solver_battery_N_kw`. A blueprint automation follows the home battery's share where it differed (sites with participants); single-battery sites dispatch exactly as before.

## [0.107.0] - 2026-11-21

**The Control Panel card shows whether dispatch is really armed again, and its manual mode buttons work.**

- **The Control Panel card links your dispatch arm switch and mode select from Nimbus's settings** (#2576). Since the generated Control Panel tab arrived (3 Nov) its card carried no arm switch or mode select, and it cannot be edited, so it read DISARMED / OFF and "Dispatch is off" on an install whose dispatch was armed and running, and its toggle and mode buttons did nothing. Two optional settings on Solver settings step 1, **Dispatch arm switch** and **Dispatch mode select**, link them; the card also takes the grid meter from Nimbus's settings. The card looks and works exactly as it did before 3 Nov once they are picked.
- **The battery dispatch blueprint obeys a manual mode select** (#2577). With the optional **Manual mode select** set (Automatic, Charge, Discharge, Preserve, Self-Consume), anything but Automatic overrides the plan until Automatic is chosen again: Charge and Discharge at the maximum battery power (a charge never above the allowed charge), Preserve your Hold actions, Self-Consume your Self-Consume actions. Never delayed by the minimum dwell; disarming still runs the fallback. See the going-live guide.
- Internal: tests 974 → 977.
- Install shapes (#2394): #2576 touches every install with the generated Control Panel tab; seen on the reference household (Control Panel read DISARMED while armed) and a tester's blueprint install. #2577 touches installs running the dispatch blueprint; seen on a tester's install.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 39e7089e** (`release/0.107.0`, installed untagged through Home Assistant's marketplace by commit and restarted at 10:50 AEST, 21 Nov; read through 10:53).
  - 123 of the 127 Nimbus entities carrying a version read 0.107.0; the other four are the daily reports (0.106.1). No Nimbus entity unavailable; plan `optimal` (10:52, and again after the settings change, 10:53).
  - Solver settings step 1 shows the two new fields; set to the test box's own `input_boolean.nimbus_test_dispatch_armed` and `input_select.nimbus_test_dispatch_mode` through the options flow, both appear on `sensor.nimbus_solver_config`, and every other Solver setting read back unchanged.
  - The served `nimbus-dispatch-card-v4.js` carries the adoption of the two settings. The card on screen was not opened here (the test box's browser session needs a sign-in); the adoption is covered by `test_blank_arm_mode_and_grid_fields_follow_nimbus_settings`, and the household checks it on screen after install.
  - #2577: the test box runs no dispatch automation, so the manual mode was not driven live; covered by `test_a_manual_mode_overrides_the_plan_until_automatic`.
- Consumer check: no setting, entity id or name is moved or renamed. Two optional settings are added, blank by default; the card and the blueprint behave exactly as before until they are set.

## [0.106.1] - 2026-11-20

**A plan kept by the 10-second stall rule is no longer logged as a slow-solve alarm.**

- **The stall rule's stop is logged as what it is** (#2567, for #2566, reported by Mark Purcell). When 0.105.2's rule stops the main plan within 0.02% of the best possible, HiGHS reports the solve as "Interrupted by user". The slow-call diagnostic did not know that status, so each such stop logged a WARNING tied to the old #773 investigation and saved the model to `nimbus_diagnostics`, although the plan was kept and correct (seen on Mark Purcell's install at 09:04, 20 Nov: 15.8 s, gap 0.0). Now such a stop is logged at DEBUG as `Stall accepted`, with no file written. A solve that is slow past half its time limit, or stopped outside the rule's gap, still warns and saves the model as before.
- Internal: tests 973 → 974.
- Install shapes (#2394): #2567 touches any install whose main plan reaches the 10 s stall; seen on Mark Purcell's install; not seen on the reference household or the test box this release.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at cbbd73af** (`release/0.106.1`, installed untagged through Home Assistant's marketplace and restarted at 09:31 AEST, 20 Nov; read through 09:33).
  - 123 of the 127 Nimbus entities carrying a version read 0.106.1; the other four are the daily reports (0.106.0).
  - The same 388 Nimbus entity ids as a read at 09:22 before the install, none unavailable; plan `optimal` on the first solve after the restart (solved 09:33); all 62 Solver numbers present with the same values as before the restart.
  - Help's What's New shows 0.106.1. No Nimbus Repairs. Log: no Nimbus ERROR and no `#773` line.
  - #2567: the stall rule did not fire in the read (no main solve reached 10 s), so the new label was not seen live here; it is covered by `test_2566_a_stall_accepted_main_plan_is_not_a_773_slow_call_alarm` (failed before the fix in CI run 44634559928, passes after).
- Consumer check: no setting, entity id or name is moved or renamed. Only logging changes: a stall-accepted main solve logs at DEBUG with no model file; the plan itself is unchanged.

## [0.106.0] - 2026-11-19

**Solcast is read for all seven days.**

- **A Solcast-only plan has solar after tomorrow again** (#2561, for #2560). Nimbus read only Solcast's today and tomorrow forecasts. Solcast PV Forecast also has Forecast Day 3 to Day 7, with the same half-hourly forecast, and Nimbus never read them, so with Solcast as the only solar source every period from the day after tomorrow had 0 kW solar: a tester's plan showed 100.2 kWh tomorrow, then 0.0 kWh on each of the next three days, midday included. Now all seven days are read, everywhere Solcast is used: the auto-included source, Device setup's solar binding and the solar-running-low check.
- Internal: tests 972 → 973.
- Install shapes (#2394): #2561 touches any install with Solcast PV Forecast; seen in a tester's diagnostics (Solcast as the only solar source); not measured on the reference household or Mark Purcell's install, which also have other solar sources.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 061668fb** (`release/0.106.0`, installed untagged through Home Assistant's marketplace and restarted at 12:15 AEST, 19 Nov; read through 12:17).
  - 123 of the 127 Nimbus entities carrying a version read 0.106.0; the other four are the daily reports (0.105.2).
  - The same 388 Nimbus entity ids as a read at 12:01 before the install, none unavailable; plan `optimal` on the first solve after the restart (solved 12:16); all 62 Solver numbers present with the same values as before the restart.
  - Help's What's New shows 0.106.0. No Nimbus Repairs. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (the flex-telemetry clamp, two implausible imported meter rows dropped).
  - #2561: the test box has no Solcast PV Forecast, so `known_sources.solcast` is empty there and the change does not run; it is covered by `test_2560_solcast_is_read_for_all_seven_days` (failed before the fix in CI run 44426989322, passes after). Checked live on the reference household after its install.
- Consumer check: no setting, entity id or name is moved or renamed. An install with Solcast PV Forecast now reads its Forecast Day 3 to Day 7 as well; nothing else changes.

## [0.105.2] - 2026-11-18

**A plan solve that gets stuck proving its last fraction of a cent stops and keeps its plan.**

- **The main plan stops after 10 s once it is within 0.02% of the best possible** (#2553, for #2552, reviewed by Mark Purcell). On Mark Purcell's install a plan solve took 30 s, and the overlap guard then skipped ticks and the shadow plans ran out of time. The test box's own saved slow solve showed why: the plan was found in about a second, then the solver spent the rest of its 60 s proving the last 0.0123% (about 6 cents of $509) without improving it, on HiGHS 1.15.1 and 1.16.0 alike. Now a main plan that has run 10 s and is within 0.02% stops there with the plan it has: on that saved solve, 10.0–10.8 s instead of 60 s, the identical plan. A solve that finishes in under 10 s is unchanged, and no other solve has the rule.
- Internal: tests 969 → 972.
- Install shapes (#2394): #2553 touches any install whose main plan is a MIP; the stall is seen in the test box's saved model and in Mark Purcell's install's log; unseen on the reference household.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 6611281b** (`release/0.105.2`, installed untagged through Home Assistant's marketplace and restarted at 00:45 AEST, 18 Nov; read through 00:47).
  - 123 of the 127 Nimbus entities carrying a version read 0.105.2; the other four are the daily reports (0.105.1).
  - The same 388 Nimbus entity ids as a read at 00:44 before the install, none unavailable; plan `optimal` on the first solve after the restart (solved 00:46); all 62 Solver numbers present with the same values as before the restart.
  - Help's What's New shows 0.105.2. No Nimbus Repairs. Log: no Nimbus ERROR; the Nimbus WARNING lines are the test box's known ones (the flex-telemetry clamp, the test EV below its floor, two implausible imported meter rows dropped).
  - #2553: the stall rule did not trigger in the read (no main solve ran 10 s); its effect was measured before the release on the test box's saved slow model (stopped at 10.0–10.8 s, identical objective, pinned refresh Optimal in 0.04 s).
- Consumer check: no setting, entity id or name is moved or renamed. A main plan that stalls 10 s within 0.02% of the best possible keeps the plan it has; any other solve is unchanged.

## [0.105.1] - 2026-11-15

**The plan keeps its tie-breaks when the solver gets stuck on a hairline.**

- **A stuck tie-break solve is retried once instead of dropped** (#2546, for #2545, reviewed by Mark Purcell). After phase 2 of the plan's solve used its time budget, the tie-break LP on phase 1's own switch choices could end `Unknown`: the row holding the plan's cost at its optimum left only about $2e-7 of room, too thin for the solver. Nimbus then fell back to the plain solve without tie-breaks and skipped the full solve for 5 minutes, about once every 7–11 minutes on the test box. Now that LP is retried once with the row opened by $1e-5 (a thousandth of a cent) before anything is reported. A solve that works today is unchanged.
- Internal: tests 968 → 969.
- Install shapes (#2394): #2546 touches any install whose plan uses the phased solve; seen live on the test box (12 fallbacks in 85 minutes on 0.105.0, then 0 fallbacks and 10 recovered retries in 30 minutes with the change); unseen on the reference household and Mark Purcell's install, where the log has not been read.
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 9bc91568** (`release/0.105.1`, installed untagged through Home Assistant's marketplace and restarted at 06:15 AEST, 15 Nov; read through 06:23).
  - 123 of the 127 Nimbus entities carrying a version read 0.105.1; the other four are the daily reports (0.104.0).
  - The same 388 Nimbus entity ids as a read at 06:14 before the install, none unavailable, no name changed; plan `optimal`; all 62 Solver numbers present with the same values as before the restart.
  - #2546: three phased solves in the read hit the pinned LP's `Unknown` and each reached `Optimal` on the one retry (15â€“53 iterations); no single-objective fallback and no Nimbus ERROR. The same change ran on devhub 04:03â€“06:14 as a branch: 10 retries in its first 30 minutes, all recovered, no fallback.
  - Help's What's New shows 0.105.1. No Nimbus Repairs. Log: the Nimbus WARNING lines are the test box's known ones (slow-phase breakdowns, the flex-telemetry clamp, the test EV below its floor and short of its departure target, two implausible imported meter rows dropped, forecaster residual-drift notes after a restart).
- Consumer check: no setting, entity id or name is moved or renamed. A plan that hit the fallback keeps its tie-breaks instead; its primary cost may differ from the strict optimum by at most $1e-5.

## [0.105.0] - 2026-11-15

**The P2P column shows whenever P2P is matched, a day-ahead spike counts for more only as it gets closer, and Device setup gets the grid and each battery right.**

- **Prices past your retailer's own forecast are trusted by how close they are** (#2535, for #2534, reviewed by Mark Purcell). Past the retailer's horizon, the plan used the regional forecast at full height, so a day-ahead spike (98.6c on 13 Nov, gone an hour later) priced the plan, the Prices ahead chart and the next day's summary as if certain. Each such price now moves towards the usual price for that time of day by how far a forecast that far ahead can be trusted: at or above the spike threshold, by how often spikes at that range have settled as spikes, from a cautious start (half at 6 hours, under a fifth a day out); below it, only once that source's error has been measured. The retailer's own prices and the current interval are never changed.
- **Device setup checks the grid's import and export the right way round** (#2538). Review what Nimbus finds copied the Energy Dashboard's grid pairing as it was; when the counter proposed as export clearly rose more between 01:00 and 05:00 (when a house imports) over several nights, the two are now swapped in the proposal, with a note saying why.
- **Each new battery gets its own capacity box** (#2537). Review what Nimbus finds had one box for every new battery, so two batteries got the same number each. Each battery's line also names its sensor, so two called "Battery" can be told apart.
- **The Whole Plan tables show the matched P2P rate and share whenever P2P is matched** (#2540). The P2P column showed a rate only when it beat the spot price, so evenings when spot paid more showed 0 even with P2P volume committed. It now shows the matched rate in every period with matched volume, with the matched share beside it (for example "20.1 · 31%"), on the Solver tab and the dispatch card.
- Internal: the price error history is now read by the plan (`read_by_dispatch`). Tests 964 → 968.
- Install shapes (#2394): #2535 touches any install whose plan runs past its price source's horizon (seen live on the reference household, 13–14 Nov); #2540 touches any LocalVolts install with matched P2P (seen live on the reference household, 14 Nov; unseen on Mark Purcell's install); #2537 and #2538 touch Device setup's review on any install with an Energy Dashboard (both found on the reference household's walkthrough, 14 Nov; unseen on the test box and Mark Purcell's install).
- Devhub validation: **confirmed live on devhub (HA 2026.11.1) at 418d21f8** (`release/0.105.0`, installed untagged through Home Assistant's marketplace and restarted at 00:25 AEST, 15 Nov; read through 00:34).
  - 123 of the 127 Nimbus entities carrying a version read 0.105.0; the other four are the daily reports (0.104.0).
  - The same 388 Nimbus entity ids as a read at 00:24 before the install, none unavailable, no name changed; plan `optimal` on the first solve after the restart; all 62 Solver numbers present with the same values as before the restart.
  - #2540: all 206 plan rows carry `p2p_rate`, and the 58 inside the P2P feed's coverage carry `p2p_share`. The Solver tab's Whole Plan cell, rendered against the live plan, reads "31.6 · 49%" at 17:00 on 15 Nov and "47.8" (rate only) on days past the feed. The release's standard Solver tab carries the new cell, so the Help tab's Nimbus Tabs card offers it as "Solver (new)" beside an existing Solver tab; it was not added on devhub. The served dispatch card carries the share.
  - Help's What's New shows 0.105.0. No Nimbus Repairs. Log: one Nimbus ERROR, the #773 phased-solve fallback on the first phased solve after the restart (plain solve used, plan `optimal`); the same fallback is on devhub's record from 13 Nov (0.104.0) and 14 Nov (before #2535), and the next phased solve after its 300 s back-off did not fail. The other Nimbus WARNING lines are the test box's known ones (the test EV below its floor, two implausible imported meter rows dropped).
  - Not seen live: #2535's trust weights on devhub's plan (not inspected in this read), and #2537/#2538 (no new battery or reversed grid pairing on devhub). #2535 was checked against a full evening of settled prices on #2535 itself; #2537 and #2538 are covered by the tests in their PRs.
- Consumer check: no setting, entity id or name is moved or renamed. Plan rows gain `p2p_rate` and `p2p_share`. Prices past the retailer's forecast can be lower or higher than before, by their trust weight. An existing install gets the new Whole Plan P2P column through Help → Nimbus Tabs card, which adds "Solver (new)" beside its Solver tab. Device setup's review asks one capacity per new battery and can propose a swapped grid pairing.

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
