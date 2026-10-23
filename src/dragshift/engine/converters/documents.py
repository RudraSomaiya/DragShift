"""Document converters through pandoc (PRD §6.1 document row).

Inputs: docx md txt html htm rtf odt epub. Targets in default petal order: DOCX MD TXT HTML EPUB
ODT RTF, minus the source's own format. PDF output needs Office or LibreOffice (stretch DS-33).

- pandoc is the binary bundled with ``pypandoc-binary``, so every machine converts alike; one on
  PATH is used only if the bundled one is missing. It runs as a subprocess that a cancel kills.
- TXT input is read literally by ``lua/plain_reader.lua``: pandoc has no plain-text reader, and
  reading it as Markdown would turn ``*stars*`` into italics.
- Offline (CONTRIBUTING.md rule 7): pandoc downloads remote images to embed or extract them, so it runs
  with its proxy set to an address Windows refuses at once. Every fetch fails without touching
  the network, and pandoc puts the image's alt text in its place.
- Images inside a DOCX, ODT, EPUB or RTF source go to a ``<stem> media`` folder next to an MD or
  HTML output, which links to them. Markdown and HTML sources keep their own image links.

Worker-only: nothing here imports pypandoc; the UI process imports this module to register Ops.
"""

import codecs
import importlib.util
import os
import shutil
import subprocess
import threading
from collections import deque
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.registry import op
from dragshift.engine.worker import new_folder

if TYPE_CHECKING:
    from dragshift.engine.worker import JobContext

DOCUMENT = frozenset({Kind.DOCUMENT})
LUA = Path(__file__).with_name("lua")
POLL_S = 0.1  # how often a running pandoc is checked for cancel
_NO_WINDOW = subprocess.CREATE_NO_WINDOW  # workers of a pythonw app have no console to share

# Source extension → pandoc reader. TXT uses the Lua reader instead.
_READERS = {"docx": "docx", "md": "markdown", "html": "html", "rtf": "rtf", "odt": "odt",
            "epub": "epub"}  # fmt: skip
# Target extension → pandoc writer options. --wrap=none keeps a paragraph on one line.
_WRITERS = {
    "docx": ["-t", "docx"],
    "md": ["-t", "gfm", "--wrap=none"],
    "txt": ["-t", "plain", "--wrap=none"],
    "html": ["-t", "html5", "--standalone"],
    "epub": ["-t", "epub3"],
    "odt": ["-t", "odt"],
    "rtf": ["-t", "rtf", "--standalone"],  # without it pandoc writes an RTF fragment
}
_CONTAINERS = {"docx", "odt", "epub", "rtf"}  # sources that carry their images inside
# pandoc's HTTP client honours these. A connect to 0.0.0.0 fails at once on Windows
# (WSAEADDRNOTAVAIL), where a closed loopback port takes about 2 s per image to be refused.
_NO_NETWORK = "http://0.0.0.0:9"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
MAX_ERROR = 300  # characters of pandoc's error output kept for the toast


class PandocError(RuntimeError):
    """pandoc failed; the message is its error output."""


# --- running pandoc --------------------------------------------------------------------------
@cache
def pandoc_exe() -> str:
    spec = importlib.util.find_spec("pypandoc")  # finds the bundled binary without importing
    if spec and spec.origin:
        bundled = Path(spec.origin).with_name("files") / "pandoc.exe"
        if bundled.is_file():
            return str(bundled)
    on_path = shutil.which("pandoc")
    if on_path is None:
        raise PandocError("pandoc was not found; run `uv sync` to install pypandoc-binary")
    return on_path


def offline_env() -> dict[str, str]:
    """This process's environment with every pandoc download routed nowhere."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in (*_PROXY_VARS, "NO_PROXY")}
    return env | dict.fromkeys(_PROXY_VARS, _NO_NETWORK)


def run(args: list[str], ctx: "JobContext", *, cwd: Path, stdin: bytes | None = None) -> bool:
    """Run pandoc offline with ``args``, killing it when the job is cancelled. Returns False if
    it was cancelled; raises PandocError if it failed."""
    proc = subprocess.Popen(
        [pandoc_exe(), *args],
        cwd=cwd,
        env=offline_env(),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    errors: deque[str] = deque(maxlen=20)

    def feed() -> None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:
            pass  # pandoc was killed (cancel) or failed early; its exit code says which

    def drain() -> None:  # read so a chatty pandoc never blocks on a full pipe
        warning = False
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("[WARNING]"):
                warning = True
            elif warning and line[:1].isspace():
                pass  # the indented rest of a multi-line warning
            elif line:
                warning = False
                errors.append(line.strip())

    threads = [threading.Thread(target=drain, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    try:
        while True:
            try:
                proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if ctx.cancelled():
                proc.kill()  # TerminateProcess: pandoc lets go of the .part at once
                proc.wait()
                return False
    finally:
        if proc.poll() is None:  # an exception in this loop: never leave pandoc running
            proc.kill()
            proc.wait()
        for t in threads:
            t.join(timeout=5)
    if proc.returncode != 0:
        message = " ".join(errors) or f"pandoc failed (exit code {proc.returncode})"
        raise PandocError(message if len(message) <= MAX_ERROR else message[: MAX_ERROR - 1] + "…")
    return True


# --- converting ------------------------------------------------------------------------------
def _utf16_without_bom(raw: bytes) -> str | None:
    """The codec of ``raw`` if it looks like UTF-16 without a BOM: mostly-ASCII text then has a
    NUL in every other byte, which UTF-8 or Windows-1252 text never has."""
    odd, even = raw[1::2].count(0), raw[0::2].count(0)
    if odd > len(raw) // 4 and odd > 4 * even:
        return "utf-16-le"
    if even > len(raw) // 4 and even > 4 * odd:
        return "utf-16-be"
    return None


def read_text(src: Path) -> bytes:
    """A .txt file as UTF-8 for the Lua reader: UTF-8 (with or without BOM), UTF-16 with a BOM
    (Notepad's "Unicode") or without one, else Windows-1252. NULs are dropped: pandoc rejects
    them."""
    raw = src.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", "replace")
    elif encoding := _utf16_without_bom(raw):
        text = raw.decode(encoding, "replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", "replace")
    return text.replace("\x00", "").encode("utf-8")


def _convert_one(src: Path, ctx: "JobContext", ext: str) -> list[Path]:
    source = normalize_ext(src.suffix)
    dst = ctx.output_path(src, ext=ext)
    # pandoc runs in the source's folder: its images resolve from there (pandoc's own
    # --resource-path would split a folder name containing ';', the Windows path separator).
    args = [*_WRITERS[ext], "-o", str(dst)]
    if source == "txt":
        args += ["-f", str(LUA / "plain_reader.lua")]
        stdin = read_text(src)
    else:
        args += ["-f", _READERS[source], str(src)]
        stdin = None
    if ext in ("html", "epub"):
        args += ["--lua-filter", str(LUA / "title.lua"), "-M", f"dragshift-title={src.stem}"]
    media = None
    if ext in ("md", "html") and source in _CONTAINERS:
        media = new_folder(dst.parent / f"{src.stem} media", ctx)
        # Relative when next to the output, so the output links to its images relatively.
        args += ["--extract-media", media.name if media.parent == src.parent else str(media)]
    try:
        if not run(args, ctx, cwd=src.parent, stdin=stdin):
            if media is not None:
                shutil.rmtree(media, ignore_errors=True)
            return []  # cancelled: run_job deletes the .part
    except BaseException:
        if media is not None:
            shutil.rmtree(media, ignore_errors=True)
        raise
    extracted = sorted(p for p in media.rglob("*") if p.is_file()) if media else []
    if media is not None and not extracted:
        shutil.rmtree(media, ignore_errors=True)  # no images: no folder
    return [dst, *extracted]


def _convert(paths: list[Path], ctx: "JobContext", ext: str) -> list[Path]:
    outputs = []
    for src in paths:
        done = _convert_one(src, ctx, ext)
        if not done:
            return []
        outputs += done
    return outputs


# --- Ops, in default petal order (§6.1; PDF is DS-33) ----------------------------------------
@op(id="convert.document.docx", wheel="convert", label="DOCX", kinds=DOCUMENT,
    hide_if_same_ext="docx")  # fmt: skip
def to_docx(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "docx")


@op(id="convert.document.md", wheel="convert", label="MD", kinds=DOCUMENT, hide_if_same_ext="md")
def to_md(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "md")


@op(id="convert.document.txt", wheel="convert", label="TXT", kinds=DOCUMENT,
    hide_if_same_ext="txt")  # fmt: skip
def to_txt(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "txt")


@op(id="convert.document.html", wheel="convert", label="HTML", kinds=DOCUMENT,
    hide_if_same_ext="html")  # fmt: skip
def to_html(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "html")


@op(id="convert.document.epub", wheel="convert", label="EPUB", kinds=DOCUMENT,
    hide_if_same_ext="epub")  # fmt: skip
def to_epub(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "epub")


@op(id="convert.document.odt", wheel="convert", label="ODT", kinds=DOCUMENT,
    hide_if_same_ext="odt")  # fmt: skip
def to_odt(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "odt")


@op(id="convert.document.rtf", wheel="convert", label="RTF", kinds=DOCUMENT,
    hide_if_same_ext="rtf")  # fmt: skip
def to_rtf(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "rtf")
