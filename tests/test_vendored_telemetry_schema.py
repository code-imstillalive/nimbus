"""nimbus issues #495 / #496: the vendored `nem-flex-telemetry` schema.

#495 has been blocked for days on one concrete thing -- getting the real
`telemetry.schema.json` v2.0 committed into this repo so the emitter can
be validated against it rather than against a guess -- and #496's
diagnostics criterion is transitively blocked on the same file. Mark
Purcell also named the condition for doing it honestly:

    "a one-time vendored copy will drift the moment your
     nem-flex-telemetry schema moves, so a recorded upstream commit ref
     + a periodic drift check is the honest version"

So the copy is pinned to a specific upstream commit, its provenance is
recorded in `schema/PROVENANCE.md`, and the drift check lives here in
two deliberately separate layers.

## Why two layers

**Always-on and offline** (everything except the last class): the file's
sha256 and its identity are pinned. These catch a LOCAL edit, which is
the specific failure a vendored file invites -- someone "fixing" a copy
of someone else's source of truth. They never touch the network, so this
repo's CI cannot be broken by another repo's outage or by a rate limit.

**Opt-in and online** (`NIMBUS_SCHEMA_DRIFT_CHECK=1`): fetches upstream
and compares. Skipped by default. An always-on network test would make
Nimbus's CI depend on `nem-flex-telemetry`'s availability, and the
failure would read as a Nimbus problem when it is not. An entirely
offline check would never notice real drift. Neither alone is honest.

A difference means the pinned ref needs re-reviewing and re-pinning --
a deliberate act, not something that should happen silently on a CI run.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _REPO_ROOT / "schema" / "telemetry.schema.json"

# Recorded in schema/PROVENANCE.md; both must move together.
_PINNED_COMMIT = "024f45c61b3f4a6cb251326f0b15955a9556d3ed"
_PINNED_SHA256 = "24478a75be3cbaae53b1ff613b911483149e8362608f83bcd29078dd3db91f35"
_RAW_URL = (
    "https://raw.githubusercontent.com/purcell-lab/nem-flex-telemetry/"
    "main/schema/telemetry.schema.json"
)

# The fields Nimbus's own emitter is specified against (#495's field
# table). Pinned as a set rather than a count so a swap -- one field
# removed and another added -- cannot pass a length check.
_REQUIRED = {
    "schema_version",
    "interval_start_utc",
    "region",
    "postcode_prefix",
    "net_import_kw",
    "solar_kw",
    "house_load_kw",
    "deferrable_load_kw",
    "naive_baseline_kw",
    "naive_baseline_method",
    "price_signal_seen",
    "price_export_seen",
    "envelope_import_limit_kw",
    "envelope_export_limit_kw",
    "flex_available_up_kw",
    "flex_available_down_kw",
    "shadow_energy_price",
    "shadow_load_forecast_price",
    "shadow_solar_forecast_price",
}


def _schema():
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


class TestTheVendoredCopyIsIntact(unittest.TestCase):
    """Catches a local edit to a file this repo does not own."""

    def test_the_file_exists_where_495_expects_it(self):
        self.assertTrue(
            _SCHEMA_PATH.is_file(),
            "#495 is blocked on exactly this file being present",
        )

    def test_the_bytes_match_what_was_vendored(self):
        """The whole point of a vendored copy is that it is the upstream
        file, unmodified. If this fails, either someone edited it here --
        which is never the right fix, the fix belongs upstream -- or it
        was re-pinned without updating PROVENANCE.md."""
        got = hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest()
        self.assertEqual(got, _PINNED_SHA256)

    def test_it_is_parseable_json(self):
        self.assertIsInstance(_schema(), dict)


class TestItIsTheSchemaNumber495Describes(unittest.TestCase):
    """Identity checks against what #495's own body and Mark's own
    confirmation state, so a re-pin to a genuinely different schema
    cannot pass as a routine version bump."""

    def test_draft_and_version(self):
        s = _schema()
        self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(str(s["version"]), "2.0")

    def test_additional_properties_is_closed_at_the_top_level(self):
        """#495 calls this out specifically: the record is validated with
        `additionalProperties: false`, so an extra field Nimbus emits is
        a hard failure rather than something silently ignored."""
        self.assertFalse(_schema()["additionalProperties"])

    def test_schema_version_is_a_const(self):
        self.assertEqual(str(_schema()["properties"]["schema_version"]["const"]), "2.0")

    def test_every_required_field_495_maps_is_present(self):
        s = _schema()
        required = set(s["required"])
        missing = _REQUIRED - required
        self.assertEqual(
            missing,
            set(),
            "fields #495's own source table maps are no longer required upstream",
        )
        # Every required field must also be declared, or the schema
        # cannot validate anything against it.
        self.assertEqual(required - set(s["properties"]), set())

    def test_the_settled_naive_baseline_method_is_still_in_the_enum(self):
        """`subtraction` was Mark's own call on 2026-09-13, chosen
        because the value #495 originally proposed (`nimbus_counterfactual`)
        was not in this enum. If the enum changes, that decision needs
        revisiting rather than silently breaking the emitter."""
        enum = _schema()["properties"]["naive_baseline_method"]["enum"]
        self.assertIn("subtraction", enum)

    def test_region_and_postcode_prefix_are_the_shapes_nimbus_shipped_for(self):
        """These two were genuinely new config (shipped v0.94.299/300)
        precisely because the schema requires them. Pinned so the config
        surface and the schema cannot drift apart."""
        props = _schema()["properties"]
        self.assertIn("QLD1", props["region"]["enum"])
        self.assertEqual(props["postcode_prefix"]["pattern"], "^[0-9]{3}$")


@unittest.skipUnless(
    os.environ.get("NIMBUS_SCHEMA_DRIFT_CHECK") == "1",
    "online drift check -- set NIMBUS_SCHEMA_DRIFT_CHECK=1 to run it. Off by "
    "default so this repo's CI does not depend on another repo's uptime.",
)
class TestItHasNotDriftedUpstream(unittest.TestCase):
    """The periodic half of Mark's 'recorded ref + drift check'. Run when
    picking up #495/#496, or on a schedule."""

    def test_upstream_main_still_matches_the_pinned_copy(self):
        import urllib.request

        with urllib.request.urlopen(_RAW_URL, timeout=30) as r:
            upstream = r.read()
        self.assertEqual(
            hashlib.sha256(upstream).hexdigest(),
            _PINNED_SHA256,
            f"upstream telemetry.schema.json has moved since "
            f"{_PINNED_COMMIT[:12]}. This is not automatically a problem -- "
            f"re-read the upstream diff, decide whether Nimbus's emitter "
            f"needs changes, then re-pin the commit, the sha256 and "
            f"schema/PROVENANCE.md together.",
        )


if __name__ == "__main__":
    unittest.main()
