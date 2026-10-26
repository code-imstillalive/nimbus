"""#875: how long a controllable load may disagree with its own command
before Nimbus re-sends it, read from the load's own setting.

The coverage-by-risk map (#1837) showed the parsing reached by no test:
every existing dispatch test leaves the setting unset. Two rules matter to a
household: 0 turns re-sending off for that load (the escape hatch for a
device that must never be commanded twice), and a typo or a negative value
falls back to the working default rather than switching the feature off.
"""

from __future__ import annotations

import pytest
from const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as KEY
from solver_dispatch import guard


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        (5, 300.0),
        ("2.5", 150.0),
        (0, 0.0),
        ("0", 0.0),
    ],
)
def test_a_configured_wait_is_read_in_seconds(raw, seconds):
    assert guard._resolve_reaffirm_after_seconds({KEY: raw}) == seconds


def test_zero_is_kept_as_zero_so_re_sending_is_off():
    # Not None: None would mean "unset" and hand the load the default.
    assert guard._resolve_reaffirm_after_seconds({KEY: 0}) is not None
