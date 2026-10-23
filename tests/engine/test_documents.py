"""Document converters through pandoc (PRD §6.1 document row): every source to every target,
literal TXT, images and media folders, offline, cancel, petals.

Every sample is built here with the bundled pandoc; nothing binary is committed. Converters run
through ``run_job`` with a plain JobContext, except for the end-to-end tests at the bottom.
"""

import hashlib
import http.server
import io
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from dragshift.engine import registry
from dragshift.engine.converters import documents
from dragshift.engine.converters.documents import pandoc_exe
from dragshift.engine.jobs import JobManager
from dragshift.engine.worker import JobContext, final_path, run_job

MODULE = "dragshift.engine.converters.documents"
SOURCES = ["docx", "md", "txt", "html", "rtf", "odt", "epub"]
TARGETS = ["docx", "md", "txt", "html", "epub", "odt", "rtf"]
FUNCS = {ext: f"to_{ext}" for ext in TARGETS}
TEXT = "Hello DragShift converts documents."
MARKDOWN = f"# Heading\n\n{TEXT}\n\n- one\n- two\n"
WRITERS = {"docx": "docx", "md": "markdown", "html": "html", "rtf": "rtf", "odt": "odt",
           "epub": "epub"}  # fmt: skip
ZIPPED = {"docx", "odt", "epub"}


# --- helpers ---------------------------------------------------------------------------------
def pandoc(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(
        [pandoc_exe(), *args], capture_output=True, check=True, cwd=cwd, timeout=60
    )
    return out.stdout.decode("utf-8")


def make(path: Path, markdown: str = MARKDOWN) -> Path:
    """A document in the format of ``path``'s extension, built by pandoc from ``markdown``."""
    ext = path.suffix.lstrip(".")
    if ext == "txt":
        path.write_text(f"Heading\n\n{TEXT}\n", encoding="utf-8")
        return path
    md = path.with_name(path.stem + ".source.md")
    md.write_text(markdown, encoding="utf-8")
    pandoc("-f", "markdown", "-t", WRITERS[ext], "-s", "-o", str(path), str(md), cwd=path.parent)
    md.unlink()
    return path


def plain(path: Path) -> str:
    """The text of any document, through pandoc's plain writer."""
    ext = path.suffix.lstrip(".")
    if ext == "txt":
        return path.read_text(encoding="utf-8")
    return pandoc("-f", WRITERS[ext], "-t", "plain", "--wrap=none", str(path))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(ext: str, src: Path, ctx: JobContext | None = None) -> dict:
    """Run an Op the way the pool does, check the source is untouched, finish the outputs."""
    before = sha(src)
    result = run_job(f"{MODULE}:{FUNCS[ext]}", [[src]], {}, ctx or JobContext("t"))
    assert sha(src) == before  # CONTRIBUTING.md rule 4
    result["outputs"] = [
        part.rename(final_path(part)) if part.suffix == ".part" else part
        for part in result["outputs"]
    ]
    return result


def converted(ext: str, src: Path) -> list[Path]:
    result = run(ext, src)
    assert result["ok"], result["message"]
    return result["outputs"]


def tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def png(path: Path) -> Path:
    Image.new("RGB", (40, 20), (200, 30, 30)).save(path, format="PNG")
    return path


# --- every source to every target ------------------------------------------------------------
@pytest.mark.parametrize(("source", "target"), [(s, t) for s in SOURCES for t in TARGETS if s != t])
def test_every_source_converts_to_every_other_target(
    tmp_path: Path, source: str, target: str
) -> None:
    src = make(tmp_path / f"doc.{source}")
    (out,) = converted(target, src)
    assert out == tmp_path / f"doc.{target}"
    if target in ZIPPED:
        assert zipfile.is_zipfile(out)
    assert TEXT in plain(out).replace("\n", " ")


def test_rtf_is_a_whole_document(tmp_path: Path) -> None:
    (out,) = converted("rtf", make(tmp_path / "doc.md"))
    assert out.read_bytes().startswith(b"{\\rtf1")


def test_html_is_a_standalone_page_titled_by_the_file_name(tmp_path: Path) -> None:
    (out,) = converted("html", make(tmp_path / "Q3 notes.md"))
    page = out.read_text(encoding="utf-8")
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "<title>Q3 notes</title>" in page
    assert 'class="title"' not in page  # the fallback adds no title heading


def test_html_keeps_the_documents_own_title(tmp_path: Path) -> None:
    src = make(tmp_path / "draft-3.md", f"---\ntitle: Quarterly Report\n---\n\n{TEXT}\n")
    (out,) = converted("html", src)
    assert "<title>Quarterly Report</title>" in out.read_text(encoding="utf-8")


def epub_title(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        opf = next(n for n in zf.namelist() if n.endswith(".opf"))
        text = zf.read(opf).decode("utf-8")
    return text.split("<dc:title", 1)[1].split(">", 1)[1].split("<", 1)[0]


def test_epub_title_is_the_file_name_unless_the_document_has_one(tmp_path: Path) -> None:
    (out,) = converted("epub", make(tmp_path / "Q3 notes.docx"))
    assert epub_title(out) == "Q3 notes"
    titled = make(tmp_path / "titled.md", f"---\ntitle: Real Title\n---\n\n{TEXT}\n")
    (out,) = converted("epub", titled)
    assert epub_title(out) == "Real Title"


def test_md_and_txt_keep_each_paragraph_on_one_line(tmp_path: Path) -> None:
    long = " ".join(["word"] * 60)
    src = make(tmp_path / "doc.docx", f"{long}\n")
    for ext in ("md", "txt"):
        (out,) = converted(ext, src)
        assert long in out.read_text(encoding="utf-8")


# --- TXT input is literal --------------------------------------------------------------------
def test_txt_is_read_literally(tmp_path: Path) -> None:
    src = tmp_path / "notes.txt"
    src.write_text("# not a heading\nkeep *stars* and_unders\n\nsecond para\n", encoding="utf-8")
    (out,) = converted("docx", src)
    text = plain(out)
    assert "# not a heading" in text
    assert "keep *stars* and_unders" in text
    (md,) = converted("md", src)
    assert not md.read_text(encoding="utf-8").startswith("# ")  # escaped, not a heading


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", b""),
        ("utf-8", b"\xef\xbb\xbf"),
        ("utf-16", b""),  # Python writes a BOM
        ("utf-16-le", b""),  # no BOM
        ("utf-16-be", b""),
        ("cp1252", b""),
    ],
)
def test_txt_encodings(tmp_path: Path, encoding: str, bom: bytes) -> None:
    src = tmp_path / "notes.txt"
    src.write_bytes(bom + "Café crème, naïve – 20 €\nsecond line\n".encode(encoding))
    (out,) = converted("docx", src)
    text = plain(out)
    assert "Café crème, naïve – 20 €" in text
    assert "\x00" not in text


# --- images ----------------------------------------------------------------------------------
def test_images_of_a_container_go_into_a_media_folder(tmp_path: Path) -> None:
    png(tmp_path / "dot.png")
    src = make(tmp_path / "doc.docx", "Look: ![a dot](dot.png)\n")
    (tmp_path / "dot.png").unlink()
    md, image = converted("md", src)
    assert image.relative_to(tmp_path).parts[0] == "doc media"
    assert image.suffix == ".png"
    link = image.relative_to(tmp_path).as_posix()
    assert link in md.read_text(encoding="utf-8")  # relative, so the pair can move together
    html, image2 = converted("html", src)
    assert image2.relative_to(tmp_path).parts[0] == "doc media (2)"  # never mixed with old
    assert image2.relative_to(tmp_path).as_posix() in html.read_text(encoding="utf-8")


def test_no_images_no_media_folder(tmp_path: Path) -> None:
    src = make(tmp_path / "doc.docx")
    converted("md", src)
    converted("html", src)
    assert tree(tmp_path) == ["doc.docx", "doc.html", "doc.md"]


def test_local_images_of_a_markdown_source_are_embedded(tmp_path: Path) -> None:
    png(tmp_path / "dot.png")
    src = tmp_path / "doc.md"
    src.write_text("Look: ![a dot](dot.png)\n", encoding="utf-8")
    (out,) = converted("docx", src)
    with zipfile.ZipFile(out) as zf:
        assert any(n.startswith("word/media/") for n in zf.namelist())


def test_local_images_resolve_in_a_folder_named_with_a_semicolon(tmp_path: Path) -> None:
    """';' separates pandoc's --resource-path entries on Windows."""
    folder = tmp_path / "Notes; 2026"
    folder.mkdir()
    png(folder / "dot.png")
    src = folder / "doc.md"
    src.write_text("Look: ![a dot](dot.png)\n", encoding="utf-8")
    (out,) = converted("docx", src)
    with zipfile.ZipFile(out) as zf:
        assert any(n.startswith("word/media/") for n in zf.namelist())


@pytest.fixture
def image_server() -> Iterator[tuple[str, list[str]]]:
    """A loopback HTTP server that serves a PNG and records every request it gets."""
    hits: list[str] = []
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 255)).save(buf, format="PNG")
    body = buf.getvalue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()


@pytest.mark.parametrize("target", ["docx", "epub", "odt", "rtf"])
def test_remote_images_are_never_fetched(
    tmp_path: Path, image_server: tuple[str, list[str]], target: str
) -> None:
    """CONTRIBUTING.md rule 7: writers that embed images would download remote ones."""
    url, hits = image_server
    src = tmp_path / "doc.md"
    src.write_text(
        f'{TEXT}\n\n![remote alt]({url}/a.png)\n\n<img src="{url}/b.png">\n', encoding="utf-8"
    )
    (out,) = converted(target, src)
    assert hits == []
    assert "remote alt" in plain(out)


def test_extracting_media_never_fetches_remote_images(
    tmp_path: Path, image_server: tuple[str, list[str]]
) -> None:
    url, hits = image_server
    src = make(tmp_path / "doc.odt", f"{TEXT}\n\n![remote alt]({url}/a.png)\n")
    assert hits  # the control: plain pandoc downloads it while building the ODT
    hits.clear()
    converted("md", src)
    assert hits == []


def test_markdown_to_html_keeps_remote_image_links(
    tmp_path: Path, image_server: tuple[str, list[str]]
) -> None:
    url, hits = image_server
    src = tmp_path / "doc.md"
    src.write_text(f"![remote alt]({url}/a.png)\n", encoding="utf-8")
    (out,) = converted("html", src)
    assert f"{url}/a.png" in out.read_text(encoding="utf-8")
    assert hits == []


def test_raw_html_keeps_its_text_and_fetches_nothing(
    tmp_path: Path, image_server: tuple[str, list[str]]
) -> None:
    """EPUB keeps raw HTML: a remote image in it must not cost the rest of the block."""
    url, hits = image_server
    src = tmp_path / "doc.md"
    src.write_text(
        f'<table><tr><td>cell text <img src="{url}/b.png" srcset="{url}/c.png 2x"></td></tr>'
        f'</table>\n\n<div style="background:url({url}/d.png)">styled text</div>\n',
        encoding="utf-8",
    )
    (out,) = converted("epub", src)
    text = plain(out)
    assert "cell text" in text and "styled text" in text
    assert hits == []


def test_pandoc_runs_with_every_proxy_pointing_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("https_proxy", "http://corp-proxy:8080")
    env = documents.offline_env()
    assert "NO_PROXY" not in {k.upper() for k in env}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert [v for k, v in env.items() if k.upper() == name] == ["http://0.0.0.0:9"]


# --- failures and cancel ---------------------------------------------------------------------
def test_a_broken_document_fails_cleanly(tmp_path: Path) -> None:
    src = tmp_path / "broken.docx"
    src.write_bytes(b"not a zip at all")
    result = run("md", src)
    assert not result["ok"]
    assert result["message"]
    assert tree(tmp_path) == ["broken.docx"]


def fake_pandoc(monkeypatch: pytest.MonkeyPatch, stderr: str) -> list[str]:
    """Make run() start a Python process that prints ``stderr`` and fails; returns its args."""
    monkeypatch.setattr(documents, "pandoc_exe", lambda: sys.executable)
    return ["-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit(1)"]


def test_the_error_message_leaves_out_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = fake_pandoc(
        monkeypatch,
        "[WARNING] Could not fetch resource x.png: HttpExceptionRequest Request {\n"
        '    host = "127.0.0.1"\n'
        "  }\n"
        "Error at line 3:\n"
        "  unexpected end of input\n",
    )
    with pytest.raises(documents.PandocError) as e:
        documents.run(args, JobContext("t"), cwd=tmp_path)
    assert str(e.value) == "Error at line 3: unexpected end of input"


def test_a_long_error_message_is_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = fake_pandoc(monkeypatch, "Error: " + "x" * 2000 + "\n")
    with pytest.raises(documents.PandocError) as e:
        documents.run(args, JobContext("t"), cwd=tmp_path)
    assert len(str(e.value)) == documents.MAX_ERROR
    assert str(e.value).endswith("…")


class CancelWhenRunning:
    """A cancel flag that turns on once pandoc has been running for a moment."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.at: float | None = None

    def __contains__(self, _job: object) -> bool:
        if self.at is None and time.monotonic() - self.start > 0.3:
            self.at = time.monotonic()
        return self.at is not None


def test_cancel_kills_pandoc_and_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "big.md"
    src.write_text("\n\n".join(f"Paragraph {i} with *some* text." for i in range(200_000)))
    flags = CancelWhenRunning()
    result = run("docx", src, JobContext("t", None, flags))
    assert result["message"] == "Cancelled"
    assert flags.at is not None and time.monotonic() - flags.at < 2  # acts within a few polls
    assert tree(tmp_path) == ["big.md"]


def test_the_bundled_pandoc_is_used() -> None:
    assert Path(pandoc_exe()).parts[-3:] == ("pypandoc", "files", "pandoc.exe")


def test_new_media_folder_is_removed_when_the_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        (tmp_path / "doc media" / "media").mkdir(parents=True)
        (tmp_path / "doc media" / "media" / "x.png").write_bytes(b"x")
        raise documents.PandocError("boom")

    src = make(tmp_path / "doc.docx")
    monkeypatch.setattr(documents, "run", boom)
    result = run("md", src)
    assert result["message"] == "PandocError: boom"
    assert tree(tmp_path) == ["doc.docx"]


# --- petals (§6.1 order, PDF is DS-33) -------------------------------------------------------
def labels(*names: str) -> list[str]:
    return [o.label for o in registry.ops_for([Path(n) for n in names], "convert")]


def test_petals_follow_the_prd_order_without_the_source_format() -> None:
    assert labels("a.docx") == ["MD", "TXT", "HTML", "EPUB", "ODT", "RTF"]
    assert labels("a.txt") == ["DOCX", "MD", "HTML", "EPUB", "ODT", "RTF"]
    assert "HTML" not in labels("a.htm")
    assert labels("a.md", "b.rtf") == ["DOCX", "TXT", "HTML", "EPUB", "ODT"]
    assert labels("a.docx", "b.pdf") == []  # mixed kinds


# --- through the real worker pool ------------------------------------------------------------
@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


@pytest.mark.parametrize(("source", "target"), [("docx", "md"), ("txt", "docx")])
def test_converts_in_a_worker_process(
    qtbot, jm: JobManager, tmp_path: Path, source: str, target: str
) -> None:
    src = make(tmp_path / f"doc.{source}")
    with qtbot.waitSignal(jm.finished, timeout=120_000) as blocker:
        jm.submit(f"convert.document.{target}", [src])
    result = blocker.args[1]
    assert result.ok, result.message
    (out,) = result.outputs
    assert out == tmp_path / f"doc.{target}"
    assert TEXT in plain(out).replace("\n", " ")
