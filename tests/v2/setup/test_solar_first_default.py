"""A new install that already has Solcast starts with solar auto-include on; an
existing switch keeps its value (#1930, #1994) (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load.const import (
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_DISPATCH_DRY_RUN,
)
from custom_components.nimbus_load.switch import fresh_install_default


def _found(*entities):
    return lambda: list(entities)


def test_solcast_present_starts_it_on():
    assert fresh_install_default(
        CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
        _found("sensor.bumbum_solbum_today"),
        False,
    )


def test_no_solcast_keeps_the_class_default():
    assert not fresh_install_default(
        CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR, _found(), False
    )


def test_other_switches_keep_their_default():
    assert not fresh_install_default(
        CONF_SOLVER_DISPATCH_DRY_RUN, _found("sensor.bumbum_solbum_today"), False
    )


def test_a_failing_lookup_keeps_the_class_default():
    def boom():
        raise RuntimeError("not ready")

    assert not fresh_install_default(CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR, boom, False)
