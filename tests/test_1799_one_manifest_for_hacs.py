"""nimbus #1799: the HACS default store's checker needs exactly one manifest.

hacs/default's `scripts/helpers/integration_path.py` walks a fresh clone of
the default branch and globs `*manifest.json`; anything other than exactly
one match fails its Core domain conflict and Hassfest checks with "No
manifest" (hacs/default#12099, 17 Oct 2026: the device-contract fixtures were
named manifest.json, ten matches). A file anywhere in the repo whose name ends
in manifest.json breaks the store listing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_exactly_one_file_ends_in_manifest_json() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    found = [p for p in tracked if p.endswith("manifest.json")]
    assert found == ["custom_components/nimbus_load/manifest.json"], found
