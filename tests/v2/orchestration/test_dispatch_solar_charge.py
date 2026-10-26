"""Period 0's "charge from solar only" flag (nimbus #2066).

Reference household, 26 Oct 2026, 07:02-07:12: the plan charged 0.8-0.9 kW
while the sun gave 0.2-0.4 kW; the automation forced the charge, and all of
it was imported at 16.8 c. The flag tells a dispatcher when Self-Consume
does what the plan wants without that risk.
"""

import pytest
import solver_publish as publish

f = publish._dispatch_solar_charge


def test_a_charge_from_solar_with_nothing_sold_is_a_solar_charge():
    assert f(-0.8, 0.0, 0.0) is True


@pytest.mark.parametrize("battery_kw", [0.0, 0.04, -0.04, 1.5])
def test_idle_or_discharging_is_not(battery_kw):
    assert f(battery_kw, 0.0, 0.0) is False


def test_a_charge_from_the_grid_is_not():
    # The 09:00 cheap charge must stay a forced charge.
    assert f(-40.0, 38.0, 0.0) is False


def test_a_charge_while_selling_is_not():
    # Self-Consume would put the planned export into the battery instead.
    assert f(-0.8, 0.0, 2.0) is False


def test_noise_below_the_deadband_reads_as_none():
    assert f(-0.8, 0.04, 0.04) is True
