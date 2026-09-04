"""Regression test for nimbus issue #355 (Mark Purcell, codebase review):
p2p_export.py's docstrings claimed its six P2P mechanisms (charge gate,
export pinning, bonus variable, bonus <= export, bonus cost, per-day cap +
latest-preferred tie-break) were "extracted verbatim" from network.py as a
single source of truth -- but network.py never actually imported the
module and still carried its own inline copy of all six, so the two would
silently drift the moment either one changed (exactly what had already
happened between network.py and stochastic.py, per the sibling issue).

Fixed by having build_plan() call the p2p_export helpers directly and
deleting the inline copies. This test asserts the two structural
properties the issue itself asks for: network.py genuinely imports
p2p_export, and no inline `export_bonus_cap_` constraint-name construction
remains in network.py's own source (that literal string only ever
existed in the deleted per-day-cap block -- p2p_export.py's own equivalent
constraint names live in that module, not network.py).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_NETWORK_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver"
    / "network.py"
)


class TestNetworkImportsP2PExport(unittest.TestCase):
    def setUp(self):
        self._source = _NETWORK_PY.read_text(encoding="utf-8")

    def test_network_imports_p2p_export_module(self):
        self.assertRegex(
            self._source,
            re.compile(r"^from \. import p2p_export$", re.MULTILINE),
            "network.py must import the shared p2p_export module rather "
            "than re-implementing its mechanisms inline",
        )

    def test_network_has_no_inline_export_bonus_cap_construction(self):
        self.assertNotIn(
            "export_bonus_cap_",
            self._source,
            "network.py should call p2p_export.add_export_bonus_cumulative_caps() "
            "rather than constructing 'export_bonus_cap_*' constraint names inline",
        )

    def test_network_calls_every_p2p_export_helper(self):
        expected_calls = [
            "p2p_export.charging_ub_during_fixed_window(",
            "p2p_export.grid_export_bounds(",
            "p2p_export.has_export_bonus(",
            "p2p_export.add_export_bonus_variable(",
            "p2p_export.add_export_bonus_le_export_constraint(",
            "p2p_export.set_export_bonus_cost(",
            "p2p_export.add_export_bonus_cumulative_caps(",
        ]
        for call in expected_calls:
            with self.subTest(call=call):
                self.assertIn(call, self._source)


if __name__ == "__main__":
    unittest.main()
