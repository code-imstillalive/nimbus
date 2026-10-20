"""#1786: a site's loss factors from AEMO's published tables.

The values pinned here are the ones verified on #1786: Mark's NSW site
(Cooma NCMA, Essential BL0A) and AEMO's own tables as supplied there.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import pytest
from solver_inputs import loss_factors as lf


def test_mark_s_site_reproduces_localvolts_scale():
    got = lf.site_factor("NCMA", "BL0A", datetime(2026, 10, 19, tzinfo=UTC))
    # 0.9912 x 1.0594 = 1.0500773; Mark measured 1.05008 (five places).
    assert got.reason == "ok"
    assert got.value == pytest.approx(1.05008, abs=5e-6)


def test_each_interval_uses_its_own_financial_year():
    # BL0A: 1.0613 in 2025-26, 1.0594 in 2026-27; NCMA 0.9792 then 0.9912.
    last_june = lf.site_factor(
        "NCMA", "BL0A", datetime(2026, 6, 30, 13, 59, tzinfo=UTC)
    )
    first_july = lf.site_factor(
        "NCMA", "BL0A", datetime(2026, 6, 30, 14, 0, tzinfo=UTC)
    )
    assert last_june.value == pytest.approx(0.9792 * 1.0613)
    assert first_july.value == pytest.approx(0.9912 * 1.0594)


def test_the_financial_year_turns_at_nem_midnight():
    # 1 July 00:00 NEM time is 30 June 14:00 UTC.
    assert lf.financial_year(datetime(2026, 6, 30, 13, 59, tzinfo=UTC)) == "2025-26"
    assert lf.financial_year(datetime(2026, 6, 30, 14, 0, tzinfo=UTC)) == "2026-27"
    assert lf.financial_year(datetime(2099, 12, 31, tzinfo=UTC)) == "2099-00"


def test_codes_are_trimmed_and_case_insensitive():
    when = datetime(2026, 10, 19, tzinfo=UTC)
    assert lf.site_factor(" ncma ", "bl0a ", when).reason == "ok"


def test_what_cannot_be_looked_up_says_why_and_never_guesses():
    when = datetime(2026, 10, 19, tzinfo=UTC)
    assert lf.site_factor(None, "BL0A", when) == lf.SiteFactor(None, "no_codes")
    assert lf.site_factor("NCMA", "", when) == lf.SiteFactor(None, "no_codes")
    assert lf.site_factor("ZZZZ", "BL0A", when).reason == "unknown_tni"
    assert lf.site_factor("NCMA", "ZZZZ", when).reason == "unknown_dlf"
    # GS93 is listed twice with different 2026-27 values.
    assert lf.site_factor("NCMA", "GS93", when) == lf.SiteFactor(None, "ambiguous")
    far = datetime(2030, 1, 1, tzinfo=UTC)
    assert lf.site_factor("NCMA", "BL0A", far) == lf.SiteFactor(None, "year_missing")


def test_the_table_covers_both_published_years():
    assert lf.years() == ["2025-26", "2026-27"]
