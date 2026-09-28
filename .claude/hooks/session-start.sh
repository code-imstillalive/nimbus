#!/bin/bash
# SessionStart hook. Two jobs, in this order, and neither may ever block a
# session from starting.
#
#   1. PRINT DERIVED STATE, in every environment.
#   2. INSTALL TEST DEPENDENCIES, in a remote/web container only.
#
# WHY (1) EXISTS, which is the part worth reading before editing this file.
#
# This repo has tried to make a fresh session start from the truth three
# times, and the first two both failed:
#
#   - Prose. CLAUDE.md carries 12 directives across 1,393 lines. A paragraph
#     cannot fail, and the one directive that acquired a machine check (the
#     closing-keyword grep, after five recurrences) is the one that stopped
#     recurring.
#   - A read-first file. `docs/handover/*.md`, pointed at from CLAUDE.md as
#     "READ THIS FIRST if you are picking up cold". It fails twice over: it
#     is voluntary, AND it stores state, so it rots. The 2026-09-21 handover
#     states under a heading reading "verified live 13:05 AEST" that
#     production runs v0.94.413 and the newest tag is v0.94.415. By
#     2026-09-28 the tag was v0.94.427. A session that dutifully read it got
#     a confidently wrong answer carrying an authoritative label.
#
# So: state is never stored here, only COMPUTED, and the harness runs this
# rather than the agent choosing to. Those are the two independent failure
# modes above, closed together. Nothing in this file may cache a version, a
# date, or a machine's status -- if you find yourself writing a number into
# this script, that is the bug it exists to prevent.
#
# It also says plainly what it CANNOT know. What is actually running on
# NUC1/NUC2/devhub needs a live read, and no file in this repository can
# answer it. Printing "UNKNOWN" is the honest output and the whole reason the
# handover tables were wrong.

set -uo pipefail
# Deliberately NOT `set -e`. A session must start even if git is unavailable,
# the checkout is shallow, or python is missing. Every step below is
# individually tolerant and says so when it cannot answer.

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT" || exit 0

# Pick the NEWEST interpreter available, not the first `python3` on PATH.
# Found by validating this hook rather than by reading it: `python3` in this
# project's own remote container is 3.11.15, so a first-match pick failed the
# install outright with "Package 'nimbus-dev' requires a different Python".
_pick_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  return 1
}
PY="$(_pick_python || true)"
PY_MM=""
if [ -n "$PY" ]; then
  PY_MM="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
fi

echo "== Nimbus session start =================================================="

# --- 1. Derived state ------------------------------------------------------
# `--report` is a display, not a check: it always exits 0 so a session is
# never blocked by drift. The ENFORCING mode of the same script is for CI and
# for a human asking the question on purpose.
if [ -n "$PY" ] && [ -f tests/gates/release_drift.py ]; then
  "$PY" tests/gates/release_drift.py --report 2>/dev/null \
    || echo "  (release state unavailable -- tests/gates/release_drift.py did not run)"
else
  echo "  (release state unavailable -- no python on PATH)"
fi

# Working-tree facts, each one derived. Branch and dirtiness are the two
# things most often assumed from earlier in a conversation and then wrong,
# and a wrong branch is how an earlier session committed onto local `main`
# and then reported a push that had not happened.
if command -v git >/dev/null 2>&1; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  echo "  checkout                 branch ${BRANCH}, ${DIRTY} uncommitted path(s)"
  if git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
    AHEAD="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
    BEHIND="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
    echo "  vs origin/main           ${AHEAD} ahead, ${BEHIND} behind"
    if [ "${BEHIND}" != "0" ] && [ "${BEHIND}" != "?" ]; then
      echo "    ^ re-read anything you 'know' about main before acting on it"
    fi
  fi
fi

# The one pointer worth printing, because the files it names are still
# committed and still read as authoritative.
echo "  docs/handover/*.md       historical. Their version tables were true"
echo "                           for about an hour. Do not read them as state."

# --- 2. Test dependencies, remote containers only --------------------------
# Measured need, not speculation: a cold container in this project cannot run
# the suite. Three separate missing-dependency walls were hit in one session
# -- pytest itself, then pytest-freezer (6 collection errors), then jsonschema
# (1 more). Each surfaced as a pytest collection error, which is exactly the
# shape that a comparison gate used to report as a clean pass (#1400).
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  echo "== local session: skipping dependency install ============================"
  exit 0
fi

echo "== remote session: installing test dependencies =========================="
# `pyproject.toml`'s [dev] extra is this project's single source of truth for
# the test dependency set (#69), and `pip install -e '.[dev]'` is the exact
# command CI's own Unit Tests job runs. Not `ci`-style pinning: the container
# state is cached after this hook completes, so the resolving install is paid
# once and reused.
if [ -z "$PY" ]; then
  echo "  WARNING: no python on PATH -- dependency install skipped"
  exit 0
fi

echo "  interpreter              ${PY} (${PY_MM:-unknown})"

# `pyproject.toml`'s [dev] extra is this project's single source of truth for
# the test dependency set (#69), and `pip install -e '.[dev]'` is the exact
# command CI's Unit Tests job runs. Keep using it: hand-listing the packages
# here would be a second, drifting copy of the set #69 created to stop.
#
# WHY --ignore-requires-python CAN BE CORRECT HERE, and it is the repo's own
# documented position rather than a convenience:
#
#   requires-python  = ">=3.14.4"   the HA RUNTIME this integration ships against
#   target-version   = "py312"      the lowest interpreter a CONTRIBUTOR may run
#
# `pyproject.toml`'s own comment block, and Mark Purcell closing #358, say the
# mismatch is intentional: "`requires-python` describes the HA runtime this
# integration actually ships against; `target-version` here describes the
# lowest interpreter a CONTRIBUTOR may run locally to edit this code."
#
# So a 3.12/3.13 dev container is a SUPPORTED editing environment that the
# project-level metadata gate nonetheless refuses. Overriding that one gate is
# the documented intent; it is announced on the line below rather than done
# quietly, and it is never used below the py312 contributor floor.
PIP_ARGS=(-q -e '.[dev]')
case "$PY_MM" in
  3.14|3.15|3.16)
    : ;;
  3.12|3.13)
    PIP_ARGS=(-q --ignore-requires-python -e '.[dev]')
    echo "  note                     ${PY_MM} is below requires-python >=3.14.4 but at or"
    echo "                           above the py312 contributor floor (#358), so the"
    echo "                           project metadata gate is overridden deliberately."
    ;;
  *)
    echo "  WARNING: ${PY_MM:-this interpreter} is below the py312 contributor floor"
    echo "           documented in pyproject.toml (#358). Not attempting an install --"
    echo "           the suite needs 3.12 or newer."
    exit 0
    ;;
esac

# Output redirected: a failed [dev] attempt prints a wall of wheel-build
# errors, and this hook's whole value is a short readable report at session
# start. The fallback message below names the cause, and the comment there
# records it permanently, so nothing is lost by keeping stderr out of the way.
if "$PY" -m pip install "${PIP_ARGS[@]}" >/dev/null 2>&1; then
  echo "  ok: pip install -e '.[dev]' on ${PY} -- full suite available"
  exit 0
fi

# FALLBACK: the stub suite only.
#
# Measured, not assumed: the full [dev] extra cannot install in this
# project's own remote container on any interpreter it has. 3.14 is absent,
# and on 3.13 `pytest-homeassistant-custom-component` drags in the HA core
# tree, two of whose transitive deps (mock-open, PyRIC) fail to BUILD --
# `AttributeError: install_layout` out of the distro's patched setuptools.
# That is an environment limitation this hook cannot fix.
#
# What it can do is get the FAST suite runnable, which is the one CI runs
# first and the only one that ever ran in this container:
#   pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant
#
# The package list is DERIVED from pyproject.toml, never hand-written. #69
# made the [dev] extra the single source of truth for the test dependency
# set, and a hand-listed copy here would be exactly the drifting second copy
# that decision exists to prevent. Only the two HA-harness packages are
# excluded, by name and with the reason attached.
#
# No project install is needed for this path: the stub tests reach the
# package through tests/_solver_path.py and tests/_ha_stubs.py, not through
# an installed distribution.
echo "  full [dev] install failed -- falling back to the stub-suite subset"
STUB_DEPS="$("$PY" - <<'PYEOF' 2>/dev/null
import pathlib
import tomllib

# The two packages that pull the real Home Assistant core tree. Excluded
# here and nowhere else; everything else in [dev] is taken as written.
HA_HARNESS = {"pytest-homeassistant-custom-component", "home-assistant-frontend"}


def name_of(spec: str) -> str:
    for sep in ("==", ">=", "<=", "~=", ">", "<", "!=", "["):
        spec = spec.split(sep)[0]
    return spec.strip().lower()


data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
project = data["project"]
specs = list(project.get("dependencies", []))
specs += list(project.get("optional-dependencies", {}).get("dev", []))
print(" ".join(s for s in specs if name_of(s) not in HA_HARNESS))
PYEOF
)"

if [ -z "$STUB_DEPS" ]; then
  echo "  WARNING: could not derive the stub dependency set from pyproject.toml."
  echo "           No install attempted. Run 'pip install -e .[dev]' by hand to"
  echo "           see pip's own error -- it is suppressed here so a session"
  echo "           start is never buried in install output."
  exit 0
fi

# shellcheck disable=SC2086 -- intentional word splitting: STUB_DEPS is a
# space-separated list of requirement specifiers, each one a single word.
if "$PY" -m pip install -q $STUB_DEPS >/dev/null 2>&1; then
  echo "  ok: stub-suite dependencies installed on ${PY}"
  echo "      run: ${PY} -m pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant"
  echo "      NOT available: tests/hass_integration/ (needs the real HA harness,"
  echo "      which cannot build in this container -- see the comment in this hook)"
else
  echo "  WARNING: even the stub-suite subset failed to install on ${PY}."
  echo "           Run 'pip install -e .[dev]' by hand to see pip's own error;"
  echo "           it is suppressed here so a session start is never buried in"
  echo "           install output."
fi

exit 0
