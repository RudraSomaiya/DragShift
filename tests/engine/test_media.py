"""Audio and video converters (PRD §6.1 audio and video rows, FR-4.5): outputs, stream copy,
progress, cancel.

Every sample is generated here with ffmpeg's lavfi sources; nothing binary is committed.
Converters run in-process, except for the tests at the bottom, which use the real worker pool.
"""

import hashlib
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from dragshift.engine import ffmpeg, registry
from dragshift.engine.converters import media
from dragshift.engine.ffmpeg import FFmpegError, MediaInfo, parse_probe, progress_seconds
from dragshift.engine.jobs import JobManager
from dragshift.engine.worker import JobContext, final_path, run_job

FF = imageio_ffmpeg.get_ffmpeg_exe()
TONE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100"]
H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
H264_444 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p", "-c:a", "aac"]
MPEG4 = ["-shortest", "-c:v", "mpeg4", "-c:a", "libmp3lame"]  # AVI as old cameras made it


def video_src(size: str, seconds: float = 2) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=25"]


def make(path: Path, *args: str) -> Path:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", *args, str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    av = [*video_src("160x120"), *TONE, "-shortest"]
    return {
        "wav": make(d / "tone.wav", *TONE),
        "flac": make(d / "tone.flac", *TONE),
        "flac24": make(d / "deep.flac", *TONE, "-sample_fmt", "s32", "-bits_per_raw_sample", "24"),
        "mkv": make(d / "clip.mkv", *av, *H264, "-c:a", "aac"),
        "444": make(d / "full.mkv", *av, *H264_444),
        "odd": make(d / "odd.avi", *video_src("161x121"), *TONE, *MPEG4),
        "silent": make(d / "silent.mp4", *video_src("160x120"), *H264),
        "wide": make(d / "wide.mp4", *video_src("1280x720", 1), *H264),
        "long": make(d / "long.mp4", *video_src("320x240", 30), *H264),
    }


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Progress:
    """A JobContext queue that records every progress report."""

    def __init__(self) -> None:
        self.reports: list[float] = []

    def put(self, item: tuple[str, float, str]) -> None:
        self.reports.append(item[1])


def convert(func, src: Path, queue: Progress | None = None) -> Path:
    """Run a converter in-process and finish its output the way JobManager does."""
    (part,) = func([src], {}, JobContext("t", queue))
    return part.rename(final_path(part))


def info(path: Path) -> MediaInfo:
    return ffmpeg.probe(path)


def about(seconds: float | None, expected: float = 2.0) -> bool:
    return seconds is not None and abs(seconds - expected) < 0.2


# --- audio -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("func", "source", "codec"),
    [
        (media.audio_to_mp3, "wav", "mp3"),
        (media.audio_to_m4a, "wav", "aac"),
        (media.audio_to_wav, "flac", "pcm_s16le"),
        (media.audio_to_flac, "wav", "flac"),
        (media.audio_to_ogg, "wav", "vorbis"),
        (media.audio_to_opus, "wav", "opus"),
        (media.audio_to_aiff, "wav", "pcm_s16be"),
    ],
)
def test_every_audio_target(samples: dict[str, Path], func, source: str, codec: str) -> None:
    src = samples[source]
    before = sha(src)
    out = convert(func, src)
    got = info(out)
    assert got.audio == codec and got.video is None
    assert about(got.duration), got.duration
    assert sha(src) == before  # the source is never touched (rule 4)
    out.unlink()


def test_24_bit_audio_stays_24_bit(samples: dict[str, Path]) -> None:
    assert info(samples["flac24"]).deep_audio
    for func, codec in ((media.audio_to_wav, "pcm_s24le"), (media.audio_to_aiff, "pcm_s24be")):
        out = convert(func, samples["flac24"])
        assert info(out).audio == codec
        out.unlink()


# --- video -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("func", "source", "video", "audio"),
    [
        (media.video_to_mp4, "mkv", "h264", "aac"),  # copied: a remux
        (media.video_to_mov, "mkv", "h264", "aac"),
        (media.video_to_mkv, "odd", "mpeg4", "mp3"),  # MKV takes both as they are
        (media.video_to_webm, "mkv", "vp9", "opus"),
        (media.video_to_gif, "mkv", "gif", None),
        (media.video_to_mp3, "mkv", None, "mp3"),
        (media.video_to_avi, "mkv", "mpeg4", "mp3"),
    ],
)
def test_every_video_target(
    samples: dict[str, Path], func, source: str, video: str | None, audio: str | None
) -> None:
    src = samples[source]
    before = sha(src)
    out = convert(func, src)
    got = info(out)
    assert (got.video, got.audio) == (video, audio)
    assert about(got.duration), got.duration
    assert sha(src) == before
    out.unlink()


def test_odd_sizes_are_padded_when_re_encoding(samples: dict[str, Path]) -> None:
    out = convert(media.video_to_mp4, samples["odd"])  # MPEG-4 Part 2 cannot be copied to MP4
    got = info(out)
    assert (got.video, got.width, got.height) == ("h264", 162, 122)
    out.unlink()


def test_4_4_4_video_is_re_encoded_for_mp4_but_copied_to_mkv(samples: dict[str, Path]) -> None:
    src = info(samples["444"])
    assert src.pix_fmt == "yuv444p"
    args, needs = media.video_args(src, media.VIDEO_TARGETS["mp4"])
    assert "libx264" in needs and "yuv420p" in args
    args, needs = media.video_args(src, media.VIDEO_TARGETS["mkv"])
    assert args[args.index("-c:v") + 1] == "copy" and needs == set()
    out = convert(media.video_to_mp4, samples["444"])
    assert info(out).pix_fmt == "yuv420p"  # plays in the Windows player and QuickTime
    out.unlink()


def test_mkv_to_mp4_is_a_lossless_copy(samples: dict[str, Path]) -> None:
    args, needs = media.video_args(info(samples["mkv"]), media.VIDEO_TARGETS["mp4"])
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "copy"
    assert needs == set()


def test_hevc_copied_to_mp4_gets_the_apple_tag() -> None:
    hevc = MediaInfo(10.0, "hevc", "aac", 3840, 2160, "yuv420p10le")
    args, _ = media.video_args(hevc, media.VIDEO_TARGETS["mov"])
    assert args[args.index("-c:v") + 1 : args.index("-c:v") + 4] == ["copy", "-tag:v", "hvc1"]


def test_gif_is_at_most_640_wide_at_15_fps(samples: dict[str, Path]) -> None:
    out = convert(media.video_to_gif, samples["wide"])
    got = info(out)
    assert (got.width, got.height) == (640, 360)
    # Frames, not the reported rate: GIF stores delays in 1/100 s, so 1/15 s reads as 16.67 fps.
    with Image.open(out) as im:
        assert im.n_frames == 15  # one second at 15 fps
    out.unlink()


def test_video_without_sound(samples: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="no audio track"):
        media.video_to_mp3([samples["silent"]], {}, JobContext("t"))
    out = convert(media.video_to_webm, samples["silent"])
    assert (info(out).video, info(out).audio) == ("vp9", None)
    out.unlink()


def test_a_file_that_is_not_media_fails_with_a_readable_message(tmp_path: Path) -> None:
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"this is not a video" * 100)
    result = run_job(media.video_to_mkv.__module__ + ":video_to_mkv", [[src]], {}, JobContext("t"))
    assert not result["ok"]
    assert result["message"].startswith("FFmpegError: ")
    assert "Invalid data" in result["message"] and str(tmp_path) not in result["message"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fake.mp4"]


def test_unicode_file_names(samples: dict[str, Path], tmp_path: Path) -> None:
    src = tmp_path / "Café Ünïcode 日本.wav"
    src.write_bytes(samples["wav"].read_bytes())
    out = convert(media.audio_to_mp3, src)
    assert out.name == "Café Ünïcode 日本.mp3" and info(out).audio == "mp3"


# --- progress and cancel (FR-4.5) ------------------------------------------------------------
def test_progress_climbs_to_one(samples: dict[str, Path]) -> None:
    queue = Progress()
    out = convert(media.video_to_webm, samples["long"], queue)
    reports = queue.reports
    assert reports == sorted(reports) and reports[-1] == 1.0
    assert any(0.05 < r < 0.95 for r in reports), reports  # real steps, not only 0 and 1
    out.unlink()


class CancelAfterProgress:
    """A cancel flag that turns on once the job has made some real progress."""

    def __init__(self, queue: Progress) -> None:
        self.queue = queue
        self.at: float | None = None

    def __contains__(self, job_id: object) -> bool:
        if self.at is None and any(r > 0.05 for r in self.queue.reports):
            self.at = time.monotonic()
        return self.at is not None


def test_cancel_kills_ffmpeg_and_deletes_the_partial_output(
    samples: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "long.mp4"
    src.write_bytes(samples["long"].read_bytes())
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        procs.append(real_popen(*args, **kwargs))
        return procs[-1]

    monkeypatch.setattr(ffmpeg.subprocess, "Popen", spy)
    queue = Progress()
    flags = CancelAfterProgress(queue)
    result = run_job(
        "dragshift.engine.converters.media:video_to_webm",
        [[src]],
        {},
        JobContext("t", queue, flags),
    )
    assert result == {"ok": False, "outputs": [], "message": "Cancelled", "cancelled": True}
    assert time.monotonic() - flags.at < 2  # cancel acts within a few polls
    assert max(queue.reports) < 0.99  # it really was stopped part-way
    (proc,) = [p for p in procs if "-progress" in p.args]  # not the probe
    assert proc.returncode is not None  # ffmpeg is gone
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.mp4"]  # no .part, no output


# --- parsing and choosing a binary -----------------------------------------------------------
def test_parse_probe() -> None:
    text = """
  Duration: 01:02:03.50, start: 0.000000, bitrate: 320 kb/s
  Stream #0:0: Audio: flac, 96000 Hz, stereo, s32 (24 bit)
  Stream #0:1: Video: mjpeg (Baseline), yuvj420p(pc), 600x600, 90k tbn (attached pic)
"""
    got = parse_probe(text)
    assert got.duration == 3723.5
    assert (got.audio, got.video, got.deep_audio) == ("flac", None, True)  # cover art skipped
    assert parse_probe("  Duration: N/A, bitrate: N/A").duration is None
    lossy = parse_probe("  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz, stereo, fltp")
    assert not lossy.deep_audio


def test_progress_lines() -> None:
    assert progress_seconds("out_time_us=1500000\n") == 1.5
    assert progress_seconds("out_time_us=N/A") is None
    assert progress_seconds("out_time_us=-9223372036854775807") is None
    assert progress_seconds("progress=continue") is None


def test_error_lines_lose_the_noise() -> None:
    lines = ["[libx264 @ 000001c2a3b4] height not divisible by 2 (161x121)", "Conversion failed!"]
    assert ffmpeg._last_error(lines[:1]) == "height not divisible by 2 (161x121)"
    path = Path("C:/x/a.mp4")
    assert ffmpeg._last_error([f"{path}: Invalid data"], path) == "Invalid data"
    assert ffmpeg._last_error([]) == "ffmpeg failed"


def test_ffmpeg_on_path_is_preferred_only_with_the_encoders_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = {"path/ffmpeg": frozenset({"libx264", "aac"}), "bundled": frozenset({"libvpx-vp9"})}
    monkeypatch.setattr(ffmpeg, "_candidates", lambda: list(builds))
    monkeypatch.setattr(ffmpeg, "encoders", builds.__getitem__)
    assert ffmpeg.ffmpeg_exe() == "path/ffmpeg"
    assert ffmpeg.ffmpeg_exe({"libx264"}) == "path/ffmpeg"
    assert ffmpeg.ffmpeg_exe({"libvpx-vp9"}) == "bundled"
    with pytest.raises(FFmpegError, match="libsvtav1"):
        ffmpeg.ffmpeg_exe({"libsvtav1"})


def test_the_bundled_ffmpeg_has_every_encoder_we_use() -> None:
    used = {"gif"}
    for target in (*media.AUDIO_TARGETS.values(), *media.VIDEO_TARGETS.values()):
        for codec in (target.audio, target.video, target.deep_audio):
            if codec is not None:
                used.add(codec.encoder)
    assert used <= ffmpeg.encoders(FF), used - ffmpeg.encoders(FF)


# --- petals (§6.1 order) ---------------------------------------------------------------------
def labels(*names: str) -> list[str]:
    return [o.label for o in registry.ops_for([Path(n) for n in names], "convert")]


def test_petals_follow_the_prd_order_without_the_source_format() -> None:
    assert labels("a.mp3") == ["M4A", "WAV", "FLAC", "OGG", "OPUS", "AIFF"]
    assert labels("a.wma") == ["MP3", "M4A", "WAV", "FLAC", "OGG", "OPUS", "AIFF"]
    assert "AIFF" not in labels("a.aif")
    assert labels("a.mp4") == ["MOV", "MKV", "WEBM", "GIF", "MP3", "AVI"]
    assert labels("a.m4v") == ["MP4", "MOV", "MKV", "WEBM", "GIF", "MP3", "AVI"]
    assert labels("a.mp3", "b.mp4") == []  # mixed kinds share no converter


# --- through the real worker pool ------------------------------------------------------------
@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


def test_progress_and_cancel_through_the_worker_pool(
    qtbot, jm: JobManager, samples: dict[str, Path], tmp_path: Path
) -> None:
    """FR-4.5 end to end: progress arrives on the Qt thread, and Cancel leaves nothing behind.
    A .part still held open by ffmpeg could not be deleted, so an empty folder also proves the
    process was killed."""
    src = tmp_path / "long.mp4"
    src.write_bytes(samples["long"].read_bytes())
    fractions: list[float] = []
    jm.progressed.connect(lambda _id, fraction, _text: fractions.append(fraction))
    job = jm.submit("convert.video.webm", [src])
    qtbot.waitUntil(lambda: any(f > 0.05 for f in fractions), timeout=60_000)
    with qtbot.waitSignal(jm.finished, timeout=10_000) as blocker:
        jm.cancel(job)
    assert blocker.args[1].message == "Cancelled"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.mp4"]


def test_a_conversion_through_the_worker_pool(
    qtbot, jm: JobManager, samples: dict[str, Path], tmp_path: Path
) -> None:
    src = tmp_path / "clip.mkv"
    src.write_bytes(samples["mkv"].read_bytes())
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit("convert.video.mp4", [src])
    result = blocker.args[1]
    assert result.ok, result.message
    assert [p.name for p in result.outputs] == ["clip.mp4"]
    assert info(result.outputs[0]).video == "h264"
