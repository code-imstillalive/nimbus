# Release process

Written in direct response to nimbus repo issue #217 (Mark Purcell, "14-day
project health assessment", item 4: "soak window before same-day fix-releases").
That thread's own comment already stated the decision ("Landing on: hold
overnight specifically for LP-behavior-changing fixes... Not a full RC
process") but never wrote it down as a real, standing policy — this document
is that write-up, not a new decision.

## The policy

**Most releases ship immediately on merge**, same-day, no soak window. This
project's median PR-merge time (2.7 min) and issue-close time (6.5h) are real,
deliberate strengths — a formal release-candidate channel would trade that
away for every change, including the ones that don't need it (docs, test-only
changes, a narrowly-scoped diagnostic addition, a config-flow field).

**One category holds overnight before release: a fix that changes LP
dispatch/pricing behaviour** — anything touching `solver/network.py`'s own
constraint/objective construction, `solver_writer.py`'s price/load/solar
resolution logic, or the resulting `battery_kw`/`grid_import_kw`/
`grid_export_kw`/`soc_pct` values a real household's automation could act on.
The reasoning: this is the one category of bug where a same-day fix-of-a-fix
(`v0.94.1 → v0.94.2 → v0.94.3` in one day, per #217's own numbers) has real
consequences beyond wasted release churn — a household running the fixed
version overnight is trusting it with real battery dispatch decisions before
anyone (including the maintainer) has watched it run for real.

**What "hold overnight" means in practice**: merge the fix, but don't tag/
release it the same day it merges. Watch it against the reference
household's own real production system (or devhub) through at least one full
day/night cycle — including a real P2P window if the fix touches anything
P2P-adjacent — before cutting the tag. If nothing looks wrong, release the
next day. If something does, fix it first and restart the clock.

## Why not a full RC channel

A formal release-candidate process (a separate pre-release channel, a fixed
soak duration applied to every change regardless of category, sign-off
gates) was considered and explicitly rejected. It would apply uniformly to
changes that don't need it — the 45%/23%/15% fix/release/feature split
#217 measured means most releases genuinely are low-risk, and gating all of
them on a fixed calendar window would slow down exactly the responsiveness
(median 2.7 min PR merge, 6.5h issue close) #217 itself called out as a real
strength worth keeping. The narrower "hold LP-behaviour fixes overnight,
release everything else immediately" rule gets the safety benefit where it
actually matters without that tradeoff.

## What this does not cover

This is a release-timing policy, not a testing policy — it doesn't replace
or substitute for `tests/regression/`'s own golden-fixture invariants (see
`docs/api-contract.md`), which run regardless of category and catch a
different class of problem (a regression against an already-verified
invariant, not "has this specific LP-behaviour change actually been watched
running for real yet").
