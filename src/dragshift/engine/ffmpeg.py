"""Running ffmpeg from a worker: binary choice, probing, progress and cancel (PRD §6.1, FR-4.5).

- Binary: ffmpeg on PATH is preferred (§6.1), but only when it has every encoder the job needs;
  otherwise the ``imageio-ffmpeg`` binary is used. A PATH build can lack encoders (the
  Miniconda build has no VP9, found in DS-14).
- Progress comes from ``-progress pipe:1`` (``out_time_us`` against the probed duration).
- Cancel kills the process; ``run_job`` then deletes the ``.part`` output.

Worker-only: nothing here is imported by the UI process.
"""

import re
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dragshift.engine.worker import JobContext

POLL_S = 0.1  # how often a running job checks for cancel and reports progress
_NO_WINDOW = subprocess.CREATE_NO_WINDOW  # workers of a pythonw app have no console to share


class FFmpegError(RuntimeError):
    """ffmpeg failed; the message is its last error line."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None  # seconds; None when the container does not say
    video: str | None  # codec of the first real video stream (not cover art)
    audio: str | None  # codec of the first audio stream
    width: int = 0
    height: int = 0
    pix_fmt: str = ""  # of that video stream, e.g. "yuv420p"
    deep_audio: bool = False  # lossless audio with more than 16 bits per sample


# --- choosing a binary -----------------------------------------------------------------------
def _candidates() -> list[str]:
    import imageio_ffmpeg

    on_path = shutil.which("ffmpeg")
    return [*([on_path] if on_path else []), imageio_ffmpeg.get_ffmpeg_exe()]


@cache
def encoders(exe: str) -> frozenset[str]:
    """Names of the encoders ``exe`` was built with (``ffmpeg -encoders``)."""
    out = subprocess.run(
        [exe, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=_NO_WINDOW,
    ).stdout
    _, _, table = out.partition("------")
    return frozenset(line.split()[1] for line in table.splitlines() if len(line.split()) > 1)


def ffmpeg_exe(needs: Iterable[str] = ()) -> str:
    """The preferred ffmpeg that has all of ``needs`` (encoder names)."""
    needs = set(needs)
    candidates = _candidates()
    for exe in candidates:
        if needs <= encoders(exe):
            return exe
    missing = ", ".join(sorted(needs - encoders(candidates[-1])))
    raise FFmpegError(f"no ffmpeg with the {missing} encoder")


# --- probing ---------------------------------------------------------------------------------
_DURATION = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")
_STREAM = re.compile(r"Stream #\d+:\d+\S*: (Video|Audio): (\w+)(.*)")
_SIZE = re.compile(r", (\d{2,})x(\d{2,})")
_ADDRESS = re.compile(r"^\[[^\]]* @ [0-9a-fA-Fx]+\] ")  # "[libx264 @ 000001c2...] "


def _last_error(lines: Iterable[str], src: Path | None = None) -> str:
    """The last meaningful line of ffmpeg's stderr, without the noise around it."""
    useful = [
        _ADDRESS.sub("", line).removeprefix(f"{src}: " if src else "").strip()
        for line in lines
        if line.strip() and "At least one output file" not in line
    ]
    return useful[-1] if useful else "ffmpeg failed"


def _fields(rest: str) -> list[str]:
    """Split a stream line at commas outside brackets: the pixel format is the second field."""
    fields, depth, start = [], 0, 0
    for i, ch in enumerate(rest):
        depth += (ch in "([") - (ch in ")]")
        if ch == "," and depth == 0:
            fields.append(rest[start:i].strip())
            start = i + 1
    return [*fields, rest[start:].strip()]


def parse_probe(text: str) -> MediaInfo:
    """Read ``ffmpeg -i`` output (there is no ffprobe in imageio-ffmpeg)."""
    m = _DURATION.search(text)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    video = audio = None
    width = height = 0
    pix_fmt = ""
    deep = False
    for kind, codec, rest in _STREAM.findall(text):
        if kind == "Video" and video is None and "(attached pic)" not in rest:
            video = codec
            if size := _SIZE.search(rest):
                width, height = int(size[1]), int(size[2])
            fields = _fields(rest)  # "(High) (avc1 / ...)", "yuv420p(progressive)", "160x120 ..."
            pix_fmt = fields[1].partition("(")[0] if len(fields) > 1 else ""
        elif kind == "Audio" and audio is None:
            audio = codec
            deep = codec.startswith(("flac", "alac", "pcm")) and (
                "(24 bit)" in rest or re.search(r", s32p?\b", rest) is not None
            )
    return MediaInfo(duration or None, video, audio, width, height, pix_fmt, deep)


def probe(src: Path) -> MediaInfo:
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(src)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=_NO_WINDOW,
    ).stderr
    if "Input #0" not in out:
        raise FFmpegError(_last_error(out.splitlines(), src))
    return parse_probe(out)


# --- running ---------------------------------------------------------------------------------
def progress_seconds(line: str) -> float | None:
    """Seconds written so far, from one ``-progress`` line (``out_time_us=1500000``)."""
    key, _, value = line.strip().partition("=")
    return int(value) / 1_000_000 if key == "out_time_us" and value.isdigit() else None


def run(
    args: list[str],
    ctx: "JobContext",
    *,
    duration: float | None,
    text: str = "",
    needs: Iterable[str] = (),
) -> bool:
    """Run ffmpeg with ``args`` (inputs, options, output). Reports progress, and kills the
    process when the job is cancelled. Returns False if it was cancelled."""
    cmd = [ffmpeg_exe(needs), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]  # fmt: skip
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    written = [0.0]  # seconds of output so far, updated by the reader thread
    errors: deque[str] = deque(maxlen=20)

    def read_progress() -> None:
        for raw in proc.stdout:
            seconds = progress_seconds(raw.decode("ascii", "replace"))
            if seconds is not None:
                written[0] = seconds

    def read_errors() -> None:  # drained, so a chatty ffmpeg never blocks on a full pipe
        for raw in proc.stderr:
            errors.append(raw.decode("utf-8", "replace").rstrip())

    readers = [threading.Thread(target=f, daemon=True) for f in (read_progress, read_errors)]
    for t in readers:
        t.start()
    reported = -1.0
    try:
        while True:
            try:
                proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if ctx.cancelled():
                proc.kill()  # TerminateProcess: ffmpeg lets go of the .part at once
                proc.wait()
                return False
            if duration and written[0] != reported:
                reported = written[0]
                ctx.progress(min(reported / duration, 0.99), text)
    finally:
        if proc.poll() is None:  # an exception in this loop: never leave ffmpeg running
            proc.kill()
            proc.wait()
        for t in readers:
            t.join(timeout=5)
    if proc.returncode != 0:
        raise FFmpegError(_last_error(errors))
    ctx.progress(1.0, text)
    return True
