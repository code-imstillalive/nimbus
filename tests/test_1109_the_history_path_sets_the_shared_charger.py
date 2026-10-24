"""nimbus #1109: the scorer's participant builder sets the shared-charger
fields and widens them on the way out. Checks that read source text, kept
apart until the decision on source-reading tests (#1837); the widening
itself is in tests/v2/reports/test_quality_score.py."""

from __future__ import annotations

import _solver_path  # noqa: F401
from solver_inputs import battery_participants


def _source():
    with open(
        battery_participants.__file__.replace(".pyc", ".py"), encoding="utf-8"
    ) as f:
        return f.read()


def test_the_history_path_sets_both_shared_charger_fields():
    text = _source()
    body = text[
        text.index("def _resolve_battery_participant_history(") : text.index(
            "def _widen_shared_charger_cap_to_achieved("
        )
    ]
    assert "shared_charger_group=" in body and "shared_charger_max_kw=" in body


def test_the_widening_runs_on_the_way_out():
    assert "return _widen_shared_charger_cap_to_achieved(results)" in " ".join(
        _source().split()
    )
