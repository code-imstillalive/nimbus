"""Diag only: for one mutant the old suite killed and main's missed, name
the old tests that fail on it."""
import ast, random, re, subprocess, sys
from pathlib import Path
sys.path.insert(0, "new/scripts")
import mutation_compare as mc  # noqa: E402

module, n = sys.argv[1], int(sys.argv[2])
original = Path("old", module).read_text(encoding="utf-8")
sites = mc._sites(ast.parse(original), set())
chosen = random.Random(1837).sample(sites, min(24, len(sites)))
index, kind = chosen[n - 1]
mutated, what = mc._mutate(original, index)
print("MUTANT", module, n, kind, what, flush=True)
Path("old", module).write_text(mutated, encoding="utf-8")
r = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "--ignore=tests/hass_integration/", "-p", "no:homeassistant",
     "-q", "-n", "auto", "-p", "no:cacheprovider", "-m", "not slow_gate", "--maxfail=10", "-rf",
     "-o", "faulthandler_timeout=1500"],
    cwd="old", capture_output=True, text=True, timeout=5400)
for line in r.stdout.splitlines():
    if line.startswith("FAILED") or re.search(r"\d+ (failed|passed)", line):
        print(line, flush=True)
