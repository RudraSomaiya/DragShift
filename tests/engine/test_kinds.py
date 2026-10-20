from pathlib import Path

import pytest

from dragshift.engine.kinds import EXTENSIONS, Kind, kind_of, normalize_ext

# One line per PRD §6.1 input row, copied from the PRD rather than from EXTENSIONS.
PRD_ROWS = {
    Kind.IMAGE: "jpg jpeg png webp heic heif avif tiff tif bmp gif ico svg",
    Kind.PDF: "pdf",
    Kind.DOCUMENT: "docx md txt html htm rtf odt epub",
    Kind.SLIDES: "ppt pptx odp",
    Kind.SHEET: "xlsx xls ods csv tsv",
    Kind.AUDIO: "mp3 m4a wav flac ogg opus aiff aif wma aac",
    Kind.VIDEO: "mp4 mov mkv webm avi wmv m4v",
    Kind.SUBTITLE: "srt vtt ass ssa",
    Kind.ARCHIVE: "zip tar gz tgz rar 7z",
}
CASES = [(ext, kind) for kind, exts in PRD_ROWS.items() for ext in exts.split()]


@pytest.mark.parametrize(("ext", "kind"), CASES)
def test_every_prd_extension_maps_to_its_kind(ext: str, kind: Kind) -> None:
    assert kind_of(Path(f"C:/x/file.{ext}")) is kind
    assert kind_of(Path(f"C:/x/FILE.{ext.upper()}")) is kind


def test_no_extension_belongs_to_two_kinds() -> None:
    all_exts = [e for exts in EXTENSIONS.values() for e in exts]
    assert len(all_exts) == len(set(all_exts))


def test_folder_and_unknown(tmp_path: Path) -> None:
    folder = tmp_path / "photos.png"  # a folder whose name looks like a file
    folder.mkdir()
    assert kind_of(folder) is Kind.FOLDER
    assert kind_of(tmp_path / "notes.xyz") is Kind.OTHER
    assert kind_of(tmp_path / "Makefile") is Kind.OTHER
    assert kind_of(tmp_path / "backup.tar.gz") is Kind.ARCHIVE


@pytest.mark.parametrize(
    ("ext", "norm"),
    [(".JPEG", "jpg"), ("jpg", "jpg"), (".tif", "tiff"), ("HEIF", "heic"), (".png", "png")],
)
def test_normalize_ext(ext: str, norm: str) -> None:
    assert normalize_ext(ext) == norm
