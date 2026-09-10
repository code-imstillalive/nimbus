"""Real household finding, 2026-09-11: `nimbus-dispatch-card-v4.js`'s own
"PLANNED DISCHARGE" status label fires purely off the sign of `battery_kw`,
with no distinction between "discharging to actually export" (VPP-shaped)
and "discharging that exactly covers the load with zero grid interaction"
(self-consume-shaped) -- even though the reasoning text right next to the
label already correctly explains "no grid import or export needed" for the
second case. Confirmed live: the real inverter's own EMS had already
correctly gone into Self Consume mode for exactly this plan (a separate,
already-fixed automation issue), while this card kept showing plain
"PLANNED DISCHARGE" for the identical period -- the label and the real
hardware mode disagreed, which is what actually prompted the household to
ask "why is Nimbus still saying planned discharge".

Approach: same "zero new infra -- no browser, no Node, no visual-diff step"
posture test_dispatch_card_compact_layout.py already established for this
file (that one parses embedded CSS with tinycss2; this one does a plain
regex-based structural check on the JS source text, since Node/a browser
isn't available in CI, confirmed directly -- no Node setup anywhere in
.github/workflows/ci.yml). The actual label-selection LOGIC was verified
directly with real Node locally before writing these assertions (three
scenarios: a genuine self-consume-shaped discharge, a genuine export, a
genuine import-alongside-discharge -- only the first produces the new
label) -- this test guards the SOURCE STRUCTURE that logic depends on
against an accidental future regression (a misplaced label, a duplicated
branch, the new label silently disappearing), not a live JS execution.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-dispatch-card-v4.js"
)


def _discharge_block() -> str:
    """The exact `if (bkw > 0.05) { ... } else if (bkw < -0.05)` block --
    isolated so assertions below can't accidentally match a DIFFERENT
    part of this large file that happens to contain similar text (e.g.
    the `!armed` real-measured-sensor branch a few hundred lines earlier,
    which uses the bare 'DISCHARGING'/'SELF-CONSUME' labels, a genuinely
    different code path this issue doesn't touch)."""
    src = CARD_JS.read_text(encoding="utf-8")
    match = re.search(
        r"if \(bkw > 0\.05\) \{(?P<body>.*?)\} else if \(bkw < -0\.05\)",
        src,
        re.DOTALL,
    )
    assert match is not None, (
        "Could not locate the bkw > 0.05 discharge block in "
        "nimbus-dispatch-card-v4.js -- has this section been restructured?"
    )
    return match.group("body")


def test_export_branch_still_uses_the_plain_discharge_label():
    block = _discharge_block()
    export_branch = re.search(
        r"if \(gridExportKw > 0\.05\) \{(.*?)\} else if \(gridImportKw > 0\.05\)",
        block,
        re.DOTALL,
    )
    assert export_branch is not None
    assert "dir = 'PLANNED DISCHARGE';" in export_branch.group(1)
    assert "SELF-CONSUME" not in export_branch.group(1)


def test_import_alongside_discharge_branch_still_uses_the_plain_discharge_label():
    block = _discharge_block()
    import_branch = re.search(
        r"else if \(gridImportKw > 0\.05\) \{(.*?)\} else \{",
        block,
        re.DOTALL,
    )
    assert import_branch is not None
    assert "dir = 'PLANNED DISCHARGE';" in import_branch.group(1)
    assert "SELF-CONSUME" not in import_branch.group(1)


def test_zero_grid_interaction_branch_uses_the_new_self_consume_label():
    block = _discharge_block()
    # The final `else` of the three-way if/else-if/else -- everything
    # after the gridImportKw branch closes.
    tail = block.split("else if (gridImportKw > 0.05)", 1)[1]
    else_branch = tail.split("} else {", 1)[1]
    assert "dir = 'PLANNED DISCHARGE (SELF-CONSUME)';" in else_branch
    # Distinct from the battery-genuinely-idle self-consume label a few
    # hundred lines away (`bkw` near zero) -- this must not collapse
    # into that same bare label, since the battery IS actively moving
    # here, a real, different state a household should still be able
    # to see.
    assert "dir = 'PLANNED SELF-CONSUME';" not in else_branch


def test_all_three_discharge_branches_keep_their_own_reasoning_text():
    # Regression guard for a different real mistake this kind of edit
    # could introduce: accidentally sharing/overwriting one branch's
    # `reasoning` string with another's. Each branch's own explanation
    # must still describe its OWN real grid condition, not a copy of a
    # sibling branch's.
    block = _discharge_block()
    assert "kW left to export at" in block
    assert "still leaves " in block and "coming from the grid at" in block
    assert "exactly covers the" in block and "no grid import or export needed" in block
