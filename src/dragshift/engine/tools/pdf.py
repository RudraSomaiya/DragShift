"""PDF tools (PRD §6.2 PDF row): Compress ▸ presets, Merge, Split, Rotate ▸, Strip metadata.

Compress first saves with ``garbage=4, deflate=True`` (unused objects dropped, streams
recompressed). If that does not reach the target, images are downsampled and recompressed as
JPEG in steps (``LEVELS``) until it does; the smallest result is kept when nothing fits
(FR-4.4). Text and vector graphics are never rasterised. Output: ``<stem> Compressed.pdf``.

Merge joins PDFs in natural file-name order (``<first> Merged.pdf``). Split writes one PDF
per page into a folder ``<stem> split`` (``<stem> 001.pdf``, …). Rotate ▸ turns every page
(``<stem> Rotated.pdf``). Strip metadata clears the Info dictionary and XMP
(``<stem> Stripped.pdf``). All save with ``garbage=4, deflate=True``.

PyMuPDF is imported inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.converters.images import natural_key
from dragshift.engine.converters.pdf import open_pdf
from dragshift.engine.kinds import Kind
from dragshift.engine.presets import Target, compress_op, size_note, target_for
from dragshift.engine.registry import op
from dragshift.engine.variants import Choice, ring_op
from dragshift.engine.worker import new_folder

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


# --- Merge, Split, Rotate, Strip metadata ----------------------------------------------------
def _bytes(doc) -> bytes:
    return doc.tobytes(garbage=4, deflate=True)


@op(id="tools.pdf.merge", wheel="tools", label="Merge", kinds=frozenset({Kind.PDF}),
    multi="all", min_files=2)  # fmt: skip
def merge(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    import pymupdf

    ordered = sorted(paths, key=natural_key)
    with pymupdf.open() as out:
        for i, src in enumerate(ordered):
            if ctx.cancelled():
                return []
            ctx.progress(i / len(ordered), src.name)
            with open_pdf(src) as doc:
                out.insert_pdf(doc)
        dst = ctx.output_path(ordered[0], ext="pdf", label="Merged")
        dst.write_bytes(_bytes(out))
    return [dst]


@op(id="tools.pdf.split", wheel="tools", label="Split", kinds=frozenset({Kind.PDF}))
def split(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    import pymupdf

    outputs = []
    for src in paths:
        with open_pdf(src) as doc:
            n = doc.page_count
            if n == 1:
                raise ValueError("the PDF has only one page")
            folder = new_folder(ctx.out_dir(src) / f"{src.stem} split", ctx)
            width = max(3, len(str(n)))
            for i in range(n):
                if ctx.cancelled():
                    return []
                ctx.progress(i / n, f"Page {i + 1} of {n}")
                with pymupdf.open() as page:
                    page.insert_pdf(doc, from_page=i, to_page=i)
                    dst = ctx.output_path(src, ext="pdf", label=f"{i + 1:0{width}d}", folder=folder)
                    dst.write_bytes(_bytes(page))
                outputs.append(dst)
    return outputs


def _rewrite(paths: list[Path], ctx: "JobContext", label: str, change) -> list[Path]:
    outputs = []
    for src in paths:
        if ctx.cancelled():
            return []
        with open_pdf(src) as doc:
            change(doc)
            dst = ctx.output_path(src, ext="pdf", label=label)
            dst.write_bytes(_bytes(doc))
        outputs.append(dst)
    return outputs


@ring_op(Kind.PDF, "rotate", "Rotate", [
    Choice("cw", "90° ⟳", {"degrees": 90}),
    Choice("ccw", "90° ⟲", {"degrees": 270}),
    Choice("180", "180°", {"degrees": 180}),
])  # fmt: skip
def rotate(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    def turn(doc) -> None:
        for page in doc:
            page.set_rotation((page.rotation + int(params["degrees"])) % 360)

    return _rewrite(paths, ctx, "Rotated", turn)


@op(id="tools.pdf.strip", wheel="tools", label="Strip metadata", kinds=frozenset({Kind.PDF}))
def strip_metadata(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    def clear(doc) -> None:
        doc.set_metadata({})
        doc.del_xml_metadata()

    return _rewrite(paths, ctx, "Stripped", clear)
