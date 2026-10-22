"""Image converters (PRD §6.1 image row).

Targets in default petal order: JPG PNG WEBP HEIC AVIF PDF TIFF BMP ICO, plus Merge to PDF for
several images. Every input in §6.1 is read: HEIC/HEIF through pillow-heif, AVIF natively in
Pillow, and SVG rasterised with QtSvg at 2x. Animated and multi-page inputs use their first frame.

Pillow, pillow-heif and Qt are imported inside the functions: the UI process imports this module
to register Ops (CLAUDE.md rule 2).
"""

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.registry import op

if TYPE_CHECKING:
    from PIL.Image import Image

    from dragshift.engine.worker import JobContext

IMAGE = frozenset({Kind.IMAGE})
QUALITY = 85  # default of Settings > Output > JPEG/WebP quality (FR-8.1)
ICO_SIZES = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]  # §6.1: sizes 16-256
SVG_SCALE = 2  # §6.1: SVG is rasterised at 2x its own size
SVG_MAX_SIDE = 8192  # ...but never larger than this, so a huge viewBox cannot exhaust memory
PDF_DPI = 96  # page size for images that do not say their own resolution


@dataclass(frozen=True)
class ImageFormat:
    format: str  # Pillow format name
    alpha: bool  # keeps transparency; False flattens it onto white
    lossy: bool = False  # takes params["quality"]
    options: dict[str, Any] = field(default_factory=dict)


TARGETS = {
    "jpg": ImageFormat("JPEG", alpha=False, lossy=True),
    "png": ImageFormat("PNG", alpha=True),
    "webp": ImageFormat("WEBP", alpha=True, lossy=True),
    "heic": ImageFormat("HEIF", alpha=True, lossy=True),
    "avif": ImageFormat("AVIF", alpha=True, lossy=True),
    "pdf": ImageFormat("PDF", alpha=False),
    "tiff": ImageFormat("TIFF", alpha=True, options={"compression": "tiff_lzw"}),
    "bmp": ImageFormat("BMP", alpha=False),  # most viewers ignore BMP alpha
    "ico": ImageFormat("ICO", alpha=True, options={"sizes": ICO_SIZES}),
}
_KEEPS_ICC = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF", "TIFF"}


# --- reading ---------------------------------------------------------------------------------
@cache
def register_heif() -> None:
    """Let Pillow open and save HEIC/HEIF (pillow-heif). Once per worker process."""
    import pillow_heif

    pillow_heif.register_heif_opener()


_qt_app: Any = None  # the worker's QGuiApplication, created on the first SVG
# The native platform with GDI fonts: it has the system fonts and never shows a window here.
# "offscreen" finds no fonts on Windows and draws every letter as a box (found in DS-13).
_WORKER_QT_ARGV = ["dragshift-worker", "-platform", "windows:fontengine=gdi"]


def _render_svg(src: Path) -> "Image":
    """§6.1: rasterise an SVG with QtSvg at SVG_SCALE x its own size, on transparency."""
    from PIL import Image
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    global _qt_app
    if QGuiApplication.instance() is None:  # text in an SVG needs fonts, which need an app
        _qt_app = QGuiApplication(_WORKER_QT_ARGV)
    renderer = QSvgRenderer(str(src))
    if not renderer.isValid():
        raise ValueError("not a valid SVG")
    size = renderer.defaultSize()
    if size.isEmpty():
        raise ValueError("the SVG has no size (no width, height or viewBox)")
    scale = min(SVG_SCALE, SVG_MAX_SIDE / max(size.width(), size.height()))
    size = size * scale
    canvas = QImage(size, QImage.Format.Format_RGBA8888)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    renderer.render(painter)
    painter.end()
    raw = canvas.constBits().tobytes()
    w, h = canvas.width(), canvas.height()
    return Image.frombytes("RGBA", (w, h), raw, "raw", "RGBA", canvas.bytesPerLine())


def open_image(src: Path) -> "Image":
    """The first frame of ``src``, loaded and turned upright by its EXIF orientation."""
    if normalize_ext(src.suffix) == "svg":
        return _render_svg(src)
    from PIL import Image, ImageOps

    register_heif()
    with Image.open(src) as im:
        im.seek(0)  # animated GIF/WebP, multi-page TIFF: the first frame
        return ImageOps.exif_transpose(im)  # a loaded copy, upright


# --- pixel modes -----------------------------------------------------------------------------
def _has_alpha(im: "Image") -> bool:
    return im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info


def _flatten_on_white(im: "Image") -> "Image":
    from PIL import Image

    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def prepare(im: "Image", target: ImageFormat) -> "Image":
    """``im`` as RGB or RGBA for ``target``. Keeps the ICC profile only when it still describes
    the pixels (RGB sources), and the DPI for PDF page sizes."""
    icc = im.info.get("icc_profile") if im.mode in ("RGB", "RGBA") else None
    dpi = im.info.get("dpi")
    if im.mode in ("I", "I;16", "I;16L", "I;16B", "I;16N"):  # 16-bit grey: Pillow would clip
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if _has_alpha(im):
        out = im.convert("RGBA") if target.alpha else _flatten_on_white(im)
    else:
        out = im.convert("RGB")  # also CMYK, palette, grey
    out.info = {k: v for k, v in (("icc_profile", icc), ("dpi", dpi)) if v}
    return out


def _square_icon(im: "Image") -> "Image":
    """Pad to a transparent square (so the icon is not squashed) at 256 px, the largest size."""
    from PIL import Image

    side = max(im.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return square.resize((256, 256), Image.Resampling.LANCZOS)


# --- writing ---------------------------------------------------------------------------------
def _pdf_dpi(im: "Image") -> float:
    dpi = im.info.get("dpi")
    return float(dpi[0]) if dpi and dpi[0] > 1 else PDF_DPI


def _save(im: "Image", dst: Path, target: ImageFormat, params: dict, **extra: Any) -> None:
    """Write ``im`` to ``dst`` (a ``.part`` path, so the format is always named)."""
    register_heif()
    kwargs = {**target.options, **extra}
    if target.lossy:
        kwargs["quality"] = int(params.get("quality", QUALITY))
    if target.format in _KEEPS_ICC and im.info.get("icc_profile"):
        kwargs["icc_profile"] = im.info["icc_profile"]
    if target.format == "PDF":
        kwargs.setdefault("resolution", _pdf_dpi(im))
    im.save(dst, format=target.format, **kwargs)


def _convert(paths: list[Path], params: dict, ctx: "JobContext", ext: str) -> list[Path]:
    target = TARGETS[ext]
    outputs = []
    for src in paths:
        im = prepare(open_image(src), target)
        if ext == "ico":
            im = _square_icon(im)
        dst = ctx.output_path(src, ext=ext)
        _save(im, dst, target, params)
        outputs.append(dst)
    return outputs


# --- Ops, in default petal order (§6.1) ------------------------------------------------------
@op(id="convert.image.jpg", wheel="convert", label="JPG", kinds=IMAGE, hide_if_same_ext="jpg")
def to_jpg(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "jpg")


@op(id="convert.image.png", wheel="convert", label="PNG", kinds=IMAGE, hide_if_same_ext="png")
def to_png(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "png")


@op(id="convert.image.webp", wheel="convert", label="WEBP", kinds=IMAGE, hide_if_same_ext="webp")
def to_webp(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "webp")


@op(id="convert.image.heic", wheel="convert", label="HEIC", kinds=IMAGE, hide_if_same_ext="heic")
def to_heic(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "heic")


@op(id="convert.image.avif", wheel="convert", label="AVIF", kinds=IMAGE, hide_if_same_ext="avif")
def to_avif(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "avif")


@op(id="convert.image.pdf", wheel="convert", label="PDF", kinds=IMAGE)
def to_pdf(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "pdf")


@op(id="convert.image.tiff", wheel="convert", label="TIFF", kinds=IMAGE, hide_if_same_ext="tiff")
def to_tiff(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "tiff")


@op(id="convert.image.bmp", wheel="convert", label="BMP", kinds=IMAGE, hide_if_same_ext="bmp")
def to_bmp(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "bmp")


@op(id="convert.image.ico", wheel="convert", label="ICO", kinds=IMAGE, hide_if_same_ext="ico")
def to_ico(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, params, ctx, "ico")


def natural_key(path: Path) -> list[int | str]:
    """File-name order as people read it: ``page 2`` before ``page 10``."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", path.name)]


@op(
    id="convert.image.merge_pdf",
    wheel="convert",
    label="Merge to PDF",
    kinds=IMAGE,
    multi="all",
    min_files=2,
)
def merge_to_pdf(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    """One PDF, one page per image in natural file-name order (Explorer's drop order starts
    with whichever file was clicked). Named ``<first> Merged.pdf`` next to the first image."""
    ordered = sorted(paths, key=natural_key)
    target = TARGETS["pdf"]
    pages = []
    for i, src in enumerate(ordered):
        if ctx.cancelled():
            return []  # run_job discards anything reserved
        pages.append(prepare(open_image(src), target))
        ctx.progress((i + 1) / (len(ordered) + 1), src.name)
    dst = ctx.output_path(ordered[0], ext="pdf", label="Merged")
    _save(pages[0], dst, target, params, save_all=True, append_images=pages[1:])
    return [dst]
