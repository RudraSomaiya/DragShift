"""Input kinds and extension → Kind detection (PRD §6.1, §9.4)."""

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    DOCUMENT = "document"
    SLIDES = "slides"
    SHEET = "sheet"
    AUDIO = "audio"
    VIDEO = "video"
    SUBTITLE = "subtitle"
    ARCHIVE = "archive"
    FOLDER = "folder"
    OTHER = "other"


# Input extensions per kind, exactly as listed in PRD §6.1.
EXTENSIONS: dict[Kind, frozenset[str]] = {
    Kind.IMAGE: frozenset(
        {
            "jpg",
            "jpeg",
            "png",
            "webp",
            "heic",
            "heif",
            "avif",
            "tiff",
            "tif",
            "bmp",
            "gif",
            "ico",
            "svg",
        }
    ),
    Kind.PDF: frozenset({"pdf"}),
    Kind.DOCUMENT: frozenset({"docx", "md", "txt", "html", "htm", "rtf", "odt", "epub"}),
    Kind.SLIDES: frozenset({"ppt", "pptx", "odp"}),
    Kind.SHEET: frozenset({"xlsx", "xls", "ods", "csv", "tsv"}),
    Kind.AUDIO: frozenset(
        {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aiff", "aif", "wma", "aac"}
    ),
    Kind.VIDEO: frozenset({"mp4", "mov", "mkv", "webm", "avi", "wmv", "m4v"}),
    Kind.SUBTITLE: frozenset({"srt", "vtt", "ass", "ssa"}),
    Kind.ARCHIVE: frozenset({"zip", "tar", "gz", "tgz", "rar", "7z"}),
}

_KIND_BY_EXT = {ext: kind for kind, exts in EXTENSIONS.items() for ext in exts}

# Spellings of the same format, so a "JPG" petal is hidden for .jpeg input too.
_EXT_ALIASES = {"jpeg": "jpg", "tif": "tiff", "heif": "heic", "htm": "html", "aif": "aiff",
                "tgz": "gz"}  # fmt: skip


def normalize_ext(ext: str) -> str:
    """Lower-case extension without the dot, with aliases folded (``.JPEG`` → ``jpg``)."""
    ext = ext.lower().lstrip(".")
    return _EXT_ALIASES.get(ext, ext)


def kind_of(path: Path) -> Kind:
    if path.is_dir():
        return Kind.FOLDER
    return _KIND_BY_EXT.get(path.suffix.lower().lstrip("."), Kind.OTHER)


def input_kind(paths: Sequence[Path]) -> str | None:
    """The one Kind shared by ``paths``, else None: the key of pinned petal order and usage
    counts (FR-2.2), for the wheel and history.json alike."""
    kinds = {kind_of(p) for p in paths}
    return kinds.pop().value if len(kinds) == 1 else None
