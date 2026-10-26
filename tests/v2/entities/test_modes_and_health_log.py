"""Household modes (what each one changes, never below zero, the thermal
band kept consistent) and the always-on log buffer behind the health
report (#1837).

Covers what lived in the household-mode, thermal band and health-log files.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

hm = importlib.import_module("custom_components.nimbus_load.household_modes")
health = importlib.import_module("custom_components.nimbus_load.health")

# --- household modes ----------------------------------------------------------------


# --- the health log buffer ------------------------------------------------------------


@pytest.fixture
def buffer():
    health.reset_log_buffer_for_tests()
    health.install_log_buffer_handler()
    health.install_log_buffer_handler()  # once per process however often it is asked
    yield logging.getLogger("custom_components.nimbus_load.test")
    health.reset_log_buffer_for_tests()


def test_the_buffer_is_bounded_and_a_broken_message_is_still_kept(buffer):
    for i in range(health._BUFFER_MAXLEN + 5):
        buffer.warning("w%d", i)
    assert len(health.get_recent_log_entries(limit=10_000)) == health._BUFFER_MAXLEN
    assert (
        health.get_recent_log_entries(limit=1)[0]["message"]
        == f"w{health._BUFFER_MAXLEN + 4}"
    )
    # A logging bug in the caller: the record cannot be formatted.
    record = logging.LogRecord(
        "custom_components.nimbus_load.x",
        logging.WARNING,
        __file__,
        1,
        "needs %d args %d",
        (1,),
        None,
    )
    health.NimbusLogBufferHandler().emit(record)
    assert health.get_recent_log_entries(limit=1)[0]["message"] == "needs %d args %d"
