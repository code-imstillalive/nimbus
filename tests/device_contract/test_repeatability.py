"""Spec DC-000 repeatability gate: every deterministic device-contract test
gives the same outcome in three fresh processes and once in reversed order.

"A test that succeeds only due to retained module/global state fails the
gate" (DC-000, Isolation and repeatability). One pytest process cannot show
that, because whatever state a test leans on is already warm. So this runs
the suite in child interpreters and compares their per-test outcomes.

The child runs exclude this file, or it would recurse.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
PLUGINS = ["-p", "no:homeassistant", "-p", "no:socket", "-p", "no:cacheprovider"]


def _node_ids() -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *PLUGINS, str(HERE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    ids = [
        line.strip()
        for line in out.splitlines()
        if "::" in line and "test_repeatability.py" not in line
    ]
    assert ids, f"collected nothing:\n{out}"
    return ids


def _outcomes(node_ids: list[str]) -> dict[str, str]:
    """Run the given tests, in the given order, in one fresh interpreter."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rA",
            "-p",
            "no:randomly",
            *PLUGINS,
            *node_ids,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    outcomes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        word, _, rest = line.partition(" ")
        if word in ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS"):
            outcomes[rest.split(" - ")[0].strip()] = word
    assert outcomes, f"no outcomes parsed:\n{proc.stdout[-2000:]}"
    return outcomes


def test_three_fresh_runs_and_a_reversed_one_agree() -> None:
    ids = _node_ids()
    runs = [_outcomes(ids) for _ in range(3)]
    runs.append(_outcomes(list(reversed(ids))))
    first = runs[0]
    assert set(first) == set(ids), sorted(set(ids) ^ set(first))[:10]
    assert all(v == "PASSED" for v in first.values()), {
        k: v for k, v in first.items() if v != "PASSED"
    }
    for n, run in enumerate(runs[1:], start=2):
        assert run == first, (
            f"run {n} differs: "
            f"{ {k: (first.get(k), run.get(k)) for k in set(first) | set(run) if first.get(k) != run.get(k)} }"
        )


def test_negative_control_an_order_dependent_test_is_caught(tmp_path: Path) -> None:
    """A pair that passes only when run in file order must differ when
    reversed, or the gate above could never fail."""
    mod = tmp_path / "test_order_dependent.py"
    mod.write_text(
        "STATE = []\n\n"
        "def test_a_sets():\n    STATE.append(1)\n\n"
        "def test_b_needs_a():\n    assert STATE\n",
        encoding="utf-8",
    )
    ids = [f"{mod}::test_a_sets", f"{mod}::test_b_needs_a"]
    forward = _outcomes(ids)
    reverse = _outcomes(list(reversed(ids)))
    assert set(forward.values()) == {"PASSED"}
    assert forward != reverse
