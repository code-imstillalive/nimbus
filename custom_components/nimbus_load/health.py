"""Always-on error/warning capture for Nimbus's own health report
(2026-08-25, direct ask: "at all times log any errors from nimbus - in
full and extra detailed diagnostics file... i wanna know what fails and
what flatlines and what is not running").

Real gap this closes: every diagnosis this project has done up to now
(the whole #100-#118 audit thread) relied on someone noticing a symptom
and THEN going and grepping HA's own error_log for it after the fact --
there was no standing, always-on record of Nimbus's own WARNING/ERROR
log lines, independent of whether anyone happened to be watching when
they fired. A plain Python `logging.Handler`, attached once to this
integration's own logger namespace, is the correct mechanism for
exactly this: HA's core logging already calls every attached handler
for every record that logger emits, so this needs no polling, no
periodic scrape, and can never miss a record between checks the way a
periodic error_log search inherently can.

Deliberately a bounded in-memory ring buffer, not a file -- Nimbus has
no filesystem access of its own (same "REST/native dual-mode, zero
direct file I/O" discipline solver_writer.py's own module docstring
already commits to), and HA's own error_log already persists to disk
for anyone who needs that. This buffer's own job is narrower and
complementary: a genuinely CURATED, Nimbus-only, always-populated view
a health-report sensor can expose directly as entity attributes --
readable via the plain REST API/diagnostics download like everything
else in this project, no separate file access needed.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime

# 200 entries is deliberately generous relative to what a real health
# check needs (recent_errors/recent_warnings below only ever surface
# the newest handful) -- see get_recent_log_entries()'s own default
# limit. Bounded so a genuinely noisy failure mode (e.g. a coordinator
# erroring every tick) can never grow this without bound.
_BUFFER_MAXLEN = 200
_LOG_BUFFER: deque[dict] = deque(maxlen=_BUFFER_MAXLEN)

_LOGGER_NAMESPACE = "custom_components.nimbus_load"

# Idempotency guard -- async_setup_entry can legitimately run more than
# once per process (a reload after every subentry add/edit/remove, see
# __init__.py's own _async_update_listener), and a logging.Handler has
# no built-in "already attached" check of its own. Without this guard,
# every reload would attach a NEW handler on top of the last one,
# double-, triple-, quadruple-counting every future log line.
_handler_installed = False


class NimbusLogBufferHandler(logging.Handler):
    """Appends every WARNING+ record from this integration's own logger
    namespace into the shared ring buffer, as a plain, REST-serialisable
    dict -- never the raw LogRecord (not JSON-safe, and holds onto
    things like exc_info that shouldn't leak into a sensor attribute).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) if self.formatter else record.getMessage()
        except Exception:  # noqa: BLE001 -- a broken log call must never break logging itself
            message = record.msg if isinstance(record.msg, str) else "<unformattable>"
        _LOG_BUFFER.append(
            {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message,
            }
        )


def install_log_buffer_handler() -> None:
    """Attach NimbusLogBufferHandler to this integration's own logger
    namespace, exactly once per process regardless of how many times
    this is called (see _handler_installed's own docstring above).
    Captures WARNING and above -- INFO/DEBUG noise (routine "solved
    optimally" lines, etc.) would drown out the genuinely actionable
    entries this buffer exists to surface.
    """
    global _handler_installed
    if _handler_installed:
        return
    handler = NimbusLogBufferHandler(level=logging.WARNING)
    logging.getLogger(_LOGGER_NAMESPACE).addHandler(handler)
    _handler_installed = True


def get_recent_log_entries(
    min_level: int = logging.WARNING, limit: int = 20
) -> list[dict]:
    """Newest-first copies of buffered log entries at or above
    min_level, capped at `limit` -- callers (the health-report sensor)
    should never receive the raw internal deque directly, since it's a
    live, mutating, process-wide singleton."""
    matching = [entry for entry in _LOG_BUFFER if entry["levelno"] >= min_level]
    return list(reversed(matching))[:limit]


def count_recent_log_entries(min_level: int = logging.ERROR) -> int:
    """Count of ALL currently-buffered entries at or above min_level --
    unlike get_recent_log_entries(), never truncated by a display limit,
    so this is the right call for a plain native_value headline count."""
    return sum(1 for entry in _LOG_BUFFER if entry["levelno"] >= min_level)


def reset_log_buffer_for_tests() -> None:
    """Test-only helper -- the buffer is a module-level singleton shared
    across the whole process, so a test suite that doesn't clear it
    between cases would see cross-test contamination."""
    _LOG_BUFFER.clear()
