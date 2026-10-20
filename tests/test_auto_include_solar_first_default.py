"""1.0.0 one-click install (#1930): a brand-new install that already has
Solcast starts with auto-include solar ON. Only the first default differs;
an existing switch keeps its value (it is restored before this is reached)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.nimbus_load.const import (
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_DISPATCH_DRY_RUN,
    SOLCAST_TODAY_ENTITY_ID,
)
from custom_components.nimbus_load.switch import fresh_install_default


def _states(*present):
    return lambda entity_id: object() if entity_id in present else None


def test_solcast_present_starts_it_on():
    assert fresh_install_default(
        CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR, _states(SOLCAST_TODAY_ENTITY_ID), False
    )


def test_no_solcast_keeps_the_class_default():
    assert not fresh_install_default(
        CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR, _states(), False
    )


def test_other_switches_keep_their_default():
    assert not fresh_install_default(
        CONF_SOLVER_DISPATCH_DRY_RUN, _states(SOLCAST_TODAY_ENTITY_ID), False
    )


def test_a_failing_lookup_keeps_the_class_default():
    def boom(_entity_id):
        raise RuntimeError("not ready")

    assert not fresh_install_default(CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR, boom, False)
