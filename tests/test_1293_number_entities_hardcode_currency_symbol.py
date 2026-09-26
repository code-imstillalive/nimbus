"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1293, FIXED 2026-09-26): CHANGELOG.md's own #1253 entry states, verbatim --

    "Money is now shown in the household's own currency everywhere, instead
    of hardcoded `AUD` and `$`. [...] Zero hardcoded currency strings remain
    in the integration."

That claim is false. `sensor_flattened.py`'s 37 flattened rows genuinely
were migrated (`_CURRENCY`/`_CURRENCY_PER_KWH` sentinels, resolved against
`hass.config.currency` at read time via
`_FlattenedAttributeSensor.native_unit_of_measurement`) -- but `number.py`'s
own `_SolverNumberDescription.unit` field, assigned directly to
`self._attr_native_unit_of_measurement` in `__init__` with no read-time
resolution at all, still carries the literal, hardcoded string `"$/kWh"`
(15 occurrences) and `"$/day"` (1 occurrence) across the Solver tuning-knob
entities -- e.g. `number.nimbus_solver_import_price_cap`,
`number.nimbus_solver_fixed_daily_charge`. A non-AUD household sees a
literal `$` on every one of these entities, the exact problem #1253 was
opened to fix, in the one file #1253's own PR never touched.

This mirrors `sensor_flattened.py`'s already-fixed sensors, and is why the
CHANGELOG's "zero" claim is checked mechanically here rather than trusted:
a codebase-wide claim needs a codebase-wide grep, not a claim about the one
file that was actually changed.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import number as number_mod

_HARDCODED_CURRENCY_LITERAL = re.compile(r'"\$/(?:kWh|day)"')


class TestNoHardcodedCurrencySymbolRemainsInNumberEntities(unittest.TestCase):
    def test_no_literal_dollar_unit_strings_in_source(self):
        src = Path(number_mod.__file__).read_text(encoding="utf-8")
        matches = _HARDCODED_CURRENCY_LITERAL.findall(src)
        self.assertEqual(
            matches,
            [],
            f"found {len(matches)} hardcoded currency-literal unit string(s) "
            f"in number.py -- these must resolve against hass.config.currency "
            f"at read time, the same as sensor_flattened.py's own "
            f"_CURRENCY/_CURRENCY_PER_KWH sentinels, not assign a literal "
            f"'$'-prefixed string to _attr_native_unit_of_measurement",
        )


class TestTheClaimIsCheckedAcrossTheWholeIntegration(unittest.TestCase):
    """The actual lesson of #1293, and why it is a separate test.

    The finding's own closing line states it: "a codebase-wide claim needs a
    codebase-wide grep, not a claim about the one file that was actually
    changed." The test above greps `number.py` only. That closes the reported
    instance and leaves the CLAIM unguarded, so the next file to grow a
    hardcoded unit reopens #1293 under a new number.

    Running the sweep the claim always needed found a SECOND live instance the
    finding does not mention: `solver_writer.py`'s offer-curve
    `price_limits.unit`, a published sensor attribute rather than a comment.

    It is fixed the OPPOSITE way to every other site, and that distinction is
    the interesting part. Those two numbers are AEMO's own Market Floor Price
    and Market Price Cap -- Australian market constants, not this household's
    money. Resolving them against `hass.config.currency` would relabel a
    genuinely-AUD figure as EUR on a European install, which is a false
    statement about the value rather than a localisation. So they are pinned
    to an explicit ISO code. That is why this test allows a bare ISO currency
    code and rejects only the ambiguous symbol.
    """

    #: The symbol inside a string literal, alone or as a unit path
    #: ("$", "$/kWh", "$/day"). Deliberately NOT a bare "$" search: this
    #: repo's comments and docstrings discuss dollars constantly (shadow
    #: prices "in $ per kW of bound", cost docstrings), and a check that
    #: punished the prose would push the reasoning out of the files -- a trade
    #: this repo has already got wrong more than once.
    _AMBIGUOUS = re.compile(r'"[$](?:/\w+)?"')

    def test_no_module_publishes_an_ambiguous_dollar_unit(self):
        package = Path(number_mod.__file__).parent
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    # A comment explaining the rule is not a breach of it.
                    continue
                if self._AMBIGUOUS.search(line):
                    offenders.append(
                        f"{path.relative_to(package)}:{lineno}: {stripped[:70]}"
                    )
        detail = "\n  ".join(offenders)
        self.assertEqual(
            offenders,
            [],
            "CHANGELOG.md claims zero hardcoded currency strings remain in the "
            "integration. These contradict it -- either resolve against "
            "hass.config.currency (household money) or pin an explicit ISO "
            f"code (a genuinely single-currency market constant):\n  {detail}",
        )


if __name__ == "__main__":
    unittest.main()
