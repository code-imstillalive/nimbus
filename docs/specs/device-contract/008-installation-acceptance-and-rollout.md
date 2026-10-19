# Nimbus Device Contract Spec DC-008: clean install, upgrade, limited rollout and release acceptance

Status: Draft for review, revision 0.1. Prepared 19 October 2026, Australia/Brisbane. Drafted, as DC-001 to DC-007 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574).

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/008-installation-acceptance-and-rollout.md`. Depends on each supported slice of DC-001 to DC-007.

This specification proposes a process. It authorises no deployment: deploying to the reference household's production is always the household's own action, and nothing here changes that.

## Responsibility

The roadmap's last gate: *"Approval of a document is not implementation; passing CI is not live acceptance; a release tag is not evidence that a particular installation has adopted it."* DC-008 says what evidence each step of a slice's life needs, from merged code to an installation that runs it in `active`, and how to come back.

| DC-008 owns | DC-008 does not own |
|---|---|
| Clean-install and upgrade scenarios, run on devhub | Building any slice (DC-001 to DC-007) |
| The release gate for a device-contract slice | The general release process (CLAUDE.md "Releases"), which it adds to, not replaces |
| A limited rollout: which installs, in what order, with whose consent | Deciding for a household; each install's owner decides |
| The rollback rehearsal | Restoring an install's data; that stays the owner's |
| The support matrix: what is supported, for which providers and equipment | Promising support for anything not on it |
| Making `active` the default for new installs, when the gates are met | Changing an existing install's mode |

## Requirements owned (from DC-000's registry)

| ID | Requirement | What DC-008 must make checkable |
|---|---|---|
| **DC-R12** (with 004) | Migration preserves intent, history links and control authorisation | The upgrade scenario on devhub shows every consumer's effective input unchanged in `legacy` and `shadow`, and control state unchanged in every mode |
| **DC-R18** (with 007) | Novice guidance refined from recorded walkthroughs | Each walkthrough is recorded before the rollout step it gates |

## The stages a slice passes

| Stage | Evidence | Who |
|---|---|---|
| **approved** | the slice's spec decisions answered on #1574 | Mark Purcell |
| **implemented** | merged, every check passing, Mark's review with no open finding | maintainer |
| **independently verified** | its acceptance tests re-run at the release commit, and DC-000's gates green | maintainer, or Mark |
| **released** | a tag with `Devhub validation:` and `Consumer check:` lines (CLAUDE.md) | maintainer |
| **installation-accepted** | the scenarios below on devhub, then one consenting household in `active` for the soak period, with nothing it had to undo | the household |

A slice is installation-accepted for the installs that ran it, not for every install.

## Scenarios on devhub, per release that carries a slice

1. **Clean install:** remove and re-add the integration on devhub. Walk DC-007's new-install path. Record the readiness result and the time taken.
2. **Upgrade, `legacy`:** install the release over the previous one. Every consumer's input is unchanged (DC-006's equivalence capture). The store is untouched.
3. **Upgrade, `shadow`:** the dry run stays clean, and the stored revision is unchanged by the upgrade.
4. **Upgrade, `active`:** start-up recovery runs once and finds nothing. The views equal the document, and no drift Repair appears.
5. **Rollback rehearsal:** install the previous release over this one, in `active`. It runs on the views (DC-004's rollback boundary), and then the household's `legacy` choice returns everything to the settings. Record what, if anything, needed a hand.
6. **No actuator call** in any scenario (DC-R15), checked by a service-call capture on devhub.

devhub's limits apply: its canonical entities carry production's mirrored data, it runs a newer HA, and no entity_id production also has is written. Where a scenario cannot be shown on devhub, the CHANGELOG says so with the reason, as for any release.

## Limited rollout

- **Order:** devhub, then the reference household in `shadow`, then the reference household in `active`, then any consenting tester. Each step waits for the previous step's soak period.
- **Soak periods:** a week in `shadow`; a week in `active` for observation and forecasting slices; two weeks for planning (DC-006 decision 6), because planning changes what the battery does.
- **Consent:** each household chooses its own mode, on its own install, and says so on #1574 or in its own issue. Nimbus never changes another install's mode, and a tester's install is never reached from here.
- **Stop rule:** any unintended change to a consumer's input, a drift Repair the household did not cause, a refused confirmation of a correct source, or an actuator call outside a plan stops the rollout at that step. Fix, re-release, and restart the step.

## The support matrix

Published with the user docs and updated per release. For each provider profile (DC-005) and equipment shape, it states one of:

- **supported:** a profile, fixtures and a consenting install have run it;
- **proposed:** a profile and fixtures, no install yet;
- **reported only:** recognised, not bound, with the reason (for example AEMO and PD7DAY until DC-001 has their roles);
- **manual:** no profile; set by hand in Advanced.

Nothing is listed as supported on fixtures alone.

## Making `active` the default for new installs

Only when every one of these holds:

1. DC-006's observation, forecasting and planning slices are installation-accepted on the reference household;
2. DC-007's new-install walkthrough is recorded on devhub and on one consenting household;
3. the rollback rehearsal has passed on the release that makes the change;
4. Mark Purcell approves the change on #1574.

Existing installs keep their mode. Each household moves when it chooses.

## Acceptance

- Every release carrying a slice has scenarios 1 to 6 recorded in its CHANGELOG, or "not shown on devhub, because …".
- The rollout log on #1574 names each install and step, with dates, soak results and any stop.
- The support matrix in the user docs matches the profiles in code. A test fails when a profile exists without a matrix row.

## Rollback

DC-004's rollback boundary applies to every slice: the previous release runs on the views, and `legacy` returns every consumer to its settings. The rehearsal (scenario 5) is what makes that a tested promise rather than a design one.

## Decisions for review

Each is proposed as written above. The build follows these unless @purcell-lab changes one.

1. **Five stages**, with installation acceptance per install, never global.
2. **Six devhub scenarios per release** that carries a slice, recorded in the CHANGELOG.
3. **Rollout order and soak periods** as above, with the stop rule.
4. **The support matrix** has four states, is published with the user docs, and is pinned to the code by a test.
5. **`active` becomes the default for new installs** only when the four conditions hold; existing installs are never moved.
