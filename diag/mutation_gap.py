"""Diag only (#1837 follow-up, never merged): plant the same bug in the same
module of two checkouts, `new` (main's tests) and `old` (the tests before
#2084, same source), and record which suite catches it. The old suite runs
only when the new one misses the bug."""
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, "new/scripts")
import mutation_compare as mc  # noqa: E402
import random  # noqa: E402

module, chunk, per, total, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
SEED = 1837
def run(root, extra, timeout):
    cmd = [sys.executable, "-m", "pytest", "tests/", "--ignore=tests/hass_integration/", "-p", "no:homeassistant",
           "-x", "-q", "-n", "auto", "-p", "no:cacheprovider", "-o", "faulthandler_timeout=1500", *extra]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
        res = "survived" if r.returncode == 0 else "killed"
        tail = r.stdout.strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        res, tail = "timeout", []
    return res, round(time.monotonic() - t0), tail
OLDX = ["-m", "not slow_gate"]
original = Path("new", module).read_text(encoding="utf-8")
assert original == Path("old", module).read_text(encoding="utf-8"), "module differs"
sites = mc._sites(__import__("ast").parse(original), set())
chosen = random.Random(SEED).sample(sites, min(total, len(sites)))
rows = []
if chunk == 0:
    rows.append({"baseline_new": run("new", [], 3600), "baseline_old": run("old", OLDX, 5400)})
else:
    for n in range((chunk - 1) * per + 1, min(chunk * per, len(chosen)) + 1):
        index, kind = chosen[n - 1]
        try:
            mutated, what = mc._mutate(original, index)
        except (ValueError, SyntaxError) as e:
            rows.append({"mutant": n, "skipped": str(e)})
            continue
        for root in ("new", "old"):
            Path(root, module).write_text(mutated, encoding="utf-8")
        row = {"mutant": n, "kind": kind, "where": what}
        row["new"], row["new_s"], row["new_tail"] = run("new", [], 2400)
        if row["new"] == "survived":
            row["old"], row["old_s"], row["old_tail"] = run("old", OLDX, 4800)
        for root in ("new", "old"):
            Path(root, module).write_text(original, encoding="utf-8")
        rows.append(row)
        print(json.dumps(row), flush=True)
Path(out).write_text(json.dumps({"module": module, "chunk": chunk, "rows": rows}, indent=1), encoding="utf-8")
