"""Image converters (PRD §6.1 image row): round trips, odd inputs, Merge to PDF, petals.

Every sample is generated here; nothing binary is committed. Converters run in-process with a
plain JobContext, except for the end-to-end tests at the bottom, which use the real worker pool.
"""

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pillow_heif
import pymupdf
import pytest
from PIL import Image

from dragshift.engine import registry
from dragshift.engine.converters import images
from dragshift.engine.jobs import JobManager
from dragshift.engine.worker import JobContext, final_path

pillow_heif.register_heif_opener()

RED, GREEN = (220, 30, 30), (30, 160, 60)
WHITE = (255, 255, 255)
SIDE = 96
CONVERTERS: dict[str, Callable] = {
    "jpg": images.to_jpg,
    "png": images.to_png,
    "webp": images.to_webp,
    "heic": images.to_heic,
    "avif": images.to_avif,
    "pdf": images.to_pdf,
    "tiff": images.to_tiff,
    "bmp": images.to_bmp,
    "ico": images.to_ico,
}
FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "heic": "HEIF", "avif": "AVIF",
           "tiff": "TIFF", "bmp": "BMP", "ico": "ICO"}  # fmt: skip
LOSSY = {"jpg", "webp", "heic", "avif"}
KEEPS_ALPHA = {"png", "webp", "heic", "avif", "tiff", "ico"}


# --- helpers ---------------------------------------------------------------------------------
def bands(path: Path, side: int = SIDE, fmt: str = "PNG") -> Path:
    """Three vertical bands: opaque red, opaque green, fully transparent."""
    im = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    third = side // 3
    im.paste((*RED, 255), (0, 0, third, side))
    im.paste((*GREEN, 255), (third, 0, 2 * third, side))
    im.save(path, format=fmt)
    return path


def convert(ext: str, src: Path | list[Path], params: dict | None = None) -> Path:
    """Run a converter in-process and finish its output the way JobManager does."""
    paths = src if isinstance(src, list) else [src]
    (part,) = CONVERTERS[ext](paths, params or {}, JobContext("t"))
    return part.rename(final_path(part))


def sample(im: Image.Image, fx: float, fy: float = 0.5) -> tuple[int, ...]:
    """The pixel at a fraction of the image's width and height, as RGBA."""
    rgba = im.convert("RGBA")
    return rgba.getpixel((int(fx * (rgba.width - 1)), int(fy * (rgba.height - 1))))


def close(a: tuple[int, ...], b: tuple[int, ...], tol: int) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=False))


def check_bands(im: Image.Image, ext: str) -> None:
    tol = 14 if ext in LOSSY else 2
    assert close(sample(im, 1 / 6), RED, tol), sample(im, 1 / 6)
    assert close(sample(im, 3 / 6), GREEN, tol), sample(im, 3 / 6)
    clear = sample(im, 5 / 6)
    if ext in KEEPS_ALPHA:
        assert clear[3] <= 8, clear
    else:
        assert close(clear, WHITE, tol), clear  # flattened on white


def pdf_page_pixels(path: Path) -> list[Image.Image]:
    with pymupdf.open(path) as doc:
        return [
            Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            for pix in (page.get_pixmap(dpi=96) for page in doc)
        ]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- round trips -----------------------------------------------------------------------------
@pytest.mark.parametrize("ext", [e for e in CONVERTERS if e not in ("pdf", "png")])
def test_round_trip_through_each_target_and_back_to_png(tmp_path: Path, ext: str) -> None:
    src = bands(tmp_path / "bands.png")
    before = sha(src)
    out = convert(ext, src)
    assert out == tmp_path / f"bands.{ext}"
    with Image.open(out) as im:
        assert im.format == FORMATS[ext]
        expected = (256, 256) if ext == "ico" else (SIDE, SIDE)
        assert im.size == expected
        check_bands(im, ext)
    back = convert("png", out)
    with Image.open(back) as im:
        check_bands(im, ext)
    assert sha(src) == before  # the source is never touched (rule 4)


def test_png_from_a_jpg_round_trip(tmp_path: Path) -> None:
    rgb = Image.new("RGB", (SIDE, SIDE), WHITE)
    rgb.paste(RED, (0, 0, SIDE // 3, SIDE))
    rgb.paste(GREEN, (SIDE // 3, 0, 2 * SIDE // 3, SIDE))
    src = tmp_path / "photo.jpg"
    rgb.save(src, quality=95)
    with Image.open(convert("png", src)) as im:
        assert im.mode == "RGB"
        check_bands(im, "jpg")  # no alpha in, white band out


def test_pdf_is_one_page_sized_by_dpi_with_alpha_on_white(tmp_path: Path) -> None:
    out = convert("pdf", bands(tmp_path / "bands.png"))
    with pymupdf.open(out) as doc:
        assert doc.page_count == 1
        assert doc[0].rect.width == pytest.approx(SIDE / images.PDF_DPI * 72)
    (page,) = pdf_page_pixels(out)
    check_bands(page, "pdf")


def test_pdf_page_size_follows_the_image_dpi(tmp_path: Path) -> None:
    src = tmp_path / "scan.png"
    Image.new("RGB", (300, 600), WHITE).save(src, dpi=(300, 300))
    with pymupdf.open(convert("pdf", src)) as doc:
        assert (doc[0].rect.width, doc[0].rect.height) == pytest.approx((72, 144), abs=0.01)


def test_jpg_quality_comes_from_params(tmp_path: Path) -> None:
    src = tmp_path / "noise.png"
    Image.effect_noise((200, 200), 64).convert("RGB").save(src)
    small = convert("jpg", src, {"quality": 30})
    large = convert("jpg", src, {"quality": 95})
    assert small.stat().st_size < large.stat().st_size


# --- odd inputs ------------------------------------------------------------------------------
def test_exif_orientation_is_applied(tmp_path: Path) -> None:
    src = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    Image.new("RGB", (40, 20), RED).save(src, exif=exif)
    with Image.open(convert("png", src)) as im:
        assert im.size == (20, 40)


def test_cmyk_becomes_rgb_without_its_cmyk_profile(tmp_path: Path) -> None:
    src = tmp_path / "print.jpg"
    Image.new("CMYK", (32, 32), (0, 255, 255, 0)).save(src, icc_profile=b"not-an-rgb-profile")
    with Image.open(convert("png", src)) as im:
        assert im.mode == "RGB"
        assert "icc_profile" not in im.info
        r, g, b = im.getpixel((16, 16))
        assert r > 200 and g < 60 and b < 60


def test_rgb_icc_profile_is_kept(tmp_path: Path) -> None:
    from PIL import ImageCms

    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "tagged.png"
    Image.new("RGB", (8, 8), RED).save(src, icc_profile=srgb)
    with Image.open(convert("webp", src)) as im:
        assert im.info.get("icc_profile") == srgb


def test_16_bit_grey_is_scaled_not_clipped(tmp_path: Path) -> None:
    src = tmp_path / "depth.png"
    Image.new("I;16", (8, 8), 32768).save(src)
    with Image.open(convert("jpg", src)) as out:
        assert close(out.getpixel((4, 4)), (128, 128, 128), 3)


def test_palette_transparency_is_kept_or_flattened(tmp_path: Path) -> None:
    src = tmp_path / "sprite.gif"
    im = Image.new("P", (16, 16), 0)
    im.putpalette([*RED, *GREEN] + [0] * (256 * 3 - 6))
    im.paste(1, (8, 0, 16, 16))
    im.save(src, transparency=1)
    with Image.open(convert("png", src)) as png:
        assert png.convert("RGBA").getpixel((12, 8))[3] == 0
        assert png.convert("RGBA").getpixel((4, 8)) == (*RED, 255)
    with Image.open(convert("jpg", src)) as jpg:
        assert close(jpg.getpixel((12, 8)), WHITE, 6)


def test_animated_gif_converts_its_first_frame(tmp_path: Path) -> None:
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (16, 16), c) for c in (RED, GREEN)]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100)
    with Image.open(convert("png", src)) as im:
        assert close(im.getpixel((8, 8)), RED, 4)


def test_heic_input(tmp_path: Path) -> None:
    src = tmp_path / "photo.heic"
    Image.new("RGB", (64, 64), RED).save(src, format="HEIF", quality=95)
    with Image.open(convert("jpg", src)) as im:
        assert im.size == (64, 64)
        assert close(im.getpixel((32, 32)), RED, 14)


def test_ico_is_square_padded_with_every_size(tmp_path: Path) -> None:
    src = tmp_path / "wide.png"
    Image.new("RGB", (60, 30), RED).save(src)
    with Image.open(convert("ico", src)) as im:
        assert im.info["sizes"] == set(images.ICO_SIZES)
        assert im.size == (256, 256)
        assert im.convert("RGBA").getpixel((128, 10))[3] == 0  # padding above
        assert close(im.convert("RGBA").getpixel((128, 128)), (*RED, 255), 2)


# --- SVG input (rendered with QtSvg in the worker) -------------------------------------------
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30">
  <rect x="0" y="0" width="20" height="30" fill="rgb(220,30,30)"/>
  <text x="22" y="20" font-size="10">Hi</text>
</svg>"""


@pytest.fixture
def svg(tmp_path: Path, qapp: object) -> Path:
    """An SVG file. Depends on qapp so the converter reuses the test's Qt application."""
    path = tmp_path / "logo.svg"
    path.write_text(SVG, encoding="utf-8")
    return path


def test_svg_is_rasterised_at_twice_its_size_on_transparency(svg: Path) -> None:
    with Image.open(convert("png", svg)) as im:
        assert im.size == (80, 60)
        assert close(sample(im, 0.2), (*RED, 255), 2)
        assert sample(im, 0.95, 0.05)[3] == 0


def test_huge_svg_is_capped(svg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "SVG_MAX_SIDE", 50)
    with Image.open(convert("png", svg)) as im:
        assert im.size == (50, 38)


@pytest.mark.parametrize(
    ("text", "error"),
    [("not xml at all", "not a valid SVG"), ('<svg xmlns="http://www.w3.org/2000/svg"/>', "size")],
)
def test_bad_svg_raises_a_readable_error(
    tmp_path: Path, qapp: object, text: str, error: str
) -> None:
    path = tmp_path / "bad.svg"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        convert("png", path)


# --- Merge to PDF ----------------------------------------------------------------------------
def test_merge_to_pdf_orders_pages_by_natural_file_name(tmp_path: Path) -> None:
    colours = {"page 10.png": (20, 20, 200), "page 2.png": GREEN, "page 1.png": RED}
    paths = []
    for name, colour in colours.items():
        Image.new("RGB", (40, 40), colour).save(tmp_path / name)
        paths.append(tmp_path / name)
    (part,) = images.merge_to_pdf(paths, {}, JobContext("t"))
    out = part.rename(final_path(part))
    assert out.name == "page 1 Merged.pdf"
    pages = pdf_page_pixels(out)
    expected = [RED, GREEN, (20, 20, 200)]
    assert [close(p.getpixel((20, 20)), c, 6) for p, c in zip(pages, expected, strict=True)] == [
        True
    ] * 3


def test_merge_to_pdf_mixes_sizes_and_alpha(tmp_path: Path) -> None:
    a = bands(tmp_path / "a.png")
    b = tmp_path / "b.jpg"
    Image.new("RGB", (30, 60), GREEN).save(b)
    (part,) = images.merge_to_pdf([a, b], {}, JobContext("t"))
    with pymupdf.open(part) as doc:
        assert doc.page_count == 2
        assert doc[0].rect.width != doc[1].rect.width


def test_cancelled_merge_writes_nothing(tmp_path: Path) -> None:
    paths = [bands(tmp_path / f"{i}.png") for i in range(3)]
    ctx = JobContext("t", cancel_flags={"t": True})
    assert images.merge_to_pdf(paths, {}, ctx) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0.png", "1.png", "2.png"]


def test_natural_key() -> None:
    names = ["img10.png", "IMG2.png", "img1.png", "img1a.png"]
    ordered = sorted((Path(n) for n in names), key=images.natural_key)
    assert [p.name for p in ordered] == ["img1.png", "img1a.png", "IMG2.png", "img10.png"]


# --- petals (FR-2.4, §6.1 order) -------------------------------------------------------------
def labels(*names: str) -> list[str]:
    return [o.label for o in registry.ops_for([Path(n) for n in names], "convert")]


def test_petals_follow_the_prd_order_without_the_source_format() -> None:
    assert labels("a.png") == ["JPG", "WEBP", "HEIC", "AVIF", "PDF", "TIFF", "BMP", "ICO"]
    assert labels("a.JPEG")[0] == "PNG"  # jpeg is jpg: no JPG petal
    assert "HEIC" not in labels("a.heif")
    assert "TIFF" not in labels("a.tif")


def test_svg_and_gif_get_every_target() -> None:
    every = ["JPG", "PNG", "WEBP", "HEIC", "AVIF", "PDF", "TIFF", "BMP", "ICO"]
    assert labels("a.svg") == every
    assert labels("a.gif") == every


def test_merge_to_pdf_needs_two_images() -> None:
    assert "Merge to PDF" not in labels("a.png")
    assert labels("a.png", "b.jpg")[-1] == "Merge to PDF"
    assert labels("a.png", "b.pdf") == []  # mixed kinds: no image converters


# --- through the real worker pool ------------------------------------------------------------
@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


@pytest.mark.parametrize("op_id", ["convert.image.heic", "convert.image.avif"])
def test_codecs_work_in_a_worker_process(qtbot, jm: JobManager, tmp_path: Path, op_id: str):
    src = bands(tmp_path / "bands.png")
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit(op_id, [src])
    result = blocker.args[1]
    assert result.ok, result.message
    with Image.open(result.outputs[0]) as im:
        check_bands(im, op_id.rsplit(".", 1)[1])


def test_svg_renders_in_a_worker_process_without_a_qt_app(
    qtbot, jm: JobManager, tmp_path: Path
) -> None:
    src = tmp_path / "logo.svg"
    src.write_text(SVG, encoding="utf-8")
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit("convert.image.png", [src])
    result = blocker.args[1]
    assert result.ok, result.message
    with Image.open(result.outputs[0]) as im:
        assert im.size == (80, 60)


def ink(path: Path) -> int:
    with Image.open(path) as im:
        return sum(1 for a in im.getchannel("A").get_flattened_data() if a > 128)


def test_svg_text_uses_real_fonts_in_a_worker(qtbot, jm: JobManager, tmp_path: Path) -> None:
    """Found live: without system fonts every letter is the same box, so "IIIII" and "WWWWW"
    cover the same pixels. Real glyphs make W far inkier than I."""
    paths = []
    for text in ("IIIII", "WWWWW"):
        path = tmp_path / f"{text}.svg"
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="40">'
            f'<text x="5" y="30" font-size="28" font-family="Segoe UI">{text}</text></svg>',
            encoding="utf-8",
        )
        paths.append(path)
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit("convert.image.png", paths)
    result = blocker.args[1]
    assert result.ok, result.message
    narrow, wide = (ink(p) for p in result.outputs)
    assert wide > 2 * narrow
