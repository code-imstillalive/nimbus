"""Every Solver number setting has a row in the user docs (household, 18 Oct
2026: "Publish docs but make sure these are kept up to date each release").

docs/configuration-reference.md said its settings table was generated from
number.py, but nothing generated it: 21 of 60 settings were missing when the
docs site was first published, the reserve and the spike override among them.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
from custom_components.nimbus_load import number

DOC = (
    pathlib.Path(__file__).resolve().parents[1] / "docs" / "configuration-reference.md"
)


def test_every_number_setting_has_a_row():
    # test_every_number_setting_has_a_row
    text = DOC.read_text(encoding="utf-8")
    missing = [
        d.key for d in number._DESCRIPTIONS if f"`number.nimbus_{d.key}`" not in text
    ]
    assert missing == [], (
        f"{missing}: add a row to the 'Live Solver tuning knobs' table in "
        "docs/configuration-reference.md"
    )

    # test_no_row_names_a_setting_that_no_longer_exists: The other direction: a removed setting (Bank solar first, 0.94.465)
    text = DOC.read_text(encoding="utf-8")
    known = {f"number.nimbus_{d.key}" for d in number._DESCRIPTIONS}
    table = text[text.index("## Live Solver tuning knobs") : text.index("**Notes:**")]
    listed = {
        cell.strip("` ")
        for line in table.splitlines()
        if line.startswith("| `number.")
        for cell in [line.split("|")[1]]
    }
    assert listed - known == set()
