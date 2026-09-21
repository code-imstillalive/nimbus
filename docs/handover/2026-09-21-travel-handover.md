# Travel handover — 2026-09-21

Written at **13:05 AEST (03:05 UTC)**, immediately before the household closes the laptop.
They travel until **2026-10-05**, over a VPN, and asked for a handover detailed enough to
survive an interruption.

**Every dynamic figure below was read live at the time of writing, not recalled.** That rule
exists because this file's own predecessors went stale: on this same day the 116KAT
infrastructure reference was found to have the NUC roles reversed for 13 days, and production
Nimbus was 22 releases behind what a session had assumed. Both were caught only by a live
read. Anything unverified is labelled as such.

---

## 1. State at departure

### The machines — verified live 13:05 AEST

```
sensor.nuc1_status   healthy   holds_vip=True    10/10 containers up   disk 27%
sensor.nuc2_status   healthy   holds_vip=False    0/11 containers up   disk  8%
```

**NUC1 (`192.168.1.223`) is MASTER and holds the VIP `192.168.1.221`.** NUC2
(`192.168.1.222`) is BACKUP. They swapped on **2026-09-08 17:49** — if a future session reads
anything claiming NUC2 is MASTER, it is stale. NUC2 showing 11 containers all stopped is a
**correct** pre-staged standby, not a fault. NUC1 running 10 not 11 is also correct:
`opt_amber2mqtt_1` was deliberately dropped from it on 2026-09-18.

### Nimbus versions — the single most important fact here

| where | version |
|---|---|
| **NUC1 (production)** | **v0.94.413** |
| `main` / latest tag | **v0.94.415** |
| devhub | **v0.94.415** |

**Production lagging is correct and deliberate** — devhub is the test environment, NUC1 is
production and is only promoted to a build that has been tested. **Do not treat the gap as a
fault and do not propose a deploy unasked.** v0.94.414 and .415 are ready on return.

### Production EPR — the thing the household actually cares about

```
sensor.nimbus_solver_quality_report   88.6
  nimbus_version  0.94.413
  epr_reliable    False
  epr_reason      achieved_soc_unreliable:disagreement
```

This read **28.61%** this morning with `epr_reliable: None` and `epr_reason: None`. It was
fixed today — see section 2.

### Live control — this is normal, do not raise it as an incident

```
automation.nimbus_battery_automation_live   on   last_triggered 2026-09-21T03:05:23Z
```

**Nimbus is live on NUC1 and driving the household's real battery, by the household's own
decision, and it is performing well.** It has run continuously since 2026-09-18 12:29 AEST.
This is *why* the NUC1 access controls are strict, not a reason to be alarmed.

A previous session filed this as a possible unauthorised recurrence (116KAT PR #854). The
household corrected it and the PR was closed as wrong-premised. **Do not re-raise it.**

---

## 2. What happened today, and the lesson that generalises

### The EPR fix — a merged fix is not a delivered fix

Ten EPR releases shipped on 2026-09-20 (v0.94.404–413). **NUC1 was running v0.94.391.** Every
one of those fixes sat on `main` doing nothing for the number the household looks at. Their
words: *"my aim here is ot fix this rotten epr issue ... which should have been addressed
yesterday"*. They were right.

The household ran the deploy themselves (commands handed over), then
`nimbus_load.rescore_history` with `days: 5`:

| day | before | after |
|---|---|---|
| 16 Sep | 106.4% (structurally impossible, #956) | **92.1%** |
| 17 Sep | 94.8% | **94.8%** (unchanged) |
| 18 Sep | 46.8% | **92.1%** |
| 19 Sep | 54.1% | **91.7%** |
| **20 Sep** | **28.6%** | **88.6%** |

Root cause was `j_ach`: the old scorer reconstructed achieved value at −$0.51 for 20 Sep
against a true −$12.52, then reported the shortfall as the household's failure. Regret fell
$12.20 → $2.26.

**Two controls that say this is a real fix, not inflation:** 17 Sep did not move at all, and
16 Sep's impossible 106.4% came *down*.

**Rules this proves, and they are in memory as `reference_nimbus_deployment_model`:**

- **A scored day is FROZEN.** Updating code does not change historical rows.
  `nimbus_load.rescore_history` (`days`, 1–30) rebuilds them and must be run after any scorer
  change reaches production.
- **Never report a scorer fix as delivered** until it is on the box the household reads AND
  the affected days are rescored.
- **Check `nimbus_version` on their sensor first**, before diagnosing any figure they report.

### A wrong alarm raised and withdrawn

Before the rescore I flagged that EPR "fell off a cliff at exactly the point Nimbus took over
dispatch" and put it to the household. **It was an artefact of the broken reconstruction.**
The corrected trend is 92.1 / 94.8 / 92.1 / 91.7 / 88.6 — high throughout, including the three
days Nimbus has been driving. Recorded because the alarm reached them and the retraction
belongs beside it.

---

## 3. Environment and access controls — reproduced in full

A resumed session may not have read the same files. These are absolute.

1. **Never execute ANY command on NUC1 or NUC2.** Not read-only, not after asking, not once.
   There is no task-scoped exception. Every NUC action is a command *handed to the household*.
2. **Verify NUC state only via the HA REST API from the Windows PC** —
   `http://192.168.1.221:8123`, token at `C:\Users\Raf_local\.ha_token`. That is an HTTP call
   from this machine, not a command on the NUC.
3. **No restarts on either NUC during 17:00–24:00 AEST**, every day, absolute.
4. **116KAT-HA-AI: always branch + PR, never merged by me.**
5. **nimbus: PRs may be merged directly, including my own.**
6. **devhub (`192.168.1.151`): full standing authorization** — HACS, restarts, config,
   entities, no asking. **One carve-out: never change or reload the `remote_homeassistant`
   connection** without specific sign-off.
7. **Never post devhub IPs, paths or add-on names into public GitHub.**
8. PR bodies: **scope first**, so no closing keyword precedes an issue number.
9. Run **both** `ruff check` and `ruff format --check` before pushing — and check their exit
   codes; piping to `tail` swallows them, which let a lint error into a commit today.

### devhub specifics that cost time today

- **Its HA is on port 80, not 8123.** A refused 8123 is *not* an outage. Nearly declared it
  down over this.
- **Canonical `sensor.nimbus_*` ids on devhub are NUC1's MIRROR. devhub's own are
  `_2`-suffixed.** Read the wrong one and you are reading production while believing you are
  reading the test box. Nearly reported NUC1's `status: optimal` as devhub's; the only tell
  was its `nimbus_version` being a release behind.
- **Duplicate `_2`/`_3` devices are expected.** The household: *"we tried ot clean and delete
  these but they would just recreate over time"*. **Deletion has been tried and does not
  hold.** Do not propose it.
- **Reading a devhub log**: exclude the startup unique-ID burst, exclude #773's intermittent
  lex-phase failure, exclude #944's 16 KB attribute-cap warnings — judge the release on what
  remains. On the v0.94.415 deploy that left **zero**.
- **A devhub restart is not free**: it triggers a reconnect that spawns a fresh duplicate set.
  Prefer `homeassistant.reload_config_entry`, which re-runs setup without restarting HA.
- **Debug logging is currently ON** for `custom_components.nimbus_load` on devhub (set live
  via `logger.set_level`). It resets on restart.

---

## 4. Work in flight — nothing is, and that is deliberate

```
nimbus    branch main, clean, 0 unpushed, manifest 0.94.415 == tag v0.94.415
116KAT    branch main, clean, 0 open PRs
devhub    v0.94.415
```

**No uncommitted work, no unpushed commits, no open PRs in either repo.** An interruption
right now loses nothing.

### Stashes — three exist, all accounted for

| stash | status |
|---|---|
| `stash@{0}` "1162-ask2-history-row-verdict" | **redundant — already shipped as v0.94.415.** Safe to drop; left in place rather than delete something I did not create the policy for |
| `stash@{1}` "WIP on fix/1012-battery-power-plane" | pre-existing, older, not from today |
| `stash@{2}` "WIP ContinuousBlockLoadConfig (paused, unfinished)" | pre-existing, older, not from today |

`stash@{0}` is the one the household saved: *"before you throw away your own work... please
just check it has not been fixed"*. It was real, the gap was still open, and it shipped.
**Lesson kept: a stash is invisible to the next session — rescue to a pushed branch.**

116KAT has its own three older stashes, one marked *"not mine to touch"*. Left alone.

---

## 5. What was verified today, and what was assumed

**Verified live:**

- NUC roles, container counts, disk, VIP holder
- Production nimbus version, EPR, reliability fields, full history table
- Live automation state and last-triggered
- devhub nimbus version and its *own* quality report
- Both ruff gates and the full test suite (3482 passed) before each release
- The new CI commit-message step actually running (job step list, not inference)

**Assumed / not verified — treat with care:**

- **That the 21 Sep score will be computed natively rather than rescored.** This is the free
  experiment queued below; nobody has seen it yet.
- **Whether NUC1 and NUC2 hold different `core.uuid` values.** This is the key to the devhub
  duplication theory in section 7 and needs a file read on both nodes — a household action.
- **Whether #1192 affects any install other than the dev one.** Production shows zero
  collisions across its whole log, but only two installs have been checked.

---

## 6. Decisions owed by the household — nobody should start these unasked

- **#987** — submitting the brand icons to `home-assistant/brands`. Assets verified
  submission-ready twice. Needs an explicit go-ahead because it is an outward-facing PR to a
  major public repo under their identity. Also `brand/icon_2.png` (500×484, invalid,
  unreferenced) awaits a delete decision.
- **#949 / #1086** — the SoC reconstruction disagreement and the efficiency figure. **No
  configured value should be changed** on the strength of anything measured so far; capacity
  and SoH are confirmed correct and must not be touched.
- **#1181** — three options for the home battery's missing gap guard. The gap framing that
  motivated it was falsified; the participant/home asymmetry is still real.
- **Promoting v0.94.414/.415 to NUC1** — a deploy, therefore theirs.

---

## 7. Open threads, with the honest state of each

### #949 — why `epr_reliable` is still False

SoC discrepancy max 20.71 / mean 9.84 on the current scored day. **Unexplained.** The capacity
attribution was retracted (#1172) — configured capacity is right, confirmed four independent
ways. What is left is genuinely open.

**Free experiment queued:** today's 21 Sep score will be computed *natively* on 0.94.413
rather than rescored. If it still trips the SoC check, the cause is the reconstruction itself
rather than the scoring path. Costs nothing but waiting for the cycle.

### #1192 — 118 duplicate unique IDs in one setup pass

Real, reproducible on the dev install, **production unaffected** (zero collisions in its whole
13,806-line log). Current supported statement, deliberately narrow: **one `async_setup_entry`
pass emits 118 duplicate unique IDs across the sensor *and* number platforms.**

**Four of my own theories were retracted on this issue** — the mirror as cause, a flex-only
framing, "a restart is needed to get the breakdown", and a re-entrant setup. Read the issue's
own correction history before building on anything in it.

**Cheapest reproduction**: `logger.set_level` to debug, then
`homeassistant.reload_config_entry` — no HA restart, so the mirror is undisturbed.

### remote_homeassistant duplication — investigated, one testable idea left

devhub runs `custom-components/remote_homeassistant` **4.6, which is the latest**. Upstream's
last release and last commit are both **2025-12-04**, nine months ago; 120 open issues; the
symptom is **not reported upstream at all** (searched titles and bodies). A fix arriving is not
something to plan around.

**Reading the source corrected a documented assumption**: the unique_id prefix is *not*
per-session. It is `f"{entry.unique_id[:16]}_{entity_id}"` where `entry.unique_id` is the
**remote instance's UUID** from `.storage/core.uuid`. NUC1 currently reports
`3d37242f4b494bfc985e4505a95f70d3`.

**Therefore the testable hypothesis:** if NUC1 and NUC2 hold *different* `core.uuid` values,
then **every failover hands devhub a new UUID → a new config entry → a new prefix → a full
re-registration**. That would explain why deletion does not hold and why filters did not help.
Roles flipped on 2026-09-08. **Needs a read of `.storage/core.uuid` on both nodes — a
household command.**

Remaining routes if that is confirmed: patch the component locally to pin the prefix (a fork
they then own), or replace the mirror with ~12 REST sensors (devhub's Nimbus references 33
entities, but most are its own outputs; the genuine external inputs are about a dozen).

---

## 8. Things it would be a mistake to do

- **Do not re-raise live, dispatching Nimbus as an incident.** It is intended.
- **Do not change capacity or SoH.** 122.2 kWh and 98% are confirmed correct — four
  independent measurements agreeing to ±0.1 kWh, and the circularity objection was tested and
  answered. Do not re-derive this.
- **Do not quote a "5.3% power-sensor under-read".** Retracted in v0.94.413; the BMS counter
  re-estimates in discrete steps, so size *and direction* are unestablished.
- **Do not quote "110 kWh measured capacity".** Retracted in v0.94.412 — a power sensor cannot
  measure capacity.
- **Do not delete devhub's duplicate entities.** Tried, does not hold.
- **Do not touch the `remote_homeassistant` connection** without specific sign-off.
- **Do not assume a raw ERROR count on devhub is a regression signal** — it is dominated by an
  artifact that regenerates.
- **Do not ship a scorer change and call it done.** It is not delivered until production has
  it and the days are rescored.

---

## 9. Suggested first actions on return

1. **Check the 21 Sep score** — it is the queued free experiment for #949, and it will have
   run by then.
2. **Ask whether to promote v0.94.414/.415 to NUC1.** Their call, and it needs the rescore
   afterwards.
3. **Read `.storage/core.uuid` on both NUCs** (household command) — settles the devhub
   duplication hypothesis in section 7 one way or the other.
4. **Weekly apt check-in is overdue** — last real one was 2026-09-18 and was deliberately
   deferred because of this trip. `apt list --upgradable` on both boxes, NUC2 first.
5. Only then, new work: #1192's cause, or the oldest open issues.

---

## 10. A note on this session's reliability

Recorded because it is the most useful thing a successor can know about the work it is
inheriting.

**Many confident claims made today did not survive checking.** Retracted within the session:
the capacity measurement, the "5.3% sensor under-read", a gap-coverage hypothesis, an
integrator that was itself the artifact, the devhub mirror as cause of #1192, a flex-only
framing, a claim that a restart was needed, and a re-entrant-setup mechanism.

Every one was caught the same way: **checking the claim against a genuinely independent
instrument or a second measurement.** Every one was caught before it reached the household as
advice, and the corrections are on the issues rather than only in chat.

The single claim that survived every attack — four independent measurements plus a deliberate
circularity test — is that **the configured battery capacity is correct**. That is the pattern
to copy: negative findings and controls held up; confident positive assertions from one view
of one instrument did not.
