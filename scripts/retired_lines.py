"""Lines only the retired test files reach (#1837).

Restores each retired file from the commit before its deletion, runs it
alone under branch coverage, and subtracts what the surviving suite plus
the golden scenarios reach at the current tree. What is left is the code
the retirement stops executing: the "no line lost" check a deletion PR
reports, as a reproducible result rather than a number in prose.

    python scripts/retired_lines.py --before <commit> --retired @list.txt \\
        --json tests/v2/retirement/<pr>.lines.json

`list.txt` holds one retired path per line (`Deletes:` lines are accepted).
The restored files are written back where they lived and removed again
afterwards, so the working tree is left as found.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "custom_components/nimbus_load"


def _paths(arg: str) -> list[str]:
    if not arg.startswith("@"):
        return [arg]
    out = []
    for line in Path(arg[1:]).read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("Deletes:").strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _pytest_cov(data: Path, targets: list[str], workers: int) -> None:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:homeassistant",
        "-p",
        "no:cacheprovider",
    ]
    if workers > 1:
        cmd += ["-n", str(workers)]
    cmd += [f"--cov={SOURCE}", "--cov-branch", "--cov-report=", *targets]
    env = {**os.environ, "COVERAGE_FILE": str(data)}
    subprocess.run(cmd, cwd=ROOT, env=env, check=False, stdout=subprocess.DEVNULL)


def _golden_cov(data_dir: Path) -> list[Path]:
    """Each golden scenario in its own interpreter, as the harness runs it."""
    sys.path.insert(0, str(ROOT / "tests"))
    from golden.harness import child_env
    from golden.scenarios import get, names

    files = []
    for name in names():
        with tempfile.TemporaryDirectory() as tmp:
            env = child_env(Path(tmp))
            env["COVERAGE_FILE"] = str(data_dir / f".coverage.golden.{name}")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    f"--source={SOURCE}",
                    "-m",
                    "golden.child",
                    name,
                    str(Path(tmp) / "r.json"),
                    str(get(name).cycles),
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                timeout=1800,
            )
        files.append(data_dir / f".coverage.golden.{name}")
    return [f for f in files if f.exists()]


def _lines(data: Path) -> dict[str, set[int]]:
    import coverage

    cov = coverage.CoverageData(str(data))
    cov.read()
    out = {}
    for path in cov.measured_files():
        key = path.replace("\\", "/").split(f"{SOURCE}/")[-1]
        out[key] = set(cov.lines(path) or [])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--before", required=True, help="a commit where the retired files still exist"
    )
    ap.add_argument("--retired", required=True, help="a path, or @file listing paths")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json")
    a = ap.parse_args()

    retired = _paths(a.retired)
    clashes = [p for p in retired if (ROOT / p).exists()]
    if clashes:
        print(
            json.dumps({"error": "still present at the current tree", "paths": clashes})
        )
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        survivors = work / ".coverage.survivors"
        _pytest_cov(
            survivors,
            ["tests/", "--ignore=tests/hass_integration/", "-m", "not slow_gate"],
            a.workers,
        )
        import coverage

        combined = work / ".coverage.kept"
        parts = [survivors, *_golden_cov(work)]
        merged = coverage.CoverageData(str(combined))
        for part in parts:
            if part.exists():
                one = coverage.CoverageData(str(part))
                one.read()
                merged.update(one)
        merged.write()

        written = []
        try:
            for p in retired:
                body = subprocess.run(
                    ["git", "show", f"{a.before}:{p}"],
                    cwd=ROOT,
                    capture_output=True,
                    check=True,
                ).stdout
                (ROOT / p).write_bytes(body)
                written.append(p)
            old = work / ".coverage.retired"
            _pytest_cov(
                old, [p for p in retired if Path(p).name.startswith("test_")], a.workers
            )
        finally:
            for p in written:
                (ROOT / p).unlink()

        kept, gone = _lines(combined), _lines(old)
    lost = {m: sorted(gone[m] - kept.get(m, set())) for m in sorted(gone)}
    lost = {m: v for m, v in lost.items() if v}
    result = {
        "before": a.before,
        "retired_files": len(retired),
        "lost_line_count": sum(len(v) for v in lost.values()),
        "lost_lines": lost,
    }
    print(json.dumps({k: v for k, v in result.items() if k != "lost_lines"}))
    for m, v in lost.items():
        print(f"{m}: {v}")
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
