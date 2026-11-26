"""The allowed-charge value moves in steps of at least 0.1 kW.

A tester, 26 Nov 2026: the value (rounded to 0.01 kW) changed on almost every
meter tick, and the dispatch woke on each change. It now keeps its last value
until the allowance moves by 0.1 kW or more, so the dispatch (which only acts
on a 0.5 kW change) wakes far less often. The sensor's live attributes are
still written every tick: the site import guard reads them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import site_limit


def test_a_small_move_keeps_the_last_value():
    assert site_limit.stepped_allowed_kw(12.0, 12.04) == 12.0
    assert site_limit.stepped_allowed_kw(12.0, 11.93) == 12.0


def test_a_move_of_a_step_or_more_takes_the_new_value():
    assert site_limit.stepped_allowed_kw(12.0, 12.1) == 12.1
    assert site_limit.stepped_allowed_kw(12.0, 9.456) == 9.46  # a kettle on


def test_no_previous_value_or_no_reading():
    assert site_limit.stepped_allowed_kw(None, 7.123) == 7.12
    assert site_limit.stepped_allowed_kw(12.0, None) is None
