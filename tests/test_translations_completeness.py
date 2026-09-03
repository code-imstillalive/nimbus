"""Real regression test for nimbus issue #361 (Mark Purcell): strings.json
was missing `data`/`data_description` entries for 9 real options-flow
fields (they render as raw snake_case keys in the actual wizard UI) and
the `compute_quality_report` service had no translation at all.

Walks every real `vol.Required`/`vol.Optional` key in every hub-options
schema and asserts a matching `strings.json` `data` (and `data_description`)
entry exists -- the same check the review's own scratch script used to
find the original gap. Also confirms strings.json and translations/en.json
stay byte-identical (this project's own established convention, since
`config_subentries`'s real-HA strings.json-fallback support is less
reliable than `config`/`options` -- see this repo's own CLAUDE.md).

Imports and exercises the REAL schema-building functions (not a
reimplementation), against tests/_ha_stubs.py's stand-in homeassistant.*
modules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.flows.hub_options import (
    _forecaster_schema,
    _solver_battery_schema,
    _solver_grid_schema,
    _solver_sources_schema,
    _switchboard_schema,
)

_STRINGS_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "strings.json"
)
_TRANSLATIONS_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "translations"
    / "en.json"
)

# (schema factory, strings.json step key) -- every real hub-options schema.
_SCHEMAS = [
    (lambda: _forecaster_schema({}), "forecaster"),
    (lambda: _solver_battery_schema({}), "solver_battery"),
    (lambda: _solver_grid_schema({}), "solver_grid"),
    (lambda: _solver_sources_schema({}), "solver_sources"),
    (lambda: _switchboard_schema({}), "switchboard"),
]


def _schema_keys(schema) -> list[str]:
    """Every real field key a vol.Schema's own markers define -- vol.
    Required/vol.Optional are both plain str subclasses, so this is just
    the schema dict's own keys, stringified."""
    return [str(k) for k in schema.schema]


def test_every_options_schema_field_has_a_strings_json_data_label():
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    missing = []
    for factory, step_key in _SCHEMAS:
        schema = factory()
        step = strings["options"]["step"][step_key]
        data_labels = step.get("data", {})
        for key in _schema_keys(schema):
            if key not in data_labels:
                missing.append(f"{step_key}.{key}")
    assert not missing, (
        f"these fields render as raw snake_case keys in the real wizard "
        f"UI -- missing a strings.json options.step.*.data entry: {missing}"
    )


def test_every_options_schema_field_has_a_strings_json_data_description():
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    missing = []
    for factory, step_key in _SCHEMAS:
        schema = factory()
        step = strings["options"]["step"][step_key]
        descriptions = step.get("data_description", {})
        for key in _schema_keys(schema):
            if key not in descriptions:
                missing.append(f"{step_key}.{key}")
    assert not missing, (
        f"these fields have a label but no help text in the real wizard "
        f"UI -- missing a strings.json options.step.*.data_description "
        f"entry: {missing}"
    )


def test_compute_quality_report_service_has_a_translation():
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    services = strings.get("services", {})
    assert "compute_quality_report" in services, (
        "compute_quality_report is a real, registered service "
        "(services.py, supports_response=True) but had no strings.json "
        "entry -- rendered untranslated in Developer Tools"
    )
    fields = services["compute_quality_report"].get("fields", {})
    for field in ("start", "end", "allow_partial"):
        assert field in fields, (
            f"compute_quality_report field {field!r} has no translation"
        )


def test_strings_json_and_translations_en_json_stay_byte_identical():
    strings_text = _STRINGS_PATH.read_text(encoding="utf-8")
    translations_text = _TRANSLATIONS_PATH.read_text(encoding="utf-8")
    assert strings_text == translations_text, (
        "strings.json and translations/en.json must be kept byte-identical "
        "-- config_subentries's own strings.json-fallback support is less "
        "reliable than config/options (see this repo's own CLAUDE.md)"
    )


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
