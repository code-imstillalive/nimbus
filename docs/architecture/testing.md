# Nimbus tests: what kinds exist, what CI runs, and when a test can go

Written for #1837 (Mark Purcell, 17-18 Oct 2026: "the pytest suite needs rationalising... Please raise pr to rationalise testing suite"). The measurements behind it are in [#1837's analysis](https://github.com/code-imstillalive/nimbus/issues/1837#issuecomment-6234295664), read from CI's own per-test timestamps at `a78acad6`.

## What CI runs on a PR

| job | what | time |
|---|---|---|
| Unit Tests (pytest), stub run | `tests/` except `hass_integration/`, against `tests/_ha_stubs.py`, under coverage | ~29 min before #1837; ~11 min on a PR expected without `slow_gate` (estimated from #1837's timings, not yet measured) |
| Unit Tests (pytest), real HA run | `tests/hass_integration/`, the real `pytest-homeassistant-custom-component` harness; carries the `--cov-fail-under` ratchet | ~1.3 min |
| No-op Patch Gate | patches that no longer reach their reader (#1316); full mode when the PR touches `custom_components/`, canary otherwise | ~7 min |
| Lint, mypy, manifest, hassfest, HACS | static checks | minutes |

**The one marker, `slow_gate`.** `test_gates_coverage_compare.py::test_real_main_compared_to_itself_has_zero_regressions` runs the golden scenarios and the whole stub suite twice under coverage: 1,078 s, about 65% of the stub job. On a PR it compares the tree with itself, so it cannot fail because of the PR's change. A PR's stub run therefore skips it, unless the PR changes `tests/gates/coverage_compare.py`, that test, or `pyproject.toml`. Every push to `main` and every manual run still runs it, so the gate's real pipeline is proven end to end on each merge. A failure there means `main` is red and is fixed like any other.

A new marker needs the same case: measured CI time, a reason the test cannot fail on a PR (or a narrower trigger that keeps it where it can), and registration in `pyproject.toml` (the suite runs `--strict-markers`).

## Kinds of test

Name a new file so its kind is visible. These are the kinds in the suite (counts at `a78acad6`, 569 stub files, 6,361 tests):

| kind | how to recognise it | files | tests | what it is for |
|---|---|---:|---:|---|
| **behaviour** | `test_<module>_<what>.py` | 346 | 3,647 | what a module does, through its public functions |
| **issue pin** | `test_<issue>_<what>.py` | 192 | 2,411 | one reported defect or IV&V finding, reproduced, so it cannot come back unnoticed |
| **guard, drift, contract** | `test_*drift*`, `test_*guard*`, `test_gates_*`, `device_contract/` | 27 | 259 | properties of the repo itself: two copies stay in step, a doc names only real entities, a ratchet does not loosen |
| **characterisation, golden** | `golden*/`, `test_*characteriz*` | 4 | 41 | a captured output that a refactor must reproduce exactly |
| **real HA** | `tests/hass_integration/` | | | config flows, entity registration and services against real Home Assistant |

Issue pins are 38% of the tests but about 10% of CI time (153 of 1,661 s). They cost readability and duplication, not minutes, so the policy below is about clarity, not speed.

## When to add which

- **A bug:** an issue pin that fails before the fix and passes after it. Test the most common real case, not only the reported edge (#582).
- **A new mechanism:** behaviour tests in the module's own file, through the real path (for the Solver, `solver_plan.assemble_and_solve_plan` and the LP, not a mock of it).
- **A rule about the repo:** a guard, and plant the thing it forbids once to show it fails (#757: a check that cannot fail is indistinguishable from one that works).
- **A refactor:** characterisation first, captured before the move.

## When an issue pin can be retired or merged

Nothing removed one before; at 192 files the default is still to keep them. A pin may be retired or folded into a broader test only when all of these hold, recorded in the PR:

1. **The broader test covers it, measured.** Per-test coverage contexts (`pytest --cov-context=test`) show the broader test executes every line the pin executes, and asserts the same outcome. Line coverage alone is not enough: the assertion has to be equivalent.
2. **The issue's own reproduction still fails without the fix.** Revert the fix on a branch and show the broader test fails, as the pin did.
3. **The pin's issue number survives**, in the broader test's docstring, so `git grep '#NNN'` still finds where the defect is guarded.
4. **No golden, guard or IV&V finding is retired this way.** Those are not issue pins.

Measuring 1 needs a coverage-context run of the whole suite, which is much heavier than CI. That is the next step on #1837, not part of this change.
