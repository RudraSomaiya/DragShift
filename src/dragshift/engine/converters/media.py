"""Audio and video converters (PRD §6.1 audio and video rows), all through ffmpeg.

- A stream that the target container can already hold is copied, not re-encoded: MKV -> MP4
  with H.264/AAC is a lossless remux that takes seconds, and AAC -> M4A loses nothing.
- Re-encoded video is padded to even sizes (yuv420p cannot hold odd ones).
- GIF: one ffmpeg run with a two-pass palette (palettegen, then paletteuse) at 15 fps and at
  most 640 px wide. Video -> MP3 writes the first audio track.
- Subtitles, data tracks and cover art are not carried over.

ffmpeg is only called inside the functions: the UI process imports this module to register Ops
(CONTRIBUTING.md rule 2).
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import op

if TYPE_CHECKING:
    from dragshift.engine.ffmpeg import MediaInfo
    from dragshift.engine.worker import JobContext

AUDIO = frozenset({Kind.AUDIO})
VIDEO = frozenset({Kind.VIDEO})
GIF_FPS = 15
GIF_MAX_WIDTH = 640
EVEN_SIZE = "pad=ceil(iw/2)*2:ceil(ih/2)*2"


@dataclass(frozen=True)
class _Codec:
    encoder: str
    args: tuple[str, ...] = ()
    copies: frozenset[str] = frozenset()  # source codecs written as they are
    copy_420_only: bool = False  # ...but only in 4:2:0 colour, which every player can show


def _codec(
    encoder: str, *args: str, copies: tuple[str, ...] = (), copy_420_only: bool = False
) -> _Codec:
    return _Codec(encoder, args, frozenset(copies), copy_420_only)


@dataclass(frozen=True)
class _Target:
    format: str  # ffmpeg muxer name; the .part suffix hides the real extension
    audio: _Codec | None
    video: _Codec | None = None  # None: an audio-only output
    deep_audio: _Codec | None = None  # for lossless sources above 16 bits
    extra: tuple[str, ...] = ()


_MP3 = _codec("libmp3lame", "-q:a", "2", copies=("mp3",))  # VBR, about 190 kb/s
_AAC = _codec("aac", "-b:a", "192k", copies=("aac",))
_X264 = _codec("libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
               copies=("h264", "hevc"), copy_420_only=True)  # fmt: skip
# MKV holds nearly anything, so more is copied and the colour format does not matter.
_MKV_AUDIO = replace(
    _AAC, copies=frozenset({"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3"})
)
_MKV_VIDEO = replace(_X264, copies=frozenset({"h264", "hevc", "vp8", "vp9", "av1", "mpeg4"}),
                     copy_420_only=False)  # fmt: skip
_VP9 = _codec("libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
              "-cpu-used", "4", "-pix_fmt", "yuv420p", copies=("vp8", "vp9", "av1"))  # fmt: skip
_OPUS = _codec("libopus", "-b:a", "128k", copies=("opus",))
_FASTSTART = ("-movflags", "+faststart")  # the index first, so players start at once

AUDIO_TARGETS = {
    "mp3": _Target("mp3", _MP3),
    "m4a": _Target("ipod", replace(_AAC, copies=frozenset({"aac", "alac"})), extra=_FASTSTART),
    "wav": _Target("wav", _codec("pcm_s16le"), deep_audio=_codec("pcm_s24le")),
    "flac": _Target("flac", _codec("flac", copies=("flac",))),
    "ogg": _Target("ogg", _codec("libvorbis", "-q:a", "5", copies=("vorbis",))),
    "opus": _Target("opus", _OPUS),
    "aiff": _Target("aiff", _codec("pcm_s16be"), deep_audio=_codec("pcm_s24be")),
}

VIDEO_TARGETS = {
    "mp4": _Target("mp4", _AAC, _X264, extra=_FASTSTART),
    "mov": _Target("mov", _AAC, _X264, extra=_FASTSTART),
    "mkv": _Target("matroska", _MKV_AUDIO, _MKV_VIDEO),
    "webm": _Target("webm", replace(_OPUS, copies=frozenset({"opus", "vorbis"})), _VP9),
    "avi": _Target("avi", _MP3, _codec("mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p")),
}


# --- building the ffmpeg arguments (pure, so it is tested without running ffmpeg) -----------
def _stream_args(
    kind: str, source: str, codec: _Codec, pix_fmt: str = ""
) -> tuple[list[str], set[str]]:
    """``-c:<kind>`` arguments for one stream, and the encoder it needs (none for a copy)."""
    plays_anywhere = pix_fmt.startswith("yuv420p")  # 8 or 10 bit 4:2:0
    if source in codec.copies and (plays_anywhere or not codec.copy_420_only):
        args = [f"-c:{kind}", "copy"]
        if kind == "v" and source == "hevc":
            args += ["-tag:v", "hvc1"]  # what Apple players need for HEVC; MKV accepts it too
        return args, set()
    args = [f"-c:{kind}", codec.encoder, *codec.args]
    if kind == "v":
        args += ["-vf", EVEN_SIZE]
    return args, {codec.encoder}


def audio_args(info: "MediaInfo", target: _Target) -> tuple[list[str], set[str]]:
    if info.audio is None:
        raise ValueError("this file has no audio track")
    codec = target.deep_audio if info.deep_audio and target.deep_audio else target.audio
    args, needs = _stream_args("a", info.audio, codec)
    return ["-map", "0:a:0", "-vn", *args, *target.extra, "-f", target.format], needs


def video_args(info: "MediaInfo", target: _Target) -> tuple[list[str], set[str]]:
    if info.video is None:
        raise ValueError("this file has no video track")
    args, needs = _stream_args("v", info.video, target.video, info.pix_fmt)
    args = ["-map", "0:v:0", "-map", "0:a?", *args]
    if info.audio is not None:
        audio, audio_needs = _stream_args("a", info.audio, target.audio)
        args += audio
        needs |= audio_needs
    return [*args, *target.extra, "-f", target.format], needs


def gif_args(info: "MediaInfo") -> tuple[list[str], set[str]]:
    if info.video is None:
        raise ValueError("this file has no video track")
    graph = (
        f"fps={GIF_FPS},scale='min({GIF_MAX_WIDTH},iw)':-1:flags=lanczos,"
        "split[a][b];[a]palettegen[p];[b][p]paletteuse"
    )
    return ["-map", "0:v:0", "-an", "-vf", graph, "-f", "gif"], {"gif"}


def convert_each(
    paths: list[Path], ctx: "JobContext", ext: str, build, label: str | None = None
) -> list[Path]:
    from dragshift.engine import ffmpeg

    outputs = []
    for src in paths:
        info = ffmpeg.probe(src)
        args, needs = build(info)
        dst = ctx.output_path(src, ext=ext, label=label)
        cmd = ["-i", str(src), *args, "-y", str(dst)]  # -y: dst is our own reserved .part
        if not ffmpeg.run(cmd, ctx, duration=info.duration, text=src.name, needs=needs):
            return []  # cancelled: run_job deletes the .part
        outputs.append(dst)
    return outputs


def _audio(paths: list[Path], ctx: "JobContext", ext: str) -> list[Path]:
    return convert_each(paths, ctx, ext, lambda info: audio_args(info, AUDIO_TARGETS[ext]))


def _video(paths: list[Path], ctx: "JobContext", ext: str) -> list[Path]:
    return convert_each(paths, ctx, ext, lambda info: video_args(info, VIDEO_TARGETS[ext]))


# --- Audio Ops, in default petal order (§6.1) ------------------------------------------------
@op(id="convert.audio.mp3", wheel="convert", label="MP3", kinds=AUDIO, hide_if_same_ext="mp3")
def audio_to_mp3(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "mp3")


@op(id="convert.audio.m4a", wheel="convert", label="M4A", kinds=AUDIO, hide_if_same_ext="m4a")
def audio_to_m4a(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "m4a")


@op(id="convert.audio.wav", wheel="convert", label="WAV", kinds=AUDIO, hide_if_same_ext="wav")
def audio_to_wav(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "wav")


@op(id="convert.audio.flac", wheel="convert", label="FLAC", kinds=AUDIO, hide_if_same_ext="flac")
def audio_to_flac(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "flac")


@op(id="convert.audio.ogg", wheel="convert", label="OGG", kinds=AUDIO, hide_if_same_ext="ogg")
def audio_to_ogg(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "ogg")


@op(id="convert.audio.opus", wheel="convert", label="OPUS", kinds=AUDIO, hide_if_same_ext="opus")
def audio_to_opus(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "opus")


@op(id="convert.audio.aiff", wheel="convert", label="AIFF", kinds=AUDIO, hide_if_same_ext="aiff")
def audio_to_aiff(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _audio(paths, ctx, "aiff")


# --- Video Ops, in default petal order (§6.1) ------------------------------------------------
@op(id="convert.video.mp4", wheel="convert", label="MP4", kinds=VIDEO, hide_if_same_ext="mp4")
def video_to_mp4(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _video(paths, ctx, "mp4")


@op(id="convert.video.mov", wheel="convert", label="MOV", kinds=VIDEO, hide_if_same_ext="mov")
def video_to_mov(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _video(paths, ctx, "mov")


@op(id="convert.video.mkv", wheel="convert", label="MKV", kinds=VIDEO, hide_if_same_ext="mkv")
def video_to_mkv(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _video(paths, ctx, "mkv")


@op(id="convert.video.webm", wheel="convert", label="WEBM", kinds=VIDEO, hide_if_same_ext="webm")
def video_to_webm(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _video(paths, ctx, "webm")


@op(id="convert.video.gif", wheel="convert", label="GIF", kinds=VIDEO)
def video_to_gif(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return convert_each(paths, ctx, "gif", gif_args)


@op(id="convert.video.mp3", wheel="convert", label="MP3", kinds=VIDEO)
def video_to_mp3(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return convert_each(paths, ctx, "mp3", lambda info: audio_args(info, AUDIO_TARGETS["mp3"]))


@op(id="convert.video.avi", wheel="convert", label="AVI", kinds=VIDEO, hide_if_same_ext="avi")
def video_to_avi(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _video(paths, ctx, "avi")
