"""Where `decision_inputs.py`'s records live (nimbus #1657, gate 2).

## Layout

`.storage/nimbus_load_<entry_id>_decision_inputs/{head,horizon}/<YYYYMMDDHH>.json.gz`,
one gzip-JSON file per UTC hour per kind:

    {"records": [...], "configs": {"<fingerprint>": {...}}}

A record names its config by fingerprint. The config body is stored in the
same file the first time that fingerprint appears in it, so any one file can
be read on its own.

Hourly files rather than one ring-buffer file: the current hour is the only
file ever rewritten, so a write costs one hour of records rather than seven
days of them, and pruning is deleting whole files.

## Writes

Records are held in memory and the current hour's files are written at most
every `FLUSH_EVERY`, when the hour rolls over, and on unload. The write runs
in the executor (gzip of one hour of records), never on the event loop, and is
atomic (temp file, then replace), so a crash mid-write leaves the previous
file intact. **A restart can therefore lose up to `FLUSH_EVERY` of records**;
that is the price of not rewriting a file on every solve.

On first use after a start, this hour's files are read back before anything
is appended, so a restart inside an hour adds to it rather than replacing it.

## Failure posture

Same as `forecast_snapshot_store.py`: every failure is logged at debug and
swallowed. This is a diagnostic. A solve that succeeded is never reported as
failed because its inputs could not be written.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import decision_inputs as di
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FLUSH_EVERY = timedelta(minutes=5)
#: The published plan; the same rows the LP was handed back, not a re-derivation.
_PLAN_ENTITY = "sensor.nimbus_solver_battery_forecast"
_CONFIG_ENTITY = "sensor.nimbus_solver_config"
#: An export over a longer window would be a very large service response.
EXPORT_MAX_WINDOW = timedelta(hours=24)


@dataclass
class _Segment:
    key: str
    records: list[dict[str, Any]] = field(default_factory=list)
    configs: dict[str, Any] = field(default_factory=dict)
    loaded: bool = False


@dataclass
class _EntryState:
    root: Path
    open: dict[str, _Segment] = field(default_factory=dict)  # kind -> segment
    dirty: set[str] = field(default_factory=set)
    last_flush: datetime | None = None
    last_horizon_at: datetime | None = None
    previous_class: str | None = None
    last_published_at: str | None = None


_STATE: dict[str, _EntryState] = {}


def _root(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}_{entry_id}_decision_inputs"))


def _state(hass: HomeAssistant, entry_id: str) -> _EntryState:
    st = _STATE.get(entry_id)
    if st is None:
        st = _EntryState(root=_root(hass, entry_id))
        _STATE[entry_id] = st
    return st


def _path(root: Path, kind: str, key: str) -> Path:
    return root / kind / f"{key}.json.gz"


def _read_file(path: Path) -> dict[str, Any] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def _write_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(data, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def _list_keys(root: Path, kind: str) -> list[str]:
    folder = root / kind
    if not folder.is_dir():
        return []
    return sorted(p.name[: -len(".json.gz")] for p in folder.glob("*.json.gz"))


def _flush_and_prune(
    root: Path, segments: list[tuple[str, str, dict[str, Any]]], now: datetime
) -> None:
    """Executor job: write the given segments, then delete expired files."""
    for kind, key, data in segments:
        _write_file(_path(root, kind, key), data)
    for kind, keep in di.KEEP.items():
        for key in di.expired(_list_keys(root, kind), now, keep):
            try:
                _path(root, kind, key).unlink()
            except FileNotFoundError:
                pass


async def _segment_for(
    hass: HomeAssistant, st: _EntryState, kind: str, key: str
) -> _Segment:
    seg = st.open.get(kind)
    if seg is not None and seg.key == key:
        return seg
    seg = _Segment(key=key)
    existing = await hass.async_add_executor_job(_read_file, _path(st.root, kind, key))
    if existing:
        seg.records = list(existing.get("records") or [])
        seg.configs = dict(existing.get("configs") or {})
    seg.loaded = True
    st.open[kind] = seg
    return seg


async def async_capture(hass: HomeAssistant, entry_id: str) -> str | None:
    """File this solve's decision inputs. Returns the kind of the largest
    record written ("horizon" or "head"), or None if nothing was filed.
    Never raises."""
    try:
        return await _async_capture(hass, entry_id)
    except Exception:
        _LOGGER.debug("Nimbus: decision-input capture skipped", exc_info=True)
        return None


async def _async_capture(hass: HomeAssistant, entry_id: str) -> str | None:
    plan = hass.states.get(_PLAN_ENTITY)
    if plan is None:
        return None
    attrs = dict(plan.attributes)
    rows = attrs.get("forecast")
    if not isinstance(rows, list) or not rows:
        return None
    st = _state(hass, entry_id)
    # The solve's identity is when the plan entity was last WRITTEN, not its
    # `generated_at`: that is the plan's first period, rounded to the period
    # boundary, so every solve inside one 5-minute period shares it. HA only
    # moves `last_updated` when a write changes something, so an unchanged
    # republish is (correctly) not filed again.
    published_at = plan.last_updated.isoformat()
    if published_at == st.last_published_at:
        return None

    now = dt_util.utcnow()
    cfg_state = hass.states.get(_CONFIG_ENTITY)
    config = di.config_body(dict(cfg_state.attributes)) if cfg_state else {}
    fingerprint = di.config_fingerprint(config) if config else None
    sources = []
    for entity_id in di.source_entities(config):
        src = hass.states.get(entity_id)
        sources.append(
            di.source_line(
                entity_id,
                src.state if src else None,
                src.last_updated.isoformat() if src else None,
                dict(src.attributes) if src else None,
            )
        )

    try:
        battery_now = float(rows[0].get("battery_kw"))
    except (AttributeError, TypeError, ValueError):
        battery_now = None
    current_class = di.dispatch_class(battery_now)
    reason = di.horizon_reason(
        st.last_horizon_at, now, st.previous_class, current_class
    )
    kind = di.KIND_HORIZON if reason else di.KIND_HEAD

    record = di.build_record(
        attrs,
        captured_at=now,
        published_at=published_at,
        kind=kind,
        config_hash=fingerprint,
        sources=sources,
        reason=reason,
    )
    key = di.segment_key(now)
    # An hour has ended: write what it holds BEFORE opening the new one, or
    # replacing the open segment would drop its unwritten records.
    if any(s.key != key for s in st.open.values()):
        await async_flush(hass, entry_id)
        for k in [k for k, s in st.open.items() if s.key != key]:
            del st.open[k]
    # A full horizon makes the head redundant for the same solve, so it is
    # filed only under `horizon`; `async_export` trims it back to a head when
    # only heads are asked for, so every solve still appears.
    seg = await _segment_for(hass, st, kind, key)
    seg.records.append(record)
    if fingerprint and fingerprint not in seg.configs:
        seg.configs[fingerprint] = config
    st.dirty.add(kind)
    if reason:
        st.last_horizon_at = now
    st.previous_class = current_class
    st.last_published_at = published_at

    if st.last_flush is None or now - st.last_flush >= FLUSH_EVERY:
        await async_flush(hass, entry_id)
    return kind


async def async_flush(hass: HomeAssistant, entry_id: str) -> None:
    """Write any unwritten records now. Never raises."""
    st = _STATE.get(entry_id)
    if st is None:
        return
    try:
        segments = [
            (kind, seg.key, {"records": seg.records, "configs": seg.configs})
            for kind, seg in st.open.items()
            if kind in st.dirty
        ]
        now = dt_util.utcnow()
        await hass.async_add_executor_job(_flush_and_prune, st.root, segments, now)
        st.dirty.clear()
        st.last_flush = now
    except Exception:
        _LOGGER.debug("Nimbus: decision-input flush failed", exc_info=True)


def _read_window(
    root: Path, kinds: list[str], start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    configs: dict[str, Any] = {}
    for kind in kinds:
        for key in di.segments_overlapping(_list_keys(root, kind), start, end):
            data = _read_file(_path(root, kind, key)) or {}
            configs.update(data.get("configs") or {})
            records.extend(
                r for r in data.get("records") or [] if di.in_window(r, start, end)
            )
    return records, configs


async def async_export(
    hass: HomeAssistant,
    entry_id: str,
    start: datetime,
    end: datetime,
    kinds: list[str],
) -> dict[str, Any]:
    """Every stored record in [start, end) of the given kinds, oldest first,
    with the config bodies they reference. Flushes first, so the answer
    includes the last few minutes."""
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > EXPORT_MAX_WINDOW:
        raise ValueError(
            f"window is {end - start}; at most {EXPORT_MAX_WINDOW} per call"
        )
    await async_flush(hass, entry_id)
    st = _state(hass, entry_id)
    records, configs = await hass.async_add_executor_job(
        _read_window, st.root, [di.KIND_HEAD, di.KIND_HORIZON], start, end
    )
    if di.KIND_HORIZON not in kinds:
        records = [di.as_head(r) for r in records]
    elif di.KIND_HEAD not in kinds:
        records = [r for r in records if r.get("kind") == di.KIND_HORIZON]
    records.sort(key=lambda r: str(r.get("captured_at")))
    used = {r.get("config") for r in records}
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "kinds": kinds,
        "count": len(records),
        "records": records,
        "configs": {k: v for k, v in configs.items() if k in used},
        "retention": {k: str(v) for k, v in di.KEEP.items()},
    }


def reset_decision_inputs_cache() -> None:
    """Unload/test hook: per-entry module state must not outlive the entry.
    Unflushed records are lost; `async_flush` runs on unload first."""
    _STATE.clear()
