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
now scores **95.71%**. A third, **larger** defect was found afterwards and is **not
fixed**: the daily scorer runs at midnight and scores the day *before* that day's
P2P settlement exists, so the published number is computed as if the household had
no P2P arrangement at all — worth about **5 EPR points** and almost certainly the
mechanism behind the long-standing "my EPR records zero P2P exports" complaint.
`main` is at **v0.94.378**, deployed to devhub. **NUC1 is still on v0.94.375 and has
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

2. **Work #1082** (the midnight/settlement timing bug). It is worth ~5 EPR points.
   #1081 is worth 0.1. Do not do them in filing order.

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

### #1082 — daily scorer runs before the settlement exists (BIGGEST, not started)

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

Three candidate fixes are written up on the issue. 1 (rescore when settlement
arrives) and 3 (a rescore service) are complementary; 2 (delay the first score) is
a guess about someone else's settlement timing.

**A WIP branch already exists for candidate 3** — see branches below.

### #1081 — residual `oracle_beaten`, $0.02 (small, investigation half-done)

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

### #1073 — Mark Purcell, mixed-window implied efficiency (~90% done, parked)

His pinning test already goes `XPASS(strict)`, which proves the fix works. What
remains is removing his `xfail` marker and updating
`test_the_implied_efficiency_recovers_the_configured_one`, which uses in=100/out=50
— a mixed window — and asserts `implied_efficiency_reason is None`.

### Still open and untouched tonight

`#1012` (reference plane / SoC divergence, 14 comments — the deepest open thread),
`#949` (fleet-blended SoC artefact), `#768` (fleet oracle + controllable-load
timing), `#944`, `#987`. 21 open issues total.

---

## Branches — exactly what is on each

All are local-only unless stated. **None should be merged without finishing the
work described.**

| branch | state | what remains |
|---|---|---|
| `fix/1073-mixed-window-confound` | ~90% | remove Mark's `xfail`, fix the one test that asserts `reason is None` on a mixed window, lint/mypy/suite, PR |
| `feat/1082-rescore-quality-history` | ~80% | **no tests written.** Adds `nimbus_load.rescore_quality_history` (`days`, `clear_history`), a `force` + `history_seed` kwarg on `publish_daily_quality_report()`, and a `services.yaml` entry |
| `wip/1081-jstar-path-delta` | ~40%, **do not merge** | computes `j_star_evaluator` / `j_star_path_delta` but does **not** publish them on the `QualityReport` dataclass or the returned dict. No tests |
| `fix/1012-battery-power-plane` | parked, older | plane conversion + config field + wizard dropdown; needs bridge-sensor key, translations, drift exemption, and a 46→47 wizard budget decision |

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
