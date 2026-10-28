"""The test suite stays at 1,010 to 1,020 tests (household, 28 Oct 2026: "I
said i did not mind 1010-1020. But now u r at 1030. Its slipping away from
u").

#1837 cut the suite from 5,496 to 983 by folding and merging, never by
dropping a check. It then crept back to 1,029 with nothing to stop it. This
counts what CI collects, both runs (the stub suite and the real Home Assistant
suite, skipped tests included), and fails above the ceiling.

Over the ceiling, new checks go in as named parts of an existing test, or a
test is folded into another in the same PR (one in, one out). Raising the
ceiling is the household's decision, not a PR's.

This gate has no pytest test of its own, which would add to the count it
polices. Instead it checks itself on every run (`_self_check`): the
comparison and the count parser, against known inputs, before counting. So
an inverted comparison or a broken regex fails CI at once, not silently
(Mark Purcell's review of #2149).

Fail-once proof (CI run 40648457824, branch `diag/2149-fail-once`):
`sed -i 's/^CEILING = 1020$/CEILING = 1000/' .github/scripts/check_test_count.py`
and run the pytest job: it fails at this step with "over the ceiling of 1000
by 17".
"""

from __future__ import annotations

import re
import subprocess
import sys

CEILING = 1020

RUNS = (
    ["tests/", "--ignore=tests/hass_integration/", "-p", "no:homeassistant"],
    ["tests/hass_integration/"],
)

_COLLECTED = re.compile(r"(\d+) tests? collected")


def over(total: int, ceiling: int) -> bool:
    return total > ceiling


def _self_check() -> None:
    """The comparison and the parser on known inputs, every run."""
    problems = []
    if not over(CEILING + 1, CEILING) or over(CEILING, CEILING):
        problems.append("the ceiling comparison is not `total > CEILING`")
    if _COLLECTED.findall("981 tests collected in 5.12s") != ["981"]:
        problems.append("the count parser misreads '981 tests collected'")
    if _COLLECTED.findall("1 test collected in 0.10s") != ["1"]:
        problems.append("the count parser misreads '1 test collected'")
    if problems:
        raise SystemExit("check_test_count.py is broken: " + "; ".join(problems))


def collected(args: list[str]) -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    found = _COLLECTED.findall(out.stdout)
    if out.returncode != 0 or not found:
        print(out.stdout[-2000:], out.stderr[-2000:], sep="\n")
        raise SystemExit(f"could not count the tests in {' '.join(args)}")
    return int(found[-1])


def main() -> int:
    _self_check()
    counts = [collected(args) for args in RUNS]
    total = sum(counts)
    print(f"tests collected: {counts[0]} stub + {counts[1]} real HA = {total}")
    if over(total, CEILING):
        print(
            f"over the ceiling of {CEILING} by {total - CEILING}: add the new "
            "check as a named part of an existing test, or fold a test into "
            "another in this PR (.github/scripts/check_test_count.py)"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
