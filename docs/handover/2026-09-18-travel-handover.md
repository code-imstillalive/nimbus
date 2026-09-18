# Travel handover — 2026-09-18

The household left on an overseas trip departing **2026-09-18, around midnight
AEST**, away for an unspecified period with intermittent connectivity. Their
laptop travels with them and is powered off for flights, so **no session,
monitoring loop or scheduled task runs while they are away.** Work happens only
inside a session they open deliberately.

This file is written to be read cold, by a session with no context, possibly
weeks later. It records the state at departure, the findings that were still
open, and the things it would be a mistake to re-derive.

Read alongside `docs/handover/2026-09-17-epr-and-scoring.md`, which remains the
authoritative account of the EPR/scoring thread. Where the two disagree, this
file is newer.

---

## 1. State at departure

| | version | state |
|---|---|---|
| **NUC1** (production, holds the VIP) | **v0.94.391** | `status: optimal`, 1.17 s solves, 202 periods, dispatching |
| **NUC2** (standby) | **v0.94.391** (`ce8efb0`) | all 11 containers present and stopped — the intended standby posture |
| **devhub** (dev/test) | **v0.94.392** | installed == available, new code confirmed loaded |
| **repo** | tag **v0.94.392** | zero open PRs, nothing half-landed |

**NUC1 and NUC2 are deliberately one release behind the repo.** v0.94.392 shipped after
the household's upgrade, and they deploy by hand — so nothing moves on production while
they are away. Do not treat the gap as drift.

NUC1 was upgraded by the household from v0.94.375 (`4812f93..ce8efb0`) and
verified afterwards: `nimbus_status: Working well`, `lp_status: optimal`,
`solve_diagnostics` carrying all 10 current-code keys, SoH 98% / 119.756 kWh
effective capacity.

**Failover was verified, not assumed.** NUC2's `nimbus_repo` was confirmed at the
same commit within ~20 minutes of NUC1's pull, which closes the 2026-08-28
failure mode (a node that became active while 8 days / 57 releases behind).
42/42 monitored jobs healthy on both nodes. NUC2's two ~17-day job ages
(`nas_backup_sync`, `sync_runtime_data`) are the VIP-gated jobs no-opping on the
standby, which is correct rather than stale.

**EPR is fixed.** 17 Sep rescored automatically on restart:

```
EPR                       94.81 %      (was 49.5 % under v0.94.375)
j_ref                      6.2265
j_ach                    -24.2835
j_star                   -24.1027      (LP objective, still published beside it)
j_star_evaluator         -25.9551      (EPR and regret now priced through this, #1081)
regret_dollars             1.6716
epr_denominator_reason     null        (#1089's guard, healthy denominator)
real_p2p_settlement_status applied
```

The only flag still set on that day is `epr_reliable: false`, driven entirely by
`soc_discrepancy_reason: "disagreement"` (19.49 pt max / 10.54 pt mean against
thresholds 15 / 8). That is **#949**'s known fleet-blend mechanism — the *home*
battery's own sensor compared against a *fleet-blended* reconstruction — not an
EPR defect. #949 is a decision, not work.

---

## 2. The finding most likely to save a future session time

**On devhub, the canonical `sensor.nimbus_*` entity ids carry NUC1's data, not
devhub's.** This was settled by a controlled experiment rather than inference:
the household upgraded NUC1 and restarted it while devhub was not touched at all,
and within about a minute every canonical reading on devhub flipped together:

| devhub entity | before | after |
|---|---|---|
| `sensor.nimbus_solver_battery_forecast` | 0.94.375 | **0.94.391** |
| `sensor.nimbus_solver_quality_report` | 0.94.375 | **0.94.391** |
| `sensor.nimbus_offer_curve` | 0.94.375 | **0.94.391** |
| `sensor.nimbus_household_load_total_forecast` | 0.94.375 | **0.94.391** |

Intervention on one side only. **This is the whole "devhub deploy not taking
effect" mystery** — sixteen recorded occurrences, every one of them reading a
version string off a canonical `sensor.nimbus_*` id and finding it behind what
HACS reported. Those readings were measuring NUC1's release lag. Devhub was
usually current.

**Do not re-derive this from `ha_get_entity(<id>).platform`.** That call returns
`platform: nimbus_load` with devhub's own config-entry id for these entities, and
on 2026-09-17 a session used exactly that to conclude the opposite. Registry
ownership is not authorship: the registry records which integration *claimed* the
entity_id, while anything that can reach `hass.states.async_set` or
`POST /api/states/` can write the state.

**Checks that actually establish devhub's own code freshness**, strongest first:

1. **Does an entity that only the new release creates exist?** A foreign writer
   can post to an entity_id but cannot register one. This is the only marker that
   cannot be forged. (`sensor.nimbus_quality_epr_denominator_reason`, new in
   v0.94.384, is the current example.)
2. **Read the `_2`/`_3`-suffixed sibling.** `sensor.nimbus_solver_quality_report_2`
   reported 0.94.391 correctly throughout, while the canonical one said 0.94.375.
3. **A `custom_components.*` line in devhub's own log** is in-process by
   definition.

Two long-standing side mysteries fall out of the same mechanism and need no
separate explanation: the duplicate-unique-ID refusals at boot (devhub's own
entities lose the canonical ids to the foreign states, surviving as `_2`/`_3`
where HA renames them) and the recorder's oversize-attribute warnings firing
every ~15 s on exactly those ids (a foreign write carries no `state_info`, so
`_unrecorded_attributes` never applies and the whole row is dropped — the #944
mechanism).

**What is not established:** whether the foreign writer is devhub's
`remote_homeassistant` mirror or NUC1's own REST writer. The observation is
identical either way and the rule above does not depend on it. Do not assert a
mechanism without evidence.

A related non-finding worth not re-chasing: when devhub logs
`Platform nimbus_load does not generate unique IDs`, grep the same boot window
for that string **without** filtering on nimbus. On 2026-09-18 the stock `cast`
integration logged it for 17 media players in the same boot, which makes it
environmental rather than a nimbus defect. The source was checked first —
`sensor.async_setup_entry` builds and adds its batch once, and a scripted
cross-check of all seven `FLATTENED_ATTRS*` tables found zero duplicate
`(entity_id_prefix, entity_id_suffix)` pairs.

---

## 3. #773 — an open finding that is NOT yet verified

Work in progress at departure, recorded so it is not lost or over-trusted.

**The warm-start proposal on #999 should not ship.** The idea was to hand phase
1's solution to phase 2 as a MIP start, to attack the 20x cost asymmetry. Two
prior attempts to measure it had no statistical power because every synthetic
phase 2 resolved at the root with nothing to prune.

That premise turned out to be wrong: **synthetic phase 2 does branch**, and the
branching scenarios are at *smaller* period counts with more loads, which is the
opposite of where everyone had been looking. Using this repo's own scenario
builder from `tests/test_773_tolerance_and_tie_slack_end_to_end.py`, six
(n_loads, n_periods, seed) points branch, up to **372 nodes / 144k iterations**.
Two further points reproduce the production failure end to end
(`phase2_secondary` → `Time limit reached`).

With real power, the warm start fails its own correctness bar:

| scenario | cold | warm |
|---|---|---|
| 24x24 seed 0 | 372 nodes, 128k iters, 13.0 s | 107 nodes, 52k iters, 4.2 s |
| 32x48 seed 0 | 113 nodes, 107k iters, 19.8 s | **1449 nodes, 240k iters, 37.1 s** |
| 16x24 seed 2 | secondary **0.1603691026** | secondary **0.1784454005** |
| 24x24 seed 1 | secondary **1.1885267800** | secondary **1.1664605846** |

Fully deterministic — three repeats each, byte-identical.

**The last two rows are the serious part and they are not about the warm start.**
Forcing `mip_rel_gap` to 0 to rule out gap termination does not reconcile them:
both runs then report `gap = 0.0` with `mip_dual_bound` equal to the primal —
i.e. both claim a *proof* — while disagreeing by up to 11 % on the answer. Same
model, same phase 1, same tie row, same `primary_value`. They cannot both be
right, and it is the cold run that is wrong in one scenario and the warm run in
the other.

### Adjudicated after the above was first written — one half is now proven

The disagreement was settled independently of HiGHS's own status reporting: dump
the phase-2 model to `.mps` at the moment phase 2 is about to run (tie row and
secondary cost vector both already in place), reload it **fresh**, pin the
binaries to each assignment, and solve the resulting pure LP.

```
24x24 seed 1   cold's assignment -> Optimal   secondary 1.1885267807
               warm's assignment -> Optimal   secondary 1.1664605846
```

Both feasible for the same tie row.

**CORRECTED later the same day — the first framing was too strong, and the
correction is the part to read.** "The production path returns a provably
sub-optimal answer with an invalid dual bound" is NOT supported. Two follow-up
experiments:

1. Reload the dumped phase-2 model in a **fresh** Highs instance with
   `mip_rel_gap = mip_abs_gap = 0` and solve from scratch, no warm start
   anywhere: **five of six scenarios agree with the in-process run**, including
   the headline one. An independent method reaches the same answer.
2. Measure where the better point sits against the tie row's real bound:
   the in-process assignment has **1.0e-07** of headroom, the better one
   **1.4e-14** — it lies *exactly on the boundary*, at machine precision.

**The supportable claim:** phase 2's true optimum can lie exactly on the
tie-row boundary, where branch-and-bound cannot reach it — the search works
inward from the relaxation and prunes on bounds computed to ~1e-7, so a point
on a constraint the algorithm derived itself is below its own resolution. The
reported answer is optimal to the resolution actually used. The gap that opens
is 1.89 % of the secondary objective.

That **strengthens** the tie-row thread rather than weakening it:
`_LEX_PRIMARY_TIE_ABS_SLACK = 1e-7` defines a band whose optimum is at its own
edge. Posted on #773 and #999, with the harness docstring corrected to match.

**One scenario points the other way and is not explained away.** 16x24 s2's
in-process run reports a value better than both the fresh re-solve and the warm
run, and its assignment returns `Solve error` when pinned at a 1e-9 tolerance —
there the production answer is the one that fails scrutiny. One case in six is
not a distribution; it is recorded because it is the half most easily dropped.

One measurement points somewhere without diagnosing anything: the two
assignments' tie-row activities differ by **exactly 1.000e-07**, the tie slack's
own width, with the better solution sitting at the very top of the band. On this
install `mip_feasibility_tolerance` is `1e-06` — ten times wider than the band —
and `primal_feasibility_tolerance` is `1e-07`, the same order as the band itself.
Same tolerance-mismatch family as the one #979 fixed at `phase2_pin_resolve`.

**The obvious remedy is confounded and must not be shipped on this evidence.**
Widening `_LEX_PRIMARY_TIE_ABS_SLACK` does lower the secondary objective at every
step (1e-7 -> 1e-6 -> 1e-5), but that is guaranteed regardless of mechanism,
since a wider band is a strictly larger feasible set — and by 1e-5 it is giving
away real primary cost, which is exactly what the tie row exists to prevent. The
test cannot separate "stopped mis-pruning" from "was handed more freedom".

**Still NOT claimed:** the mechanism, and that any of this reproduces on
production-shaped instances. This issue has a long documented history of tidy
explanations that did not survive measurement, several of them from this
project's own sessions. Do not name a cause without a test that discriminates.

Impact worth keeping in proportion: the secondary objective is the **tie-break**
term, and primary cost stays pinned by the tie row to within 1e-7 dollars either
way. This does not move money — it changes which of several near-equal-cost
dispatches is chosen. The reason to care is that an unreliable optimality proof
is the same raw material as the spurious `Infeasible` this issue is named after.

The reproduction scripts were left in the session scratchpad, not committed. They
are cheap to rebuild: the scenario builder is already in
`tests/test_773_tolerance_and_tie_slack_end_to_end.py`, and the instrumentation
is a monkeypatch of `lp._ensure_optimal_value` that records `h.getInfo()` per
phase and optionally calls `h.setSolution(n, index, value)` before
`phase2_secondary`.

One practical note: `/tmp/nimbus_773_slow_lex_phase_phase2_secondary.mps` is
**not** meaningfully perishable. It was rewritten eight minutes after a restart
that was expected to destroy it, because a slow `phase2_secondary` recurs within
minutes. Do not treat preserving it as a reason to defer a restart.

---

## 3b. Shipped after the handover was first written (v0.94.392)

Two issues landed late on departure day, both verified live on devhub.

**#1120 — each `history` row now carries the release that scored it.** A day is scored
once and frozen, so a scoring-formula change splits the table into two incomparable halves.
Measured live: 94.8% and 106.4% on adjacent cards from the same sensor, the second carrying
the `oracle_beaten` signature #1081 had already fixed. Devhub confirmed both load-bearing
properties — the rewritten row carries `v: 0.94.392`, and the two prior rows correctly stay
unstamped rather than being back-dated.

**#1120 is NOT finished — two parts remain, and the priority order was corrected by
measurement.** The `history` table holds 2–3 rows, not a 60-day mixed run, and the 30-day
trend card does not read it at all: it reads recorder **long-term statistics**, where the
bucket for day D holds the score for day **D-1** and today's bucket blends across any
mid-day change. So "one source of truth for the trend" is now the second priority, not the
third, because it is the one that fixes what the household actually looks at. A persisting
rescore path is the third. Note the #944 constraint: on a cron install the `history`
attribute is not in the database, because the payload measured **20,738 bytes — 27% over
the 16 KB cap** and the whole attribute row is dropped.

**#496 — the flex family and the offer curve reach the diagnostics dump.** Its remaining
criterion (the emitted telemetry record) stays blocked on #495's emitter, which does not
exist: the vendored `schema/telemetry.schema.json` is referenced only by its own drift test.

**A process failure worth carrying forward.** Both PRs auto-closed their issues on merge,
because the bodies read *"Closes #1120's first of three parts"* and *"Closes #496's
unblocked criterion"* — GitHub's scanner stops at the number and discards the qualifier.
Both were reopened. This is the **fifth** recurrence of that trap and the first two of the
*qualifier* shape rather than the negation shape. Put the scope first: *"Part 1 of 3 for
#1120"*.

---

## 4. Decisions owed — nobody should start these unasked

**Household** (and the household is away, so these simply wait):

- **#485** — the SoC floor while `away`. Deliberately absent from
  `household_modes.py`'s preset table, because how much reserve a house keeps
  while empty is a safety-and-money judgement, and a table default would be
  exactly the invented number the no-hardcoding rule exists to prevent.
- **#949** — three options for the fleet-blend SoC comparison.
- **#944** — the structural half (moving the big arrays to a companion entity is
  an entity-contract change, since dashboard cards read `attributes.forecast`).
- **#987** — go-ahead for the home-assistant/brands PR.
- **#1067**.

**Mark Purcell:**

- **#768** — the `target_kwh` convention, which gates the rest of that issue.
- **#1086** — more closed-loop days from his own `home` pack.

**#485's hours question is answered and needs no decision.** `household_modes.py`
already states that deadline/earliest hours are deliberately untouched, and the
presets express "no HWS deadline while away" as `deferrable_shortfall_price`
x 0.25 and "earlier deadline for guests" as x 1.5 — urgency rather than clock
maths. A structural reason to keep it that way was found on 2026-09-18: the sign
of `deadline_hour - earliest_hour` selects between two different scheduling
paths (#612's multi-day `AdequacyWindow` list versus the pre-#612 single-window
path), so a mode that flipped that sign would silently change which machinery
schedules a load. If hours are ever moded, the invariant to guard is that **a
preset may move the hours but must not change the sign of that difference.**

---

## 5. Remote access while the household is away

Everything is behind Cloudflare Zero Trust and works from any network:

- `homeassistant.116kathouse.xyz` — full HA UI.
- `vsc.116kathouse.xyz` — the openvscode browser terminal, which mounts `/opt` as
  `/home/workspace` and is where every NUC command is normally run.
- Portainer for container-level restarts.

cloudflared runs on **both** NUCs against the **same** tunnel, so remote access
follows a failover automatically — confirmed during a real incident.

**The gap:** a fully powered-off NUC cannot be woken remotely. No Wake-on-LAN is
configured, and a magic packet would need a sender inside the LAN. One node
failing is covered by keepalived; both failing is not recoverable remotely.

Alerting reaches the household only via ntfy topic `116kat-nuc-watch-8f3a2c`.

---

## 6. Things it would be a mistake to do

- **Never run any command on NUC1 or NUC2**, including read-only. Verify NUC1
  through devhub's mirrored entities instead, which needs no NUC access at all —
  that is how every reading in section 1 was taken.
- **Never merge anything in `116KAT-HA-AI`.** Branch, commit, push, open a PR;
  the merge is the household's.
- **Do not apply the pending OS updates.** ~105 packages on NUC1 and ~85 on NUC2
  were checked on 2026-09-18 and deliberately deferred: kernel 6.8.0-138 -> 139,
  libc6, openssh, python3.12, snapd, polkitd, plus `fwupd` 1.9.33 -> 2.0.20,
  `cloud-init` 25.2 -> 26.1, `nftables` and `apparmor` on NUC1. That is precisely
  the restart-cascade class that caused the 2026-09-01 failover. Apply on return,
  NUC2 first, while present and clear of the 17:00-24:00 P2P window.
- **Do not "fix" devhub's duplicate entities** with an entity prefix, a filter, or
  by removing the mirror. The household has rejected that twice.
- **Do not retry a HACS remove+reinstall** to clear a devhub staleness reading.
  0 for 16.

`opt_amber2mqtt_1` is absent from NUC1 by intent — the household dropped it. NUC2
still carries a stopped copy from before that decision, which is harmless. A
Sankey dashboard element was also removed as unused. Neither is a fault to
investigate.
