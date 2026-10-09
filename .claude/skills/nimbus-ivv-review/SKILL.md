---
name: nimbus-ivv-review
description: Run an independent verification & validation (IV&V) pass over a range of commits on the Nimbus repo (code-imstillalive/nimbus) - scope a commit range, dispatch parallel review agents with exact file/line citations, personally re-verify the consequential findings, file a head GitHub issue plus one sub-issue per confirmed finding, and pin each confirmed gap as a real pytest test in a PR. Use this whenever someone asks for an "IV&V pass", "audit the last N hours/commits", "review today's releases", "sanity check what shipped", "codebase review" in the spirit of a past one (e.g. #336, #364, #594, #950), or wants a second, adversarial pass over recently merged work rather than a first-pass code review of an open PR. Also use it as the reference for how findings from any other live-verification or IV&V-shaped check (a dispatch report, a devhub deploy check, a live-data cross-check) should be filed and pinned, even if the word "IV&V" is never said.
---

# Nimbus IV&V review

An IV&V pass is a second, adversarial look at code that already merged — not a pre-merge
code review. Two have run so far on this repo (a 48-hour pass, #950; a same-day pass, #996),
and both converged on the same shape, described here. The goal each time is the same: find
real, concrete defects a careful human reviewer would find, say plainly when nothing is
there, and leave the finding in a form someone else can act on without re-deriving it —
a filed issue with exact citations, and (as of the same-day pass) a real test that fails
until it's fixed.

Treat every step below as something to adapt, not a checklist to satisfy. The point is
genuine findings, not a review that merely looks thorough.

## 1. Scope the range

Pick a commit range with a real boundary — "the last 48 hours", "today's releases", "since
the last release tag", whatever the person asked for. Get it in Brisbane local time (this
household's timezone, no DST) since that's what every date boundary in this repo's own
worklog and CLAUDE.md is written in:

```bash
git fetch origin main -q
TZ=Australia/Brisbane git log origin/main --pretty=format:'%h %ad %s' \
  --date=format-local:'%Y-%m-%d %H:%M:%S' \
  --since="<local midnight> +1000" --until="<local now> +1000"
```

Check a checkout out at that range's tip on its own branch (`ivv/<range>-<date>`,
tracking `origin/main`) — read-only until step 6. Count the commits and skim the one-line
messages before deciding how to split the review work; a quiet range might not need
parallel agents at all.

## 2. Group commits and dispatch parallel review agents

Group the commits by area, not by chronology — the #773 solver-tolerance fixes belong
together even if a docs commit landed between them. Somewhere between 3 and 6 groups is
usually right; more than that and the per-agent context gets too thin to catch anything
subtle.

For each group, launch a background `Agent` (general-purpose) whose prompt:

- Names the exact commit hashes and file/line ranges to read — `git show <hash>`, not
  "review the recent solver changes". A vague prompt gets a vague, unverifiable report.
- States the real mechanism/history behind the area if you know it (e.g. "#773 has a long
  history of 'confirmed fixed' claims that didn't hold up — check whether this fix has a
  real end-to-end reproduction, not just an isolated unit test"). This is what turns a
  surface-level pass into one that catches the actual failure mode.
- Explicitly asks the agent to run any directly relevant tests, not just read code.
- Explicitly asks for a plain "nothing found" on anything genuinely clean, and forbids
  manufacturing a finding to have something to report. This instruction matters — without
  it, agents tend to pad reports with cosmetic nitpicks that dilute the real findings.
- Asks for a one-line verdict per commit (CLEAN / CONCERN, with why) and a word budget
  (400-700 words is usually enough) so the report stays skimmable.

Send all the groups' `Agent` calls in one message so they run concurrently. While they run,
do the mechanical parts yourself: pull the **actual GitHub Actions conclusion** for the
range's tip commit (every check run, not just whether the PR merged) and skim any commit
whose message alone raises a question (a stale comment claim, a suspiciously round number,
a "should work" instead of "confirmed"). **Never run the test suite or `ruff` locally for
this** — see the standing rule after step 4.

## 3. Personally re-verify before trusting a report

An agent's report is a claim, not a fact — read `git show <hash>` yourself for anything
you're about to file, especially the ones that sound most consequential. This has caught
real problems before: a "test coverage gap" claim that turned out to have a mitigating
factor the agent's framing missed (device-registry `sw_version` partially covering a
missing state attribute), and a "misleading commit title" claim that turned out to be
accurate once the actual CHANGELOG entry (not just the git title) was read in full. Don't
soften a real finding to be polite, but don't file an agent's framing verbatim either —
verify it holds up, and correct the framing if it's overstated or understated.

## 4. Never run the suite or the linter locally — read GitHub's own Actions results instead

**Standing rule, household instruction (2026-10-09): the test suite and `ruff` run as a
GitHub Action on GitHub's own server, never in this sandbox.** This isn't a preference —
running them locally has already cost real time and produced real false alarms on this
exact repo:

- **A tool-version mismatch between the sandbox and CI.** One session's local `ruff`
  resolved to `0.15.8` from a stray second install, even though `pip show ruff` reported
  the correct pinned `0.16.4` — `which ruff`/`ruff --version` told a different story than
  `pip show`. That single mismatch produced 300+ spurious `E402` findings around this
  repo's own `sys.path.insert()`-then-import idiom in `tests/`, none of them real.
- **A background pytest run killed mid-flight** when the session was suspended and resumed,
  silently losing the whole baseline with no error surfaced until checked.
- **An environment gap already documented in CLAUDE.md.** This sandbox's Python tops out
  at 3.13; this repo's real `requires-python` is `>=3.14.4`, and `tests/hass_integration/`
  needs the real HA harness, which cannot build here at all.

Every one of these is avoided by never running the suite or the linter locally in the first
place. Instead: read the **actual GitHub Actions conclusion** for the commit in question —
whatever GitHub access this session has (GitHub MCP tools, the `gh` CLI, a browser) works;
the point is reading the real workflow run's own per-check conclusions (`completed` and
`success` on every job, per the repo's own NEVER CHEAT directive — `pending`/`in_progress`
is not a pass), not reproducing them in this sandbox. CI already ran on every commit in the
pass's range when its PR was open; there is normally nothing left to run. If a commit's own
CI run genuinely isn't available (a rare gap, not the default case), say so plainly in the
head issue's "ruled out" section rather than falling back to a local run to fill the gap.

If a review agent reports a local test or lint failure anyway (agents may not carry this
same restriction), treat it exactly as untrusted until the real Actions conclusion for that
commit is checked — never file it as a finding on the strength of a local run alone. See
`references/gotchas.md` for the full worked example of the ruff-version trap above.

## 5. Decide what's genuinely worth filing

Not every agent-flagged item earns an issue. Skip filing (but still list under "clean" or
"ruled out" in the head issue, so a reader can see it was actually checked):

- **A limitation the code already discloses in its own docstring or comment.** If the
  function's own docstring already says "an interior gap does not reduce the span —
  accepted" or equivalent, filing that as a "finding" is noise, not news. This repo's own
  prior IV&V pass explicitly validated declining to file two such items for exactly this
  reason.
- **Something already fixed or superseded**, verified by reading the *current* code at
  its *current* line numbers — not by trusting that "a PR merged" closed it. Re-verifying
  the prior pass's own findings on the new HEAD (do they still hold up, word for word,
  against the code as it stands now) is itself worth doing every pass, since it's cheap
  and has caught real drift before.

File everything else. When in doubt between "worth a sub-issue" and "worth a sentence in
the head issue", the test is whether someone who only reads the head issue would learn
something they need from the sub-issue — if yes, it's a sub-issue.

## 6. File the head issue and sub-issues

One head issue: title states the scope and range plainly (`IV&V pass — <range>, <N>
commits`). Body sections, in this order:

1. **Scope** — the exact commit range, branch, commit count.
2. **Method** — how it was reviewed (agent groups, what each covered), and the local
   validation run (suite pass/fail counts, ruff).
3. **Ruled out** — anything investigated and found not to be a real finding, with the
   evidence (see step 4). Skipping this section is how a future reader ends up
   re-investigating the same dead end.
4. **Clean** — commits/areas an agent or you personally verified sound, one line each with
   enough detail that "clean" reads as checked, not assumed.
5. **Findings filed as sub-issues** — a list, deferring detail to each sub-issue.

Then one sub-issue per finding (never bundle — this repo's own standing rule, and it holds
here too: a reader deciding whether to act on one finding shouldn't have to parse three).
Whatever GitHub access is available, create each as its own issue parented to the head
issue — **not** as a comment on the head issue, which is an easy mistake to make under
time pressure and reads very differently to someone skimming the issue list (if you make
it, close the stray issue immediately as created-in-error and don't leave it lying around).

Each sub-issue body:

- States the finding with the exact file:line citation and, where useful, the actual code
  or log output that demonstrates it — not just a description.
- Names the regression test that now pins it (see step 7) and quotes its failure message.
- Proposes a fix shape, but doesn't over-prescribe — the maintainer may reasonably choose
  a different shape, and has before (targeting `lb` instead of `ub` on an asymmetric
  bound; documenting a guard's scope instead of patching around it). Say what you'd try,
  not what must happen.

## 7. Pin each confirmed finding as a real test

This is additive to the issue, not a replacement for it — per the household's own steer,
"define the IV&V report as a unit test" where the finding is code-shaped. Not every finding
is (a stale docstring claim doesn't need a test; a real behavioral gap does).

Three shapes, depending on what the finding actually is:

- **A genuine gap in current behavior** (the code does the wrong thing, or does nothing
  when it should do something): write a test asserting the *correct* behavior. It will
  fail against current code — that's the point. Mark it
  `@pytest.mark.xfail(reason="...", strict=True)` so the suite (and this PR's own CI)
  stays green while the gap is open, and so the test flips to a loud `XPASS` failure the
  moment someone fixes it — which is the signal to delete the marker. Put the issue number
  in the `reason=` string.
- **A genuine, real end-to-end reproduction achievable with real effort**: build it for
  real — construct an actual scenario through the real entry point (`build_plan()`, not a
  mocked stand-in), not a trivially-true assertion. This repo's own solver has produced a
  real one before: sweeping ~150 (n_loads, n_periods, seed) combinations to find a scenario
  that deterministically drives a specific HiGHS failure mode, then asserting it fails
  pre-fix and passes post-fix. This is worth real, patient effort — a fix shipped without a
  reproduction is exactly the gap worth closing.
- **No real reproduction found despite genuine effort, and the original fix's own authors
  never found one either**: say so honestly in the test file's own docstring (the exact
  sweep parameters tried, the negative result), and fall back to real end-to-end **path
  coverage** instead — confirm the mechanism actually executes with real inputs and a real
  computed value, not a hand-picked constant. A null result stated honestly is worth more
  than a fabricated pass.

Don't validate the new test locally (see step 4 — the sandbox is never the source of truth).
Commit it and push; the real validation is the GitHub Actions run on that branch/PR, read
after pushing, not a local `pytest`/`ruff` run before it.

## 8. Branch, commit, push, open the PR

One commit per finding is fine (keeps each one reviewable and revertable on its own), or
squash lightly if the findings are tightly related. Commit messages and the PR body should
read like the issue bodies: factual, cited, no padding. The PR body links every sub-issue
it addresses and explains the `xfail(strict=True)` choice inline if any test uses it — a
reviewer shouldn't have to open a second file to understand why a red test is expected.

Push the final branch state straight away — no local suite or `ruff` run first (step 4's
standing rule). Open the PR against `main`, link it back as a comment on the head issue,
then read the real GitHub Actions conclusion on that PR before treating anything as
validated: every check `completed` and `success`, and the xfail count in the real CI log
matching the number of deliberately-red tests exactly — a `pending` or `in_progress` check
is not a pass, per the repo's own NEVER CHEAT directive.

## 9. Watch it through

Subscribe to the PR's activity if the session has that capability. If someone else pushes
a fix directly to the PR (this has happened — the maintainer closed both findings in one
commit within the hour, including fixing a real test-isolation bug the new tests exposed),
independently re-verify the fix before treating it as done: pull it, re-run the full suite,
re-check CI's actual conclusion, read the diff rather than trusting the commit message
alone. When it merges, close the head issue with a short summary of the final outcome, and
leave any sub-issue that's an honest "no reproduction found, not a to-do" open by design —
say so in the closing comment so nobody mistakes it for neglect.

## What "good" looks like

The two passes that produced this skill both landed on the same real ratio: most reviewed
work turns out clean, and the findings that do surface are small in number but real enough
that the maintainer fixes them within the hour once they're filed with a citation and a
test. If a pass is turning up a long list of vague concerns, that's a sign the review
prompts were too broad, not that the code is unusually bad — tighten the citations and
re-run rather than filing a large batch of low-confidence items.
