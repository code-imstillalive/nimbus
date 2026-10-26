"""Period 0's "charge from solar only" flag (nimbus #2066).

Reference household, 26 Oct 2026, 07:02-07:12: the plan charged 0.8-0.9 kW
while the sun gave 0.2-0.4 kW; the automation forced the charge, and all of
it was imported at 16.8 c. The flag tells a dispatcher when Self-Consume
does what the plan wants without that risk.
"""

import solver_publish as publish

f = publish._dispatch_solar_charge


def test_a_charge_while_selling_is_not():
    # Self-Consume would put the planned export into the battery instead.
    assert f(-0.8, 0.0, 2.0) is False


def test_noise_below_the_deadband_reads_as_none():
    assert f(-0.8, 0.04, 0.04) is True
