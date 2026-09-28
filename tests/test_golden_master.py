"""Spec 000 Part A: the golden master every refactor spec is accepted against.

Each scenario in ``tests/golden/scenarios*.py`` runs ``solver_writer.main()``
in a fresh interpreter (``golden.harness.run_isolated``) against a fake Home
Assistant, under a frozen clock, and everything it did (states posted,
services called, entities read, WARNING and above logged, state files
written) is compared exactly with ``tests/golden/snapshots/<name>.json``.

A refactor that changes any of it fails here. A change that is meant to
alter behaviour regenerates the snapshots, and the diff goes in the PR:

    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_master.py -p no:homeassistant

Only wall-clock durations are excluded (``VOLATILE_KEYS``).

## Why the snapshots are stored uncompressed (nimbus issue #1359)

They were ``.json.gz`` until #1359. ``_dump()`` pinned ``mtime=0``, which
removes gzip's timestamp, but it cannot pin zlib's own compressed output:
that differs between zlib builds, so regenerating on a different interpreter
than the one that recorded a snapshot rewrote **every** file's bytes with not
one recorded value changed. That made the update rule above worthless -- the
diff a reviewer needs was buried under twelve unreadable binary files.

Measured over 7 successive one-value revisions of all 14 snapshots, storing
them as text costs a larger working tree and a *smaller* repository:

    ============  working tree  .git after 7 revisions
    .json.gz            0.25 MB                0.92 MB
    .json               7.19 MB                0.25 MB

Text deltas across revisions; a gzip stream does not, so each revision stores
a whole new incompressible blob. The working tree is a constant, history is
what grows forever.

The ``nemweb/`` fixtures stay compressed deliberately: those are recorded
*inputs*, never read as a diff, and 2.1 MB of them expands far further.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from golden import harness, scenarios

SNAPSHOTS = Path(__file__).parent / "golden" / "snapshots"

# Measured, not asserted: the solve's wall-clock duration, published as
# ``solve_seconds``. The only such key in custom_components/nimbus_load; a
# new timing field must be added here by name.
VOLATILE_KEYS = frozenset({"solve_seconds"})


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
    """The canonical on-disk bytes for a record.

    Returns bytes rather than str so the caller writes with ``write_bytes``:
    ``write_text`` translates a newline to ``os.linesep``, which would record
    CRLF on Windows and LF in CI for byte-identical content.
    """
    return (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")


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
    record = harness.run_isolated(name)
    got = comparable(record)
    path = SNAPSHOTS / f"{name}.json"
    if os.environ.get("GOLDEN_UPDATE") == "1":
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        fresh = _dump(got)
        # Content-addressed: leave the file alone when nothing it records
        # moved, so a regeneration run for one real reason does not hand the
        # reviewer thirteen unrelated touched files (#1359).
        if not path.exists() or path.read_bytes() != fresh:
            path.write_bytes(fresh)
        return
    assert path.exists(), f"no snapshot for {name}; run with GOLDEN_UPDATE=1"
    want = json.loads(path.read_bytes())
    diff = _first_difference(want, got)
    assert diff is None, (
        _version_note(record) + f"golden master {name} changed at {diff}"
    )


def _version_note(record: dict) -> str:
    """Name a Python mismatch first, so a float difference from another
    interpreter is not read as a behaviour change. Not a hard check: that
    would fail or skip the gate on every other version."""
    ran = tuple(record.get("python", ())[:2])
    if ran == harness.RECORDED_PYTHON:
        return ""
    want = ".".join(map(str, harness.RECORDED_PYTHON))
    got = ".".join(map(str, ran))
    return (
        f"snapshots were recorded on Python {want} and this ran on {got}; "
        "a float difference may be the interpreter, not your change. "
    )
