"""Regression test for nimbus issue #359 (Mark Purcell, codebase review):
several `manifest.json` fields either didn't match the integration's real
behaviour or were missing entirely, with nothing to catch a future
accidental regression since hassfest only validates SHAPE (is this a
real, well-formed manifest), not these specific VALUES.

1. `frontend` was missing from `dependencies` despite `frontend.py`
   importing `homeassistant.components.frontend.add_extra_js_url`
   directly -- fixed in v0.94.76.
2. `single_config_entry` wasn't declared despite the integration already
   being single-entry in practice (`config_flow.py`'s own
   `_abort_if_unique_id_configured`) -- fixed in v0.94.76.
3. `integration_type` was `"service"` (HA's own documented meaning: a
   cloud/web-service integration) despite Nimbus being a purely local
   integration that creates one hub config entry with multiple
   subentry-devices attached (Load, Power Signal, Power Source, PV
   String, Battery Tower) -- exactly HA's own documented `"hub"`
   pattern. Fixed 2026-09-05.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "manifest.json"
)


class TestManifestJsonFields(unittest.TestCase):
    def setUp(self):
        with _MANIFEST_PATH.open(encoding="utf-8") as f:
            self.manifest = json.load(f)

    def test_frontend_is_a_declared_dependency(self):
        self.assertIn("frontend", self.manifest["dependencies"])

    def test_single_config_entry_is_declared_true(self):
        self.assertIs(self.manifest["single_config_entry"], True)

    def test_integration_type_is_hub_not_service(self):
        # "service" is HA's own documented type for a cloud/web-service
        # integration -- Nimbus is purely local. "hub" is the documented
        # type for an integration whose config entry represents a hub
        # with multiple devices (subentries) attached, which is exactly
        # this integration's real shape.
        self.assertEqual(self.manifest["integration_type"], "hub")
