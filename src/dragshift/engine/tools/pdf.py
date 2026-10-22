"""PDF tools (PRD §6.2 PDF row): Compress ▸ presets.

Compress first saves with ``garbage=4, deflate=True`` (unused objects dropped, streams
recompressed). If that does not reach the target, images are downsampled and recompressed as
JPEG in steps (``LEVELS``) until it does; the smallest result is kept when nothing fits
(FR-4.4). Text and vector graphics are never rasterised. Output: ``<stem> Compressed.pdf``.

PyMuPDF is imported inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.converters.pdf import open_pdf
from dragshift.engine.kinds import Kind
from dragshift.engine.presets import Target, compress_op, size_note, target_for

if TYPE_CHECKING:
    from dragshift.engine.worker import JobContext

# (image DPI, JPEG quality), gentlest first. 150 DPI still prints well; 50 is for the screen.
LEVELS = ((150, 80), (120, 70), (96, 60), (72, 50), (50, 40))
QUALITY_DPI = 150  # Custom… with a quality: images above this DPI come down to it


def _optimised(src: Path, dpi: int | None = None, quality: int = 0) -> bytes:
    """``src`` saved compactly, with its images at most ``dpi`` and at JPEG ``quality``."""
    with open_pdf(src) as doc:
        if dpi is not None:
            # Only images above the threshold are touched: a little headroom, so an image a
            # hair over the DPI is not recompressed for nothing.
            doc.rewrite_images(dpi_threshold=dpi + dpi // 4, dpi_target=dpi, quality=quality)
        return doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                           use_objstms=1)  # fmt: skip


def _fit(src: Path, size: int, ctx: "JobContext") -> bytes | None:
    """The gentlest level that is at most ``size`` bytes, or the smallest one. None if the job
    was cancelled."""
    best = _optimised(src)
    for i, (dpi, quality) in enumerate(LEVELS):
        if len(best) <= size:
            break
        if ctx.cancelled():
            return None
        ctx.progress((i + 1) / (len(LEVELS) + 1), f"Images at {dpi} DPI")
        best = min(best, _optimised(src, dpi, quality), key=len)
    return best


@compress_op(Kind.PDF)
def compress(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    outputs = []
    for src in paths:
        goal: Target = target_for(params, src.stat().st_size)
        if goal.quality is not None:
            data: bytes | None = _optimised(src, QUALITY_DPI, goal.quality)
        else:
            data = _fit(src, goal.size, ctx)
        if data is None:
            return []  # cancelled: run_job deletes anything reserved
        dst = ctx.output_path(src, ext="pdf", label="Compressed")
        dst.write_bytes(data)
        ctx.note(size_note(goal, len(data)))
        outputs.append(dst)
    return outputs
