---
name: nimbus-move-code
description: Move functions, constants or state out of one Nimbus module into another (the solver_writer.py split, #2289 item 6, and any later restructuring) without changing behaviour or silently breaking a test patch. Use for any "move", "extract", "split", "relocate" or "consolidate" refactor in custom_components/nimbus_load, and when a No-op Patch Gate (#1316) or a source-reading test fails after a move.
---

# Moving code in Nimbus without breaking it

A move is supposed to change nothing. In this repo it has broken things in
quiet ways, mostly through **test patches that keep passing while
intercepting nothing** (#861). This is the procedure that has worked, and the
traps found along the way.

## 1. Measure before choosing what moves

```bash
python tests/analyse_module_dependencies.py --functions a,b,c            # what they depend on
python tests/analyse_module_dependencies.py --callers --functions a,b,c  # who calls or patches them
```

- Group by **what the code does and where its siblings live**, not by name.
  A name-based group picks up strangers. For example, `_grid_meter_sign`
  matched "grid" but reads a live meter, not the time grid.
- **Module-level state goes with its functions**, and only if it is
  *mutated* (`.clear()`, `.update()`). If any function **rebinds** it
  (`global X; X = ...`), the function stays, or the state is reached through
  the module that owns it. Otherwise two copies drift apart.
- **Persisted paths** (`*_PATH` read from `os.environ`) stay in
  `solver_writer`. `tests/_isolated_state.py` and the persisted-paths gate
  key them there. Moved code reads them at call time through its module's
  existing `_solver_writer()` reference.
- **`set_native_hass`** stays in `solver_writer`. It rebinds that module's
  own globals.

## 2. Move verbatim, by AST span

Cut each function, assignment or constant with its leading comment lines,
using `ast` line numbers, and paste it unchanged. Then let
`ruff check --select F821,F401,F811` list exactly what to resolve:

- a name that lives elsewhere: call it as `<module>.<name>(...)`;
- a call in the old module to a moved name: `<newmodule>.<name>(...)`;
- the old module's import of a moved helper: remove it, unless other
  modules read it as `sw.<name>`, in which case keep it with
  `# noqa: F401 -- re-export: ...`.

**The #861 rule:** call a moved function through the module that now holds
it. Never add a new `_solver_writer()` late-import seam. A module that
already uses one may keep using it for names that stay in `solver_writer`.

An AST span ends at the function's **last statement**. A trailing comment at
the end of its body sits after that line, so it is left behind as an orphan
in the old module. Check the lines just after each cut, and move or delete
them.

Ruff cannot check **attribute** names. After the move, confirm every
`module.attr` the moved code uses really exists in that module.

## 3. Move every test patch with the function

A test that patched the old module must patch the new one in the same PR.
If it doesn't, the test runs the real function and may still pass.

```bash
grep -rnE "(sw|solver_writer)\.(name1|name2)|setattr\((sw|solver_writer)[^,]*, *\"(name1|name2)\"|patch\.object\((sw|solver_writer)[^,]*, *\"(name1|name2)\"" tests
grep -rnE "\"(name1|name2)\"" tests   # names held in patch tables, e.g. _REPORT_PUBLISHERS
```

- **Indirect dependencies:** a moved function that reads `ha_get` now
  resolves it in its new module. A test that runs it, or `main()`, with only
  `sw.ha_get` patched must also patch the new module's `ha_get`.
- **Harnesses that compare against an older capture** (`tests/golden_1310`)
  must patch *every* module that holds the function, so both revisions read
  the same fake.
- **Source-reading tests** (`Path(...).read_text()` on `solver_writer.py`)
  must read the file the code moved to.

## 4. Lint without collateral damage

- **Never** run `ruff check --fix` with `RUF100` (or an unrestricted `--fix`)
  on a whole module. It once stripped 56 deliberate `noqa` comments from
  `solver_writer.py`.
- Remove an unneeded `noqa` only on lines you have read, by line number.
  Never use a list from a second lint run.
- Restrict auto-fixes to safe rules on named files:
  `ruff check <files> --select I001,F401 --fix`.
- Check the diff removes no `noqa` from code you did not move:
  `git diff -U0 | grep -E "^-.*noqa"`.

## 5. Gates that catch what review misses

- **No-op Patch Gate (#1316)** reports a patch site that went from touched to
  untouched. Treat it as a real finding:
  - a patch on the old module (fix the patch);
  - or the run taking a different path than before (find out why before
    merging).
- **Size ratchet** (`tests/gates/size_ratchet.py`, `tests/test_gates_size_ratchet.py`):
  a move that takes a function over 60 lines out of `solver_writer.py`
  lowers the count. Set the baseline and the test's expected text to the new
  number. Parallel move PRs each lower it; the second to merge resolves to
  the combined number.
- **Import-linter:** removing the last import between two modules makes its
  `ignore_imports` entry unmatched, which fails. Remove the entry and lower
  `_EXPECTED_IGNORED_IMPORT_COUNT` in `tests/test_import_linter_contract.py`.

## Merging stacked moves

- **Resolve each hunk by intent.** For a function this step moved, keep the
  deletion. For a nit fixed on `main`, keep `main`'s side. Then confirm with
  `ast` that no moved function still exists in the old module.
- **The size ratchet:** when `tests/gates/size_ratchet.py` is itself in
  conflict, its script cannot run. Resolve every other file first, take the
  baseline from a live count
  (`python tests/gates/size_ratchet.py --baseline-count 99 <file>`), and only
  then write the number into both ratchet files. Check the result with
  `git diff origin/main -- tests/test_gates_size_ratchet.py`: only the two
  lines with the number should change.
- **Afterwards:** grep for `<<<<<<<`/`>>>>>>>` in every touched file before
  committing.
- **A hunk resolved to neither side leaves extra blank lines.** Run
  `ruff format` on the file, and make the format check gate the commit:
  `ruff format --check <files> && git commit ...`. A check on its own line
  does not stop the commit (it happened twice on 7 Nov 2026).

## 6. The PR

- **One group per PR.** State what moved, what stayed and why, the callers
  and test patches changed, and "no behaviour change".
- **The same test count.** Tests run on CI only.
- **Stacked moves:** say so in the body. After the base merges, merge `main`
  in (never force-push) and wait for green on that head.
- **Reviewer:** moves of solve-path code wait for Mark Purcell's review.
