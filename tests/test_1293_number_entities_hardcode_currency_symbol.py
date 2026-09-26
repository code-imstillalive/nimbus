"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1293): CHANGELOG.md's own #1253 entry states, verbatim --

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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import number as number_mod

_HARDCODED_CURRENCY_LITERAL = re.compile(r'"\$/(?:kWh|day)"')


class TestNoHardcodedCurrencySymbolRemainsInNumberEntities(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1293: number.py still hardcodes literal \"$/kWh\"/"
        "\"$/day\" unit strings on 16 Solver tuning-knob entities, "
        "contradicting CHANGELOG.md's own #1253 entry (\"Zero hardcoded "
        "currency strings remain in the integration\") -- these were never "
        "migrated to hass.config.currency the way sensor_flattened.py's "
        "37 rows were",
        strict=True,
    )
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


if __name__ == "__main__":
    unittest.main()
