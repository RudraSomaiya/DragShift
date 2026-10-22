"""Compress + size presets (PRD FR-4.3, FR-4.4, §6.2) for image, PDF, video and audio.

Every sample is generated here (Pillow, PyMuPDF, ffmpeg's lavfi sources); nothing binary is
committed. Tools run through ``run_job`` with a plain JobContext, except for the tests at the
bottom, which use the real worker pool.
"""

import hashlib
import subprocess
from collections.abc import Iterator
from pathlib import Path

import imageio_ffmpeg
import pymupdf
import pytest
from PIL import Image, ImageCms

from dragshift.engine import ffmpeg, presets, registry
from dragshift.engine.ffmpeg import MediaInfo
from dragshift.engine.jobs import JobManager
from dragshift.engine.presets import MB, Target, human_size, mb_text, size_note, target_for
from dragshift.engine.tools import audio, video
from dragshift.engine.worker import JobContext, final_path, run_job
from dragshift.ui import job_toasts

FF = imageio_ffmpeg.get_ffmpeg_exe()
KINDS = ("image", "pdf", "video", "audio")
RING = ["Email 25 MB", "WhatsApp 16 MB", "Discord 10 MB", "Small 50 %", "Custom…"]


# --- helpers ---------------------------------------------------------------------------------
def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(kind: str, src: Path, params: dict, ctx: JobContext | None = None) -> dict:
    """Run a Compress tool the way the pool does, check the source is untouched, finish the
    outputs."""
    before = sha(src)
    result = run_job(f"dragshift.engine.tools.{kind}:compress", [[src]], params,
                     ctx or JobContext("t"))  # fmt: skip
    assert sha(src) == before  # CLAUDE.md rule 4
    result["outputs"] = [part.rename(final_path(part)) for part in result["outputs"]]
    return result


def compressed(kind: str, src: Path, params: dict) -> tuple[Path, list[str]]:
    result = run(kind, src, params)
    assert result["ok"], result["message"]
    (out,) = result["outputs"]
    return out, result.get("notes", [])


def noise(path: Path, size: tuple[int, int] = (800, 600), **save) -> Path:
    """Random pixels: the worst case for every codec, so sizes are big and predictable."""
    Image.effect_noise(size, 90).convert("RGB").save(path, **save)
    return path


def make_media(path: Path, *args: str) -> Path:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", *args, str(path)], check=True)
    return path


def make_pdf(path: Path, image: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, filename=str(image))
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=24)
    doc.save(path)
    doc.close()
    return path


class Progress:
    def __init__(self) -> None:
        self.fractions: list[float] = []

    def put(self, item: tuple[str, float, str]) -> None:
        self.fractions.append(item[1])


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    picture = ["-f", "lavfi", "-i", "testsrc2=duration=4:size=1280x720:rate=25"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-pix_fmt", "yuv420p"]
    return {
        "clip": make_media(
            d / "clip.mp4", *picture, *tone, "-shortest", *x264, "-c:a", "aac", "-b:a", "256k"
        ),
        "silent": make_media(d / "silent.mkv", *picture, *x264),
        # A phone video: stored 1280x720, shown upright as 720x1280.
        "portrait": make_media(
            d / "portrait.mp4", "-display_rotation", "90", "-i", str(d / "clip.mp4"), "-c", "copy"
        ),
        "wav": make_media(d / "tone.wav", *tone),
        "m4a": make_media(d / "tone.m4a", *tone, "-c:a", "aac", "-b:a", "256k"),
    }


def copy(src: Path, folder: Path) -> Path:
    dst = folder / src.name
    dst.write_bytes(src.read_bytes())
    return dst


# --- presets (pure) --------------------------------------------------------------------------
def test_each_preset_gives_its_size() -> None:
    big = 100 * MB
    assert target_for({"preset": "email"}, big) == Target(size=25 * MB, limit=25 * MB)
    assert target_for({"preset": "whatsapp"}, big).size == 16 * MB
    assert target_for({"preset": "discord"}, big).size == 10 * MB
    assert target_for({"preset": "small"}, big) == Target(size=50 * MB, limit=50 * MB)
    assert target_for({}, big).size == 50 * MB  # a drop on Compress itself runs Small


def test_a_file_under_the_limit_is_not_made_larger() -> None:
    goal = target_for({"preset": "discord"}, 3 * MB)
    assert goal == Target(size=3 * MB, limit=10 * MB, already_under=True)
    assert size_note(goal, 2 * MB) == "Already under 10 MB"


def test_custom_takes_a_size_or_a_quality() -> None:
    assert target_for({"preset": "custom", "target_mb": 2.5}, 9 * MB).size == int(2.5 * MB)
    # The App always sends the Settings quality; without target_mb it is the dialog's.
    assert target_for({"preset": "custom", "quality": 40}, MB) == Target(quality=40)
    assert target_for({"preset": "custom", "quality": 500}, MB).quality == 100
    with pytest.raises(ValueError, match="more than 0"):
        target_for({"preset": "custom", "target_mb": 0}, MB)


def test_an_unknown_preset_fails_with_the_known_ones() -> None:
    with pytest.raises(ValueError, match="email, whatsapp, discord, small, custom"):
        target_for({"preset": "tiktok"}, MB)


def test_a_missed_target_is_retried_scaled_by_the_overshoot() -> None:
    goal = Target(size=100_000, limit=100_000)
    assert presets.retarget(goal, 99_000) is None
    assert presets.retarget(goal, 125_000).size == 77_600  # 100k * 0.8 * 0.97
    assert presets.retarget(Target(quality=50), 125_000) is None


def test_the_fr_4_4_note() -> None:
    goal = target_for({"preset": "discord"}, 200 * MB)
    assert size_note(goal, int(14.2 * MB)) == "Couldn't reach 10 MB — got 14.2 MB"
    assert size_note(goal, int(10.02 * MB)) == "Couldn't reach 10 MB — got 10.02 MB"
    assert size_note(goal, 9 * MB) == ""
    assert size_note(Target(quality=50), 9 * MB) == ""
    assert mb_text(int(2.5 * MB)) == "2.5 MB"
    assert mb_text(640 * 1024) == "640 KB"
    assert mb_text(12) == "12 B"


def test_sizes_are_counted_the_way_explorer_and_the_toast_count_them() -> None:
    assert MB == 1_048_576
    assert human_size(int(14.2 * MB)) == "14 MB"
    assert human_size(int(2.5 * MB)) == "2.5 MB"
    assert job_toasts.human_size is human_size  # one formatter for toasts and notes


def test_ring_ops_carry_their_preset() -> None:
    assert presets.params_for("tools.video.compress.discord") == {"preset": "discord"}
    assert presets.params_for("tools.video.compress") == {}
    assert presets.params_for("actions.copy_to.1") == {}
    assert presets.is_compress("tools.pdf.compress")
    assert presets.is_compress("tools.pdf.compress.custom")
    assert not presets.is_compress("convert.image.png")


# --- registry: the Compress petal and its ring (FR-4.3) --------------------------------------
@pytest.mark.parametrize(
    ("name", "kind"),
    [("a.jpg", "image"), ("a.pdf", "pdf"), ("a.mp4", "video"), ("a.mp3", "audio")],
)
def test_every_kind_has_compress_with_the_preset_ring(name: str, kind: str) -> None:
    ops = registry.ops_for([Path(name)], "tools")
    assert ops[0].id == f"tools.{kind}.compress"
    assert ops[0].label == "Compress ▸"
    ring = registry.children_for(ops[0], [Path(name)])
    assert [o.label for o in ring] == RING
    assert [o.dialog for o in ring] == [None, None, None, None, "compress"]
    assert {o.func for o in ring} == {ops[0].func}  # one worker function, preset in params


# --- image -----------------------------------------------------------------------------------
def test_jpeg_small_halves_it_and_keeps_the_format(tmp_path: Path) -> None:
    src = noise(tmp_path / "photo.jpeg", quality=95)
    out, notes = compressed("image", src, {"preset": "small"})
    assert out.name == "photo Compressed.jpeg"
    assert out.stat().st_size <= src.stat().st_size // 2
    assert notes == []
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (800, 600)  # quality alone was enough


def test_a_tight_target_scales_the_image_down(tmp_path: Path) -> None:
    src = noise(tmp_path / "photo.jpg", (2000, 1500), quality=95)
    out, notes = compressed("image", src, {"preset": "custom", "target_mb": 0.1})
    assert out.stat().st_size <= 0.1 * MB
    assert notes == []
    with Image.open(out) as im:
        assert im.width < 2000
        assert im.width / im.height == pytest.approx(4 / 3, abs=0.01)


def test_an_unreachable_target_writes_the_smallest_and_says_so(tmp_path: Path) -> None:
    src = noise(tmp_path / "photo.jpg", quality=95)
    result = run("image", src, {"preset": "custom", "target_mb": 0.0001})
    assert result["ok"]
    (note,) = result["notes"]
    assert note.startswith("Couldn't reach 104 B — got ")
    assert result["outputs"][0].stat().st_size < src.stat().st_size


def test_png_stays_png_with_fewer_colours_and_its_transparency(tmp_path: Path) -> None:
    src = tmp_path / "shot.png"
    Image.radial_gradient("L").resize((600, 600)).convert("RGBA").save(src)
    out, _ = compressed("image", src, {"preset": "small"})
    assert out.suffix == ".png"
    assert out.stat().st_size <= src.stat().st_size // 2
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.convert("RGBA").getchannel("A").getextrema() == (255, 255)


def test_png_becomes_webp_only_when_allowed_and_needed(tmp_path: Path) -> None:
    src = noise(tmp_path / "noise.png", (600, 400))  # noise defeats PNG, even with 256 colours
    tight = {"preset": "custom", "target_mb": 0.1}
    out, _ = compressed("image", src, tight)
    assert out.suffix == ".png"
    with Image.open(out) as im:
        assert im.width < 600  # PNG had to be scaled down
    out, _ = compressed("image", src, tight | {"allow_format_change": True})
    assert out.name == "noise Compressed.webp"
    assert out.stat().st_size <= 0.1 * MB
    with Image.open(out) as im:
        assert im.format == "WEBP"
    smooth = tmp_path / "gradient.png"
    Image.radial_gradient("L").resize((600, 400)).convert("RGB").save(smooth)
    out, _ = compressed("image", smooth, {"preset": "small", "allow_format_change": True})
    assert out.suffix == ".png"  # 256 colours halve it: a PNG that fits stays PNG


def test_quality_mode(tmp_path: Path) -> None:
    src = noise(tmp_path / "photo.jpg", quality=95)
    low, _ = compressed("image", src, {"preset": "custom", "quality": 20})
    high, _ = compressed("image", src, {"preset": "custom", "quality": 80})
    assert low.stat().st_size < high.stat().st_size < src.stat().st_size


def test_orientation_is_applied_and_exif_and_profile_kept(tmp_path: Path) -> None:
    src = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW to view
    exif[0x010F] = "Camera Maker"
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.effect_noise((300, 200), 90).convert("RGB").save(
        src, quality=95, exif=exif, icc_profile=srgb
    )
    out, _ = compressed("image", src, {"preset": "small"})
    with Image.open(out) as im:
        assert im.size == (200, 300)  # upright
        assert im.getexif().get(0x0112) is None  # so viewers do not turn it again
        assert im.getexif()[0x010F] == "Camera Maker"
        assert im.info["icc_profile"] == srgb


def test_a_camera_mpo_jpeg_is_compressed_as_its_photo(tmp_path: Path) -> None:
    """Many cameras write .jpg as MPO: the photo plus a preview or depth frame."""
    src = tmp_path / "camera.jpg"
    photo = Image.effect_noise((800, 600), 90).convert("RGB")
    photo.save(src, format="MPO", quality=95, save_all=True, append_images=[photo.resize((80, 60))])
    with Image.open(src) as im:
        assert (im.format, im.n_frames) == ("MPO", 2)
    out, _ = compressed("image", src, {"preset": "small"})
    with Image.open(out) as im:
        assert (im.format, im.size) == ("JPEG", (800, 600))


@pytest.mark.parametrize("name", ["anim.gif", "icon.ico"])
def test_formats_compress_cannot_keep_fail_clearly(tmp_path: Path, name: str) -> None:
    src = tmp_path / name
    Image.new("RGB", (32, 32), "red").save(src)
    result = run("image", src, {"preset": "small"})
    assert not result["ok"]
    assert "not compressed" in result["message"]
    assert result["outputs"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]  # no .part left


def test_animated_webp_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "anim.webp"
    frames = [Image.new("RGB", (32, 32), c) for c in ("red", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:])
    result = run("image", src, {"preset": "small"})
    assert "animated" in result["message"]


# --- PDF -------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def heavy_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("pdf")
    return make_pdf(d / "scan.pdf", noise(d / "page.png", (1600, 2000)))


def test_pdf_small_downsamples_images_and_keeps_the_text(heavy_pdf: Path, tmp_path: Path) -> None:
    src = copy(heavy_pdf, tmp_path)
    out, notes = compressed("pdf", src, {"preset": "small"})
    assert out.name == "scan Compressed.pdf"
    assert out.stat().st_size <= src.stat().st_size // 2
    assert notes == []
    with pymupdf.open(out) as doc:
        assert doc.page_count == 2
        assert "Page 2" in doc[1].get_text()


def test_pdf_unreachable_target_keeps_the_smallest(heavy_pdf: Path, tmp_path: Path) -> None:
    src = copy(heavy_pdf, tmp_path)
    result = run("pdf", src, {"preset": "custom", "target_mb": 0.001})
    assert result["ok"]
    assert result["notes"][0].startswith("Couldn't reach 1 KB — got ")
    with pymupdf.open(result["outputs"][0]) as doc:
        assert doc.page_count == 2


def test_pdf_quality_mode(heavy_pdf: Path, tmp_path: Path) -> None:
    src = copy(heavy_pdf, tmp_path)
    out, _ = compressed("pdf", src, {"preset": "custom", "quality": 30})
    assert out.stat().st_size < src.stat().st_size


def test_a_text_only_pdf_already_under_the_limit(tmp_path: Path) -> None:
    src = tmp_path / "memo.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Short memo")
    doc.save(src)
    doc.close()
    out, notes = compressed("pdf", src, {"preset": "email"})
    assert notes == ["Already under 25 MB"]
    with pymupdf.open(out) as doc:
        assert "Short memo" in doc[0].get_text()


# --- video -----------------------------------------------------------------------------------
def info(duration: float | None = 60.0, audio: str | None = "aac", height: int = 1080):
    return MediaInfo(duration, "h264", audio, 1920, height, "yuv420p")


def test_the_bitrate_comes_from_the_size_and_duration() -> None:
    plan = video.plan_for(info(60), Target(size=10 * MB))  # Discord, one minute
    # 10 MB * 8 / 60 s = 1398 kb/s, 95 % of it for the streams, 128 for the sound.
    assert (plan.video_kbps, plan.audio_kbps, plan.crf) == (1200, 128, None)
    assert plan.max_side == 720


def test_a_low_bitrate_lowers_the_height_and_the_sound() -> None:
    plan = video.plan_for(info(600), Target(size=25 * MB))  # ten minutes into Email
    assert (plan.audio_kbps, plan.video_kbps) == (64, 268)
    assert plan.max_side == 360


def test_too_low_a_bitrate_falls_back_to_crf() -> None:
    plan = video.plan_for(info(7200), Target(size=10 * MB))  # §5: 2 h into 10 MB
    assert plan.video_kbps is None
    assert plan.crf == video.FALLBACK_CRF
    assert plan.max_side == 360


def test_video_without_sound_or_duration() -> None:
    assert video.plan_for(info(audio=None), Target(size=10 * MB)).audio_kbps == 0
    assert video.plan_for(info(None), Target(size=10 * MB)).crf == video.FALLBACK_CRF


def test_video_quality_maps_to_crf() -> None:
    assert video.plan_for(info(), Target(quality=100)).crf == 18
    assert video.plan_for(info(), Target(quality=0)).crf == 40


def test_video_reaches_its_target_in_two_passes(media: dict[str, Path], tmp_path: Path) -> None:
    src = copy(media["clip"], tmp_path)
    ctx = JobContext("t", Progress())
    result = run("video", src, {"preset": "custom", "target_mb": 0.2}, ctx)
    assert result["ok"], result["message"]
    (out,) = result["outputs"]
    assert out.name == "clip Compressed.mp4"
    assert out.stat().st_size <= 0.2 * MB
    assert "notes" not in result
    got = ffmpeg.probe(out)
    assert (got.video, got.audio) == ("h264", "aac")
    assert (got.width, got.height) == (640, 360)  # 4 s into 0.2 MB leaves ~334 kb/s
    assert abs(got.duration - 4) < 0.2
    fractions = ctx._queue.fractions
    assert fractions == sorted(fractions) and fractions[-1] == 1.0  # a retry never goes back
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip Compressed.mp4", "clip.mp4"]


def test_a_portrait_phone_video_is_capped_on_its_short_side(
    media: dict[str, Path], tmp_path: Path
) -> None:
    src = copy(media["portrait"], tmp_path)
    out, _ = compressed("video", src, {"preset": "custom", "target_mb": 0.2})
    got = ffmpeg.probe(out)
    assert (got.width, got.height) == (360, 640)  # upright, and as sharp as a landscape 360p


def test_video_without_sound(media: dict[str, Path], tmp_path: Path) -> None:
    src = copy(media["silent"], tmp_path)
    out, _ = compressed("video", src, {"preset": "small"})
    assert out.name == "silent Compressed.mp4"
    assert ffmpeg.probe(out).audio is None


def test_video_that_cannot_fit_says_how_close_it_got(
    media: dict[str, Path], tmp_path: Path
) -> None:
    src = copy(media["clip"], tmp_path)
    result = run("video", src, {"preset": "custom", "target_mb": 0.01})
    assert result["ok"], result["message"]
    assert result["notes"][0].startswith("Couldn't reach 10 KB — got ")


def test_an_audio_file_is_not_a_video(media: dict[str, Path], tmp_path: Path) -> None:
    src = copy(media["wav"], tmp_path)
    result = run("video", src, {"preset": "small"})
    assert result["message"] == "ValueError: this file has no video track"


# --- audio -----------------------------------------------------------------------------------
def test_audio_bitrate_from_the_target() -> None:
    song = MediaInfo(240.0, None, "flac")
    # 5 MB * 8 / 240 s = 174.8 kb/s, 97 % = 169.5, rounded down to LAME's 160.
    assert audio.kbps_for(song, Target(size=5 * MB), "mp3") == 160
    assert audio.kbps_for(song, Target(size=5 * MB), "m4a") == 169
    assert audio.kbps_for(song, Target(size=100 * MB), "mp3") == 320
    assert audio.kbps_for(song, Target(size=1000), "mp3") == 32
    assert audio.kbps_for(song, Target(quality=100), "mp3") == 320


def test_audio_format_follows_the_source() -> None:
    assert audio.format_for(MediaInfo(1.0, None, "aac")) == "m4a"
    assert audio.format_for(MediaInfo(1.0, None, "pcm_s16le")) == "mp3"


@pytest.mark.parametrize(("name", "ext", "codec"), [("wav", "mp3", "mp3"), ("m4a", "m4a", "aac")])
def test_audio_small(media: dict[str, Path], tmp_path: Path, name: str, ext: str, codec: str):
    src = copy(media[name], tmp_path)
    out, notes = compressed("audio", src, {"preset": "small"})
    assert out.name == f"tone Compressed.{ext}"
    assert out.stat().st_size <= src.stat().st_size // 2
    assert notes == []
    assert ffmpeg.probe(out).audio == codec


# --- notes, cancel and the worker pool -------------------------------------------------------
def test_a_failed_file_loses_its_note(tmp_path: Path) -> None:
    good = noise(tmp_path / "good.jpg", quality=95)
    bad = tmp_path / "bad.gif"
    Image.new("RGB", (8, 8)).save(bad)
    result = run_job("dragshift.engine.tools.image:compress", [[good], [bad]],
                     {"preset": "custom", "target_mb": 0.0001}, JobContext("t"))  # fmt: skip
    (note,) = result["notes"]  # good.jpg's, not one for the GIF that failed
    assert note.startswith("good.jpg: Couldn't reach")  # which file, as errors say


class CancelPartWay(JobContext):
    """Cancelled once the first progress report arrives: during an ffmpeg pass."""

    def __init__(self) -> None:
        super().__init__("t", Progress())

    def cancelled(self) -> bool:
        return bool(self._queue.fractions)


def test_cancel_kills_the_encode_and_leaves_nothing(media: dict[str, Path], tmp_path: Path):
    src = copy(media["clip"], tmp_path)
    result = run("video", src, {"preset": "small"}, CancelPartWay())
    assert result.get("cancelled")
    assert [p.name for p in tmp_path.iterdir()] == ["clip.mp4"]


@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


def test_the_note_reaches_a_successful_job_result(
    qtbot, jm: JobManager, media: dict[str, Path], tmp_path: Path
) -> None:
    """S4 at test scale: drop on Discord, get a result that is ok and says what happened."""
    src = copy(media["wav"], tmp_path)
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit("tools.audio.compress.discord", [src], {"preset": "discord"})
    result = blocker.args[1]
    assert result.ok
    assert result.message == "Already under 10 MB"
    assert [p.name for p in result.outputs] == ["tone Compressed.mp3"]


def test_the_cli_runs_a_preset_op(media: dict[str, Path], tmp_path: Path, capsys) -> None:
    from dragshift.__main__ import main

    src = copy(media["wav"], tmp_path)
    assert main(["tool", "tools.audio.compress.small", str(src)]) == 0
    assert capsys.readouterr().out.strip().endswith("tone Compressed.mp3")
