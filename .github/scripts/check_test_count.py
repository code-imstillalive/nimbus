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
    counts = [collected(args) for args in RUNS]
    total = sum(counts)
    print(f"tests collected: {counts[0]} stub + {counts[1]} real HA = {total}")
    if total > CEILING:
        print(
            f"over the ceiling of {CEILING} by {total - CEILING}: add the new "
            "check as a named part of an existing test, or fold a test into "
            "another in this PR (.github/scripts/check_test_count.py)"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
