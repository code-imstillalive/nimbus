"""The new-install wizard's questions (DC-007 slice 1): one per thing discovery could not decide, the
battery capacity among them, each answer kept for the summary (#2393: closing config_flow.py's
coverage gap, one behaviour per test).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.config_flow import NimbusConfigFlow, _askable

SEVERAL = {
    "code": "several_sources",
    "device": "site",
    "role": "grid_power",
    "candidates": [{"source": "sensor.meter_a"}, {"source": "sensor.meter_b"}],
}
CAPACITY = {"code": "fact_needed", "device": "battery", "fact": "capacity"}


def _flow(*questions) -> NimbusConfigFlow:
    flow = NimbusConfigFlow.__new__(NimbusConfigFlow)
    flow.hass = MagicMock()
    flow._proposal = SimpleNamespace(questions=list(questions))
    flow._questions = list(range(len(questions)))
    flow._question_index = 0
    flow._answers = {}
    flow._devices = [
        {"id": "battery", "name": "Home battery"},
        {"id": "site", "name": "Site"},
    ]
    flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
    flow.async_step_finish = AsyncMock(return_value="summary")
    return flow


def test_each_question_is_asked_in_turn_and_its_answer_kept():
    # test_each_question_is_asked_in_turn_and_its_answer_kept
    flow = _flow(SEVERAL, CAPACITY)
    form = asyncio.run(flow.async_step_question(None))
    assert form["step_id"] == "question"
    assert form["description_placeholders"] == {"name": "Site", "role": "grid_power"}
    choices = [
        o["value"]
        for o in form["data_schema"]
        .schema[next(iter(form["data_schema"].schema))]
        .config["options"]
    ]
    assert choices == ["sensor.meter_a", "sensor.meter_b", "skip"]
    form = asyncio.run(flow.async_step_question({"source": "sensor.meter_b"}))
    assert form["step_id"] == "capacity"  # the battery's capacity has its own fields
    assert form["description_placeholders"] == {
        "name": "Home battery",
        "role": "capacity",
    }
    done = asyncio.run(
        flow.async_step_capacity({"capacity_kwh": 13.5, "basis": "nominal"})
    )
    assert done == "summary"
    assert flow._answers == {
        0: "sensor.meter_b",
        1: {"value": 13.5, "basis": "nominal"},
    }


def test_leaving_a_question_as_proposed_and_an_empty_capacity_record_nothing():
    # test_leaving_a_question_as_proposed_and_an_empty_capacity_record_nothing
    flow = _flow(SEVERAL, CAPACITY)
    asyncio.run(flow.async_step_question({"source": "skip"}))
    asyncio.run(flow.async_step_capacity({"basis": "usable"}))
    assert flow._answers == {0: None}


@pytest.mark.parametrize(
    ("question", "asked"),
    [
        (SEVERAL, True),
        ({**SEVERAL, "candidates": []}, False),  # nothing to choose between
        (
            {"code": "confirm_history_match", "candidates": [{"source": "sensor.x"}]},
            True,
        ),
        (CAPACITY, True),
        (
            {"code": "fact_needed", "fact": "rated_power"},
            False,
        ),  # left for Configure -> Device setup
        ({"code": "unit_unknown", "candidates": [{"source": "sensor.x"}]}, False),
    ],
)
def test_only_the_questions_the_wizard_can_answer_are_asked(question, asked):
    # test_only_the_questions_the_wizard_can_answer_are_asked
    assert _askable(question) is asked
