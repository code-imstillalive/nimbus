# Handover — EPR / quality-scoring, 2026-09-17

Written because the household is travelling overseas and this work must survive a
context loss, a new session, a new machine, or a week of silence. It is
deliberately long. Read the **State of play** and **If you only do three things**
sections first; everything below them is reference.

Nothing in here is a plan or an intention. Every number quoted was measured, and
the section **What is NOT verified** lists what wasn't.

---

## State of play, in one paragraph

The household's EPR score was reading **51.47%** on 16 Sep and they said, correctly,
that they "used to get 95". Two real defects were found and fixed and the same day
now scores **95.71%**. A third, **larger** defect was found afterwards and is **also now fixed**
(v0.94.380): the daily scorer ran at midnight and scored the day *before* that day's
P2P settlement existed, so the published number was computed as if the household had
no P2P arrangement at all — worth about **5 EPR points** and almost certainly the
mechanism behind the long-standing "my EPR records zero P2P exports" complaint.
Confirmed live: devhub's published score for 16 Sep self-corrected from **90.6% to
95.71%** on the first cycle after deploy.
`main` is at **v0.94.382**, deployed to devhub. **NUC1 is still on v0.94.375 and has
none of this** — the household is deploying it themselves.

---

## If you only do three things

1. **Deploy NUC1** (household's call, household's hands — never run commands there):
   ```
   cd /opt/homeassistant/config/nimbus_repo && git pull origin main && docker restart opt_homeassistant_1
   ```
   Plus the cron writer copy-outs if that install uses them:
   ```
   git show origin/main:scripts/nimbus_solver_quality_writer.py > /opt/nimbus_solver_quality_writer.py
   ```
   NUC1 currently has **neither** the timezone fix nor the P2P premium fix.

2. **#1082 is done** (v0.94.380) and #1073 is done (v0.94.379). The largest thing
   still open on this thread is **#1081**, worth $0.02 — genuinely small. The bigger
   remaining work is elsewhere: **#1012** (reference plane / SoC divergence) and
   **#949** (fleet-blended SoC).

3. **Do not trust `sensor.nimbus_solver_quality_report` on devhub.** It is a
   *mirror of NUC1*. Devhub's own is `..._quality_report_2`. This has burned this
   project repeatedly — see **The mirror trap** below.

---

## What was actually wrong, and what fixing it did

### Defect 1 — hour-of-day decisions were made in UTC (#1077, shipped v0.94.376)

`grid_times` carry UTC on the daily scoring path. A bare `.hour` read therefore
inherited whatever timezone the caller attached. The P2P gate `17 <= gt.hour < 24`
was comparing **17:00–24:00 UTC**, which in Brisbane is **03:00–10:00 local** — the
household's solar *charging* window, where grid export is genuinely zero.

So a 12 kW × 7 h commitment was recorded as **entirely undelivered**.

Fixed by adding `_local()` in `solver_writer.py` and converting **22 sites**. A new
AST sweep, `tests/test_local_hour_is_used_everywhere.py`, fails the build on any
bare `.hour`/`.minute` read in that file — it found 6 sites grep had missed, all
`.minute`.

Measured on devhub, same day, before/after:

| | before | after |
|---|---|---|
| `p2p_commitment_shortfall_kwh` | 76.9061 | **0.5185** |
| `j_ach` | −15.8848 | −20.8856 |
| `epr` | 45.18% | 51.47% |

The evening import price also corrected from $0.41 to $0.55/kWh — the network fee
tier landing on the right hours.

**The AST guard only covers `solver_writer.py`.** `ml/model.py`, `ml/features.py`
and `solver/regret.py` still contain bare `.hour` reads. Those were checked by hand
and are on paths that build their own local `now`, but nothing enforces it.

### Defect 2 — the oracle was paid the P2P premium at hours it could never be earned (#1079, shipped v0.94.378)

`_compute_report_for_window()` reads the day's real settled P2P and rebuilds
`grid_oracle` so the oracle prices export the way the settlement actually paid. It
computed the rate correctly:

```python
bonus_rate = real_p2p_dollars / real_p2p_volume_kwh
export_bonus_price = np.full(n_periods, bonus_rate)   # flat, all 24 hours
```

A P2P premium is a property of the **block**, not of the day.

With a real rate of `$10.4032 / 47.668 kWh = $0.2183/kWh`, `j_star` charged the
battery at 01:00–02:00 local paying **$0.215/kWh** and dumped **20 kW** at **05:00
local** into a spot export price of **$0.088/kWh**. A 13c/kWh loss on its face; it
only clears because the phantom premium turns $0.088 into $0.306. All of that fake
profit lands in `j_star`, and regret is measured against `j_star`.

The same `grid_oracle` object is reused for `j_ref` pricing (#1026), so the
do-nothing baseline was credited the premium too — on ~41 kWh of **midday solar
export**, none of it in the window. Both ends of the EPR fraction moved, both
downward.

Two further copies were fixed with it: the Nimbus-only counterfactual replay, and
the **static-config fallback pricing branch**. That last one matters most — it
prices a **live dispatch LP**, so on a generic install an ungated premium buys real
energy at real cost to chase revenue that never arrives. Its LocalVolts sibling had
been fixed this way on 2026-09-05 after a live catch of gold "P2P active" colouring
at midday; the fallback never got the equivalent gate. **Same defect, same file,
found once and not swept** — worth remembering as a pattern.

The gate is `p2p_bonus_price_by_period()`, which keys off
`fetch_p2p_fixed_export_kw()` — the array the LP is genuinely pinned by — rather
than re-reading the block hours, so it cannot drift from the window the LP uses.

`fetch_p2p_fixed_export_kw()` returns three distinct things and the gate handles
each: `float("nan")` = uncommitted (no bonus), `0.0` = post-midnight self-consume
where export is pinned off (no bonus), `rate_kw` = committed (bonus). **If you
change that function, re-check the gate.**

#### Measured result

| 16 Sep | v0.94.376 | v0.94.378 |
|---|---|---|
| **EPR** | 51.47% | **95.71%** |
| `j_ref` | −6.2453 | **+5.366** |
| `j_ach` | −20.8856 | −20.8856 *(unchanged)* |
| `j_star` | −34.6887 | **−22.0617** |
| **regret** | **$13.80** | **$1.18** |
| `epr_reason` | null | null |
| `regret_reliable` | true | true |

`j_ach` — what the household actually earned — is **identical to four decimal
places**. Their performance never changed. The yardstick was broken. This is the
single most important sentence in this document when explaining the work to them.

15 Sep also moved, 62.48% → **100.11%**, and that one overshoots — see #1081.

---

## Open issues, in the order they should be worked

### #1082 — daily scorer ran before the settlement existed — SHIPPED v0.94.380

Same install, same code (v0.94.378), same day (16 Sep), two scores:

| | daily publisher (00:00 on 17 Sep) | ad-hoc rescore (21:30 on 17 Sep) |
|---|---|---|
| `real_p2p_settlement_status` | `no_settlement_entry_for_this_date` | `applied` |
| `real_p2p_dollars` | **0** | 14.5364 |
| `j_ach` | −6.3493 | −20.8856 |
| `epr_pct` | **90.6** | **95.71** |

`−6.3493 − 14.5364 = −20.8857`. The whole gap is the P2P revenue.

And it is never corrected, because `publish_daily_quality_report()` has an
idempotency fast-path: once `latest_date` matches, it re-pushes the cached
attributes verbatim and returns. The P2P-blind score written at 00:05 stands
forever.

This is very likely the answer to the household's repeated
*"I export and your system says zero P2P exports"*. It was read as a gating bug
more than once. It is a **timing** bug.

**Fixed 2026-09-17 via candidate 1** — a score carrying a transient settlement
status (`no_settlement_entry_for_this_date`, `settlement_sensor_unreadable`) is no
longer treated as final; the publisher falls through and scores the day again once
the settlement has landed. `applied` is final and `no_sensor_configured` /
`window_is_not_one_local_calendar_day` are permanent, so an install with no
settlement sensor never re-scores.

Retries are spaced an hour apart using the published `generated_at` — no new state,
survives a restart, worst case ~24 extra solves on a day that never settles, and the
window closes when the scored date rolls over. Retrying per cycle would have
repeated #773.

**Confirmed live**: devhub's published sensor for 16 Sep went **90.6% → 95.71%** on
the first cycle after deploy, `real_p2p_dollars` 0 → 14.5364, `status`
`no_settlement_entry_for_this_date` → `applied`, `generated_at` moving from midnight
to 23:01. That is the default path healing itself, not an ad-hoc rescore.

Candidate 3 (a rescore service) is still unbuilt and still useful — it is what makes
a *future* scoring fix reach days already scored. WIP on
`feat/1082-rescore-quality-history`, local only. The household flagged it as
over-built for the immediate symptom; confirm before finishing it.

**A WIP branch already exists for candidate 3** — see branches below.

### #1081 — `oracle_beaten` — ROOT-CAUSED, fix is a modelling decision (still open)

After #1079, 15 Sep scores EPR **100.11%** with `regret_dollars: −0.0214` and
`epr_reason: "oracle_beaten"`. Above 100% is structurally impossible. It is 0.14%
of the objective, and 37× smaller than the −$0.79 of #956 (closed).

Progress made tonight:

- **Candidate 2 (the post-midnight self-consume pin) is FALSIFIED.**
  `_widen_export_pin_to_achieved()` does widen a hard `0.0` pin into
  `[0, achieved]`, and the `> 0.0` bonus gate is correct on all three
  `fixed_export_kw` cases. Ruled out by reading, not assumed.
- **A better hypothesis emerged.** `j_star` and `j_ach` come from **two different
  computation paths**:
  ```
  j_ach  = evaluate_realized_cost_multi(achieved) - real settled $
  j_star = oracle_plan.total_cost                  <- the LP objective
  ```
  "`j_star <= j_ach` by construction" is an argument about **trajectories**. It
  only carries over to the **numbers** if both are priced by the same arithmetic.
  An LP objective carries terms an evaluator does not (soft-SoC penalty, slack
  penalties, the bonus as a *chosen variable* rather than an *allocated credit*).
  A disagreement between paths can manufacture a violation out of two individually
  correct answers.
- An instrument for this is half-built on `wip/1081-jstar-path-delta` (see below).

`p2p_export.realized_export_bonus_credit()`'s own docstring already names the risk:
*"Both must answer the same question the same way, or a counterfactual scored here
is not comparable to one the LP produced."*

**SETTLED 2026-09-17 by measurement (v0.94.381 ships the instrument).** Scoring the
15 Sep evening window on the real install:

```
j_ach             = -5.0798
j_star            = -4.3148     <- LP objective
j_star_evaluator  = -5.0898     <- identical plan, priced j_ach's way
j_star_path_delta =  0.7750
regret_dollars    = -0.7649     <- impossible
epr_pct           =  110.98
```

`j_ach - j_star` = **-0.7650** (impossible). `j_ach - j_star_evaluator` = **+0.0100**
(correct sign). The delta minus the true regret is exactly the impossible regret.
EPR repriced through the evaluator is **99.87%**, under the ceiling where it belongs.

That window has **no P2P at all**, so the bonus model is excluded as an explanation.

It was never a $0.02 rounding oddity. `j_star` is charged for terms `j_ach` never
pays — the soft-SoC penalty being the largest identified, with at least one more
unaccounted for — which inflates `j_star`, shrinks regret and pushes EPR up.

**The remaining work is a modelling decision, not a bug fix**, which is why the issue
is still open and no fix was made unilaterally:

1. Take `j_star` from the evaluator, keeping the LP purely as the thing that *chooses*
   the oracle's plan. Restores the invariant by construction. **Probably correct** —
   the soft-SoC penalty is explicitly a modelling device, and #586 already zeroes it
   for the oracle in one special case, which is itself an admission that it distorts
   the comparison.
2. Charge `j_ach` the same terms. Defensible for real costs, indefensible for a
   penalty that exists only to steer the LP.

Either rescores every historical day on every install. Mark has been asked for a view.

### #1073 — Mark Purcell, mixed-window implied efficiency — SHIPPED v0.94.379

**Done and closed 2026-09-17.** A window where the smaller direction carries >=1%
of the larger now reports `mixed_window_not_decisive` (Mark's own suggested name),
or `mixed_window_not_decisive_implied_above_unity` when also above 1.0. The numbers
are kept, not nulled — the caveat is attached to them.

Deliberate departure from the issue: a **relative** 1% threshold rather than the
absolute `1e-6` floor it suggested mirroring, because 100 kWh in against 0.01 kWh
out is effectively one-directional and flagging it would suppress a decisive reading
the caller genuinely has. Same floor #1072 settled on. Flagged to Mark in the PR as
his call to reverse.

Verified live on devhub: `home` (75.216 kWh in / 40.183 kWh out) now reports
`mixed_window_not_decisive_implied_above_unity` where it previously said only
`implied_efficiency_above_unity`; `Test EV` (0 in / 0 out) still correctly reports
`no_charge_throughput`, so the gate does not over-fire.

**CI caught what the targeted local run missed**: `_is_mixed_direction_window` is a
new top-level function and the #357 drift guard failed until it was accounted for as
`INTENTIONAL_NATIVE_ONLY` — the cron forecast script computes no quality report, so
a helper cannot be a drift gap in a copy lacking the function it serves.

### Still open and untouched tonight

`#1012` — **substantially narrowed 2026-09-18 by measurement, and the fix direction
has changed.** Four pure-direction charge windows on 16 Sep show the charge-side
excess is a function of **SoC band**, not rate, not solar, not a plane constant:

| window | counted | excess | excess % | avg rate | SoC band |
|---|---|---|---|---|---|
| 10:00–12:00 | 25.521 | 0.945 | 3.70% | 12.8 kW | low |
| 08:00–12:00 | 32.489 | 1.522 | 4.68% | 8.1 kW | low |
| 11:00–15:00 | 92.971 | 7.983 | 8.59% | 23.2 kW | 20→99 |
| 13:00–15:00 | 42.624 | 5.159 | **12.10%** | 21.3 kW | 59→87 |

The last runs at a *lower* rate than the third and shows a *higher* excess, which
eliminates rate. A constant capacity or plane factor would give a constant
multiplier; it ranges 1.0370→1.1210 on one day. Un-metered DC solar was refuted
separately (near-identical solar, 5× different excess).

What survives: **the SoC sensor's scale is non-linear in energy in the upper band.**
The reconstruction integrates power (linear in energy) while the sensor does not, so
they separate most near full — which is where `soc_discrepancy_max_pct` lands every
time. Consistent with the pure-discharge window being correct to 0.8% despite
covering the same band (charging up through it is wrong; discharging down is fine),
and with #1013's BMS-reports-100%-at-119.72-kWh finding.

**MECHANISM FOUND 2026-09-18, at 5-minute resolution, on the RAW SENSORS with no
scorer involved — and it is a discrete event, not a gradual compression.**

Charge power sits pinned at a constant −30.4 kW for over two hours on 16 Sep. SoC
gain per 5-minute interval is rock-steady at 2.15–2.37 points… then ONE interval
near 88% gains **4.81 points** at the same power. 15 Sep reproduces it: SoC jumps
**6.39 points** in one interval at 14:25 while power was *falling* from −40 to
−26.7 kW. Both days then pin SoC at **99.3** while 3–12 kW keeps flowing in.

| condition | kWh per SoC point |
|---|---|
| 16 Sep, −30.4 kW | 1.10 |
| 15 Sep, −30 kW | 1.092 |
| 15 Sep, −40 kW | 1.100 |
| **the jump interval** | **0.435 – 0.503** |

1.092 at −30 kW vs 1.100 at −40 kW **refutes rate from the raw sensors** — a 33%
current change moves it 0.7%. And the high-excess windows are exactly the ones
containing the jump; the 3.70% and 4.68% windows both end before it.

**This is not a scorer defect at all.** The power series, the configured efficiency
and the plane are all measured correct. The BMS takes a step no integration of power
can reproduce, because no energy corresponding to it ever flowed. So
`soc_discrepancy` on this hardware is partly measuring a **real BMS recalibration**,
an unclamped integration must diverge across one by construction, and
`epr_reliable: false` driven by it is a false alarm of the same family as #949's
fleet-blend artefact.

Cause of the step is inferred, not confirmed (voltage-based re-estimation during CV
absorption fits, but the voltage series is confounded by IR drop at 30–40 kW).

**Third day (14 Sep) QUALIFIES this.** The dramatic step is NOT a daily certainty —
14 Sep's largest interval is 3.33 points (~24% above expectation) against 2.2× and
2.5× on 15/16 Sep. Top-band compression appears on all three days; the discrete step
does not. Something conditions it that three days cannot resolve.

What IS settled, five measurements across three days and a 33% current spread,
clustered within 2.2%: **kWh per SoC point in the linear region is 1.080 / 1.092 /
1.10 / 1.104 / 1.100.** Rate-independence is no longer in question. All three days
also pin SoC at 99.3 while 3–12 kW keeps flowing.

**The larger, unresolved discrepancy.** In the linear region a SoC point costs ~1.09
kWh counted. At the configured 122.2 kWh and 0.9263 efficiency it should cost
`1.222 / 0.9263 = 1.319`. The SoC scale moves ~**21% faster** than the configured
capacity/efficiency pair predicts, in the well-behaved middle of the range, with no
recalibration involved. #1013's 119.72 kWh moves this by 2%, nowhere near 21%. This
is upstream of everything else on the issue and is NOT explained by the top-band
step. Flagged, not concluded — extrapolating a capacity from a scale that compresses
at the top and pins at 99.3 is the kind of inference this thread has retracted twice.

**Correction recorded:** the earlier "counted energy, efficiency and plane are all
measured correct" holds for DISCHARGE only. The 21% above means it was overstated
for charge.

### THE HEADLINE RESULT (2026-09-18): measured round-trip efficiency is **1.045**

The 21% is now pinned to a quantity that involves **no capacity assumption at all**:

```
charge:     counted_in  per SoC point = (C/100) / e_c
discharge:  counted_out per SoC point = (C/100) * e_d
ratio       = e_c * e_d = ROUND-TRIP EFFICIENCY     <- C cancels
```

Discharge, 15 Sep 18:30–21:00, steady 13.1 kW, SoC 83.4→67.3 (linear region):
0.9515 points per 5-min interval → **1.1473 kWh delivered per SoC point**.
Charge, three days: 1.092 / 1.100 / 1.101 → **~1.098 kWh counted per point**.

```
measured round-trip   = 1.1473 / 1.098 = 1.045      <- ABOVE UNITY, impossible
configured round-trip = 0.9263^2       = 0.858
```

Assuming the configured e = 0.9263, the two directions imply different capacities:
**discharge → 123.2 kWh** (within 1% of configured 122.2, 3% of #1013's 119.72);
**charge → 101.7 kWh** (18% below both). Capacity is a constant, so discharge is
consistent with everything else known about the pack and charge is not.

**Rules out** a uniform sensor scale factor (cancels in the ratio), a capacity error
(cancels), the SoC calibration (cancels), and the top-band step (both windows avoid
it). What survives is an **asymmetry between directions** in the counted energy:
charge is under-counted ~22% relative to discharge, per unit of SoC.

**Likely — flagged, not confirmed:** an inverter register reporting DC pack power on
discharge but AC-side power on charge, or any equivalent asymmetric reference. That
is this issue's original plane hypothesis but **asymmetric**, which is exactly why
the symmetric conversion on `fix/1012-battery-power-plane` cannot fix it. Confirming
it needs the inverter's documentation or a second meter on the battery DC bus —
not resolvable from here.

**This is the thing to fix first.** `soc_discrepancy`, the 37-point divergence and
`epr_reliable` are all downstream of integrating a charge series that does not
balance against its own discharge series.

### THE PLANE HYPOTHESIS IS DEAD (2026-09-18, measured directly)

The per-inverter `battery_voltage_*` / `battery_current_*` sensors give true DC pack
power as V x I. Against `logger_battery_power`:

```
CHARGE  16 Sep, logger = -30.4 kW
  12:10  inv1 515.2x-29.8 = -15.35   inv2 538.0x-27.8 = -14.96   total -30.31
  12:11  inv1 517.3x-48.3 = -24.99   inv2 535.3x-10.0 =  -5.35   total -30.34
  12:15  inv1 516.1x-29.8 = -15.38   inv2 537.2x-27.9 = -14.99   total -30.37

DISCHARGE  15 Sep 19:00, logger = 13.06 kW
  inv1 512.2x0.0 = 0.00   inv2 524.6x24.9 = 13.06   total 13.06
```

Matches to **0.3–0.5% in BOTH directions**. The sensor is pack-side DC throughout.
**`fix/1012-battery-power-plane` should be deleted, not re-shaped** — there is no
plane error to convert. My own "asymmetric plane" proposal was also wrong.

### But a confound was found in the round-trip measurement

Charge ran **both** inverters in parallel; discharge ran **one at a time**. Verified:
the two alternate on discharge, swapping every 10–20 min (inv1→0.0 at 17:13:52,
inv2→25.1 at 17:14:03; inv2→0.0 at 17:23:15, inv1→26.1 the same second). **No
discharge window will ever have both active** — a "next measurement" I specified
before checking it was constructible, and it is not.

With two unequal packs (70.96 + 51.20) behind one blended SoC, the blend's weighting
decides whether charge and discharge windows are comparable at all:

| blend | kWh per point, inv1 alone | inv2 alone |
|---|---|---|
| capacity-weighted | 1.132 | 1.132 |
| simple average | 1.315 | 0.949 |

Measured: inv2-alone (83→67%) = **1.147**, close to capacity-weighted. inv1-alone =
**1.202** but the only available stretch sits at **92–95% SoC**, inside the known
non-linear top band, so it is contaminated and settles nothing.

### THE CONTROL LANDED — blend is capacity-weighted, round-trip stands

Found the required window: 15 Sep 20:15:34–20:34:54, inv2 at **0.0** while inv1 runs
25.8–26.0 A. Nineteen minutes, inv1 alone, at **SoC 59–63%** — clear of the top band.

| window | pack | capacity | measured | cap-weighted predicts | simple-avg predicts |
|---|---|---|---|---|---|
| 15 Sep 20:15–20:25 | inv1 alone | 70.96 kWh | **1.148–1.160** | 1.132 | 1.315 |
| 15 Sep 18:30–21:00 | inv2 alone | 51.20 kWh | **1.147** | 1.132 | 0.949 |

**Two packs differing 39% in capacity give the same kWh per blended point, to within
1%.** A simple average would have separated them by 39%. So the blend is
capacity-weighted, energy-per-point is invariant to which pack works, and the
alternating-discharge confound is eliminated.

**Therefore the round-trip stands**, band-matched over SoC 67–83.5:

```
charge    (both packs)  1.0937 kWh/point
discharge (inv2 alone)  1.1474 kWh/point
round-trip = 1.049      <- impossible, vs configured 0.858
```

At the configured e = 0.9263: **discharge implies 123.8 kWh** (within 1.3% of the
configured 122.2, 3.4% of #1013's 119.72); **charge implies 101.3 kWh** (17% below
both). Discharge is consistent with everything else known; charge is the outlier.

### RESOLVED: the counters are sound, the SoC scale is DIRECTION-DEPENDENT

Charge does not under-count. Over a **closed SoC loop** any direction-dependent SoC
error cancels, and solving `in*e - out/e = stored_delta` on the two scored days:

| day | in | out | ΔSoC | implied one-way e |
|---|---|---|---|---|
| 16 Sep | 109.667 | 106.041 | −1.078 kWh | **0.9784** |
| 15 Sep | 106.602 | 101.209 | +1.557 kWh | **0.9817** |

Both under unity, both physically possible, agreeing to 0.3%. Against the
band-restricted 1.049, which is not. Same counters, same sensor, same days — the
only difference is whether the window closes the loop.

**So the counters are sound** (a 17% one-directional scale error could not produce a
physically possible closed loop) **and the SoC scale is direction-dependent by ~5%
within a band**. Traversing 67→83.5 up costs less per point than 83.5→67 down
returns; over a full cycle the halves cancel. That is SoC hysteresis — the battery's
behaviour, not the integration's.

**This is the whole of #1012.** The achieved trajectory integrates real power; the
sensor it is compared against carries a direction-dependent offset that only cancels
over a complete cycle. Within a day the pack charges hard midday and discharges hard
in the evening, so the offset accumulates for hours before reversing — exactly the
shape of the ~37-point divergence and exactly why it peaks late afternoon.

**No conversion, efficiency, capacity or plane correction can fix it.** Nothing is
wrong with the inputs; two quantities are being compared that are equal only over a
closed cycle.

Actions: **delete `fix/1012-battery-power-plane`** (premise retired twice over);
accept that **`soc_discrepancy` cannot reach zero on this hardware** and that
`epr_reliable: false` from this source is a false alarm (same conclusion #949 reached
from the fleet-blend side); and note that **the configured efficiency looks
pessimistic** — now filed separately as **#1086** with a third day added:

| day | in | out | ΔSoC | implied one-way e |
|---|---|---|---|---|
| 14 Sep | 107.364 | 103.682 | +0.719 | **0.9861** |
| 15 Sep | 106.602 | 101.209 | +1.557 | **0.9817** |
| 16 Sep | 109.667 | 106.041 | −1.078 | **0.9784** |

Three days clustered within 0.8%, mean 0.982 one-way (0.965 round-trip) against a
configured 0.9263 (0.858) — **pessimistic by ~6 points one-way, ~11 round-trip.**

**QUALIFIED 2026-09-18: the estimate is CUT-sensitive by more than the day-to-day
spread.** Same day, different window boundary:

```
15 Sep  00:00 -> 00:00   in 106.602  out 101.209  delta +1.557   e = 0.9817
15 Sep  06:00 -> 06:00   in 106.602  out 105.967  delta -0.719   e = 0.9937
```

`energy_in` is **identical**; the whole difference is in `out`, because the midday
charge sits inside both windows while the evening discharge is split differently.
**1.2 points from the cut alone**, against the 0.8-point three-day spread I was
quoting as the error bar.

Why: a small `measured_soc_delta_kwh` says the pack returned to the same *reading*,
not to the same *place on the hysteresis loop*. A window cut at 06:00 enters and
leaves that loop at a different phase from one cut at midnight. "Closed loop" was a
necessary condition, not a sufficient one.

**Survives:** direction and rough magnitude — every cut and clean day lands between
**0.978 and 0.994** against a configured 0.9263. A 1.2-point sensitivity does not
erase a 5–7 point gap. **Does not survive:** the precision. Honest statement is
"roughly 0.98–0.99, with at least ±0.6 points of systematic uncertainty from window
placement", on three days of one install.

**Averaging more days at the same cut will NOT reduce this** — it is systematic, and
every midnight window shares the phase bias.

**AND THE FIX I PROPOSED DOES NOT WORK.** I suggested anchoring windows at a SoC
extreme (this pack bottoms at ~2% around 06:00 daily). Tested, and it makes the
scatter worse:

| window | in | out | ΔSoC | e |
|---|---|---|---|---|
| 14 Sep 00:00 | 107.364 | 103.682 | +0.719 | 0.9861 |
| 15 Sep 00:00 | 106.602 | 101.209 | +1.557 | 0.9817 |
| 16 Sep 00:00 | 109.667 | 106.041 | −1.078 | 0.9784 |
| **15 Sep 06:00** | 106.602 | 105.967 | −0.719 | **0.9937** |
| **16 Sep 06:00** | 109.667 | 102.941 | **0.000** | **0.9689** |

The 16 Sep floor-anchored window closes **perfectly** (ΔSoC exactly 0.000 — the ideal
case) and is the furthest from the rest. **2.5 points apart across the two anchored
windows, against 0.8 across three midnight cuts.** A perfectly closed loop is not
more trustworthy than an imperfectly closed one, so the scatter is not about closure.

Likelier: each 24 h contains one deep charge and one deep discharge, and #1012's
top-band recalibration step lands differently relative to the boundary depending on
the cut. A step adding apparent SoC without matching energy biases `e` up or down
according to whether it falls inside or is straddled. Both anchors still contain it.

**Honest state: measured one-way e is 0.9689–0.9937 across five windows; configured
is 0.9263.** The ~5.6-point gap exceeds the ~2.5-point scatter and every window lands
above the configured value, so the finding survives — but the number cannot be pinned
closer than ~0.97–0.99 and **I have no remaining proposal for tightening it.**

**Do not set the config value from this.** The useful next step is confirming the
DIRECTION on a second install — one closed-loop day on Mark's hardware says whether
~0.98 against 0.9263 is this pack or the calculation. If it reproduces, pursue it; if
not, close #1086 as an artefact of one install.

This matters because `solver_efficiency_percent` prices the **live dispatch LP**, not
just the scorer: too low makes every stored kWh look dearer to acquire and cheaper to
release, so genuinely profitable arbitrage gets declined, export is taxed twice by a
modelled loss, and the P2P commitment economics shift. It shows up as a plan quietly
more conservative than the hardware warrants, never as an error.

**Deliberately NOT changed** — re-pricing every future dispatch decision on a live
install is a household call. #1086 recommends re-running the same three-number
arithmetic over a fortnight including a cloudy stretch first, to separate the pack
from the season.

**A fourth day (13 Sep) was added and had to be EXCLUDED**, which supplies the filter
any longer run needs. It computes 1.0226 — impossible — and fails a screen needing no
efficiency model at all: on a near-closed loop its `energy_out` (113.328) **exceeds**
its `energy_in` (108.136). Corroborated by `load_nowcast_skill_coverage: 0.958` and
by #1012's own note that this specific day has missing mirrored recorder history.

**Screen to apply mechanically before including a day:**
1. `energy_out` must not exceed `energy_in` when `|measured_soc_delta_kwh|` is small —
   catches 13 Sep using only the three numbers already in hand
2. `load_nowcast_skill_coverage` at or near 1.0 (clean days 0.979–1.000; the failure 0.958)
3. the loop must close — all four qualify here (0.2–1.6 kWh against ~105 kWh throughput),
   so this is not what separates them

Without (1), a fortnight average silently absorbs impossible days and drags the result
toward unity — the direction that would make the finding look like nothing.

**Screen (1) is now automatic — v0.94.382.** `battery_energy_balance()` emits
`energy_out_exceeds_in_on_closed_loop` when a window returns to within 5% of
throughput of its starting SoC and still delivers more than it received. Gated on the
loop closing so a legitimate start-full-end-empty window (79 kWh out against zero in)
is never flagged. Verified live on devhub against the motivating day.

**And the live verification corrected my own framing.** The changelog and my #1086
comment attributed the violation to missing recorder data. Scoring 12 Sep 18:00 →
13 Sep 18:00 fires the check with `load_nowcast_skill_coverage: 1.0` — 48 of 48
periods, nothing missing — and still shows out 110.367 against in 108.136 (`e =
1.027`). **Missing data can cause it; it is not necessary for it.**

The reason string is neutral and survives: it states the observation, not a cause. The
likelier cause given #1012 is the direction-dependent SoC scale — a window cut at
18:00 samples the hysteresis loop differently from midnight-to-midnight (same day:
1.0226 vs 1.0270). So the check flags **windows whose endpoints are not comparable**,
whatever the reason, which makes it more useful rather than less. Screen (2),
coverage, is therefore NOT redundant with (1).

**Method note worth keeping:** the closed-loop calculation is insensitive to the SoC
hysteresis above, because a direction-dependent error cancels over a complete cycle —
which is exactly why it works where the band-restricted measurement did not. It must
be done on the quality report's own time-weighted figures; an attempt using hourly
statistic means gave 1.026 (impossible) on the same day that properly measures
0.9861.

Caveats: two days, one install; the arithmetic assumes `e_c = e_d` (one equation per
day cannot separate them); and the top-band recalibration sits inside both days, so
it is folded into the 0.978/0.982 rather than excluded.

**Consequence for the fix: it is not an input-boundary conversion at all.**
The counted energy and the configured efficiency are both demonstrably right. What
cannot be expressed by a single `capacity_kwh` constant is the SoC→kWh mapping.
Mechanism (BMS advancing SoC on voltage during CV absorption) is hypothesis, not
established. Next cheap step: repeat on a second day to confirm the dependence is
not specific to 16 Sep.

Also still open:
`#949` (fleet-blended SoC artefact), `#768` (fleet oracle + controllable-load
timing), `#944`, `#987`. 21 open issues total.

---

## Branches — exactly what is on each

All are local-only unless stated. **None should be merged without finishing the
work described.**

| branch | state | what remains |
|---|---|---|
| `feat/1082-rescore-quality-history` | ~80% | **no tests written.** Adds `nimbus_load.rescore_quality_history` (`days`, `clear_history`), a `force` + `history_seed` kwarg on `publish_daily_quality_report()`, and a `services.yaml` entry |
| `wip/1081-jstar-path-delta` | ~40%, **do not merge** | computes `j_star_evaluator` / `j_star_path_delta` but does **not** publish them on the `QualityReport` dataclass or the returned dict. No tests |
| `fix/1012-battery-power-plane` | parked — **do not merge as shaped** | plane conversion + config field + wizard dropdown. Measured 2026-09-17: it converts BOTH directions, but on pure-direction windows the **discharge side is already correct** (implied 0.9336 vs 0.9263 configured) while the charge side is impossible at either plane (1.0859 — the pack gains ~8 kWh MORE than counted). Converting both would break the half that works. See #1012 |

Two **pre-existing, unrelated** stashes exist (`stash@{0}` from
`fix/1012-battery-power-plane`, `stash@{1}` "ContinuousBlockLoadConfig"). They are
not from this session. **Leave them alone.**

### On `feat/1082-rescore-quality-history` specifically

The household pushed back on this as over-built — *"this should be quite simple...
u seem to be over complicating it?"* — and they were right about the **immediate
symptom**, which has a zero-code workaround (below). The service is still the only
thing that covers a multi-day range and works on a HAOS install with no host shell.
Their explicit earlier ask was *"I want the history deleted and the last 3 days
rescored once we fix it"*, which the workaround cannot do. **Confirm with them
before finishing it.**

One design note worth keeping: the publisher's fast-path `if` line is matched as
**literal source text** by
`tests/test_solver_writer_family_a_freshness_repush.py`. The branch prefixes it
with `not force and` because that test does a substring check. Anything that
reformats that expression breaks the guard.

---

## The zero-code workaround for a stale devhub score

Developer Tools → **States** → `sensor.nimbus_solver_quality_report_2` → change
`latest_date` from the stale date to anything else → **Set State**.

The fast-path only skips a day when `latest_date` matches. Break the match and the
next Nimbus cycle rescores yesterday with current code — and if the settlement has
since landed, it books the real P2P.

Covers **yesterday only**. Cannot reach older days. Needs a human each time.

---

## The mirror trap — read this before diagnosing anything on devhub

devhub carries a `remote_homeassistant` mirror of NUC1. **The canonical
`sensor.nimbus_*` entity ids resolve to the MIRROR.** devhub's own entities are the
`_2`-suffixed duplicates.

Measured tonight:

| entity | is | `nimbus_version` | showed |
|---|---|---|---|
| `sensor.nimbus_solver_quality_report` | **NUC1 mirror** | 0.94.375 | 106.43%, `scored_participants: ["home"]` |
| `sensor.nimbus_solver_quality_report_2` | **devhub's own** | 0.94.378 | 90.6%, `scored_participants: ["home", "Test EV"]` |

`nimbus_version` (added by #972) is the one-line check. Use it every time.

**Do not** "fix" this by adding an `entity_prefix`, a filter, or removing the mirror
connection. The household has rejected that twice and has already decided to move
off mirrors to real NUC1 sensors. It is not a bug to be tidied.

Also live on devhub: a participant named **`Test EV`** with
`implied_efficiency_reason: "soc_moved_without_throughput"` — zero recorded
throughput but a real SoC delta (−10.992 kWh on 15 Sep, 18.32% of its capacity).
Mark filed this mechanism on #768 today. It matters here because `battery_oracle`
hands the oracle **every** participant's full SoC envelope, so a phantom asset the
real system never used inflates `j_star`. Not quantified yet.

---

## What is NOT verified

State these plainly rather than letting them be assumed:

- **Only two days were rescored** (15 and 16 Sep), **both on devhub**. Devhub is
  not the household's install, and it carries the synthetic `Test EV` participant
  that NUC1 does not.
- **NUC1's real EPR after these fixes is unknown.** It is still on v0.94.375.
- **95.71% is one day.** No multi-day distribution exists post-fix.
- The **#1081 LP-vs-evaluator hypothesis is a hypothesis.** The instrument to test
  it is half-built. It has not been measured. Do not repeat it as a finding.
- The **history deletion + 3-day rescore the household asked for has not happened**,
  because no service currently writes the sensor.
- devhub's `epr_reliable` is `false` on both days, but for `soc_discrepancy`
  reasons (#1012 / #949), **not** because of anything shipped today. On 16 Sep
  `epr_reason` is `null` and `regret_reliable` is `true`.

---

## Reference — things a fresh session would otherwise re-derive

**Releases today:** v0.94.376 (#1077 timezone), v0.94.377 (#1072/#1074 Mark's
IV&V), v0.94.378 (#1079 P2P premium gating). All tagged and released; HACS only
sees tags.

**Local suite** (~19 min, 3076 tests):
```
python -m pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant -q
```
**Lint (exactly what CI runs):**
```
ruff check custom_components/ tests/ docs/real-world-integration/files/ --exclude research
ruff format --check custom_components/ tests/ docs/real-world-integration/files/ --exclude research
```
**mypy baselines — do not "fix" these, just don't add to them:**
```
mypy custom_components/nimbus_load/solver custom_components/nimbus_load/ml --ignore-missing-imports --follow-imports=silent   # 6 errors
mypy custom_components/nimbus_load --ignore-missing-imports                                                                   # 211 errors
```

**The repo is CRLF throughout.** A Python heredoc doing `assert old in s` will fail
against `\n` strings. Use the Edit tool, or `.replace("\n", "\r\n")`.

**Verifying a guard actually bites.** Every guard added today was proven to fail
without its fix by temporarily reverting the call site — e.g. the #1079 guard
reports the exact phantom premium, `bonus at uncommitted periods:
[0.21824284635394814]`. This repo has found **three** guards enforcing less than
they appeared to in a single morning (2026-09-16). A guard that cannot fail is
indistinguishable from one that works. Do not skip this step.

**Do not use `git stash push -- <paths> -q`.** The `-q` after `--` is parsed as a
pathspec, the push silently fails, and a following `git stash pop` will try to
apply an unrelated older stash onto your tree. This happened tonight and was caught
only by checking `git stash list`.

**Household conventions that cost time when forgotten:** never quote raw UTC —
always convert to Brisbane local. Read an issue's full body and every comment
before triaging. Never write "does NOT close #N" (GitHub's scanner has no negation
awareness and will close it). Always cut a tagged release after a merged batch.
Defer to Mark Purcell's technical judgment unless there is a verified reason not
to. Never run any command against NUC1/NUC2, not even read-only.

---

## The thread of the argument, for whoever picks this up cold

The household said the number was nonsense. It was. Three separate defects, all in
the same metric, all found within about a day of each other, and each one only
became visible once the previous was fixed:

1. The window was on the **wrong hours** (UTC vs local). Fixing it revealed —
2. The premium was being paid **outside the window entirely**. Fixing it revealed —
3. The score is taken **before the settlement exists**, so on most days there is no
   premium to place at all.

Each fix was correct and each was insufficient, and the reason is worth carrying
forward: all three were invisible because the number they produced was *plausible*.
Nothing crashed. Nothing logged an error. `real_p2p_settlement_status` had been
honestly reporting `no_settlement_entry_for_this_date` since #1016 and nobody read
it.

The recurring failure mode in this project is not the crash. It is the confident
wrong number.
