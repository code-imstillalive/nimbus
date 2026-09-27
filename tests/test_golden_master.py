"""Spec 000 Part A: the golden master every refactor spec is accepted against.

Each scenario in ``tests/golden/scenarios*.py`` runs ``solver_writer.main()``
in a fresh interpreter (``golden.harness.run_isolated``) against a fake Home
Assistant, under a frozen clock, and everything it did (states posted,
services called, entities read, WARNING and above logged, state files
written) is compared exactly with ``tests/golden/snapshots/<name>.json.gz``.

A refactor that changes any of it fails here. A change that is meant to
alter behaviour regenerates the snapshots, and the diff goes in the PR:

    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_master.py -p no:homeassistant

Only wall-clock durations are excluded (``VOLATILE_KEYS``).
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest
from golden import harness, scenarios

SNAPSHOTS = Path(__file__).parent / "golden" / "snapshots"

# Measured, not asserted: each is a wall-clock duration of the solve.
VOLATILE_KEYS = frozenset({"solve_seconds", "solve_time_s", "elapsed_s"})


def canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: canonical(v) for k, v in sorted(value.items()) if k not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, float):
        return float(repr(value))
    return value


def comparable(record: dict) -> dict:
    out = dict(record)
    out.pop("python", None)  # recorded for diagnosis, not compared
    return canonical(out)


def _dump(record: dict) -> bytes:
    text = json.dumps(record, sort_keys=True, indent=1) + "\n"
    return gzip.compress(text.encode("utf-8"), mtime=0)


def _first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} {a!r:.200} != {type(b).__name__} {b!r:.200}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: present on one side only"
            d = _first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            d = _first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r:.200} != {b!r:.200}"


@pytest.mark.parametrize("name", scenarios.names())
def test_golden_master(name: str) -> None:
    got = comparable(harness.run_isolated(name))
    path = SNAPSHOTS / f"{name}.json.gz"
    if os.environ.get("GOLDEN_UPDATE") == "1":
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dump(got))
        return
    assert path.exists(), f"no snapshot for {name}; run with GOLDEN_UPDATE=1"
    want = json.loads(gzip.decompress(path.read_bytes()))
    diff = _first_difference(want, got)
    assert diff is None, f"golden master {name} changed at {diff}"
