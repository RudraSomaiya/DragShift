"""Add BG, the worker side (PRD §8.6): padding, backdrop, ratio, corners, shadow, always a PNG."""

from pathlib import Path

from PIL import Image, ImageCms
from test_tools import BLUE, RED, done, photo, run

from dragshift.engine import backdrop, registry

GREEN = (0, 255, 0)


def params(**over) -> dict:
    solid = {"kind": "solid", "color": "#00FF00"}
    return {"bg": solid, "padding": 20, "corners": 0, "shadow": 0, "ratio": "Auto"} | over


def test_it_pads_the_picture_on_a_solid_colour(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png")  # 400x200, red left half, blue right half
    (out,) = done("tools.image.add_bg", [src], params())
    assert out.name == "a BG.png"
    with Image.open(out) as got:
        assert got.size == (440, 240)
        assert got.getpixel((5, 5)) == GREEN  # the padding
        assert got.getpixel((30, 30)) == RED  # the picture, in place
        assert got.getpixel((410, 30)) == BLUE


def test_a_gradient_runs_from_the_top_left_to_the_bottom_right(tmp_path: Path) -> None:
    spec = {"kind": "gradient", "colors": ["#000000", "#FFFFFF"]}
    (out,) = done("tools.image.add_bg", [photo(tmp_path / "a.png")], params(bg=spec, padding=100))
    with Image.open(out) as got:
        last = got.getpixel((got.width - 1, got.height - 1))[0]
        assert got.getpixel((0, 0))[0] < 8 and last > 247
        assert got.getpixel((0, 0))[0] < got.getpixel((got.width - 1, 0))[0] < last


def test_a_ratio_grows_the_canvas_and_centres_the_picture(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png", (200, 200))
    (out,) = done("tools.image.add_bg", [src], params(padding=0, ratio="16:9"))
    with Image.open(out) as got:
        assert got.size == (356, 200)
        assert got.getpixel((0, 100)) == GREEN and got.getpixel((100, 100)) == RED


def test_corners_are_cut_and_the_shadow_falls_below(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (200, 100), RED).save(src)
    (out,) = done("tools.image.add_bg", [src], params(corners=40, shadow=60, padding=60))
    with Image.open(out) as got:
        assert got.getpixel((60, 60)) != RED  # the corner is cut away
        assert got.getpixel((160, 110)) == RED
        below = got.getpixel((160, 60 + 100 + 8))  # the shadow darkens the green under it
        assert below[1] < 230 and below[0] < 60


def test_a_picture_can_be_the_background(tmp_path: Path) -> None:
    wall = tmp_path / "wall.png"
    Image.new("RGB", (50, 50), (10, 20, 30)).save(wall)
    spec = {"kind": "image", "path": str(wall)}
    (out,) = done("tools.image.add_bg", [photo(tmp_path / "a.png")], params(bg=spec))
    with Image.open(out) as got:
        assert got.getpixel((2, 2)) == (10, 20, 30)


def test_transparency_shows_the_backdrop(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    Image.new("RGBA", (40, 40), (255, 0, 0, 0)).save(src)
    (out,) = done("tools.image.add_bg", [src], params(padding=10))
    with Image.open(out) as got:
        assert got.mode == "RGB" and got.getpixel((30, 30)) == GREEN


def test_the_exif_orientation_is_applied_and_a_jpg_becomes_a_png(tmp_path: Path) -> None:
    src = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored 400x200, upright 200x400
    Image.new("RGB", (400, 200), RED).save(src, exif=exif)
    (out,) = done("tools.image.add_bg", [src], params(padding=0))
    assert out.name == "phone BG.png"
    with Image.open(out) as got:
        assert got.size == (200, 400)


def test_it_works_without_params_using_the_defaults(tmp_path: Path) -> None:
    (out,) = done("tools.image.add_bg", [photo(tmp_path / "a.png")], {})
    with Image.open(out) as got:
        assert got.size == (400 + 2 * 96, 200 + 2 * 96)


def test_a_result_too_large_for_memory_is_refused(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png", (100, 12000))
    result = run("tools.image.add_bg", [src], params(padding=0, ratio="16:9"))
    assert not result["ok"] and "too large" in result["message"]
    assert "100 megapixels" in result["message"]


def test_it_is_offered_for_images_the_worker_can_read_between_crop_and_resize() -> None:
    def tools(name: str) -> list[str]:
        return [o.id for o in registry.ops_for([Path(name)], "tools")]

    assert "tools.image.add_bg" in tools("a.jpg") and "tools.image.add_bg" in tools("a.webp")
    assert "tools.image.add_bg" not in tools("a.gif")
    order = tools("a.png")
    assert order.index("tools.image.crop") < order.index("tools.image.add_bg")
    assert order.index("tools.image.add_bg") < order.index("tools.image.resize")
    assert registry.get("tools.image.add_bg").dialog == "add_bg"


def test_the_layout_helpers() -> None:
    assert backdrop.layout((400, 200), 20, "Auto") == ((440, 240), (20, 20))
    assert backdrop.layout((200, 200), 0, "4:5") == ((200, 250), (0, 25))
    assert backdrop.corner_radius((100, 50), 200) == 25
    assert backdrop.shadow_look(0)[0] == 0


def test_the_shadow_follows_the_pictures_transparency(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    cut_out = Image.new("RGBA", (400, 200), (255, 0, 0, 255))  # opaque left half, clear right half
    cut_out.paste((0, 0, 0, 0), (200, 0, 400, 200))
    cut_out.save(src)
    (out,) = done("tools.image.add_bg", [src], params(shadow=100, padding=60, corners=0))
    with Image.open(out) as got:
        below = 60 + 200 + 12
        assert got.getpixel((60 + 100, below))[1] < 200  # under the opaque half: darkened
        assert got.getpixel((60 + 330, below))[1] > 240  # under the clear half: no shadow
        r, g, b = got.getpixel((60 + 300, 60 + 100))  # the backdrop shows through the clear half
        assert g > 240 and r < 20 and b < 20


def test_a_tagged_picture_is_converted_to_srgb_and_the_output_has_no_profile(
    tmp_path: Path,
) -> None:
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "a.png"
    Image.new("RGBA", (40, 40), (200, 30, 30, 255)).save(src, icc_profile=srgb)
    (out,) = done("tools.image.add_bg", [src], params(padding=10))
    with Image.open(out) as got:
        assert "icc_profile" not in got.info
        assert got.getpixel((30, 30)) == (200, 30, 30)  # sRGB to sRGB changes nothing


def test_an_unreadable_profile_is_dropped_rather_than_failing_the_job(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (40, 40), RED).save(src, icc_profile=b"not a profile")
    (out,) = done("tools.image.add_bg", [src], params(padding=10))
    with Image.open(out) as got:
        assert "icc_profile" not in got.info and got.getpixel((30, 30)) == RED


def test_a_big_gradient_is_still_smooth_corner_to_corner(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png", (1200, 800))
    spec = {"kind": "gradient", "colors": ["#000000", "#FFFFFF"]}
    (out,) = done("tools.image.add_bg", [src], params(bg=spec, padding=400, shadow=0))
    with Image.open(out) as got:  # 2000 x 1600: made at 512 px and scaled up
        assert got.size == (2000, 1600)
        assert got.getpixel((0, 0))[0] < 8 and got.getpixel((1999, 1599))[0] > 247
        mid = got.getpixel((400, 300))[0]
        assert 0 < mid < 128  # still ramping inside the padding, not a flat block
