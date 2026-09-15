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

**The one carve-out: provably identity on the default path, with a test.**
(Household decision, 2026-09-15, [#594](https://github.com/code-imstillalive/nimbus/issues/594).)
A change that touches dispatch code but cannot alter dispatch for an install
that has not opted into it may ship same-day — **only when a test in the same
PR demonstrates that**. Exercise the default path and show the behaviour is
unchanged; an assertion in the PR description that it is a no-op does not
qualify.

The condition is the point, not a formality. The two releases that prompted
this (v0.94.315 and v0.94.318, household-mode presets) were genuinely
identity on the default path — `home` is absent from the preset tables
entirely, so the mode lookup returns the input object unchanged — and that
absence is itself tested. That test is what earns the carve-out. Without it,
"I am confident this is a no-op" is worth very little: the same day this rule
was written, a fix was twice reported as not working on the strength of a
live reading that turned out to come from ~30-release-old code, and a `0`
that was documented as "disables re-sending" would have meant "re-send every
cycle" until a test caught it.

Worked examples, from the day the rule was adopted:

| change | same-day? | why |
|---|---|---|
| household-mode presets | **yes** | `home` absent from the tables, absence tested — identity proven |
| re-affirming a dispatch a device is ignoring ([#875](https://github.com/code-imstillalive/nimbus/issues/875)) | **no** | a diverging load genuinely behaves differently; that is the feature |
| net-vs-gross thermal rate ([#897](https://github.com/code-imstillalive/nimbus/issues/897)) | **no** | changes the arithmetic behind a real hot-water guarantee |
| tests-only, docs-only | **yes** | not dispatch code at all; the rule never applied |

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
