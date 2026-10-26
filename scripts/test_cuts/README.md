# The #1837 test cuts

The scripts that cut the suite from 5,496 tests to 983, kept so the change can be audited. They are a record of what ran, not a tool to run again: on today's tree they find little or nothing left to do, and anything they do find goes through the same review as any test change.

| script | PR | what it did |
|---|---|---|
| `cut1_delete.py` | #2084 | deleted the tests a per-test coverage run showed reach no line or branch the kept tests miss |
| `cut2_fold.py` | #2084 | folded fixture-free parametrized tests into one test looping over the cases |
| `cut3_merge.py` | #2084 | merged runs of consecutive fixture-free tests into one |
| `cut4_fold.py` | #2087 | the fold again, now for tests taking fixtures |
| `cut5_merge.py` | #2087 | the merge again, for tests taking fixtures, up to 16 at a time (`--chunk=16`) |

`kept_whole.txt` is the `--skip` list for cuts 4 and 5: eight files, then 69 tests. 65 of the tests failed on CI when merged; the other 4 passed but took a different path through a shared fixture, which the coverage proof caught.

How a merged test behaves when it fails is in [docs/architecture/testing.md](../../docs/architecture/testing.md#merged-and-folded-tests).
