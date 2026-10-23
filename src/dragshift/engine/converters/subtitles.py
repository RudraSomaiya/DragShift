"""Subtitle converters (PRD §6.1 subtitle row): SRT VTT ASS TXT, through pysubs2.

Input encoding: UTF-8 (with or without a BOM), else Windows-1252, the usual encoding of older
SRT files. Output is UTF-8. TXT is the spoken text only: one line per cue, styling removed,
comment lines skipped.

pysubs2 is imported inside the functions: the UI process imports this module to register Ops
(CONTRIBUTING.md rule 2).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import op

if TYPE_CHECKING:
    from pysubs2 import SSAFile

    from dragshift.engine.worker import JobContext

SUBTITLE = frozenset({Kind.SUBTITLE})
_FORMATS = {"srt": "srt", "vtt": "vtt", "ass": "ass"}  # our extension -> pysubs2 format


def load(src: Path) -> "SSAFile":
    import pysubs2

    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return pysubs2.load(str(src), encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("unreachable: cp1252 decodes any byte")  # pragma: no cover


def plain_text(subs: "SSAFile") -> str:
    lines = [ev.plaintext.strip() for ev in subs if not ev.is_comment]
    return "\n".join(line for line in lines if line) + "\n"


def _convert(paths: list[Path], ctx: "JobContext", ext: str) -> list[Path]:
    outputs = []
    for src in paths:
        subs = load(src)
        dst = ctx.output_path(src, ext=ext)
        if ext == "txt":
            dst.write_text(plain_text(subs), encoding="utf-8")
        else:
            subs.save(str(dst), encoding="utf-8", format_=_FORMATS[ext])
        outputs.append(dst)
    return outputs


# --- Ops, in default petal order (§6.1) ------------------------------------------------------
@op(id="convert.subtitle.srt", wheel="convert", label="SRT", kinds=SUBTITLE, hide_if_same_ext="srt")
def to_srt(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "srt")


@op(id="convert.subtitle.vtt", wheel="convert", label="VTT", kinds=SUBTITLE, hide_if_same_ext="vtt")
def to_vtt(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "vtt")


@op(id="convert.subtitle.ass", wheel="convert", label="ASS", kinds=SUBTITLE, hide_if_same_ext="ass")
def to_ass(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "ass")


@op(id="convert.subtitle.txt", wheel="convert", label="TXT", kinds=SUBTITLE)
def to_txt(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "txt")
