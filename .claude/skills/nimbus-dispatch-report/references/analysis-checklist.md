# Analysis checklist

What to look for, in the order it usually pays off. Each item names the field it comes from
so the claim in the report can be traced to the diagnostics.

## The day-ahead plan (`plan_analysis.json`)

**Structure first.** Read the charge / discharge / export runs the script prints. A good
plan is usually two or three blocks: buy in the cheap window, carry the house through the
expensive one, sometimes a small overnight top-up. If it is a mess of short reversals, say
so; `network.py`'s proximal / rate / smoothness terms are supposed to prevent that.

**Timing at the cliffs.** Where does the import price step (e.g. 4 ¢ -> 30 ¢)? Is the pack
full just before, or does it reach full an hour early (wasted headroom) or late (bought at
peak)? `soc_max_at` vs the first period of the high price answers this.

**Floor time.** `hours_at_floor` and `soc_min_at`. Hitting the configured minimum is not a
fault by itself; sitting there through a shoulder-price morning is. Compare with
`risk_aversion` in the meta: a plan that runs exactly to the floor with a low risk aversion is
choosing money over reserve, and that is a household decision worth making visible.

**Caps pinned.** Periods at `grid_import_kw == import limit` and `battery_kw == -max charge`.
Pinned caps mean the plan wanted more; unpinned caps in a cheap window mean it did not.

**Export logic.** `grid_export_kw` total against the export price range. Zero export is right
when feed-in is below the value of holding; export at a price below the next hour's import
is a red flag (check `flow_battery_to_grid_kw`).

**Price blend.** Count of periods where blended != raw, and when the first one occurs. Every
decision after that point rests on the secondary / forecast source. The
`price_blend_algorithm` string in the meta names the rule.

**Load forecast vs meter.** `load_summed_18_now_kw` vs `load_whole_house_cross_check_now_kw`.
More than ~15 % apart is worth a weakness bullet; the coordinator's "residual drift"
warnings in the HA log corroborate it.

**Solar.** `solar_delivery_ratio` (actual / forecast recently) and
`flow_pv_to_battery_kw` vs `flow_grid_to_battery_kw`: is the pack charged from solar or from
cheap grid? Neither is wrong, but the exposure differs (a midday import-price spike vs a
cloudy day).

**Cost split.** `cost_breakdown` over the horizon: `grid_net`, `degradation`, `charge_fee`,
`discharge_fee`, `terminal_value_credit`. When degradation approaches the grid cost the plan
is being steered by that parameter; say what changes if it is halved or doubled.

**Uncertainty.** `cost_band` (lower / upper / width) against `total_cost`. A band wider than
the expected cost with zero export means the plan has no hedge against the price it is
exposed to.

**Efficiency and losses.** `ac_bus_losses_kwh`, `efficiency_convention`; SoC deltas should
reconcile with charge x eff and discharge / eff. If they do not, the sign convention flag
(`solver_battery_power_positive_is_charge`) is the first suspect.

**Household overrides.** The startup log line "household-specific override(s) active" lists
constants (fixed daily charge, post-midnight export pin, hard-coded discharge schedule) that
are applied regardless of the wizard. Mention any that affect the numbers shown.

## Yesterday's scorecard (`yesterday.json`)

**Definitions.** `j_ref` = idle battery, `j_ach` = the day as scored, `j_star` = perfect
foresight oracle. `EPR = (j_ref - j_ach) / (j_ref - j_star)`; `regret = j_ach - j_star`.
`hourly_regret[h]` is the achieved-minus-oracle cost in that hour, so negative hours are
where the oracle *spent* (charging) to earn later, not hours the household "beat" it.

**Where the regret sits.** Sort the hours. Usually one decision dominates (a reserve held
through the export peak, a cheap window not fully bought). Name it with the kWh and the price
gap, and translate the gap into dollars net of fees, not just the raw regret.

**The oracle cycles for free.** `_compute_report_for_window()` builds both LPs without
`degradation_cost_per_kwh` (defaults to 0 in `BatteryConfig`), while the live plan pays the
configured value (often 3 ¢/kWh). The oracle therefore over-cycles - selling at 6-7 ¢ to
rebuy at 1-2 ¢ is "optimal" for it and worth nothing to the household after 2 ¢ of fees plus
degradation. `score_day.py` prints a re-pricing estimate (oracle and achieved throughput x
degradation); treat the re-priced oracle as an upper bound on its cost, since a
degradation-aware oracle would re-optimise. Use it to separate *defensible* missed value from
*artefact* regret before writing lessons.

**"Achieved" is integrated, not measured.** The scored trajectory comes from the battery
power sensor via the efficiency model, anchored to the day's initial SoC; it is not the SoC
sensor. Compare `real_soc` (recorder) with `ach_soc` hour by hour. Discrepancies of tens of
points mean the two sensors cover different things (a combined SoC across two batteries vs
one battery's power, a sensor dropping samples) and the achieved cost is a model. Check the
grid sign too: the report's `grid_kw` vs the meter's mean tells you whether the house was
importing or exporting in the hours the report thinks it imported.

**Tracking fidelity.** `tracking_fidelity` = 1.0 and `tracking_cost` = 0 with Nimbus in
shadow mode (another automation drives the battery) is vacuous. Check which automation owns
the battery (`ha_search` over automation bodies for the inverter name) before interpreting.

**Counterfactual sensor.** `sensor.nimbus_counterfactual_soc` gives Nimbus's own would-have
closing SoC vs the real one. It tells you which direction Nimbus would have moved relative
to what actually ran - useful for "would Nimbus have done better" without over-claiming.

**Key labels.** The service response keys `hourly_regret` by UTC hour; the sensor keys it by
local hour. `score_day.py` normalises to local; do not mix the two sources by hand.

**Cross-check.** `cqr.json` (the service) must reproduce the sensor's `epr`, `j_ref`,
`j_ach`, `j_star` for the same window. If it does not, the sensor is stale or the window is
off by the timezone; say which before using either.

## Writing the lessons

Lead with the single largest attributable decision, then the metric caveats, then anything
structural (a sensor mismatch, a parameter that steers both the plan and the score). Tie the
lessons back to the day-ahead plan where they connect - a reserve held too high yesterday
and a plan sitting on the floor today are the same parameter seen from two sides.
