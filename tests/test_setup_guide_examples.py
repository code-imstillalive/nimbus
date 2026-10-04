"""Exercise the guide's literal examples, without issuing HA actions.

HA's state and clock functions are represented by small read-only fixtures.
These tests validate template branches and card structure, not a live HA UI.
"""

import math
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "setup-guide.md"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
PLAN = "sensor.nimbus_solver_battery_forecast"
HEALTH = "sensor.nimbus_health_report"


def _blocks(language):
    return re.findall(
        rf"```{language}\n(.*?)\n```", GUIDE.read_text(encoding="utf-8"), re.DOTALL
    )


def _as_timestamp(value, default=None):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return default


def _is_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _render(rows, health=None):
    blocks = _blocks("jinja")
    assert len(blocks) == 1, "Keep one acceptance template, not divergent copies."
    attributes = {PLAN: {"forecast": rows}, HEALTH: health or {}}
    env = Environment(undefined=StrictUndefined)
    return env.from_string(blocks[0]).render(
        states=lambda entity: "unknown",
        state_attr=lambda entity, key: attributes.get(entity, {}).get(key),
        as_timestamp=_as_timestamp,
        is_number=_is_number,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        {},
        "unknown",
        [None],
        [{}],
        [{"time": "invalid", "hours": 1}],
        [{"time": "2026-10-04T12:00:00+00:00", "hours": "invalid"}],
        [{"time": "2026-10-04T12:00:00+00:00", "hours": 0}],
        [{"time": "2026-10-04T12:00:00+00:00", "hours": -1}],
        [{"time": "2026-10-04T12:00:00+00:00", "hours": float("nan")}],
        [{"time": "2026-10-04T12:00:00+00:00", "hours": float("inf")}],
        [{"time": float("nan"), "hours": 1}],
        [{"time": float("inf"), "hours": 1}],
    ],
)
def test_missing_or_invalid_period_never_claims_current(rows):
    output = _render(rows)
    assert "Period check: missing or invalid period" in output
    assert "covers now" not in output


@pytest.mark.parametrize(
    ("start", "hours", "expected"),
    [
        ("2026-10-04T11:00:00+00:00", 1, "expired period"),
        ("2026-10-04T12:05:00+00:00", 1, "future period"),
        ("2026-10-04T12:00:00+00:00", 0.0833, "covers now"),
        ("2026-10-04T21:55:00+10:00", 0.25, "covers now"),
    ],
)
def test_period_boundaries_and_timezone(start, hours, expected):
    assert f"Period check: {expected}" in _render([{"time": start, "hours": hours}])


def test_health_diagnostics_do_not_hide_empty_or_missing_values():
    empty = _render([], {"recent_errors": [], "never_trained": []})
    missing = _render([])
    warning = _render([], {"recent_warnings": ["example warning"]})
    assert "Recent errors: []" in empty
    assert "Never trained: []" in empty
    assert "Recent errors: None" in missing
    assert "example warning" in warning


def test_flat_price_templates_render_dollars_not_cents():
    text = GUIDE.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| State |"))
    templates = re.findall(r"`([^`]+)`", row)
    assert len(templates) == 2
    values = [float(Environment().from_string(t).render()) for t in templates]
    assert values == [0.30, 0.05]


def test_first_card_has_only_sensors_and_no_control_helpers():
    blocks = _blocks("yaml")
    assert len(blocks) == 1
    card = yaml.safe_load(blocks[0])
    assert card["type"] == "entities"
    assert card["show_header_toggle"] is False
    assert len(card["entities"]) >= 4
    assert all(entity.startswith("sensor.nimbus_") for entity in card["entities"])
    assert "armed_entity" not in card
    assert "mode_select_entity" not in card
