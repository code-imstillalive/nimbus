# Worked example: chasing a false alarm properly

From the same-day IV&V pass (#996, 2026-09-16). Included in full because the shape of the
investigation matters more than the specific numbers — the same method applies to any
"is this local failure real" question.

## The symptom

Full local suite run on the reviewed commit range's HEAD:

```
1 failed, 2817 passed, 14 skipped, 346 subtests passed
FAILED tests/test_main_golden_output_guardrail.py::TestMainGoldenOutput::test_fixed_inputs_produce_the_exact_expected_plan
E   AssertionError: forecast[1]['shadow_price'] = 0.2999, expected 0.3
```

A pinned "golden output" guardrail test (the kind this repo built specifically so an
extraction/refactor "fails on any byte of drift") disagreeing by 0.0001 on one dual value.
Small enough to be numerical noise, specific enough that it might not be.

## First hypothesis: introduced by the reviewed range

Bisected via isolated worktrees across every commit in the reviewed range that touched the
LP solver:

```bash
git worktree add -q --detach <scratch>/wt/<hash> <hash>
# ...for each candidate commit...
cd <scratch>/wt/<hash> && python -m pytest tests/test_main_golden_output_guardrail.py \
  -p no:homeassistant -q
```

Every one of them failed identically — including the common parent, *before* any commit in
the reviewed range. That ruled out the range entirely: whatever this is, it predates the
review scope by at least a day. Continued walking backward a little further (still failing
at the previous day's last commit) before deciding the exact origin commit wasn't worth
chasing further — it was already outside what this pass was scoped to review.

## Second hypothesis: a real, currently-live defect on `main`

A test failing on the current tip of `main`, if genuinely reproducible, would be worth its
own issue regardless of whether the reviewed range caused it. Checked the one thing that
actually answers this: the real GitHub Actions conclusion for that exact commit and that
exact test file — via the GitHub MCP tools this session had attached, but any working
path to that same conclusion (the `gh` CLI, the Actions tab in a browser) answers the same
question equally well; the mechanism isn't the point.

```
list the workflow runs for the current main HEAD, filtered to completed
  -> CI run on the current HEAD: conclusion "success"
list that run's own jobs
  -> "Unit Tests (pytest)" job: conclusion "success", ran the stub-based suite that
     includes this exact test file
```

CI genuinely passed this exact test on this exact commit. A local failure that CI doesn't
reproduce, on code CI actually exercises, is not a live defect on `main` — it's an artifact
of *this* environment specifically.

## Third hypothesis, confirmed: environment mismatch

This repo's `pyproject.toml` pins `requires-python = ">=3.14.4"` deliberately (a documented
comment explains why — it matches the real Home Assistant runtime this integration deploys
into). The sandbox available for this session tops out at Python 3.13. A tiny floating-point
divergence in a degenerate/near-tied LP solve landing on a different vertex between Python
minor versions (different math library builds, different BLAS) is a completely plausible
mechanism, and it's consistent with every other symptom: reproducible locally, not
reproducible on the real pinned version, off by a small but non-random-looking amount.

## What went in the head issue

Not silence, not a filed finding — an explicit "ruled out" entry with the evidence chain
(bisection result, CI run link, the version-pin discrepancy) so a future pass doesn't have
to redo this same investigation from scratch if the same test flakes locally again.

---

# The other version-mismatch gotcha: `ruff`

Separately, the same pass wrote new test files, ran `ruff check` on them, and got a clean
pass. Later, after using `ruff format` (which reformatted an unrelated pre-existing line),
a re-check surfaced a fresh `E402` on a line that hadn't previously been flagged — a
`sys.path.insert()`-then-import pattern this test suite uses throughout (see any
`test_*.py` file that imports `_solver_path`/`_ha_stubs` before importing
`custom_components.nimbus_load.*`).

The cause: the sandbox's default `ruff` was `0.15.8`. `.github/workflows/ci.yml` pins
`ruff==0.16.4`. The two versions disagree about how many statements can precede an import
before `E402` fires on this exact idiom — confirmed by finding the *identical* pattern,
unmodified, in an already-merged, presumably-CI-green file (`tests/test_sensor_flattened.py`)
and getting the same spurious `E402` from the local 0.15.8 binary.

Fix: install the exact CI-pinned version into whatever venv is being used for validation
before trusting a "ruff clean" result —

```bash
pip install "ruff==0.16.4"   # match .github/workflows/ci.yml's own pin, not the number
                              # written above; check the workflow file for the current one
ruff check <files>
ruff format --check <files>
```

— and re-run. Don't chase a lint error that only exists in a tool version CI doesn't run.
