"""The Help tab's Features section and pictures (household, 18 Oct 2026:
"make sure all new features are described in the help section... it needs a
lot more detail... preferably with some images").

Images are served from GitHub (HACS installs only the integration folder), so
a renamed or deleted file would show a broken picture on every install with
nothing failing. These tests fail instead.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import help_tab

IMAGES = REPO / "docs" / "images"


def _image_paths() -> list[str]:
    paths = [image for _t, _b, image in help_tab.FEATURES if image]
    paths += [image for _c, image in help_tab.SETUP_PICTURES]
    return paths


def test_every_picture_the_help_tab_shows_is_in_the_repo() -> None:
    missing = [p for p in _image_paths() if not (IMAGES / p).is_file()]
    assert missing == []


def test_pictures_are_served_from_this_repo() -> None:
    assert help_tab.IMAGES_URL.startswith(
        "https://raw.githubusercontent.com/code-imstillalive/nimbus/"
    )
    rendered = help_tab.features_markdown() + help_tab.setup_pictures_markdown()
    for src in re.findall(r'src="([^"]+)"', rendered):
        assert src.startswith(help_tab.IMAGES_URL), src


def test_every_feature_says_what_it_does_and_where() -> None:
    """Each entry carries at least two of the labelled parts, so none is a
    one-liner."""
    for title, body, _image in help_tab.FEATURES:
        labels = re.findall(r"\*\*[^*]+\.\*\*", body)
        assert len(labels) >= 2, title


def test_the_features_band_sits_before_each_tab() -> None:
    names = [
        help_tab._banner_name(section)
        for section in help_tab.template()
        if help_tab._banner_name(section)
    ]
    assert names.index("Features") == names.index("Each tab") - 1
