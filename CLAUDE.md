# CLAUDE.md — Nimbus

Rules for any Claude session working on this repo. Rules only: the history and reasoning behind them is in [docs/handover/2026-10-18-claude-md-archive.md](docs/handover/2026-10-18-claude-md-archive.md) and the dated worklogs. Read those for why, never for current state.

## Start of every session

- Run `python tests/gates/release_drift.py --report` (the SessionStart hook prints it). It gives the newest tag, the manifest version and what is on `main` untagged, at the moment you run it.
- Read the last two files in `docs/worklog/`.

## Daily worklog (household rule)

- **Every day real work happens in this repo, write `docs/worklog/YYYY-MM-DD.md` before the day ends**, and merge it as its own docs PR. Never batch several days.
- Each entry records, with AEST times:
  - PRs merged, and releases cut;
  - what was validated on devhub, and what was not, with the reason;
  - findings, and decisions made by the household or Mark Purcell, in their words where given;
  - what is open at the end of the day, and what it waits on.
- A worklog is history: it states what was true when written. Never cite one as current state without checking live.

## How to work

1. **Follow the written rules; don't invent reasons.** When a rule exists, follow it. When a fact is checkable, check it. A rule in another repo's CLAUDE.md governs that repo only.
2. **Never state anything from memory.** PR, issue and CI state: `gh pr view`, `gh pr checks`, `gh issue view`, at the moment you report it. Live system state: read it now. "Still", "unchanged" and "awaiting" are claims about the present; check them.
3. **Read every issue and PR in full**, the body and every comment and review, before acting on it, citing it or closing it. A title, a label or a `--json` field is an index, not the content. To find a collaborator's activity, query comments, not `author`.
4. **Every check, every time.** Nothing merges, releases or ships until every check is `completed` and `success`. Pending is not a pass. A rebase or force-push restarts CI. Diagnose a failure from its log before calling it external or flaky. Being asked to hurry is not a reason to skip a check.
5. **Fix it fully.** Don't scope a fix, comment and move on. Finish it, or record exactly what remains and why.
6. **On an issue Mark Purcell raises, own the solution:** test the options, implement the best on evidence, and report what was measured. Defer to his technical judgement unless you have a verified reason not to.

## Authority

- **Merging:** Claude merges its own PRs in this repo. Always use `gh pr merge --repo code-imstillalive/nimbus <n> --squash`, the form the permission rule allows. After merging, confirm every referenced issue is in the intended state.
- **Review before merge:**
  - Behaviour and dispatch code needs a purcell-lab review with no open findings.
  - Household presentation PRs (dashboard, tabs, docs) merge on green CI.
  - The household or Mark can waive review for a named PR.
  - Release PRs and devhub-verdict PRs merge on green CI.
- **Deploying to NUC1/NUC2 is always the household's action.** Claude never runs anything on them. Production is read-only, through the HA REST API.
- **devhub** is the test box. Claude may install, restart and configure it, but never writes an entity_id production also has.
- See **Sites** below for how Cottonwood, devhub, NUC1/NUC2 and third-party installs differ, and what Claude may do on each.

## Sites

Nimbus runs on more than one install. Don't assume "production" means the same box every session — check which site a session's `Home_Assistant` MCP connector actually reaches before acting, and name the site explicitly in anything written to the user, a worklog or an issue.

- **Cottonwood** — a production site. Whichever session's `Home_Assistant` MCP connector reaches it is talking to Cottonwood specifically, not necessarily NUC1/NUC2. **Deploying to Cottonwood via HACS (install + restart) is authorised for Claude** (per Mark Purcell, 2026-10-18), unlike NUC1/NUC2 below. Still follow the full **Release validation** checklist for every Cottonwood deploy: only a tagged, released version; restart; confirm the new version on the live sensors; check the log for new WARNING/ERROR; report what was and wasn't verified.
- **NUC1/NUC2** — the household's other production install(s), per **Authority** above. Claude never runs anything on them, deploys nothing, and only reads them through the HA REST API. Deploying to NUC1/NUC2 is always the household's own action, never Claude's, even when Claude is separately authorised to deploy to Cottonwood.
- **devhub** — the test box. Claude may install, restart and configure it, but never writes an entity_id production (Cottonwood or NUC1/NUC2) also has. See the devhub limits under **Release validation**.
- **Other/third-party sites** — installs belonging to other people (testers, other households mentioned in issues). Claude has no access to these and never attempts to install, configure, restart or deploy anything on them. Anything said about them in an issue or comment is reporter-supplied data, not something to verify live.

## Runtime rule

- Nimbus never reads another optimiser's plan or forecast entities at runtime, as an input, a feature or a comparison. It uses measured sensors, or forecasts from real providers or from its own models.
- Reading other projects' source for design is encouraged: see the next section.

## Prior art before a new mechanism

Before designing a solver, load-model, scoring or output mechanism:
1. Check EMHASS first: `src/emhass/optimization.py` and `docs/config.md`.
2. Check HAEO second: its element and graph model.
3. Reuse code where the licence allows, with attribution.
4. Record one line in the issue or PR: "EMHASS: `<param>` / HAEO: `<element>` / neither".
5. Say why, where Nimbus departs from them.

## Releases

1. Open a release PR. It needs:
   - a `## [X.Y.Z]` CHANGELOG section;
   - the `manifest.json` version bump;
   - a `help_tab.WHATS_NEW` entry;
   - the previous release's `Devhub validation:` and `Consumer check:` lines carried back to `main`;
   - the user docs (`docs/`, published at code-imstillalive.github.io/nimbus) updated for every change in it a user would see: a new, changed or removed setting, entity, tab or behaviour. Household, 18 Oct 2026: "make sure these are kept up to date each release". `tests/test_docs_every_number_setting_listed.py` catches a missing or removed setting; other changes are checked by hand.
2. Cut `release/X.Y.Z` from the merge commit. Install it on devhub untagged via HACS, restart, and validate.
3. Merge a verdict PR into `release/X.Y.Z`, carrying `Devhub validation: ... at <commit>` and `Consumer check:`. "Devhub validation: not claimed, because X" is a valid answer.
4. Tag the head of `release/X.Y.Z`, never `main`'s tip. `release.yml`'s gate publishes the GitHub Release only when the lines are present and `custom_components/` matches the validated commit.
5. Deploy only a tag with a published GitHub Release (`gh release view vX.Y.Z`).
6. Update the living comparison issue (#1797) for every release: re-score the rows the release changes, or post one line saying none moved.
7. Keep devhub on the newest release afterwards.

## Release validation (before calling a release done)

1. Install on devhub and restart. HACS must show the new version installed with no update pending.
2. Run a real solve and confirm `optimal`.
3. Check the HA log for new WARNING or ERROR lines.
4. Check every new or changed entity's id and value on the first solve.
5. If a subentry or wizard field changed, exercise the reload path.
6. Open the card or page a household member would use, and say what they would see.
7. Write what was verified in the CHANGELOG, and what was not, with the reason.
   After the release, open the published docs page for each changed feature and confirm it says what the release does.
8. For a callable surface (a service, a field, a wizard step), exercise it with a realistic partial input and read back what was persisted.
9. Confirm the touched issues are in the state you intended.

Limits of devhub:
- Since 23 Oct 2026 it has no mirror of production: production's source readings arrive as read-only `*.testbox_*` copies, and every `sensor.nimbus_*` entity on it is its own. Read versions from a histogram of `nimbus_version` across entities, never one sensor.
- Its Nimbus history began on 23 Oct 2026 (hourly long-term statistics imported from production for the training sensors), and it carries test devices and a test EV production does not; compare plans with production only with that in mind.
- It runs a newer HA than production, so write "confirmed live on devhub (HA <version>)", and say so where a change could depend on the HA version.

## Deploy (household reference)

```
cd /opt/homeassistant/config/nimbus_repo && git fetch --tags origin && git checkout v<X.Y.Z>
docker restart opt_homeassistant_1
```

Use a validated tag, never `main`. A Python change needs a full restart, not a reload.

## Before every push

- Grep the PR body and commit message for closing keywords: `grep -iE "(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]+#[0-9]+"`. Rephrase any match on an issue that should stay open. GitHub ignores negation and qualifiers.
- Run `ruff check` and `ruff format --check` on the files you changed. Never run `ruff format <dir>` or `--select` on the whole tree.
- Branch and PR for every change; never push to `main`.
- A version bump goes in the release PR only, never in a feature branch.

## Testing

```
pip install -e '.[dev]'
pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant
pytest tests/hass_integration/
```

- The stub run is fast. The real-HA run is the one that catches config-flow, entity and coordinator regressions, so confirm it on CI for anything HA-facing.
- Kinds of test, what CI runs and when a test may be retired: `docs/architecture/testing.md` (added by #1845).
- A guard must be shown to fail once, by planting the thing it forbids.

## Code conventions

- `strings.json` and `translations/en.json` stay byte-identical; edit both. `strings.json` is the source.
- No hardcoded entities, retailers or household values: every one is a config field or a live entity.
- `solver/` and `ml/` stay HA-import-free.

## Where things live

| what | where |
|---|---|
| Daily history | `docs/worklog/` |
| Older narrative (forecaster bug chain, roadmap, incidents) | `docs/handover/` |
| Specs and the device contract | `docs/specs/` |
| Architecture and the test policy | `docs/architecture/` |
| User docs (MkDocs site) | `docs/` minus the excluded folders in `mkdocs.yml` |
