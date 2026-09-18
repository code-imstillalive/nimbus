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
| **devhub** (dev/test) | **v0.94.391** | installed == available, new code confirmed loaded |
| **repo** | tag **v0.94.391** | zero open PRs, nothing half-landed |

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

**Working hypothesis, explicitly NOT verified:** the tie row itself. A
near-equality bound roughly 1e-7 wide across a ~12k-term float sum is
numerically ill-posed, so which optimum HiGHS "proves" depends on the search
path. If that holds, #773's `Infeasible`, its time-limit overruns, and this
wrong-but-confident optimum are one phenomenon rather than three — the row
sometimes cuts off everything, sometimes prunes the true optimum, sometimes
explodes the tree.

**Do not post that hypothesis as a finding without more work.** This issue has a
long, documented history of tidy explanations that did not survive measurement,
including several from this project's own sessions. What is postable today is
only the narrow part: the warm start changes phase 2's answer, so it should not
ship.

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
