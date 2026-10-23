"""Image tools (PRD §6.2 image row): Compress ▸ presets, Crop, Resize ▸, Rotate ▸, Strip metadata.

Resize ▸ (50 %, 1080p, 4K), Rotate ▸ and Strip metadata keep the format too. Resize re-encodes at
the Settings quality, the others at 95 (they change no detail). 1080p and 4K fit the picture
inside 1920x1080 or 3840x2160 (turned to match a portrait picture) and never enlarge it. Strip
metadata drops EXIF (GPS included), XMP and text chunks, and keeps the colour profile; a JPEG is
stripped without a re-encode unless its orientation must be applied first.
Crop cuts the box the dialog sends (``params["crop"]``, fractions of the upright picture).
Outputs: ``<stem> Cropped|Resized|Rotated|Flipped|Stripped.<ext>``.

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
from dragshift.engine.registry import op
from dragshift.engine.variants import Choice, ring_op

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
_KEEPS_DPI = {"JPEG", "PNG", "TIFF"}


def _encode(im: "Image", target: ImageFormat, quality: int | None, exif: bytes) -> bytes:
    register_heif()
    kwargs = dict(target.options)
    if target.lossy:
        kwargs["quality"] = quality
    if target.format in ("JPEG", "PNG"):
        kwargs["optimize"] = True
    if im.info.get("icc_profile"):
        kwargs["icc_profile"] = im.info["icc_profile"]
    if im.info.get("dpi") and target.format in _KEEPS_DPI:  # a scan keeps its printed size
        kwargs["dpi"] = im.info["dpi"]
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


def _read(src: Path, verb: str = "compressed") -> tuple["Image", bytes]:
    """The upright first frame and its EXIF; refuses what a tool cannot keep as it is."""
    from PIL import Image

    ext = normalize_ext(src.suffix)
    if ext not in _FORMATS:
        raise ValueError(f"{ext.upper()} images are not {verb}; convert to PNG or JPG first")
    register_heif()
    with Image.open(src) as im:
        # MPO is how many cameras write a .jpg: the photo plus a preview or depth frame. Its
        # first frame is the photo, and a JPEG holds only that, as a JPEG converter would.
        if getattr(im, "n_frames", 1) > 1 and im.format != "MPO":
            raise ValueError(f"animated and multi-page images are not {verb}")
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


# --- Crop, Resize, Rotate, Strip metadata -----------------------------------------------------
LOSSLESS_Q = 95  # for edits that change no detail: a re-encode costs nothing visible


_APP_DROPPED = {0xE1, *range(0xE3, 0xEE), 0xEF, 0xFE}  # EXIF/XMP, IPTC and the like, COM
_PIXEL_DIMENSIONS = (0xA002, 0xA003)  # Exif IFD: the size the picture had


def _fresh_exif(exif: bytes) -> bytes:
    """``exif`` without the pixel size it recorded, which an edit makes wrong."""
    from PIL import Image

    if not exif:
        return exif
    tags = Image.Exif()
    tags.load(exif)
    for key in _PIXEL_DIMENSIONS:
        tags.get_ifd(0x8769).pop(key, None)
    return tags.tobytes()


def _strip_jpeg(src: Path) -> bytes | None:
    """A JPEG's bytes without its metadata segments: no re-encode, so no loss. None when that
    is not possible (not a JPEG, a camera MPO whose offsets would move, or an EXIF orientation,
    which must be applied to the pixels before the tag goes)."""
    from PIL import Image

    data = src.read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    out, i = bytearray(data[:2]), 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: the rest is picture data
            return bytes(out + data[i:])
        end = i + 2 + int.from_bytes(data[i + 2 : i + 4], "big")
        segment = data[i:end]
        if marker == 0xE2 and segment[4:8] == b"MPF\0":
            return None
        if marker == 0xE1 and segment[4:10] == b"Exif\0\0":
            tags = Image.Exif()
            tags.load(segment[4:])
            if tags.get(0x0112, 1) != 1:
                return None
        if marker not in _APP_DROPPED:
            out += segment
        i = end
    return None


def _redo(paths: list[Path], params: dict, ctx: "JobContext", label: str, change, *,
          quality: int | None = None, keep_exif: bool = True, raw=None) -> list[Path]:  # fmt: skip
    """Each image through ``change(im, params, ctx)``, written back in its own format. ``raw``
    may give the finished bytes without decoding (None: decode as usual)."""
    outputs = []
    for i, src in enumerate(paths):
        if ctx.cancelled():
            return []  # run_job discards anything reserved
        ctx.progress(i / len(paths), src.name)
        data = raw(src) if raw else None
        if data is None:
            im, exif = _read(src, label.lower())
            target = TARGETS[_FORMATS[normalize_ext(src.suffix)]]
            im = prepare(im, target)
            info = dict(im.info)
            im = change(im, params, ctx)
            im.info = info  # a transpose forgets the colour profile and the DPI
            q = quality if quality is not None else int(params.get("quality", 85))
            data = _encode(im, target, q, _fresh_exif(exif) if keep_exif else b"")
        dst = ctx.output_path(src, ext=src.suffix.lstrip(".").lower(), label=label)
        dst.write_bytes(data)
        outputs.append(dst)
    return outputs


def _cropped(im: "Image", params: dict, ctx: "JobContext") -> "Image":
    """The Crop dialog's box, given as fractions of the upright picture."""
    w, h = im.size
    left, top, right, bottom = params["crop"]
    left, top = min(round(left * w), w - 1), min(round(top * h), h - 1)
    box = (left, top, max(round(right * w), left + 1), max(round(bottom * h), top + 1))
    return im.crop(box)


@op(id="tools.image.crop", wheel="tools", label="Crop", kinds=frozenset({Kind.IMAGE}),
    dialog="crop", only_exts=frozenset(_FORMATS.values()))  # fmt: skip
def crop(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _redo(paths, params, ctx, "Cropped", _cropped, quality=LOSSLESS_Q)


def _resized(im: "Image", params: dict, ctx: "JobContext") -> "Image":
    if "scale" in params:
        factor = params["scale"]
    else:
        long_side, short_side = params["box"]
        w, h = im.size
        box = (short_side, long_side) if h > w else (long_side, short_side)
        factor = min(box[0] / w, box[1] / h)
    if factor >= 1:
        ctx.note("Already smaller than that; not enlarged")
    return _scaled(im, factor)


@ring_op(Kind.IMAGE, "resize", "Resize", [
    Choice("half", "50 %", {"scale": 0.5}),
    Choice("1080p", "1080p", {"box": [1920, 1080]}),
    Choice("4k", "4K", {"box": [3840, 2160]}),
])  # fmt: skip
def resize(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _redo(paths, params, ctx, "Resized", _resized)


def _turned(im: "Image", params: dict, ctx: "JobContext") -> "Image":
    from PIL import Image

    return im.transpose(getattr(Image.Transpose, params["turn"]))


@ring_op(Kind.IMAGE, "rotate", "Rotate", [
    Choice("cw", "90° ⟳", {"turn": "ROTATE_270"}),  # Pillow turns counter-clockwise
    Choice("ccw", "90° ⟲", {"turn": "ROTATE_90"}),
    Choice("180", "180°", {"turn": "ROTATE_180"}),
    Choice("flip_h", "Flip H", {"turn": "FLIP_LEFT_RIGHT"}),
    Choice("flip_v", "Flip V", {"turn": "FLIP_TOP_BOTTOM"}),
])  # fmt: skip
def rotate(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    label = "Flipped" if params["turn"].startswith("FLIP") else "Rotated"
    return _redo(paths, params, ctx, label, _turned, quality=LOSSLESS_Q)


@op(id="tools.image.strip", wheel="tools", label="Strip metadata", kinds=frozenset({Kind.IMAGE}))
def strip_metadata(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _redo(paths, params, ctx, "Stripped", lambda im, p, c: im, quality=LOSSLESS_Q,
                 keep_exif=False, raw=_strip_jpeg)  # fmt: skip
