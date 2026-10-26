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


def test_every_feature_says_what_it_does_and_where() -> None:
    """Each entry carries at least two of the labelled parts, so none is a
    one-liner."""
    for title, body, _image in help_tab.FEATURES:
        labels = re.findall(r"\*\*[^*]+\.\*\*", body)
        assert len(labels) >= 2, title
