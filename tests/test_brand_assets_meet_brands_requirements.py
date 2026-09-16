"""nimbus issue #987 -- the brand icons are the artefact a future PR to
`home-assistant/brands` will submit, and nothing checked they qualify.

HA and HACS do not read integration artwork from the integration. Both
fetch it from `https://brands.home-assistant.io/nimbus_load/icon.png`,
which 404s because `nimbus_load` was never submitted -- hence the blank
white tile in update notifications and on the integrations page.

Fixing that is a PR to a major public repository under the maintainer's
own identity, so it is deliberately not opened unilaterally (#987 says
so). What *can* be done here is making sure the assets actually qualify
before anyone submits them.

**They did not.** #987's own table asserts "Both match what
`home-assistant/brands` requires", which was half right. Verified
against the brands README directly rather than from memory:

> "The image should be trimmed, so it contains the minimum amount of
> empty space on the edges. This includes things like white/black/any
> color borders or transparent spacing around the actual subject in the
> image."

Measured before the fix:

    icon.png     256x256   transparent padding l,t,r,b = 10, 13, 12, 12
    icon@2x.png  512x512   transparent padding l,t,r,b = 21, 26, 23, 25

Dimensions and format were right; trim was not. Regenerated from the
512x512 source (the highest-resolution original, so nothing is upscaled)
by cropping to the alpha bounding box and rescaling to fill the square.
Residual padding is now 0/2/0/2 and 0/4/0/4 -- the artwork is 468x461,
genuinely not 1:1, so a couple of pixels on one axis is the minimum
achievable rather than slack.

These tests are the durable half. The requirements live in another
repository and are easy to drift away from silently, since nothing in a
normal Nimbus release exercises them.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

_BRAND = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "brand"
)

# Straight from the brands README. Keep the numbers here rather than
# deriving them from the files, or the test asserts whatever happens to
# be on disk.
_REQUIRED = {"icon.png": 256, "icon@2x.png": 512}

# The artwork is 468x461 at source. Perfect edge-to-edge on both axes is
# therefore impossible; this is the slack that leaves, expressed as a
# fraction of the canvas so it holds for both sizes.
_MAX_PADDING_FRACTION = 0.02


def _png_header(path: Path) -> tuple[int, int, int, int]:
    """(width, height, bit_depth, colour_type) from the IHDR chunk.

    Deliberately stdlib-only: Pillow is not a declared dependency of
    this project, and a guard that silently skips when an optional
    import is missing is the class of guard #757 is about.
    """
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height, raw[24], raw[25]


class TestBrandAssetsWouldPassBrandsReview(unittest.TestCase):
    def test_both_required_icons_exist(self):
        for name in _REQUIRED:
            with self.subTest(name=name):
                self.assertTrue(
                    (_BRAND / name).is_file(),
                    f"{name} is missing -- it is what the home-assistant/"
                    "brands submission uploads (nimbus #987)",
                )

    def test_dimensions_are_exactly_what_brands_specifies(self):
        """'Icon size must be: 256x256 pixels for normal version.
        512x512 pixels for the hDPI version.'"""
        for name, size in _REQUIRED.items():
            with self.subTest(name=name):
                width, height, _, _ = _png_header(_BRAND / name)
                self.assertEqual((width, height), (size, size))

    def test_they_are_png_with_an_alpha_channel(self):
        """'The filetype of all images must be PNG.' and 'Images with
        transparency are preferred.' Colour type 6 is RGBA, 4 is
        grey+alpha -- anything else has no transparency to trim to."""
        for name in _REQUIRED:
            with self.subTest(name=name):
                _, _, depth, colour_type = _png_header(_BRAND / name)
                self.assertEqual(depth, 8)
                self.assertIn(
                    colour_type,
                    (4, 6),
                    "no alpha channel, so the icon cannot sit on the "
                    "transparent background brands prefers",
                )

    def test_they_are_trimmed_of_empty_space(self):
        """The requirement #987 missed, and the reason this file exists.

        Skips rather than passes when Pillow is absent: a trim check
        that quietly succeeds without measuring anything would be worse
        than no check, which is this repo's own #757 lesson.
        """
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow is optional here
            self.skipTest("Pillow unavailable -- trim genuinely unmeasured")

        for name, size in _REQUIRED.items():
            with self.subTest(name=name):
                image = Image.open(_BRAND / name).convert("RGBA")
                bbox = image.getchannel("A").getbbox()
                self.assertIsNotNone(bbox, f"{name} is fully transparent")
                left, top, right, bottom = bbox
                padding = (left, top, size - right, size - bottom)
                allowed = size * _MAX_PADDING_FRACTION
                self.assertLessEqual(
                    max(padding),
                    allowed,
                    f"{name} has {max(padding)}px of empty space on one "
                    f"edge (allowed {allowed:.0f}px). brands requires the "
                    "image be 'trimmed, so it contains the minimum amount "
                    "of empty space on the edges' -- an untrimmed icon "
                    "renders smaller than its neighbours in the "
                    f"integrations list. Measured padding l,t,r,b={padding}",
                )

    def test_the_two_sizes_are_the_same_artwork(self):
        """A 2x asset that is not the same image at twice the size would
        render as a different icon on hDPI displays, which is worse than
        having no 2x at all."""
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("Pillow unavailable")

        small = Image.open(_BRAND / "icon.png").convert("RGBA")
        large = Image.open(_BRAND / "icon@2x.png").convert("RGBA")
        small_bbox = [v / 256 for v in small.getchannel("A").getbbox()]
        large_bbox = [v / 512 for v in large.getchannel("A").getbbox()]
        for a, b in zip(small_bbox, large_bbox, strict=True):
            self.assertAlmostEqual(
                a,
                b,
                places=2,
                msg="icon.png and icon@2x.png have differently-shaped "
                "content, so they are not the same artwork at two sizes",
            )


class TestTheInvalidLeftoverIsNotMistakenForAnAsset(unittest.TestCase):
    """`brand/icon_2.png` is 500x484 -- not square, not a brands size,
    referenced by nothing. It is left in place deliberately (deleting
    artwork is the household's call, and #987 flags it as a separate
    question), so this pins that it must never be submitted."""

    def test_it_is_not_a_valid_brands_asset(self):
        leftover = _BRAND / "icon_2.png"
        if not leftover.is_file():
            self.skipTest("already removed -- delete this test with it")
        width, height, _, _ = _png_header(leftover)
        self.assertNotEqual(
            (width, height),
            (256, 256),
            "icon_2.png now looks like a valid asset, which makes it "
            "ambiguous which file the brands PR should use",
        )
        self.assertNotEqual(width, height, "expected the non-square leftover")


if __name__ == "__main__":
    unittest.main()
