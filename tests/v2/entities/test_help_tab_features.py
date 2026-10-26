"""The Help tab's Features section and pictures: every feature described
(#1837).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import help_tab

IMAGES = REPO / "docs" / "images"


def _image_paths() -> list[str]:
    paths = [
        image
        for _t, _b, images in help_tab.FEATURES
        for image in help_tab.feature_images(images)
    ]
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


def test_a_feature_with_several_pictures_shows_each_in_order() -> None:
    """The plan's prices entry carries two pictures; both must render."""
    rendered = help_tab.features_markdown()
    first = rendered.index("tabs/plan-prices-ahead.png")
    assert rendered.index("tabs/plan-days.png") > first
