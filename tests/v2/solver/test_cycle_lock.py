"""One solve at a time (#757): a lock for threads in this process, and a
PID file for a separate cron process (#1837).

The lock never blocks, refuses an overlap without wedging, and reclaims a
PID file whose process is gone, is this process, or cannot be read.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from solver import cycle_lock


@pytest.fixture
def lock_path(tmp_path):
    path = str(tmp_path / "solver.lock")
    yield path
    for _ in range(4):  # the process-local lock is shared with the whole suite
        try:
            cycle_lock._IN_PROCESS_LOCK.release()
        except RuntimeError:
            break


def _on_another_thread(path):
    """acquire_lock on a second thread, which must answer at once: a
    blocking acquire would park an executor thread for a whole solve."""
    result = []
    thread = threading.Thread(
        target=lambda: result.append(cycle_lock.acquire_lock(path))
    )
    started = time.monotonic()
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive() and time.monotonic() - started < 1.0
    return result[0]


def _pid_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _pid_in(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_a_second_thread_is_refused_until_the_first_releases(lock_path):
    assert cycle_lock.acquire_lock(lock_path)
    assert _pid_in(lock_path) == str(os.getpid())
    assert not _on_another_thread(lock_path)
    cycle_lock.release_lock(lock_path)
    assert not os.path.exists(lock_path)
    assert _on_another_thread(lock_path)
    cycle_lock.release_lock(lock_path)
    for _ in range(5):  # one solve after another is unaffected
        assert cycle_lock.acquire_lock(lock_path)
        cycle_lock.release_lock(lock_path)


def test_exactly_one_of_many_simultaneous_threads_wins(lock_path):
    start = threading.Barrier(12)
    wins = []

    def race():
        start.wait()
        wins.append(cycle_lock.acquire_lock(lock_path))

    threads = [threading.Thread(target=race) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(wins) == [False] * 11 + [True]


def test_a_live_process_in_the_pid_file_blocks_and_the_thread_lock_is_let_go(
    lock_path, monkeypatch
):
    _pid_file(lock_path, str(os.getpid() + 1))
    monkeypatch.setattr(cycle_lock.os, "kill", lambda pid, sig: None)  # alive
    assert not cycle_lock.acquire_lock(lock_path)
    os.remove(lock_path)
    # The refusal released the process-local lock: nothing is wedged.
    assert _on_another_thread(lock_path)


def _dead(pid, sig):
    raise ProcessLookupError


@pytest.mark.parametrize(
    "content",
    ["{other}", "not a pid", ""],
    ids=["dead_process", "corrupt", "empty"],
)
def test_a_pid_file_nobody_is_using_is_reclaimed(lock_path, monkeypatch, content):
    _pid_file(lock_path, content.format(other=os.getpid() + 1))
    monkeypatch.setattr(cycle_lock.os, "kill", _dead)
    assert cycle_lock.acquire_lock(lock_path)
    assert _pid_in(lock_path) == str(os.getpid())


def test_a_stale_file_naming_this_very_process_is_reclaimed(lock_path):
    """Left by an unclean stop: this process is alive, so asking the OS
    whether that PID runs would say yes and wedge every solve after."""
    _pid_file(lock_path, str(os.getpid()))
    assert cycle_lock.acquire_lock(lock_path)


def test_releasing_what_was_never_taken_is_harmless(lock_path):
    cycle_lock.release_lock(lock_path)
    assert cycle_lock.acquire_lock(lock_path)


def test_the_writers_own_lock_is_the_one_at_its_lock_path(lock_path, monkeypatch):
    import solver_writer as sw

    monkeypatch.setattr(sw, "LOCK_PATH", lock_path)
    assert sw._IN_PROCESS_LOCK is cycle_lock._IN_PROCESS_LOCK
    assert sw.acquire_lock() and _pid_in(lock_path) == str(os.getpid())
    sw.release_lock()
    assert not os.path.exists(lock_path)
