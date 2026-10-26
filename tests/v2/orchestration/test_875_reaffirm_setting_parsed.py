"""#875: how long a controllable load may disagree with its own command
before Nimbus re-sends it, read from the load's own setting.

The coverage-by-risk map (#1837) showed the parsing reached by no test:
every existing dispatch test leaves the setting unset. Two rules matter to a
household: 0 turns re-sending off for that load (the escape hatch for a
device that must never be commanded twice), and a typo or a negative value
falls back to the working default rather than switching the feature off.
"""

from __future__ import annotations

from const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as KEY
from solver_dispatch import guard


def test_a_configured_wait_is_read_in_seconds():
    for raw, seconds in [
        (5, 300.0),
        ("2.5", 150.0),
        (0, 0.0),
        ("0", 0.0),
    ]:
        assert guard._resolve_reaffirm_after_seconds({KEY: raw}) == seconds


def test_unset_garbled_or_negative_falls_back_to_the_default():
    for raw in [None, "soon", "", [], -1, "-0.5"]:
        data = {} if raw is None else {KEY: raw}
        assert guard._resolve_reaffirm_after_seconds(data) is None
