# Nimbus tests: what kinds exist, what CI runs, and when a test can go

Written for #1837 (Mark Purcell, 17-18 Oct 2026: "the pytest suite needs rationalising... Please raise pr to rationalise testing suite"). The measurements behind it are in [#1837's analysis](https://github.com/code-imstillalive/nimbus/issues/1837#issuecomment-6234295664), read from CI's own per-test timestamps at `a78acad6`.

## What CI runs on a PR

| job | what | time |
|---|---|---|
| Unit Tests (pytest), stub run | `tests/` except `hass_integration/`, against `tests/_ha_stubs.py`, under coverage, in parallel (`-n auto`, pytest-xdist) | ~29 min before #1837; ~18 min at 1,668 tests (#2084's PR run) |
| Unit Tests (pytest), real HA run | `tests/hass_integration/`, the real `pytest-homeassistant-custom-component` harness; carries the `--cov-fail-under` ratchet | ~1.3 min |
| No-op Patch Gate | patches that no longer reach their reader (#1316); full mode when the PR touches `custom_components/`, canary otherwise | ~7 min |
| Lint, mypy, manifest, hassfest, HACS | static checks | minutes |

**No markers.** The coverage gate's end-to-end self-compare ran the golden scenarios and the whole stub suite twice under coverage, and from 25 Oct 2026 it passed its 30-minute limit on every push to `main`. It was removed (#1837); `coverage_compare.py` keeps its toy-repo tests in `test_gates_coverage_compare.py`.

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

Measuring 1 needs a coverage-context run of the whole suite, which is much heavier than CI. `scripts/coverage_by_risk.py` (below) runs exactly that.

## Merged and folded tests

#1837 cut the suite to about 1,000 tests by putting several old tests into one, without deleting any check. Two shapes:

- **Merged:** one test runs several former tests one after another. Each part starts with a comment naming the test it was, such as `# test_wrong_device_class_is_never_suggested`.
- **Folded:** a former parametrized test loops over its cases.

The scripts that made these changes are in `scripts/test_cuts/`.

What this costs when a test fails:

1. **The first real failure stops the rest.** In a merged test, the parts after a failing one never run, so CI shows one failure where there may be several. After fixing it, rerun: a later part may fail too. A failure inside `pytest.raises` is not one of these, since it is expected.
2. **The failure carries the merged test's name**, which is the first part's. To find the part that failed, take the line number in the traceback and look up to the nearest `# test_...` comment.
3. **A folded test doesn't say which case failed.** Rerun with `-l` to see the loop's values.

When a merged part keeps causing confusion, split it back out into its own test. The count staying near 1,000 matters, not 1,000 exactly, and a test that is clearly missing may be added.

## Coverage by risk

#1837's first question: which parts of Nimbus are tested because they carry risk, against tested because a bug once landed there. `scripts/coverage_by_risk.py` answers it as a measurement:

- every module is in one of four risk tiers (`RISK_TIERS`): 1, dispatch and money; 2, forecasts and inputs; 3, setup and configuration; 4, reports and presentation. `tests/test_gates_coverage_risk_tiers.py` fails when a module has none;
- it runs the stub suite, the real-HA suite and every golden scenario under per-test coverage contexts, and refuses to report if a stage did not run;
- per module, tier and area it reports line and branch coverage, and the share of covered lines that only issue pins reach;
- it flags each module below its tier's bar (`FLAGS`, proposals for review, not CI gates).

Run it from the Actions tab (**Coverage by risk**, manual, about an hour) and commit the artifact to `docs/architecture/coverage-by-risk.md` and `.json`. Locally it needs the same Home Assistant as CI for the real-HA stage.

## Parallel runs

The stub run uses `pytest -n auto`. Measured on the household's 16-thread machine: 14.7 min against ~56 min serial for the same selection. A test must therefore not depend on running in one process:

- A subtest's labels travel between processes, so pass strings (`repr(x)`), not objects.
- A test that starts pytest in a child process strips `PYTEST_*` from the child's environment, so the child is not taken for an xdist worker. It also gives the child its own `--basetemp`, because a child on the shared one prunes the oldest numbered temp directory, which can belong to another worker.

