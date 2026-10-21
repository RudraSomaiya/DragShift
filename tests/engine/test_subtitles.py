"""Subtitle converters (PRD §6.1 subtitle row). Samples are generated here."""

from pathlib import Path

import pysubs2
import pytest

from dragshift.engine import registry
from dragshift.engine.converters import subtitles
from dragshift.engine.worker import JobContext, final_path

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello <i>there</i>

2
00:00:03,000 --> 00:00:04,000
Two
lines
"""


def convert(func, src: Path) -> Path:
    (part,) = func([src], {}, JobContext("t"))
    return part.rename(final_path(part))


@pytest.fixture
def srt(tmp_path: Path) -> Path:
    path = tmp_path / "film.srt"
    path.write_text(SRT, encoding="utf-8")
    return path


@pytest.mark.parametrize(("func", "fmt"), [(subtitles.to_vtt, "vtt"), (subtitles.to_ass, "ass")])
def test_round_trip_keeps_timing_and_text(srt: Path, func, fmt: str) -> None:
    out = convert(func, srt)
    assert pysubs2.formats.autodetect_format(out.read_text(encoding="utf-8")) == fmt
    back = convert(subtitles.to_srt, out)
    subs = pysubs2.load(str(back))
    assert [(e.start, e.end) for e in subs] == [(1000, 2500), (3000, 4000)]
    assert subs[1].plaintext == "Two\nlines"


def test_txt_is_only_the_spoken_text(srt: Path) -> None:
    out = convert(subtitles.to_txt, srt)
    assert out.read_text(encoding="utf-8") == "Hello there\nTwo\nlines\n"


def test_old_windows_1252_files_are_read(tmp_path: Path) -> None:
    src = tmp_path / "old.srt"
    src.write_bytes(SRT.replace("Hello", "Café").encode("cp1252"))
    out = convert(subtitles.to_txt, src)
    assert out.read_text(encoding="utf-8").startswith("Café there")


def test_ssa_becomes_ass(tmp_path: Path, srt: Path) -> None:
    ssa = tmp_path / "film.ssa"
    pysubs2.load(str(srt)).save(str(ssa), format_="ssa")
    out = convert(subtitles.to_ass, ssa)
    assert "[V4+ Styles]" in out.read_text(encoding="utf-8")


def test_a_file_that_is_not_subtitles_fails_readably(tmp_path: Path) -> None:
    src = tmp_path / "bad.srt"
    src.write_text("just some words", encoding="utf-8")
    with pytest.raises(Exception, match="(?i)format"):
        subtitles.to_vtt([src], {}, JobContext("t"))


def test_petals() -> None:
    assert [o.label for o in registry.ops_for([Path("a.srt")], "convert")] == ["VTT", "ASS", "TXT"]
