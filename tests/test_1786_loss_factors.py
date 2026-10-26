"""#1786: a site's loss factors from AEMO's published tables.

The values pinned here are the ones verified on #1786: Mark's NSW site
(Cooma NCMA, Essential BL0A) and AEMO's own tables as supplied there.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
from solver_inputs import loss_factors as lf


def test_what_cannot_be_looked_up_says_why_and_never_guesses():
    # test_what_cannot_be_looked_up_says_why_and_never_guesses
    when = datetime(2026, 10, 19, tzinfo=UTC)
    assert lf.site_factor(None, "BL0A", when) == lf.SiteFactor(None, "no_codes")
    assert lf.site_factor("NCMA", "", when) == lf.SiteFactor(None, "no_codes")
    assert lf.site_factor("ZZZZ", "BL0A", when).reason == "unknown_tni"
    assert lf.site_factor("NCMA", "ZZZZ", when).reason == "unknown_dlf"
    # GS93 is listed twice with different 2026-27 values.
    assert lf.site_factor("NCMA", "GS93", when) == lf.SiteFactor(None, "ambiguous")
    far = datetime(2030, 1, 1, tzinfo=UTC)
    assert lf.site_factor("NCMA", "BL0A", far) == lf.SiteFactor(None, "year_missing")

    # test_the_table_covers_both_published_years
    assert lf.years() == ["2025-26", "2026-27"]
