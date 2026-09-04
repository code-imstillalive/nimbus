<!--
  Nimbus PR template. Keep the whole thing in the description; each
  section is short by design. Delete a section only if it clearly does
  not apply (e.g. "Screenshots" for a pure-refactor PR).
-->

## What this PR does

<!-- One or two sentences from the user's perspective, not the diff's. -->

## Why

<!-- What symptom, issue, or opportunity does this address? Link the
     issue (`Fixes #NN`) or the discussion. -->

## Type of change

- [ ] Bugfix (non-breaking)
- [ ] Feature (non-breaking)
- [ ] Breaking change (users must migrate config, entity IDs, or workflow)
- [ ] CI / tooling / docs only
- [ ] Release PR (bumps version + rolls CHANGELOG `Unreleased` into a dated section)

## Changelog entry

<!--
  Every user-visible change must land a line in CHANGELOG.md under
  `## [Unreleased]` in this PR. Copy the exact line here so reviewers
  see it inline. Use Added / Changed / Deprecated / Removed / Fixed / Security.

  Skip only for pure internal refactors, CI-only changes, or docs-only
  changes that a user would never notice.
-->

```
### <Added | Changed | Fixed | ...>
- <one-line human-phrased summary> ([#NN](...)).
```

- [ ] `## [Unreleased]` section of `CHANGELOG.md` updated in this PR, OR this change is not user-visible

## Testing

<!-- What did you actually run? "All tests pass" is not enough; call out
     the specific command (`pytest tests/`, `ruff check`, a manual HA
     reload against your own instance, etc.) so someone else can
     reproduce. -->

## Screenshots / diagnostics (if applicable)

<!-- For UI or wizard changes: before/after screenshots. For solver /
     forecaster / diagnostics changes: a snippet from the affected
     sensor or the downloaded diagnostics payload. -->

## Release PR checklist

<!--
  Only fill this in when Type of change is "Release PR". Otherwise
  delete this whole section.
-->

- [ ] Bumped `custom_components/nimbus_load/manifest.json` version
- [ ] Renamed `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] — YYYY-MM-DD` and added a fresh empty `## [Unreleased]` above it
- [ ] Every non-trivial change since the last release has an entry
- [ ] Post-merge: tag `git tag -a vX.Y.Z -m "release: <theme>" && git push --tags`
