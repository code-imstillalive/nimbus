"""nimbus issue #479 (sub-issue 3/10 of the controllable-loads spec, #476):
per-load run-state -- currently_on/on_since/off_since/delivered_today_kwh/
carry_kwh/day_key -- the shared foundation #484 (monitoring's chatter-guard,
which needs persisted commanded_state/commanded_since to hysteresis-guard
relay calls across 5-minute re-solves) and #480 (early completion) both need
before they can be built correctly, plus #479's own daily quota carry/
rollover math ("pool pump 4h/day; missed hours roll into tomorrow").

Scope, honestly: this module is the state store and the pure rollover math
(tested against #479's own synthetic 3-day acceptance scenario), wired to
update from each Controllable Load's own (optional) real power sensor every
solve tick -- for EVERY configured kind, not just quota, since #484's
chatter-guard needs currently_on/on_since/off_since regardless of kind. It
does NOT add a new selectable "quota" wizard kind (#486 already reserved the
name in the data model without exposing it -- see docs/controllable-loads.md
-- and that stays true here); until a load can actually be configured as
quota-kind, effective_target_kwh()/remaining_kwh() have nothing real to
attach to yet, but are ready, tested, and waiting for #479's own wizard
follow-up.

Pure functions only below the two dataclasses -- no HA imports at module
level, so this resolves the same whether imported as part of the real
package (native mode) or as a bare top-level module (this project's own
test harness, and the standalone/cron deployment's own import shape,
even though LoadRunStateStore itself is never exercised there -- there is
no ConfigSubentries concept in standalone mode, same reasoning as
solver_writer.py's own build_controllable_loads()).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

# A load reading below this is "off" for on_since/off_since transition
# purposes -- real sensors rarely settle at an exact 0.0 (standby draw,
# CT-clamp noise), so a hard >0 threshold would flap constantly.
DEFAULT_ON_THRESHOLD_KW = 0.05

# Caps how large a single sample's elapsed-time*power energy credit can be,
# so a restart or a long sensor-unavailable gap (last_sample_at from hours
# ago) doesn't spuriously credit delivered_today_kwh with power*(a huge
# gap) that never actually happened. A normal solve tick is ~5 min; 1h is a
# generous margin for a missed tick or two, not an invitation to guess.
MAX_SAMPLE_GAP_HOURS = 1.0


@dataclass(frozen=True)
class LoadRunState:
    """One Controllable Load's own persisted run-state. Every field
    defaults to a genuine "never sampled yet" value -- LoadRunStateStore.
    async_read() returns exactly this for a load it has no entry for."""

    currently_on: bool = False
    on_since: float | None = None  # epoch seconds
    off_since: float | None = None  # epoch seconds
    delivered_today_kwh: float = 0.0
    carry_kwh: float = 0.0
    day_key: str = ""  # "" = never sampled; see apply_power_sample's own
    # day-key-change check, which treats "" as "no real yesterday to roll
    # from" rather than crediting a fake day-zero rollover.
    last_sample_at: float | None = None  # epoch seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "currently_on": self.currently_on,
            "on_since": self.on_since,
            "off_since": self.off_since,
            "delivered_today_kwh": self.delivered_today_kwh,
            "carry_kwh": self.carry_kwh,
            "day_key": self.day_key,
            "last_sample_at": self.last_sample_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> LoadRunState:
        return LoadRunState(
            currently_on=bool(data.get("currently_on", False)),
            on_since=data.get("on_since"),
            off_since=data.get("off_since"),
            delivered_today_kwh=float(data.get("delivered_today_kwh", 0.0)),
            carry_kwh=float(data.get("carry_kwh", 0.0)),
            day_key=str(data.get("day_key", "")),
            last_sample_at=data.get("last_sample_at"),
        )


def compute_rollover(
    *,
    quota_kwh_per_day: float,
    carry_prev_kwh: float,
    delivered_yesterday_kwh: float,
    carry_cap_kwh: float,
) -> float:
    """#479's own rollover formula, run once at each local-midnight
    day_key change: carry = min(max(0, quota + carry_prev - delivered),
    carry_cap). A day that fell short of quota rolls the shortfall forward
    as extra carry (up to the cap); a day that met or exceeded quota drains
    previously-owed carry instead of adding more, and never goes negative
    (a day that overshoots quota is not a debt against tomorrow)."""
    raw = quota_kwh_per_day + carry_prev_kwh - delivered_yesterday_kwh
    return min(max(0.0, raw), carry_cap_kwh)


def effective_target_kwh(
    *,
    quota_kwh_per_day: float,
    carry_kwh: float,
    max_per_day_kwh: float | None,
) -> float:
    """Today's real target the LP should see: quota plus whatever carried
    in, capped by max_per_day_kwh if the household set one (a day with
    max_per_day reached carries the remainder again rather than dropping
    it -- #479's own acceptance criteria -- since the cap only limits
    TODAY's target, never the carry_kwh value itself)."""
    target = quota_kwh_per_day + carry_kwh
    if max_per_day_kwh is not None:
        target = min(target, max_per_day_kwh)
    return target


def remaining_kwh(*, target_kwh: float, delivered_today_kwh: float) -> float:
    """The EMHASS "remaining hours" idea, computed by Nimbus instead of
    asked of the user -- what's left to deliver today, never negative."""
    return max(0.0, target_kwh - delivered_today_kwh)


def apply_power_sample(
    state: LoadRunState,
    *,
    now: datetime,
    day_key: str,
    power_kw: float,
    on_threshold_kw: float = DEFAULT_ON_THRESHOLD_KW,
    quota_kwh_per_day: float | None = None,
    carry_cap_kwh: float = 0.0,
) -> LoadRunState:
    """One solve-tick update. Rolls the day over first if `day_key` has
    changed since the last sample -- applying compute_rollover() only when
    quota_kwh_per_day is given (None for a non-quota-kind load, which still
    gets its own delivered_today_kwh/on-off tracking reset daily for #484's
    benefit, but never accrues carry, since #486 has no quota kind to
    configure one against yet) and only when state.day_key is non-empty
    (a load's very first-ever sample has no real "yesterday" to roll from)
    -- then folds in this sample's on/off transition and energy delta."""
    now_ts = now.timestamp()
    if day_key != state.day_key:
        carry = (
            compute_rollover(
                quota_kwh_per_day=quota_kwh_per_day,
                carry_prev_kwh=state.carry_kwh,
                delivered_yesterday_kwh=state.delivered_today_kwh,
                carry_cap_kwh=carry_cap_kwh,
            )
            if quota_kwh_per_day is not None and state.day_key
            else 0.0
        )
        state = replace(
            state, delivered_today_kwh=0.0, carry_kwh=carry, day_key=day_key
        )

    is_on = power_kw > on_threshold_kw
    on_since = state.on_since
    off_since = state.off_since
    if is_on and not state.currently_on:
        on_since = now_ts
    elif not is_on and state.currently_on:
        off_since = now_ts

    delivered = state.delivered_today_kwh
    if state.last_sample_at is not None:
        dt_hours = (now_ts - state.last_sample_at) / 3600.0
        if 0.0 < dt_hours <= MAX_SAMPLE_GAP_HOURS:
            delivered += power_kw * dt_hours

    return replace(
        state,
        currently_on=is_on,
        on_since=on_since,
        off_since=off_since,
        delivered_today_kwh=delivered,
        last_sample_at=now_ts,
    )


@dataclass
class LoadRunStateStore:
    """One Store per hub -- every configured Controllable Load's own
    run-state lives in the SAME small JSON file (keyed by subentry_id),
    same reasoning as number.py's own _SharedNumberStore: independent
    per-load Store instances editing the same file would race a
    read-modify-write against each other if two loads are sampled back
    to back. `store` is a real homeassistant.helpers.storage.Store
    instance, passed in by the caller (native mode only -- see this
    module's own top docstring) rather than constructed here, so this
    file never has to import homeassistant.* at module level."""

    store: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def async_read(self, load_key: str) -> LoadRunState:
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 -- a corrupt/unreadable store file
            # must never block a solve cycle; this is a durability
            # BACKSTOP (same posture as _SharedNumberStore's own reads),
            # not a required dependency -- a fresh LoadRunState is a
            # perfectly safe "never sampled" starting point.
            return LoadRunState()
        if not data or load_key not in data:
            return LoadRunState()
        try:
            return LoadRunState.from_dict(data[load_key])
        except (TypeError, ValueError):
            return LoadRunState()

    async def async_write(self, load_key: str, state: LoadRunState) -> None:
        async with self.lock:
            try:
                data = await self.store.async_load() or {}
            except Exception:  # noqa: BLE001 -- same reasoning as async_read
                data = {}
            data[load_key] = state.to_dict()
            await self.store.async_save(data)
