# Process audit — what was missed, 26–28 Sep 2026

Audited 2026-09-28 17:00 AEST against `main` at `406f013` (v0.94.427), which was
green. Built from full issue and PR comment bodies rather than listings, and
cross-checked against the code and the live CI state rather than against the
threads' own summaries.

This is a companion to `2026-09-28.md`, not a replacement: that file records what
was done, this one records what was claimed and was not true, or was never
reached at all.

## Provenance — read this before quoting anything below

Claims here fall into two kinds, and they do not carry the same weight:

- **Verified from the repository**, by the author of this file: commit
  timestamps, `merge-base` ancestry, file and line locations, CI run results,
  what is and is not in a given tag. These can be re-checked from a clone.
- **Reported by someone with live access**, and repeated here: anything about
  what a production, 116KAT or devhub install was doing at a point in time.
  The author of this file has no route to any of those machines and measured
  none of it.

Every statement about a running install in this document is the second kind.
Section 6 records what happened when that distinction was let slip.

## The pattern, stated once

Work was repeatedly declared done at **"merged to `main`"**, when the standard
that matters is **"deployed to an install and measured."** Behind most of it sits
a second habit: making claims about the running system from repository state
instead of reading the install.

## 1. The release/deploy gap

| event | AEST |
|---|---|
| v0.94.426 tagged | 27 Sep 10:40 |
| production deployed (`git pull` + restart) | 27 Sep **16:10** |
| #1361 merged — solve-failure diagnostics | 27 Sep **20:57** |
| #1363 merged — controllable-load reconstruction | 27 Sep **23:20** |

Both landed after the deploy. `git merge-base --is-ancestor` is false for both
against the tag, and `solver_inputs/controllable_load_history.py` does not exist
in it at all. **Three separate issues — #1360, #768, #1357 — were each blocked on
the same single unperformed action.**

Forty PRs sat between v0.94.426 and v0.94.427, eleven of them touching shipped
code, on no install. `CLAUDE.md`'s standing "cut a release after merging" rule
went unfollowed for two days. v0.94.427 was cut 05:10 on 28 Sep and, at the time
of this audit, is still undeployed — so #1360's healthy-cycle base-rate check,
described in its own thread as the check most likely to kill the leading
hypothesis, remains impossible.

## 2. Reported fixed, not true on the running install

| issue | what was claimed | what is true |
|---|---|---|
| #1360 | "What the next occurrence will now record" | Describes `main`, not the install. Production's `sensor.nimbus_solver_solve_seconds` carries 15 attributes and none of the four new diagnostic keys. The next occurrence records what 20 Sep recorded: a warning in a log discarded at the next container restart. |
| #768 | Option 1 code-complete | "Nothing is deployed." Never run against real recorder data. |
| #1299 | Two functions moved out of `solver_writer.py` | Still at `solver_writer.py:6520` and `:6689`. |
| #1386 | Thermal relaxation is "not published, not logged, not notified" | The WARNING log line existed all along. A grep for *consumers* returned only tests, and that was written up as absence of logging. |
| #1385 | The `native_thermal_relaxed` scenario reaches the infeasibility-retry path | It never relaxed. The target was derived from an assumed 1-hour window; the real one is 1.5 h because the grid coarsens, putting it on the feasible side — the exact knife edge its own docstring warns about. Caught an hour later by #1389. |
| #1318 | `j_star_path_delta` is "the leading candidate" for the 00:00 regret hour | It is a whole-day scalar (`quality_report.py:999`): $1.16 total against $2.40 in that one hour. At least $1.24 of that hour still has no explanation. |

## 3. Concluding from a source that could not support it

- **#1396** — devhub's solver was reported stuck, from `error_log` alone, which
  is WARNING-and-above. One DEBUG window inverted the conclusion: five real
  solves in three minutes, 23–38 s each, all healthy. The skip messages are
  mostly DEBUG and only repeats escalate, so a healthy solver with benign
  cadence-versus-duration overlap read as a hung one.
- **#1355** — a four-day recorder outage was nearly reported on a live install.
  HA's history API silently truncates a wide window at roughly five thousand rows
  and returns the *earliest* ones, with no indication that it did. Only a
  high-cardinality control entity caught it.
- **Specs 003 and 004** — 858 lines of reviewed work were deleted from `main` by
  `30116ff`, an unrelated golden-snapshot commit, in what reads as a `git add -A`
  sweep picking up an unrelated working-tree deletion. Not noticed by the author;
  found externally; restored in #1393 / #1394.

## 4. CI discipline

Worklog #1392 records four CI failures in a single session. #1390 alone needed
three follow-up commits for things CI caught — a text-count gate, a function-set
drift gate, and `sensor.py`'s documented `solve_diagnostics` key count. #1391's
own commit message states the lesson plainly: *"That sweep is what I should have
run before the first push."*

The narrower miss inside #1390 is the instructive one: the golden master, the
guardrail and the existing `solve_diagnostics` contract test were all run, and
the one test whose entire job is to notice a new key was not.

## 5. Never checked, never built, or not checkable where claimed

- **#1373** — the `00:00:30` log read that would explain the quality report going
  `unavailable` was never performed (HA connectivity lost mid-investigation). It
  remains the next step and depends on no deploy or release.
- **#1355 step 2** — a durable dated-JSON capture of the daily score and
  `soc_discrepancy_hourly`, modelled on #1289's forecast-snapshot layer. Named as
  the cause-independent follow-up; unbuilt.
- **#496 criterion 1** — 24 h of long-term statistics was never measured
  continuously (~14 h only).
- **#496** — the dispatch-report skill's headroom panel was never attempted.
- **#1357** — a switch that moves published EPR shipped with its direction
  unmeasured. 116KAT has zero Controllable Loads, so it is structurally inert
  there, and no comparison was run anywhere before merge. No install validated
  it.
- **#1303–#1306** — Phases 3 to 6 have merged, reviewed specs and zero
  implementation. Phase 6's spec self-marks provisional because half its measured
  blockers belong to Phase 3, whose code has not moved. Phase 7 is unspec'd.
- **#1318** — nobody confirmed that v0.94.426's lock-file fix is deployed on
  production. That is the defect which aborts HA's nightly backup, at roughly
  1,440 collision windows a night, silently.

## 6. A correction to this audit, recorded rather than quietly edited out

The first draft of this file carried a section asserting that devhub had been
unusable as a test bed for weeks, reasoning from #1396's entity-collision
findings back to a claim about the machine's health over a period.

**That was wrong, and the household corrected it: devhub is fine, and had at most
a ~10-minute outage on port 8123.** #1396's own measurements are what they are,
but the conclusion drawn *from* them here — a multi-week window of
unusability — was never measured and was not the issue's claim either.

Recorded in place rather than deleted, because the error is the same one this
whole audit is about: **a statement about a live install, made from issue text
instead of from the install.** An audit of that failure that commits it on its
first page is worth keeping visible.

The practical consequence for section 5: "no install validated #1357" stands on
its own evidence — no EPR-with versus EPR-without comparison was run anywhere
before merge — and does not depend on any claim about devhub's availability.

## The action that clears the most

**Deploy v0.94.427 to production** — outside 17:00–24:00 AEST, not mid-charge
window, on whichever NUC holds the VIP:

```bash
cd /opt/homeassistant/config/nimbus_repo
git pull origin main
docker restart opt_homeassistant_1
```

This unblocks #1360's base-rate check, #768's and #1357's validation, and
confirms the #1318 backup fix, in one restart.
