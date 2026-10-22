"""Audio tools (PRD §6.2 audio row): Compress ▸ presets.

Compress re-encodes at a constant bitrate computed from the duration and the target size:
AAC in M4A for AAC sources, MP3 for everything else (every player and service takes it).
Bitrates stay between 32 and 320 kb/s; when even 32 kb/s is too big the toast says how close
it got (FR-4.4). Cover art is dropped; tags are kept. Output: ``<stem> Compressed.<m4a|mp3>``.

ffmpeg is only called inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind
from dragshift.engine.presets import Target, compress_op, retarget, size_note, target_for

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


def audio_args(info: "MediaInfo", goal: Target) -> tuple[str, list[str], set[str]]:
    """(output extension, ffmpeg output arguments, encoders needed)."""
    if info.audio is None:
        raise ValueError("this file has no audio track")
    ext = format_for(info)
    rate = ["-b:a", f"{kbps_for(info, goal, ext)}k"]
    if ext == "m4a":
        return ext, ["-map", "0:a:0", "-vn", "-c:a", "aac", *rate, "-movflags", "+faststart",
                     "-f", "ipod"], {"aac"}  # fmt: skip
    return ext, ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", *rate, "-f", "mp3"], {"libmp3lame"}


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
