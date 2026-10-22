"""Image tools (PRD §6.2 image row): Compress ▸ presets.

Compress keeps the format (§6.2). A size target is met by searching the highest quality that
fits (JPEG, WebP, HEIC, AVIF), or by reducing a PNG to 256 colours, and then by scaling down
in steps. PNG becomes WebP only when Settings > Output allows a format change for size
presets (``params["allow_format_change"]``) and PNG cannot fit at full size.
Output: ``<stem> Compressed.<ext>`` (FR-5.1).

The colour profile and EXIF (without the orientation, which is applied) are kept. GIF, ICO and
SVG are not compressed, and neither are animated images: only the first frame would be left.
A camera's MPO .jpg is compressed as its main photo.

Pillow is imported inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

import io
import math
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.converters.images import (
    TARGETS,
    ImageFormat,
    open_image,
    prepare,
    register_heif,
)
from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.presets import Target, compress_op, size_note, target_for

if TYPE_CHECKING:
    from PIL.Image import Image

    from dragshift.engine.worker import JobContext

# Source extension -> the format it is written back in (Compress keeps the format).
_FORMATS = {"jpg": "jpg", "png": "png", "webp": "webp", "heic": "heic", "avif": "avif",
            "tiff": "tiff", "bmp": "bmp"}  # fmt: skip
MIN_QUALITY = 30  # below this JPEG falls apart into blocks; scale down instead
MAX_QUALITY = 92  # above this files grow fast for no visible gain
MAX_STEPS = 8  # scale-downs before giving up with the smallest result (FR-4.4)
MIN_SIDE = 16
PNG_COLOURS = 256
_NO_EXIF = {"BMP"}


def _encode(im: "Image", target: ImageFormat, quality: int | None, exif: bytes) -> bytes:
    register_heif()
    kwargs = dict(target.options)
    if target.lossy:
        kwargs["quality"] = quality
    if target.format in ("JPEG", "PNG"):
        kwargs["optimize"] = True
    if im.info.get("icc_profile"):
        kwargs["icc_profile"] = im.info["icc_profile"]
    if exif and target.format not in _NO_EXIF:
        kwargs["exif"] = exif
    buf = io.BytesIO()
    im.save(buf, format=target.format, **kwargs)
    return buf.getvalue()


def _quantized(im: "Image", colours: int) -> "Image":
    """A palette image of at most ``colours``, keeping transparency (what PNG shrinkers do)."""
    from PIL import Image

    out = im.quantize(colours, method=Image.Quantize.FASTOCTREE)
    out.info = dict(im.info)
    return out


def _at_size(im: "Image", target: ImageFormat, size: int, exif: bytes) -> bytes:
    """The best encoding of ``im`` at this scale that is at most ``size`` bytes, or else the
    smallest one tried."""
    if target.lossy:  # binary search for the highest quality that fits
        best = _encode(im, target, MIN_QUALITY, exif)
        if len(best) > size:
            return best
        lo, hi = MIN_QUALITY, MAX_QUALITY
        while lo < hi:
            mid = (lo + hi + 1) // 2
            data = _encode(im, target, mid, exif)
            if len(data) <= size:
                best, lo = data, mid
            else:
                hi = mid - 1
        return best
    data = _encode(im, target, None, exif)
    if len(data) > size and target.format == "PNG":
        data = min(data, _encode(_quantized(im, PNG_COLOURS), target, None, exif), key=len)
    return data


def _scaled(im: "Image", scale: float) -> "Image":
    from PIL import Image

    if scale >= 1:
        return im
    w, h = (max(1, round(side * scale)) for side in im.size)
    out = im.resize((w, h), Image.Resampling.LANCZOS)
    out.info = dict(im.info)
    return out


def _fit(
    im: "Image", target: ImageFormat, size: int, exif: bytes, ctx: "JobContext"
) -> bytes | None:
    """Encode ``im`` in at most ``size`` bytes, scaling down when the quality search is not
    enough; the smallest result when nothing fits (FR-4.4). None if the job was cancelled."""
    best: bytes | None = None
    scale = 1.0
    for step in range(MAX_STEPS):
        if ctx.cancelled():
            return None
        ctx.progress(step / MAX_STEPS)
        frame = _scaled(im, scale)
        data = _at_size(frame, target, size, exif)
        if len(data) <= size:
            return data
        best = data if best is None else min(best, data, key=len)
        if min(frame.size) <= MIN_SIDE:
            break
        # Bytes grow with the pixel count, so the side shrinks by about sqrt(size ratio).
        scale *= min(0.9, max(0.5, math.sqrt(size / len(data))))
    return best


def _at_quality(im: "Image", target: ImageFormat, quality: int, exif: bytes) -> bytes:
    """Custom… with a quality: lossy formats at that quality, PNG with fewer colours."""
    if target.lossy:
        return _encode(im, target, min(quality, MAX_QUALITY + 3), exif)
    if target.format == "PNG" and quality < 100:
        im = _quantized(im, max(2, round(PNG_COLOURS * quality / 100)))
    return _encode(im, target, None, exif)


def _read(src: Path) -> tuple["Image", bytes]:
    """The upright first frame and its EXIF; refuses what Compress cannot keep as it is."""
    from PIL import Image

    ext = normalize_ext(src.suffix)
    if ext not in _FORMATS:
        raise ValueError(f"{ext.upper()} images are not compressed; convert to PNG or JPG first")
    register_heif()
    with Image.open(src) as im:
        # MPO is how many cameras write a .jpg: the photo plus a preview or depth frame. Its
        # first frame is the photo, and a JPEG holds only that, as a JPEG converter would.
        if getattr(im, "n_frames", 1) > 1 and im.format != "MPO":
            raise ValueError("animated and multi-page images are not compressed")
    im = open_image(src)
    exif = im.getexif()  # exif_transpose has removed the orientation tag
    return im, exif.tobytes() if len(exif) else b""


@compress_op(Kind.IMAGE)
def compress(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    outputs = []
    for src in paths:
        goal: Target = target_for(params, src.stat().st_size)
        im, exif = _read(src)
        ext = _FORMATS[normalize_ext(src.suffix)]
        out_ext = src.suffix.lstrip(".").lower()  # .jpeg stays .jpeg
        target = TARGETS[ext]
        im = prepare(im, target)
        data: bytes | None = None
        if goal.quality is not None:
            data = _at_quality(im, target, goal.quality, exif)
        elif ext == "png" and params.get("allow_format_change"):
            # §6.2: WebP only when the user allows it, and only when PNG cannot fit at full
            # size (a 256-colour PNG is often smaller than a WebP).
            data = _at_size(im, target, goal.size, exif)
            if len(data) > goal.size:
                ext = out_ext = "webp"
                target = TARGETS[ext]
                data = None
        if data is None:
            data = _fit(im, target, goal.size, exif, ctx)
        if data is None:
            return []  # cancelled: run_job deletes anything reserved
        dst = ctx.output_path(src, ext=out_ext, label="Compressed")
        dst.write_bytes(data)
        ctx.note(size_note(goal, len(data)))
        outputs.append(dst)
    return outputs
