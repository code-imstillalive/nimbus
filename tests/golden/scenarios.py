"""Golden-master scenarios: fixed Home Assistant inputs for ``main()``.

Spec 000 Part A (synthetic tier) and Part D (real NEMWEB market inputs).
Each scenario is a frozen instant plus the complete set of entity states
and recorded history the fake Home Assistant serves. Anything a scenario
does not list answers 404, exactly as a missing entity does live; the
snapshot records which entities ``main()`` asked for, so a refactor that
starts reading a new entity is visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from golden.fake_ha import FakeHA


@dataclass(frozen=True)
class Scenario:
    name: str
    instant: str  # ISO 8601, UTC offset explicit
    build: Callable[[], FakeHA]
    purpose: str
    cycle_minutes: int = 5
    cycles: int = 1
    # nimbus issue #1335: a native-mode scenario. When set, the child
    # builds a hass from this scenario's own FakeHA and injects it via
    # set_native_hass() before the first cycle, so main() takes the
    # in-process branch of every seam -- the only way to reach
    # build_controllable_loads()/apply_commanded_state_guard(), which
    # return early on `_NATIVE_HASS is None`. None (the default) is the
    # standalone/REST path every Part A and Part D scenario uses.
    native: Callable[[FakeHA], Any] | None = None

    def instant_for_cycle(self, i: int) -> str:
        t = datetime.fromisoformat(self.instant) + timedelta(
            minutes=self.cycle_minutes * i
        )
        return t.isoformat()


_REGISTRY: dict[str, Scenario] = {}


def register(s: Scenario) -> Scenario:
    if s.name in _REGISTRY:
        raise ValueError(f"duplicate golden scenario {s.name!r}")
    _REGISTRY[s.name] = s
    return s


def get(name: str) -> Scenario:
    _load_all()
    return _REGISTRY[name]


def names() -> list[str]:
    _load_all()
    return sorted(_REGISTRY)


def _load_all() -> None:
    # Imported for their register() side effect.
    from golden import (  # noqa: F401
        scenarios_native,
        scenarios_nemweb,
        scenarios_synthetic,
    )
