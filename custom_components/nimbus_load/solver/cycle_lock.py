"""Mutual exclusion between overlapping solve cycles.

Extracted from `solver_writer.py` by nimbus #1306 (spec 007, Phase 7a). The two
mechanisms and every comment justifying them are the originals, unedited --
including the #757 reasoning for why a PID file and a `threading.Lock` are both
required and neither substitutes for the other.

## Why the path is a parameter rather than a module constant

Spec 007 step 2 says to move `LOCK_PATH` here verbatim alongside the functions.
It stays in `solver_writer.py` instead, and the functions take it, because the
tests rebind it in four places and **not one of them is visible to a
literal-name scan**:

| site | how |
|---|---|
| `tests/_isolated_state.py:74` | `patch.object(solver_writer, const, ...)`, `const` from a tuple of strings |
| `tests/hass_integration/conftest.py:85` | same shape, its own tuple |
| `tests/test_solve_overlap_guard.py:63` | `solver_writer.LOCK_PATH = ...`, direct assignment |
| `tests/test_solver_writer_lock_self_pid.py:37` | direct assignment |

Spec 007's own inventory table records `LOCK_PATH` as having **0 patch sites**
for exactly that reason -- the same blindness #1400 and #1401 record for
`_NATIVE_HASS`, which is why 7b is blocked on fixing it.

Had the constant moved, a rebind site missed in the sweep would keep writing to
`solver_writer.LOCK_PATH` while `acquire_lock` read this module's copy -- i.e.
`/opt/nimbus_solver_forecast_writer.lock`, on a machine where `/opt` exists.
That is precisely the #1330 defect class (tests silently writing production
state), which the suite built a guard against three phases ago. Keeping the
constant where the rebinds already point makes that failure structurally
impossible rather than merely swept-for.

It also puts the split in the right place: `os.environ.get(...)` is
configuration and belongs with the other three persisted paths in
`solver_writer.py`; the guard is mechanism. `solver/` reads no environment.

`solver_writer.acquire_lock`/`release_lock` are one-line wrappers passing
`LOCK_PATH`, so every existing caller and every existing rebind keeps working
untouched.
"""

from __future__ import annotations

import os
import threading

# nimbus issue #757: the process-local half of the overlap guard.
#
# LOCK_PATH below is a PID file, which is exactly right for the
# standalone/cron script -- two runs there genuinely are two
# processes. In native mode every solve runs in the SAME hass
# process, on different executor worker threads, so the PID in that
# file is always our own -- and acquire_lock()'s own #346 branch
# (`if old_pid == os.getpid()`) therefore reads a genuine concurrent
# solve as a stale file and hands out the lock. Measured directly
# against the real function before this existed:
#
#     thread A acquires: True
#     thread B acquires WHILE A HOLDS IT: True
#
# So in native mode the overlap guard had never refused anything,
# and solver_runtime.py's own #315 'previous cycle still in
# progress' WARNING was unreachable code. That is the concurrency
# half of #757: concurrent solves, each publishing over the last,
# which is why ten investigations of that issue disagreed with each
# other about whether a battery participant was being excluded.
#
# Both mechanisms are kept because they answer genuinely different
# questions and neither substitutes for the other. A PID file cannot
# see a sibling thread; a process-local lock cannot see a sibling
# process. #346's branch stays exactly as it was -- it is still the
# only thing that reclaims a lock file left behind by a worker
# thread killed mid-solve, which in a container frequently holds a
# PID identical to ours after a restart.
_IN_PROCESS_LOCK = threading.Lock()


def acquire_lock(lock_path: str) -> bool:
    """Two-mechanism overlap guard. Returns True (caller should
    proceed) if no other run is genuinely still active; False (caller
    should exit cleanly, no error) if one is.

    A process-local threading.Lock covers a concurrent solve on
    another worker thread of THIS process (native mode -- see
    _IN_PROCESS_LOCK's own comment above for the #757 defect that
    exists to fix, and why a PID file structurally cannot see it).
    The PID file below covers a genuinely separate process (the
    standalone/cron script) -- 2026-08-17, see LOCK_PATH's own
    comment; makes a genuine 1-minute cron cadence safe against the
    real, measured 45-52s solve time without needing a slower, more
    conservative interval "just in case".

    Stale-lock safe: if LOCK_PATH exists but the PID inside it is no
    longer a real running process (a previous run crashed hard enough to
    skip its own cleanup, e.g. a killed container), os.kill(pid, 0)
    raises -- on real POSIX deploy targets specifically ProcessLookupError
    ("No such process"), confirmed via Python's own os.kill() docs; a
    real, live discrepancy found testing this same check on Windows
    (where a nonexistent PID instead raises a plain OSError, not that
    specific subclass) is exactly why this catches OSError broadly, not
    just the one POSIX-specific subclass -- ProcessLookupError/
    PermissionError are both already OSError subclasses, so this loses
    no real specificity, and stays correct regardless of which platform
    it happens to run on. ANY failure to positively confirm the old PID
    is a real, currently-running process is treated as "not actually
    locked" -- the stale file is overwritten with this run's own PID
    rather than ever permanently wedging every future run.
    """
    if not _IN_PROCESS_LOCK.acquire(blocking=False):
        # nimbus issue #757: a genuine concurrent solve on another
        # worker thread of THIS process -- the one case the PID file
        # below structurally cannot see. Non-blocking deliberately:
        # parking an HA executor worker for the whole of another
        # solve would be worse than the bug, and is exactly the
        # executor starvation #773 documents. The caller's contract
        # is unchanged -- False still means 'skip this tick
        # cleanly', and solver_runtime.py already logs it.
        return False
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            # nimbus issue #346 (Mark Purcell): in native mode this file
            # holds HA's OWN pid, not a genuinely separate process's --
            # `solver_runtime.py`'s own driver calls this in-process, on a
            # worker thread of the same `hass` process, every cycle. A
            # worker thread mid-LP-solve when HA is stopped/killed is not
            # guaranteed to reach this function's own `release_lock()`
            # (called from solver_runtime.py's `finally:`), so the file
            # can be left behind holding this same process's own PID. In
            # a Docker/HAOS container that PID is frequently identical
            # across restarts (PID 1, or close to it) -- without this
            # check, `os.kill(old_pid, 0)` genuinely succeeds (it's us),
            # every single tick returns False forever, and nothing ever
            # deletes the stale file on its own. A PID that IS our own
            # can never indicate a real overlapping run (we are, by
            # definition, not currently blocked acquiring this lock).
            if old_pid == os.getpid():
                pass  # stale file from an unclean stop -- safe to reclaim
            else:
                os.kill(old_pid, 0)  # raises if that PID isn't real; sends no signal
                # Hand the process-local lock straight back: this run
                # is not proceeding, and holding it would refuse every
                # FUTURE tick on this install forever, long after the
                # other process is gone (nimbus issue #757).
                _IN_PROCESS_LOCK.release()
                return False  # a genuine previous run is still alive
        except (ValueError, OSError):
            pass  # empty/corrupt/stale lock file, or a PID that's since exited -- safe to reclaim
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except OSError:
        pass  # already gone, or never created -- either way, nothing left to clean up
    finally:
        # nimbus issue #757: in a `finally:` so a failure to remove
        # the PID file can never strand the process-local lock and
        # wedge every subsequent solve. RuntimeError is the
        # already-unlocked case -- release_lock() is itself called
        # from a `finally:` and must never be the thing that raises.
        try:
            _IN_PROCESS_LOCK.release()
        except RuntimeError:
            pass
