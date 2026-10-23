"""Other tools (PRD §6.2): image resize/rotate/strip, PDF merge/split/rotate/strip, video
mute/extract audio/GIF/resize, audio normalize/bitrate.

Samples are generated here (Pillow, PyMuPDF, ffmpeg's lavfi sources); nothing binary is
committed. Tools run through ``run_job`` with a plain JobContext.
"""

import hashlib
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pymupdf
import pytest
from PIL import Image, ImageCms

from dragshift.engine import ffmpeg, registry, variants, worker
from dragshift.engine.kinds import Kind
from dragshift.engine.tools import audio
from dragshift.engine.worker import JobContext, final_path, run_job

FF = imageio_ffmpeg.get_ffmpeg_exe()
RED, BLUE = (255, 0, 0), (0, 0, 255)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(op_id: str, sources: list[Path], params: dict | None = None) -> dict:
    """Run an Op the way the pool does (one group for a multi="all" Op), check the sources
    are untouched, finish the outputs."""
    found = registry.get(op_id)
    before = [sha(s) for s in sources]
    groups = [sources] if found.multi == "all" else [[s] for s in sources]
    result = run_job(
        found.func, groups, variants.params_for(op_id) | (params or {}), JobContext("t")
    )
    assert [sha(s) for s in sources] == before  # CONTRIBUTING.md rule 4
    result["outputs"] = [part.rename(final_path(part)) for part in result["outputs"]]
    return result


def done(op_id: str, sources: list[Path], params: dict | None = None) -> list[Path]:
    result = run(op_id, sources, params)
    assert result["ok"], result["message"]
    return result["outputs"]


def make_media(path: Path, *args: str) -> Path:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", *args, str(path)], check=True)
    return path


def photo(path: Path, size: tuple[int, int] = (400, 200), **save) -> Path:
    """Left half red, right half blue."""
    im = Image.new("RGB", size, RED)
    im.paste(BLUE, (size[0] // 2, 0, size[0], size[1]))
    im.save(path, **save)
    return path


def make_pdf(path: Path, pages: int = 3, **metadata: str) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}", fontsize=24)
    doc.set_metadata(metadata)
    doc.save(path)
    doc.close()
    return path


# --- registry --------------------------------------------------------------------------------
def names(ops: list) -> list[str]:
    return [o.label for o in ops]


def test_rings_are_registered_for_their_kind(tmp_path: Path) -> None:
    jpg, pdf = photo(tmp_path / "a.jpg"), make_pdf(tmp_path / "a.pdf")
    clip = make_media(tmp_path / "a.mp3", "-f", "lavfi", "-i", "sine=duration=1")
    tools = {p: names(registry.ops_for([p], "tools")) for p in (jpg, pdf, clip)}
    assert tools[jpg][:1] == ["Compress ▸"]
    assert {"Resize ▸", "Rotate ▸", "Strip metadata"} <= set(tools[jpg])
    assert {"Split", "Rotate ▸", "Strip metadata"} <= set(tools[pdf]) and "Merge" not in tools[pdf]
    assert {"Normalize", "Bitrate ▸"} <= set(tools[clip])
    resize = registry.get("tools.image.resize")
    assert names(registry.children_for(resize, [jpg])) == ["50 %", "1080p", "4K"]
    rotate = registry.get("tools.image.rotate")
    assert names(registry.children_for(rotate, [jpg])) == [
        "90° ⟳",
        "90° ⟲",
        "180°",
        "Flip H",
        "Flip V",
    ]
    assert names(registry.children_for(registry.get("tools.audio.bitrate"), [clip])) == [
        "96 kbps",
        "128 kbps",
        "192 kbps",
        "320 kbps",
    ]


def test_merge_needs_two_pdfs(tmp_path: Path) -> None:
    a, b = make_pdf(tmp_path / "a.pdf"), make_pdf(tmp_path / "b.pdf")
    assert "Merge" in names(registry.ops_for([a, b], "tools"))
    assert "Merge" not in names(registry.ops_for([a], "tools"))


def test_params_for_a_ring_op_and_its_parent() -> None:
    assert variants.params_for("tools.image.resize.1080p") == {"box": [1920, 1080]}
    assert variants.params_for("tools.image.resize") == {"scale": 0.5}  # a drop on the parent
    assert variants.params_for("tools.image.strip") == {}
    assert variants.params_for("tools.video.compress.discord") == {"preset": "discord"}


def test_bitrate_drop_on_the_petal_is_not_the_lowest_choice() -> None:
    assert variants.params_for("tools.audio.bitrate") == {"kbps": 128}


# --- image -----------------------------------------------------------------------------------
@pytest.mark.parametrize("ext", ["jpg", "png", "webp"])
def test_image_resize_half_keeps_format(tmp_path: Path, ext: str) -> None:
    (out,) = done("tools.image.resize.half", [photo(tmp_path / f"a.{ext}")])
    assert out.name == f"a Resized.{ext}"
    with Image.open(out) as im:
        assert im.size == (200, 100)


@pytest.mark.parametrize(
    ("op_id", "size", "expected"),
    [
        ("1080p", (4000, 3000), (1440, 1080)),
        ("1080p", (3000, 4000), (1080, 1440)),  # portrait: the box is turned
        ("1080p", (5000, 1000), (1920, 384)),  # a panorama is limited by its width
        ("4k", (6000, 3000), (3840, 1920)),
    ],
)
def test_image_resize_fits_the_box(
    tmp_path: Path, op_id: str, size: tuple[int, int], expected: tuple[int, int]
) -> None:
    (out,) = done(f"tools.image.resize.{op_id}", [photo(tmp_path / "a.png", size)])
    with Image.open(out) as im:
        assert im.size == expected


def test_image_resize_never_enlarges(tmp_path: Path) -> None:
    result = run("tools.image.resize.4k", [photo(tmp_path / "a.png")])
    (out,) = result["outputs"]
    with Image.open(out) as im:
        assert im.size == (400, 200)
    assert result["notes"] == ["Already smaller than that; not enlarged"]


def test_image_rotate_and_flip(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png")  # red left, blue right
    corners = {}
    for key in ("cw", "ccw", "180", "flip_h", "flip_v"):
        (out,) = done(f"tools.image.rotate.{key}", [src])
        with Image.open(out) as im:
            corners[key] = (im.size, im.getpixel((0, 0)), im.getpixel((im.width - 1, 0)))
        out.unlink()
    # Clockwise: the red left half ends up on top, the blue right half at the bottom.
    assert corners["cw"] == ((200, 400), RED, RED)
    assert corners["ccw"] == ((200, 400), BLUE, BLUE)  # blue on top, in both top corners
    assert corners["180"] == ((400, 200), BLUE, RED)
    assert corners["flip_h"] == ((400, 200), BLUE, RED)
    assert corners["flip_v"] == ((400, 200), RED, BLUE)


def test_image_rotate_names(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.jpg")
    assert done("tools.image.rotate.cw", [src])[0].name == "a Rotated.jpg"
    assert done("tools.image.rotate.flip_v", [src])[0].name == "a Flipped.jpg"


def test_image_rotate_applies_the_exif_orientation_once(tmp_path: Path) -> None:
    src = tmp_path / "phone.jpg"
    im = Image.new("RGB", (400, 200), RED)
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: shown 90° clockwise, so upright it is 200x400
    im.save(src, exif=exif)
    (out,) = done("tools.image.rotate.180", [src])
    with Image.open(out) as got:
        assert got.size == (200, 400)
        assert 0x0112 not in got.getexif()


def test_image_crop_cuts_the_box_given_as_fractions(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png")  # 400x200: red left half, blue right half
    (out,) = done("tools.image.crop", [src], {"crop": [0.5, 0.0, 1.0, 0.5]})
    assert out.name == "a Cropped.png"
    with Image.open(out) as got:
        assert got.size == (200, 100)
        assert got.getpixel((0, 0)) == BLUE


def test_image_crop_applies_the_exif_orientation_first(tmp_path: Path) -> None:
    src = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored 400x200, upright it is 200x400
    Image.new("RGB", (400, 200), RED).save(src, exif=exif)
    (out,) = done("tools.image.crop", [src], {"crop": [0, 0, 1, 0.5]})
    with Image.open(out) as got:
        assert got.size == (200, 200)
        assert 0x0112 not in got.getexif()


def test_image_crop_never_makes_an_empty_image(tmp_path: Path) -> None:
    src = photo(tmp_path / "a.png")
    (out,) = done("tools.image.crop", [src], {"crop": [1, 1, 1, 1]})
    with Image.open(out) as got:
        assert got.size == (1, 1)


def test_image_strip_removes_exif_gps_and_keeps_the_profile(tmp_path: Path) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[0x010F] = "Acme Camera"  # Make
    exif.get_ifd(0x8825)[1] = "N"  # GPSLatitudeRef
    src = photo(tmp_path / "a.jpg", exif=exif, icc_profile=profile)
    with Image.open(src) as im:
        assert im.getexif().get_ifd(0x8825)  # the sample really has GPS
    (out,) = done("tools.image.strip", [src])
    assert out.name == "a Stripped.jpg"
    with Image.open(out) as got:
        assert len(got.getexif()) == 0 and "exif" not in got.info
        assert got.info["icc_profile"] == profile
        assert got.size == (400, 200)


def test_image_strip_png_text_chunks(tmp_path: Path) -> None:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Author", "Someone")
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8), RED).save(src, pnginfo=info)
    (out,) = done("tools.image.strip", [src])
    with Image.open(out) as got:
        assert got.info.get("Author") is None


def test_image_tools_refuse_what_they_cannot_keep(tmp_path: Path) -> None:
    gif = tmp_path / "a.gif"
    Image.new("P", (8, 8)).save(gif, save_all=True, append_images=[Image.new("P", (8, 8))])
    result = run("tools.image.resize.half", [gif])
    assert not result["ok"] and "not resized" in result["message"]
    assert not list(tmp_path.glob("*.part"))


# --- PDF -------------------------------------------------------------------------------------
def texts(path: Path) -> list[str]:
    with pymupdf.open(path) as doc:
        return [page.get_text().strip() for page in doc]


def test_pdf_merge_in_natural_order(tmp_path: Path) -> None:
    ten = make_pdf(tmp_path / "part 10.pdf", pages=1)
    two = make_pdf(tmp_path / "part 2.pdf", pages=2)
    (out,) = done("tools.pdf.merge", [ten, two])  # dropped in the "wrong" order
    assert out.name == "part 2 Merged.pdf"
    assert texts(out) == ["Page 1", "Page 2", "Page 1"]  # part 2 (two pages), then part 10


def test_pdf_split_one_file_per_page(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "book.pdf", pages=3)
    outs = done("tools.pdf.split", [src])
    assert [o.relative_to(tmp_path).as_posix() for o in outs] == [
        f"book split/book {n:03d}.pdf" for n in (1, 2, 3)
    ]
    assert [texts(o) for o in outs] == [["Page 1"], ["Page 2"], ["Page 3"]]


def test_pdf_split_a_one_page_pdf_fails_cleanly(tmp_path: Path) -> None:
    result = run("tools.pdf.split", [make_pdf(tmp_path / "one.pdf", pages=1)])
    assert not result["ok"] and "only one page" in result["message"]
    assert not (tmp_path / "one split").exists()  # the empty folder is removed again


def test_pdf_split_twice_never_mixes_folders(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "book.pdf", pages=2)
    done("tools.pdf.split", [src])
    outs = done("tools.pdf.split", [src])
    assert outs[0].parent.name == "book split (2)"


@pytest.mark.parametrize(("key", "degrees"), [("cw", 90), ("ccw", 270), ("180", 180)])
def test_pdf_rotate(tmp_path: Path, key: str, degrees: int) -> None:
    src = make_pdf(tmp_path / "a.pdf", pages=2)
    with pymupdf.open(src) as doc:  # one page already turned
        doc[1].set_rotation(90)
        doc.saveIncr()
    (out,) = done(f"tools.pdf.rotate.{key}", [src])
    assert out.name == "a Rotated.pdf"
    with pymupdf.open(out) as doc:
        assert [p.rotation for p in doc] == [degrees % 360, (90 + degrees) % 360]


def test_pdf_strip_metadata(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "a.pdf", author="Someone", title="Secret plans", creator="Word")
    with pymupdf.open(src) as doc:
        doc.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'>Someone</x:xmpmeta>")
        doc.saveIncr()
    (out,) = done("tools.pdf.strip", [src])
    assert out.name == "a Stripped.pdf"
    raw = out.read_bytes()
    assert b"Secret plans" not in raw and b"Someone" not in raw
    with pymupdf.open(out) as doc:
        assert not any(doc.metadata[k] for k in ("author", "title", "creator"))
        assert not doc.get_xml_metadata()
    assert texts(out) == ["Page 1", "Page 2", "Page 3"]


def test_pdf_tools_reject_a_password_protected_pdf(tmp_path: Path) -> None:
    src = tmp_path / "locked.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    result = run("tools.pdf.rotate.cw", [src])
    assert not result["ok"] and "password" in result["message"]


# --- video -----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("clips")
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]

    def picture(size: str) -> list[str]:
        return ["-f", "lavfi", "-i", f"testsrc2=duration=2:size={size}:rate=25"]

    return {
        "hd": make_media(
            d / "hd.mp4", *picture("1920x1080"), *tone, "-shortest", *x264, "-c:a", "aac"
        ),
        "small": make_media(
            d / "small.mkv", *picture("320x240"), *tone, "-shortest", *x264, "-c:a", "libopus"
        ),
        "silent": make_media(d / "silent.mp4", *picture("320x240"), *x264),
        "wav": make_media(d / "tone.wav", *tone),
        "mp3": make_media(d / "song.mp3", *tone, "-ar", "44100"),
        "m4a": make_media(d / "song.m4a", *tone, "-c:a", "aac", "-ar", "44100"),
    }


def copy(src: Path, folder: Path) -> Path:
    dst = folder / src.name
    dst.write_bytes(src.read_bytes())
    return dst


def test_video_mute_copies_the_picture(tmp_path: Path, clips: dict[str, Path]) -> None:
    for name in ("hd", "small"):
        src = copy(clips[name], tmp_path)
        (out,) = done("tools.video.mute", [src])
        info = ffmpeg.probe(out)
        assert out.name == f"{src.stem} Muted{src.suffix}"
        assert info.audio is None and info.video == ffmpeg.probe(src).video
        assert (info.width, info.height) == (ffmpeg.probe(src).width, ffmpeg.probe(src).height)


def test_video_extract_audio_to_m4a(tmp_path: Path, clips: dict[str, Path]) -> None:
    for name in ("hd", "small"):  # AAC copied, Opus re-encoded
        src = copy(clips[name], tmp_path)
        (out,) = done("tools.video.extract_audio", [src])
        info = ffmpeg.probe(out)
        assert out.name == f"{src.stem} Audio.m4a"
        assert info.audio == "aac" and info.video is None


def test_video_extract_audio_without_a_track_fails(tmp_path: Path, clips: dict[str, Path]) -> None:
    result = run("tools.video.extract_audio", [copy(clips["silent"], tmp_path)])
    assert not result["ok"] and "no audio track" in result["message"]


def test_video_to_gif(tmp_path: Path, clips: dict[str, Path]) -> None:
    (out,) = done("tools.video.gif", [copy(clips["hd"], tmp_path)])
    assert out.suffix == ".gif"
    with Image.open(out) as im:
        assert im.format == "GIF" and im.width <= 640


def test_video_resize_shrinks_the_short_side(tmp_path: Path, clips: dict[str, Path]) -> None:
    src = copy(clips["hd"], tmp_path)
    (out,) = done("tools.video.resize.720p", [src])
    info = ffmpeg.probe(out)
    assert out.name == "hd 720p.mp4"
    assert (info.width, info.height) == (1280, 720) and info.audio == "aac"
    assert info.duration == pytest.approx(2, abs=0.3)


def test_video_resize_never_enlarges(tmp_path: Path, clips: dict[str, Path]) -> None:
    result = run("tools.video.resize.1080p", [copy(clips["small"], tmp_path)])
    (out,) = result["outputs"]
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (320, 240)
    assert result["notes"] == ["Already 1080p or smaller; not enlarged"]


# --- audio -----------------------------------------------------------------------------------
@pytest.mark.parametrize("kbps", [96, 128, 192, 320])
def test_audio_bitrate_mp3_and_m4a(tmp_path: Path, clips: dict[str, Path], kbps: int) -> None:
    for name, ext in (("wav", "mp3"), ("mp3", "mp3"), ("m4a", "m4a")):
        src = copy(clips[name], tmp_path)
        (out,) = done(f"tools.audio.bitrate.{kbps}", [src])
        assert out.name == f"{src.stem} {kbps}k.{ext}"
        # 2 s of audio: size / duration is the bitrate. MP3 is constant; AAC of a plain tone
        # takes fewer bits than it is allowed.
        rate = out.stat().st_size * 8 / 2 / 1000
        assert rate <= kbps * 1.2 and (ext == "m4a" or rate == pytest.approx(kbps, rel=0.2))


def test_audio_normalize_keeps_format_and_sample_rate(
    tmp_path: Path, clips: dict[str, Path]
) -> None:
    for name, codec in (("mp3", "mp3"), ("m4a", "aac"), ("wav", "pcm_s16le")):
        src = copy(clips[name], tmp_path)
        (out,) = done("tools.audio.normalize", [src])
        info = ffmpeg.probe(out)
        assert out.name == f"{src.stem} Normalized{src.suffix}"
        assert info.audio == codec
        assert info.sample_rate == ffmpeg.probe(src).sample_rate


def test_audio_normalize_reaches_the_target_loudness(
    tmp_path: Path, clips: dict[str, Path]
) -> None:
    (out,) = done("tools.audio.normalize", [copy(clips["wav"], tmp_path)])
    proc = subprocess.run(
        [FF, "-hide_banner", "-nostdin", "-i", str(out), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = proc.stderr.rsplit("Integrated loudness:", 1)[1]
    lufs = float(summary.split("I:")[1].split("LUFS")[0])
    assert lufs == pytest.approx(-16, abs=1.5)


def test_normalize_args_never_copy_and_ask_for_the_source_rate() -> None:
    info = ffmpeg.MediaInfo(10.0, None, "mp3", sample_rate=44100)
    args, needs = audio.normalize_args(info, "mp3")
    assert needs == {"libmp3lame"} and "copy" not in args
    assert args[args.index("-ar") + 1] == "44100"
    deep = ffmpeg.MediaInfo(10.0, None, "flac", deep_audio=True)
    assert audio.normalize_args(deep, "wav")[1] == {"pcm_s24le"}


def test_probe_reads_the_sample_rate() -> None:
    text = "  Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 128 kb/s"
    assert ffmpeg.parse_probe(text).sample_rate == 44100
    assert ffmpeg.parse_probe("  Stream #0:0: Audio: mp3").sample_rate == 0


def test_kinds_of_the_new_ops() -> None:
    for op_id, kind in (("tools.pdf.merge", Kind.PDF), ("tools.video.mute", Kind.VIDEO)):
        assert registry.get(op_id).kinds == {kind}


# --- review follow-ups -------------------------------------------------------------------------
def test_image_edits_keep_the_dpi_and_drop_the_stale_pixel_size(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0xA002] = 400  # PixelXDimension
    exif.get_ifd(0x8769)[0xA003] = 200
    exif[0x010F] = "Acme"
    src = photo(tmp_path / "scan.jpg", exif=exif, dpi=(300, 300))
    for op_id in ("tools.image.rotate.cw", "tools.image.resize.half"):
        (out,) = done(op_id, [src])
        with Image.open(out) as got:
            assert tuple(round(d) for d in got.info["dpi"]) == (300, 300)
            tags = got.getexif()
            assert tags[0x010F] == "Acme"
            assert 0xA002 not in tags.get_ifd(0x8769) and 0xA003 not in tags.get_ifd(0x8769)
        out.unlink()


SOS = bytes([0xFF, 0xDA])  # a JPEG's start-of-scan marker


def test_image_strip_jpeg_is_not_re_encoded(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x010F] = "Acme"
    src = photo(tmp_path / "a.jpg", exif=exif, quality=77)
    (out,) = done("tools.image.strip", [src])
    raw = out.read_bytes()
    assert b"Acme" not in raw and b"Exif" not in raw
    scan = src.read_bytes().index(SOS)  # the picture data is byte for byte the same
    assert raw[raw.index(SOS) :] == src.read_bytes()[scan:]


def test_image_strip_jpeg_with_an_orientation_is_turned_upright(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    src = photo(tmp_path / "phone.jpg", exif=exif)
    (out,) = done("tools.image.strip", [src])
    with Image.open(out) as got:
        assert got.size == (200, 400) and 0x0112 not in got.getexif()


def test_video_resize_note_is_skipped_when_the_size_is_unknown(
    tmp_path: Path, clips: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = ffmpeg.probe
    unknown = lambda p: ffmpeg.MediaInfo(2.0, "h264", "aac")
    monkeypatch.setattr(ffmpeg, "probe", unknown)
    result = run("tools.video.resize.720p", [copy(clips["hd"], tmp_path)])
    monkeypatch.setattr(ffmpeg, "probe", probe)
    assert result["ok"], result["message"]
    assert "notes" not in result


ALL_TOOLS = [
    ("tools.image.resize.half", "a.png"),
    ("tools.image.rotate.cw", "a.png"),
    ("tools.image.strip", "a.png"),
    ("tools.pdf.merge", "a.pdf"),
    ("tools.pdf.split", "a.pdf"),
    ("tools.pdf.rotate.cw", "a.pdf"),
    ("tools.pdf.strip", "a.pdf"),
]


@pytest.mark.parametrize(("op_id", "name"), ALL_TOOLS)
def test_a_cancelled_tool_writes_nothing(tmp_path: Path, op_id: str, name: str) -> None:
    src = photo(tmp_path / name) if name.endswith("png") else make_pdf(tmp_path / name)
    ctx = JobContext("t", cancel_flags={"t"})
    func = worker.resolve(registry.get(op_id).func)
    paths = [src, make_pdf(tmp_path / "b.pdf")] if op_id.endswith("merge") else [src]
    assert func(paths, variants.params_for(op_id), ctx) == []
    assert not ctx.reserved


def test_several_files_are_each_done_and_a_bad_one_loses_only_itself(tmp_path: Path) -> None:
    good, other = photo(tmp_path / "a.png"), photo(tmp_path / "b.jpg")
    gif = tmp_path / "c.gif"
    Image.new("P", (8, 8)).save(gif, save_all=True, append_images=[Image.new("P", (8, 8))])
    result = run("tools.image.rotate.cw", [good, gif, other])
    assert not result["ok"] and "c.gif" in result["message"]
    assert sorted(o.name for o in result["outputs"]) == ["a Rotated.png", "b Rotated.jpg"]


def test_pdf_merge_with_a_password_protected_pdf_fails_and_writes_nothing(tmp_path: Path) -> None:
    locked = tmp_path / "locked.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    result = run("tools.pdf.merge", [make_pdf(tmp_path / "a.pdf"), locked])
    assert not result["ok"] and "password" in result["message"]
    assert not list(tmp_path.glob("*Merged*"))
