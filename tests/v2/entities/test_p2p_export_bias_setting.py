"""The P2P export bias is a Nimbus setting on the Solver tab's P2P card.

Household, 25 Nov 2026: "Why write a fix if dashboard is not reflecting this
setting." A setting is done only where its siblings are (4 Nov 2026, the Site
Import Limit): the export bias belongs on the P2P card beside the block
amounts, with its help text, as number.nimbus_solver_p2p_export_bias_kw.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import help_tab, number, solver_tab

KEY = "solver_p2p_export_bias_kw"


def test_the_export_bias_is_a_setting_on_the_p2p_card_with_help():
    spec = {d.key: d for d in number._DESCRIPTIONS}[KEY]
    assert (spec.min_value, spec.max_value, spec.step, spec.unit) == (0, 3, 0.1, "kW")
    assert spec.default == 0
    assert spec.sub_device == "p2p"
    rows = [row[0] for row in solver_tab._P2P[1]]
    assert rows.index(KEY) == rows.index("solver_p2p_block_lead_time_minutes") + 1
    assert KEY in help_tab.SETTINGS
