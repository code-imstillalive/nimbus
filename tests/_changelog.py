"""CHANGELOG.md plus its archive, read as one file, newest first.

#2289 item 12 moved releases before 0.95.0 word for word into
`docs/changelog/`, one file per version band. Guards that walk every
release read them all through `changelog_text()`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
ARCHIVE_DIR = REPO_ROOT / "docs" / "changelog"


def _lower_bound(path: Path) -> tuple[int, ...]:
    # "0.94.300-0.94.399.md" -> (0, 94, 300)
    return tuple(int(p) for p in path.stem.split("-")[0].split("."))


def archive_files() -> list[Path]:
    """The archive files, newest band first."""
    return sorted(ARCHIVE_DIR.glob("*.md"), key=_lower_bound, reverse=True)


def changelog_text() -> str:
    parts = [CHANGELOG.read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in archive_files()]
    return "\n".join(parts)
