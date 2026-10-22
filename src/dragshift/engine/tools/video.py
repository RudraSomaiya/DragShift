"""Video tools (PRD §6.2 video row): Compress ▸ presets.

Compress is two-pass H.264 + AAC in MP4 at a bitrate computed from the duration and the target
size (§6.2). Low bitrates also lower the resolution (``SIDES``), since a sharp 480p beats a
smeared 1080p; the cap is on the short side, after ffmpeg has turned a phone video upright. When the bitrate left for the picture is too low to watch, or the duration is
unknown, it falls back to one CRF pass, and the toast says how close it got (FR-4.4). Two-pass
lands a few % over on short clips, so a miss is encoded once more at a scaled-down bitrate
(the last ``RETRY_SHARE`` of the progress bar).
Output: ``<stem> Compressed.mp4``.

ffmpeg is only called inside the functions: the UI process imports this module to register Ops
(CLAUDE.md rule 2).
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.converters.media import EVEN_SIZE
from dragshift.engine.kinds import Kind
from dragshift.engine.presets import Target, compress_op, retarget, size_note, target_for

if TYPE_CHECKING:
    from dragshift.engine.ffmpeg import MediaInfo
    from dragshift.engine.worker import JobContext

MUX_SHARE = 0.95  # of the target for the streams; the rest is MP4 overhead and rate error
MIN_VIDEO_KBPS = 150  # below this H.264 is a slideshow: CRF instead (§6.2)
FALLBACK_CRF = 32
# Video kb/s below which the short side is capped (360p, 480p, …), so the bits go a long way.
SIDES = ((400, 360), (800, 480), (1600, 720), (3200, 1080))
RETRY_SHARE = 0.2
NEEDS = {"libx264", "aac"}
_X264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
_MP4 = ["-movflags", "+faststart", "-f", "mp4"]


@dataclass(frozen=True)
class Plan:
    """How one video is encoded: a bitrate (two passes) or a CRF (one pass)."""

    video_kbps: int | None
    audio_kbps: int  # 0: no audio track
    crf: int | None = None
    max_side: int | None = None  # the short side, in pixels


def audio_kbps_for(total_kbps: float) -> int:
    return 128 if total_kbps >= 1000 else 64 if total_kbps >= 300 else 32


def max_side_for(video_kbps: float) -> int | None:
    return next((side for kbps, side in SIDES if video_kbps < kbps), None)


def plan_for(info: "MediaInfo", goal: Target) -> Plan:
    """Pure, so it is tested without running ffmpeg."""
    has_audio = info.audio is not None
    if goal.quality is not None:  # Custom…: quality 100 -> CRF 18, 0 -> CRF 40
        return Plan(None, 128 if has_audio else 0, crf=round(40 - 22 * goal.quality / 100))
    if not info.duration:
        return Plan(None, 64 if has_audio else 0, crf=FALLBACK_CRF, max_side=480)
    total = goal.size * 8 / info.duration / 1000 * MUX_SHARE
    audio = audio_kbps_for(total) if has_audio else 0
    video = total - audio
    if video < MIN_VIDEO_KBPS:
        return Plan(None, audio, crf=FALLBACK_CRF, max_side=SIDES[0][1])
    return Plan(int(video), audio, max_side=max_side_for(video))


def scale_filter(side: int) -> str:
    """Shrink so the short side is at most ``side``, to even sizes, keeping the shape. It runs
    after ffmpeg's autorotate, so a portrait phone video is measured as it is shown."""
    factor = f"min(1,{side}/min(iw,ih))"
    return f"scale='trunc(iw*{factor}/2)*2':'trunc(ih*{factor}/2)*2',setsar=1"


def _filters(plan: Plan) -> list[str]:
    return ["-vf", scale_filter(plan.max_side) if plan.max_side else EVEN_SIZE]


def _audio(plan: Plan) -> list[str]:
    if not plan.audio_kbps:
        return ["-an"]
    return ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{plan.audio_kbps}k"]


class _Span:
    """A JobContext seen by one ffmpeg pass: its progress fills ``[start, start + span]``."""

    def __init__(self, ctx: "JobContext | _Span", start: float, span: float) -> None:
        self.ctx, self.start, self.span = ctx, start, span

    def progress(self, fraction: float, text: str = "") -> None:
        self.ctx.progress(self.start + fraction * self.span, text)

    def cancelled(self) -> bool:
        return self.ctx.cancelled()


def _encode(src: Path, dst: Path, info: "MediaInfo", plan: Plan, ctx: "JobContext | _Span") -> bool:
    """Write ``dst``; False if the job was cancelled."""
    from dragshift.engine import ffmpeg

    run = {"duration": info.duration, "text": src.name, "needs": NEEDS}
    video = ["-map", "0:v:0", *_X264, *_filters(plan)]
    if plan.crf is not None:
        args = ["-i", str(src), *video, "-crf", str(plan.crf), *_audio(plan), *_MP4]
        return ffmpeg.run([*args, "-y", str(dst)], ctx, **run)
    with tempfile.TemporaryDirectory(prefix="dragshift-") as tmp:
        rate = ["-b:v", f"{plan.video_kbps}k", "-passlogfile", str(Path(tmp, "pass"))]
        first = ["-i", str(src), *video, *rate, "-pass", "1", "-an", "-f", "null", "NUL"]
        if not ffmpeg.run(first, _Span(ctx, 0, 0.5), **run):
            return False
        second = ["-i", str(src), *video, *rate, "-pass", "2", *_audio(plan), *_MP4]
        return ffmpeg.run([*second, "-y", str(dst)], _Span(ctx, 0.5, 0.5), **run)


@compress_op(Kind.VIDEO)
def compress(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    from dragshift.engine import ffmpeg

    outputs = []
    for src in paths:
        goal = target_for(params, src.stat().st_size)
        info = ffmpeg.probe(src)
        if info.video is None:
            raise ValueError("this file has no video track")
        dst = ctx.output_path(src, ext="mp4", label="Compressed")
        first = _Span(ctx, 0, 1 - RETRY_SHARE)
        if not _encode(src, dst, info, plan_for(info, goal), first):
            return []  # cancelled: run_job deletes the .part
        retry = retarget(goal, dst.stat().st_size)
        if retry is not None and plan_for(info, retry).video_kbps:  # a missed bitrate: once more
            again = _Span(ctx, 1 - RETRY_SHARE, RETRY_SHARE)
            if not _encode(src, dst, info, plan_for(info, retry), again):
                return []
        ctx.progress(1.0, src.name)
        ctx.note(size_note(goal, dst.stat().st_size))
        outputs.append(dst)
    return outputs
