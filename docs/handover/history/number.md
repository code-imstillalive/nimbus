# number.py: comment history

Moved out of `custom_components/nimbus_load/number.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim. Each block's rule, reason and issue number stay in the code as a short comment; the narrative is kept here, keyed by the code line it sat above. Line numbers are the file's at `main` before the move.

## Line 193: above `_ONE_TIME_VALUE_CHANGES: dict[str, tuple[str, float, float]] = {`

```text
# One-time value changes applied on upgrade: key -> (marker, from, to). The
# marker is written once the check has run, so a household that sets the old
# value again keeps it.
#
# Self-consume after midnight, 4 -> 0 (household, 19 Oct 2026; promised on
# #1700): 4 was this repository's reference household's value, shipped as the
# default, and no install chose it -- a stored 4 cannot be told apart from the
# default (every restart writes the restored value back), and the household
# confirmed none of the testers set it. 0 leaves the hours after a midnight P2P
# block free: the plan may charge, or sell into an early-morning spike, and the
# battery reserve is what stops it selling cheaply.
```

## Line 216: above `_STORAGE_VERSION = 1`

```text
# nimbus issue: real, live incident 2026-09-02 (devhub restart) -- 14 of
# these entities (grid limits, P2P block 1, all three network-fee
# tiers, min SoC, SoH, efficiency, charge cost) silently reset to their
# schema placeholder. Root cause: RestoreNumber's own restore-state has a
# genuine, still-not-fully-diagnosed HA-core startup timing race (the
# `restore_state` integration's cache isn't guaranteed warm by the time
# this platform's own async_added_to_hass() runs), and this module's own
# docstring above explains entry.options is DELIBERATELY never kept in
# sync with a dashboard edit (to avoid a full-hub reload on every value
# change) -- so a restore-state miss on any field that was ever only
# ever set from the dashboard (every P2P/network-fee/risk-aversion field,
# none of which are in the wizard) had ZERO real fallback and free-fell
# straight to _desc.default. This Store is a durable, independent third
# layer: written on every successful restore/seed AND on every dashboard
# edit, read as a fallback BEFORE ever reaching entry.options/default. A
# plain Store read is a direct JSON-file load with no comparable startup
# race, so it survives exactly the case RestoreNumber alone couldn't.
```

## Line 286: above `data[key] = {"value": value, "written_at": time.time()}`

```text
            # nimbus issue #342: was a bare `data[key] = value` -- no way
            # to tell which of a Store entry and a RestoreNumber restore
            # is actually newer, so async_added_to_hass() unconditionally
            # trusted whatever RestoreNumber returned and overwrote the
            # Store with it even when the Store already held a genuinely
            # NEWER, real user edit that RestoreNumber's own periodic
            # dump (STATE_DUMP_INTERVAL, 15 min) hadn't captured yet
            # before an unclean stop. Real failure scenario: a value set
            # at 10:00, last restore-state dump at 09:55, container killed
            # at 10:05 -- the correct 10:00 edit gets silently overwritten
            # by the stale 09:55 one, with entry.options never in sync
            # either (this module's own top docstring), making the edit
            # permanently unrecoverable. Recording when THIS layer's own
            # write happened lets async_added_to_hass() compare the two
            # sources honestly instead of blindly preferring one.
```

## Line 378: above `device_class: NumberDeviceClass | None = None`

```text
    # entity-device-class (Gold): only set where HA's own DEVICE_CLASS_UNITS
    # table (homeassistant/components/number/const.py, checked live against
    # HA core's real current source 2026-08-22, not guessed) genuinely
    # accepts this field's exact unit string. Deliberately left None on
    # every $/kWh, "%", and "hour" field below -- none of those have a real
    # matching NumberDeviceClass (MONETARY expects a plain ISO4217 currency
    # unit, not a per-kWh rate; BATTERY means "% currently charged", not
    # "SoH"/"min or max SoC config"/"efficiency"; DURATION's own accepted
    # unit strings are HA's UnitOfTime enum values like "h", not our own
    # literal "hour" string) -- forcing one in would risk a rejected/
    # warned-about device_class at entity registration, not just be a
    # missed nicety.
```

## Line 401: above `_DESCRIPTIONS: tuple[_SolverNumberDescription, ...] = (`

```text
# Real bounds/units/defaults mirrored exactly from flows/hub_options.py's own
# _num() calls for these same 14 fields, minus the 5 entity-pointer fields
# that stay wizard-only (see this module's own docstring for why). The 5
# fields with no real universal default (capacity/max-charge/max-discharge/
# grid-limits -- genuinely household-specific hardware numbers, same as the
# wizard's own vol.Required with no default) use their own min bound as a
# clearly-a-placeholder value here -- only ever actually seen on a genuinely
# fresh install that's never run the wizard AND never had this platform
# create an entity before, since async_added_to_hass() below always prefers
# a real seeded/restored value first.
```

## Line 662: above `_SolverNumberDescription(`

```text
    # Real portability bug found and fixed live (nimbus repo issue #100,
    # Mark Purcell). See const.py's own CONF_SOLVER_INVERTER_SELF_
    # CONSUMPTION_KW comment for the full "this used to be a hardcoded
    # reference-household-specific constant, silently added to every OTHER
    # install's own load total" story. 0.0 (the default) is a genuine
    # no-op. min=0/max=5/step=0.001 -- a real inverter self-consumption
    # bias is a small correction (this project's own reference value is
    # 0.215 kW), not a hardware capacity number; the finer 0.001 step
    # (vs. the 0.1 step every kW hardware field above uses) is needed to
    # enter that reference value exactly.
```

## Line 743: above `_SolverNumberDescription(`

```text
    # nimbus issue #565: shared across all 3 blocks (not per-block --
    # this household's own real usage is one active P2P block at a
    # time, and the issue itself proposes a single field, not a tripled
    # one) -- shifts every configured block's own effective START minute
    # earlier by this many minutes, absorbing real dispatch-side lag
    # around the hour boundary structurally instead of chasing solve
    # latency. 0 (default) is a complete no-op, byte-identical to every
    # existing install's current whole-hour-only behaviour. See
    # fetch_p2p_fixed_export_kw()'s own docstring (solver_writer.py) for
    # the exact mechanism.
```

## Line 763: above `_SolverNumberDescription(`

```text
    # Real per-kWh import FEES on top of the raw spot price -- network
    # TOU tariff + a flat always-on charge (certificates, etc.), same
    # shape as the P2P blocks above (2026-08-22, direct household
    # demand: "how do they configure fees column... I TOLD U NO
    # HARDCODED INPUTS - this has to work as user setting"). See const.py's
    # own comment on CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE for the full
    # "how a flat/2-tier/3-tier retailer each configures this" story.
    #
    # step=0.000001 (not 0.0001) on every rate field below: a real,
    # bill-verified Energex NTC 6900 tariff (2026-08-22, direct household
    # bill cross-check) needs a full 6 significant digits to enter exactly
    # (e.g. Peak 0.214863 $/kWh, Shoulder 0.066759, Off-peak 0.004774,
    # Certificates 0.008246) -- the old 4-decimal step silently rounded
    # every one of these to a slightly-wrong value (0.2149 vs 0.214863,
    # a real ~$0.006/day-scale error compounded across every peak-hour
    # kWh) with no visible warning. "we should have matching precision."
```

## Line 878: above `_SolverNumberDescription(`

```text
    # Risk-aversion dials (2026-08-21) -- 0.0 = trust the point forecast
    # completely, 1.0 = fully hedge toward the pessimistic bound. Three
    # separate dials on purpose (household ask: "more flexibility the
    # better") -- Risk Aversion hedges solar/load forecast error; Import/
    # Export Price Risk Aversion each hedge their own side of the price
    # forecast independently (2026-08-21 split, per Mark Purcell's direct
    # feedback: a single shared price_risk_aversion scalar forces charge
    # and discharge hedging to move together even though they're
    # economically opposite decisions -- hedging "import might be more
    # expensive than forecast" should never also force hedging "export
    # might be worth less than forecast" by the same amount, and vice
    # versa). Trusting one kind of forecast doesn't mean trusting another.
```

## Line 933: above `_SolverNumberDescription(`

```text
    # nimbus issue #538 (Mark Purcell, real household finding): the
    # quality report's own soc_discrepancy_reliable/epr_reliable flags
    # used to test only whether the achieved SoC integration stayed
    # inside the physical [0, 100] range -- these two dials are the
    # second, independent test (max/mean disagreement against the real
    # SoC sensor, in points), tunable per household exactly like every
    # other Solver number, no release needed to adjust a noisier
    # install's own tolerance.
```

## Line 1022: above `_SolverNumberDescription(`

```text
    # Real household finding (2026-09-08, NUC1's own first day of live
    # dispatch): a single solve's own battery_kw jumped mid-band (e.g.
    # -16.2 -> -25.9 kW) while the real import price hadn't actually
    # changed yet -- classic LP degeneracy (network.py's own
    # _add_intraplan_smoothness_penalty docstring), not a real decision.
    # The mechanism to fix this already existed (added 2026-08-20) but
    # was always passed network.py's own hardcoded DEFAULT_SMOOTHNESS_
    # WEIGHT_KW constant -- no household could ever see or tune it. Small
    # values (comfortably below the smallest real price step this
    # household's data shows, ~1c/kWh = 0.01 $/kWh) only ever break a
    # genuine tie between economically-identical schedules; they can
    # never override a real price/cost signal, so turning this UP
    # doesn't make the plan less responsive to real price changes, only
    # less willing to pick an arbitrary jagged shape when nothing real
    # favours one shape over another.
```

## Line 1061: above `_SolverNumberDescription(`

```text
    # nimbus issue #692 (household, real live plan mishaps: a critically-
    # low battery sitting idle through a perfectly good charging price,
    # only charging later at an equal or worse one): the battery's own
    # charge decision never had an earliness tie-break the way #613 gave
    # deferrable loads -- this is that same tiny nudge, applied to
    # battery_charge_{name}_{t} instead. Same "never hardcoded, always a
    # real dashboard field" discipline as its two siblings above, per a
    # direct household ask to be able to adjust this themselves if it
    # ever turns out insufficient. Default matches smoothness_weight/
    # proximal_weight's own magnitude exactly, not #613's own load-side
    # budget -- verified live this matters: at the load's own larger
    # spread, this term's own per-period gradient outweighed smoothness_
    # weight in a real scenario, reintroducing the exact jagged charge
    # burst that mechanism exists to eliminate (front-loading charge
    # until the battery's own max_soc_kwh ceiling forced a sudden drop to
    # a much slower rate) -- a real hard-ceiling interaction a load's own
    # target_kwh delivery never has to contend with, since it has no
    # equivalent capacity ceiling to slam into.
```

## Line 1107: above `_SolverNumberDescription(`

```text
    # nimbus issue #567: the real, live-adjustable discharge-rate slider
    # -- the household's own real point, "during spikes... the long
    # dragging ones it's always best to control the output cos u could
    # sell too fast," argues against a plain max/off toggle. 0 (default)
    # means the override, even if armed and a spike is detected, forces
    # nothing (a genuinely inert 0kW pin) until the household sets a
    # real rate -- same "off by default, explicit opt-in" convention as
    # every other threshold/armed field this issue introduces. Max
    # bound is a generous headroom ceiling, not tied to any one
    # household's own real max_discharge_kw (a plain number entity has
    # no way to read another entity's live value as its own bound);
    # BatteryConfig.spike_override_discharge_kw's own __post_init__
    # validation is the real, authoritative ceiling check against the
    # actual configured battery.
    #
    # nimbus issue #694 caveat, worth knowing before tuning this: if a
    # P2P block is active, its own committed rate becomes a FLOOR on
    # grid_export during the override (P2P still "remains", per the
    # household's own instruction), but grid_export can never physically
    # exceed solar + total battery discharge (the pre-existing same-
    # period wash-trade guard, network.py). Set this RATE ABOVE every
    # configured P2P block's own rate, or an armed override firing
    # during an active P2P window can make that solve infeasible --
    # there'd be nowhere for the P2P-floor-mandated export to come from.
```

## Line 1189: above `integration = await async_get_integration(hass, DOMAIN)`

```text
    # Same real, live version read as sensor.py's own NimbusForecastSensor/
    # NimbusSolverConfigSensor -- kept independent (not passed between
    # platform modules) since Platform.NUMBER/Platform.SENSOR forward-setup
    # order isn't something to depend on; HA merges device info across
    # entities sharing the same identifier regardless of which one loads
    # first. hub_device_id below does its own independent registry lookup
    # (same reasoning) -- sensor_flattened.resolve_hub_device_id() carries
    # zero dependency on sensor.py's own async_setup_entry having run
    # first, it's a shared helper, not a hand-off.
```

## Line 1214: above `for subentry in entry.subentries.values():`

```text
    # nimbus issue #645: same live-dashboard-editable pattern as the
    # Solver hub-level numbers above, applied per Controllable Load --
    # see NimbusControllableLoadNumber's own docstring for the full
    # mechanism. Reuses the SAME shared_store instance (keyed by
    # subentry_id + field, so no collision with the hub-level keys
    # above, which are never subentry-ULID-prefixed) rather than a
    # second Store file -- one JSON file, one lock, for every number
    # this config entry owns.
    #
    # nimbus issue #680 (Mark Purcell, real live regression, found within
    # 24h of #645 shipping): this used to build ONE combined list mixing
    # entities from every controllable_load subentry, then call
    # async_add_entities() ONCE with no config_subentry_id at all --
    # sensor.py's own commanded_state family (the SAME per-load device,
    # `(DOMAIN, subentry.subentry_id)`) has always registered WITH
    # config_subentry_id=subentry.subentry_id (see that module's own
    # docstring: "Each entity is added with config_subentry_id set,
    # which is what makes each..."). HA's own core warning names this
    # exactly: "assigns an existing device to a different config
    # subentry, by calling async_get_or_create or by adding entities
    # from several subentries that share a device; this silently moves
    # the device" -- the real, live consequence was sensor.py's own
    # commanded_state/status/next_start/etc family for that device
    # vanishing from the entity registry after a restart, backend data
    # completely intact, only the publish/registration step broken.
    # Fixed by looping per-subentry and passing config_subentry_id on
    # each call, exactly matching sensor.py's own established, correct
    # pattern for the same shared device.
```

## Line 1359: above `restored_state = await self.async_get_last_state()`

```text
            # nimbus issue #342 (Mark Purcell): this used to backfill the
            # Store from ANY successful restore unconditionally -- but
            # RestoreNumber's own restore-state dump (STATE_DUMP_INTERVAL,
            # every 15 min, plus on a clean shutdown) and this Store's own
            # write (synchronous, on every set) have genuinely different
            # cadences. A value set at 10:00, killed at 10:05 with the
            # last restore dump at 09:55, restores as the STALE 09:55
            # value -- and unconditionally backfilling then overwrote the
            # Store's own correct, newer value with it, permanently
            # (entry.options is deliberately never kept in sync either,
            # see this module's own top docstring). The restored STATE's
            # own last_updated (when HA itself last wrote that state, a
            # real proxy for "how current is this restored value") is now
            # compared against the Store's own written_at -- only backfill
            # when the restore is not older than what the Store already
            # holds, so a genuinely newer Store entry always survives a
            # stale restore.
```

## Line 1506: above `_CONTROLLABLE_LOAD_DESCRIPTIONS: tuple[_ControllableLoadNumberDescript`

```text
# Real bounds mirrored exactly from flows/controllable_load_subentry.py's
# own selector configs for these same fields (_HOUR_SELECTOR's 0-23.75/
# step 0.25, _KW_SELECTOR's min=0, _KWH_SELECTOR's min=0, _DOLLAR_PER_
# KWH_SELECTOR's min=0) -- a live dashboard slider should offer the
# identical real range the wizard already validated, not a
# independently-invented one. min_hold_minutes/max_activations_per_day
# have no wizard-specified max (a plain NumberSelector with no upper
# bound); the values here are generous, real headroom, not an
# architectural limit.
#
# nimbus issue #645's own explicit scope: does NOT include the 3
# sheddable-only fields (nominal_kw/min_fraction/shed_cost) -- not named
# in the issue's own proposal list, left for a future, separate ask
# rather than guessed into scope here.
```

## Line 1582: above `_ControllableLoadNumberDescription(`

```text
    # nimbus issue #645: a genuine, deliberate behaviour nuance vs the
    # wizard's own field -- the wizard's None (unset) means "no activation
    # cap at all" (load_run_state.activation_allowed()'s own contract),
    # a state a live NumberEntity structurally cannot represent (it
    # always carries SOME real number). Once this live entity exists for
    # a load, "unlimited" is no longer expressible through it -- a real,
    # generous default (10/day) stands in instead. A household that
    # genuinely wants no cap at all is not yet served by the live entity
    # path; documented here rather than silently changing behaviour with
    # no explanation.
```
