"""A module's own view of `time`, with a monotonic clock the test moves.

Patch the module's `time` name with this, never `time.monotonic` itself:
`module.time` IS the global `time` module, and asyncio's event loop reads
`time.monotonic` too. A frozen loop clock never lets a timer fall due on
Linux, where the clock resolves to nanoseconds, so `call_later` and
`wait_for` spin forever; on Windows the 15.6 ms resolution hides it. That
hung CI for #1837.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class ModuleTime:
    """`time`, except `monotonic()`, which reads `clock`."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock()

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)
