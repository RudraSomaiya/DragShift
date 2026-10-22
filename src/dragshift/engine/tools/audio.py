"""Audio tools (PRD §6.2 audio row): Compress ▸ presets, Normalize, Bitrate ▸.

Compress re-encodes at a constant bitrate computed from the duration and the target size:
AAC in M4A for AAC sources, MP3 for everything else (every player and service takes it).
Bitrates stay between 32 and 320 kb/s; when even 32 kb/s is too big the toast says how close
it got (FR-4.4). Cover art is dropped; tags are kept. Output: ``<stem> Compressed.<m4a|mp3>``.

Bitrate ▸ is the same encode at 96/128/192/320 kb/s (``<stem> 128k.<m4a|mp3>``). Normalize is
EBU R128 ``loudnorm`` in one pass, written in the source's format (MP3 for WMA), at its sample
rate (``<stem> Normalized.<ext>``).

ffmpeg is only called inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.converters.media import AUDIO_TARGETS
from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.presets import Target, compress_op, retarget, size_note, target_for
from dragshift.engine.registry import op
from dragshift.engine.variants import Choice, ring_op

if TYPE_CHECKING:
    from dragshift.engine.ffmpeg import MediaInfo
    from dragshift.engine.worker import JobContext

MUX_SHARE = 0.97  # of the target for the audio; the rest is container overhead and tags
MIN_KBPS, MAX_KBPS = 32, 320
UNKNOWN_DURATION_KBPS = 96
# LAME's CBR bitrates: an MP3 rate is rounded down to one of these, never up past the target.
MP3_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


def format_for(info: "MediaInfo") -> str:
    return "m4a" if info.audio == "aac" else "mp3"


def kbps_for(info: "MediaInfo", goal: Target, ext: str) -> int:
    """Pure, so it is tested without running ffmpeg."""
    if goal.quality is not None:
        kbps = MIN_KBPS + (MAX_KBPS - MIN_KBPS) * goal.quality / 100
    elif info.duration:
        kbps = goal.size * 8 / info.duration / 1000 * MUX_SHARE
    else:
        kbps = UNKNOWN_DURATION_KBPS
    kbps = min(MAX_KBPS, max(MIN_KBPS, kbps))
    if ext == "mp3":
        return max(r for r in MP3_KBPS if r <= kbps)
    return int(kbps)


def encode_args(ext: str, kbps: int) -> tuple[list[str], set[str]]:
    """(ffmpeg output arguments, encoders needed) for constant-bitrate ``ext``."""
    rate = ["-b:a", f"{kbps}k"]
    if ext == "m4a":
        return ["-map", "0:a:0", "-vn", "-c:a", "aac", *rate, "-movflags", "+faststart",
                "-f", "ipod"], {"aac"}  # fmt: skip
    return ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", *rate, "-f", "mp3"], {"libmp3lame"}


def audio_args(info: "MediaInfo", goal: Target) -> tuple[str, list[str], set[str]]:
    """(output extension, ffmpeg output arguments, encoders needed)."""
    if info.audio is None:
        raise ValueError("this file has no audio track")
    ext = format_for(info)
    return (ext, *encode_args(ext, kbps_for(info, goal, ext)))


@compress_op(Kind.AUDIO)
def compress(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    from dragshift.engine import ffmpeg

    outputs = []
    for src in paths:
        goal = target_for(params, src.stat().st_size)
        info = ffmpeg.probe(src)
        ext = format_for(info)
        dst = ctx.output_path(src, ext=ext, label="Compressed")
        attempt: Target | None = goal
        while attempt is not None:  # AAC lands a few % over on short files: then once more
            _, args, needs = audio_args(info, attempt)
            cmd = ["-i", str(src), *args, "-y", str(dst)]  # -y: dst is our own reserved .part
            if not ffmpeg.run(cmd, ctx, duration=info.duration, text=src.name, needs=needs):
                return []  # cancelled: run_job deletes the .part
            retry = retarget(goal, dst.stat().st_size) if attempt is goal else None
            lower = retry is not None and kbps_for(info, retry, ext) < kbps_for(info, goal, ext)
            attempt = retry if lower else None
        ctx.note(size_note(goal, dst.stat().st_size))
        outputs.append(dst)
    return outputs


def _each(paths: list[Path], ctx: "JobContext", label: str, build) -> list[Path]:
    """Re-encode each file with ``build(info, src) -> (ext, ffmpeg output args, encoders)``."""
    from dragshift.engine import ffmpeg

    outputs = []
    for src in paths:
        info = ffmpeg.probe(src)
        if info.audio is None:
            raise ValueError("this file has no audio track")
        ext, args, needs = build(info, src)
        dst = ctx.output_path(src, ext=ext, label=label)
        cmd = ["-i", str(src), *args, "-y", str(dst)]  # -y: dst is our own reserved .part
        if not ffmpeg.run(cmd, ctx, duration=info.duration, text=src.name, needs=needs):
            return []  # cancelled: run_job deletes the .part
        outputs.append(dst)
    return outputs


@ring_op(
    Kind.AUDIO,
    "bitrate",
    "Bitrate",
    [Choice(str(k), f"{k} kbps", {"kbps": k}) for k in (96, 128, 192, 320)],
    default="128",  # a drop on the petal itself must not be the lossiest choice
)
def bitrate(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    kbps = int(params["kbps"])

    def build(info: "MediaInfo", src: Path) -> tuple[str, list[str], set[str]]:
        ext = format_for(info)
        return (ext, *encode_args(ext, kbps))

    return _each(paths, ctx, f"{kbps}k", build)


LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"  # EBU R128 streaming target


def normalize_args(info: "MediaInfo", ext: str) -> tuple[list[str], set[str]]:
    """Pure, so it is tested without running ffmpeg. Always re-encodes: a filter cannot copy."""
    target = AUDIO_TARGETS[ext]
    codec = target.deep_audio if info.deep_audio and target.deep_audio else target.audio
    # loudnorm resamples to 192 kHz internally; ask for the source's rate back.
    rate = ["-ar", str(info.sample_rate)] if info.sample_rate else []
    return [
        "-map", "0:a:0", "-vn", "-af", LOUDNORM, *rate, "-c:a", codec.encoder, *codec.args,
        *target.extra, "-f", target.format,
    ], {codec.encoder}  # fmt: skip


@op(id="tools.audio.normalize", wheel="tools", label="Normalize", kinds=frozenset({Kind.AUDIO}))
def normalize(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    def build(info: "MediaInfo", src: Path) -> tuple[str, list[str], set[str]]:
        ext = normalize_ext(src.suffix)
        if ext not in AUDIO_TARGETS:  # WMA, raw AAC: no encoder for them
            ext = format_for(info)
        return (ext, *normalize_args(info, ext))

    return _each(paths, ctx, "Normalized", build)
