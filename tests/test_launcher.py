"""Quick Launch slots (FR-3.3, FR-3.4): app targets, site URLs, launching, the favicon cache.

Nothing here touches the internet: the favicon tests use a server on 127.0.0.1.
"""

import http.server
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dragshift.config import LauncherApp, LauncherSite
from dragshift.launcher import apps, favicon, sites

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ICO = b"\x00\x00\x01\x00" + b"\x00" * 64
CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"


# --- apps ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target", "aumid"),
    [
        (CALC, CALC),
        ("shell:AppsFolder\\" + CALC, CALC),
        ("SHELL:APPSFOLDER\\" + CALC, CALC),
        ("C:\\Program Files\\App\\app.exe", None),
        ("C:\\weird!name\\app.exe", None),  # a path, even with a "!"
        ("shell:AppsFolder\\", None),
    ],
)
def test_aumid_of(target: str, aumid: str | None) -> None:
    assert apps.aumid_of(target) == aumid


@pytest.mark.parametrize(
    ("app", "label"),
    [
        (LauncherApp(label="  VS Code ", target="x.exe"), "VS Code"),
        (LauncherApp(target="C:\\Tools\\Code.exe"), "Code"),
        (LauncherApp(target="C:\\Users\\me\\Desktop\\Notes.lnk"), "Notes"),
        (LauncherApp(target="D:\\"), "D:\\"),
        (LauncherApp(target=CALC), "Calculator"),
        (LauncherApp(target="Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe!App"), "Edge"),
        (LauncherApp(target="SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"), "Spotify Music"),
    ],
)
def test_app_labels(app: LauncherApp, label: str) -> None:
    assert apps.label_of(app) == label


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(apps.os, "startfile", lambda *a, **k: calls.append(("startfile", a, k)))
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: calls.append(("popen", a, k)))
    return calls


def test_an_exe_starts_in_its_own_folder(tmp_path: Path, started: list[tuple]) -> None:
    exe = tmp_path / "tool.exe"
    exe.touch()
    assert apps.launch_app(LauncherApp(target=str(exe))) is None
    assert started == [("startfile", (exe,), {"cwd": str(tmp_path)})]


def test_a_shortcut_or_folder_is_opened_as_is(tmp_path: Path, started: list[tuple]) -> None:
    lnk = tmp_path / "Notes.lnk"
    lnk.touch()
    assert apps.launch_app(LauncherApp(target=str(lnk))) is None
    assert apps.launch_app(LauncherApp(target=str(tmp_path))) is None
    assert started == [("startfile", (lnk,), {}), ("startfile", (tmp_path,), {})]


def test_environment_variables_are_expanded(
    tmp_path: Path, started: list[tuple], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_TEST_DIR", str(tmp_path))
    assert apps.launch_app(LauncherApp(target="%DS_TEST_DIR%")) is None
    assert started == [("startfile", (tmp_path,), {})]


def test_a_store_app_opens_through_the_apps_folder(started: list[tuple]) -> None:
    assert apps.launch_app(LauncherApp(target=CALC)) is None
    assert started == [("popen", (["explorer.exe", "shell:AppsFolder\\" + CALC],), {})]


def test_missing_or_empty_targets_give_a_reason(tmp_path: Path, started: list[tuple]) -> None:
    assert "does not exist" in apps.launch_app(LauncherApp(target=str(tmp_path / "gone.exe")))
    assert apps.launch_app(LauncherApp(target="  ")) == "this slot is empty"
    assert started == []


# --- sites -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "normal"),
    [
        ("github.com", "https://github.com"),
        ("  https://news.ycombinator.com/ ", "https://news.ycombinator.com/"),
        ("http://192.168.1.1", "http://192.168.1.1"),
        ("ftp://files.example.com", None),
        ("javascript:alert(1)", None),
        ("file:///C:/Windows/notepad.exe", None),
        ("", None),
        ("https://", None),
    ],
)
def test_normalize_url(url: str, normal: str | None) -> None:
    assert sites.normalize_url(url) == normal


def test_site_labels() -> None:
    assert sites.label_of(LauncherSite(label="Mail", url="mail.google.com")) == "Mail"
    assert sites.label_of(LauncherSite(url="https://www.github.com/x")) == "github.com"


def test_launch_site(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(sites.os, "startfile", opened.append)
    assert sites.launch_site(LauncherSite(url="github.com")) is None
    assert "not a web address" in sites.launch_site(LauncherSite(url="file:///C:/x.exe"))
    assert opened == ["https://github.com"]  # only ever a web address goes to the shell


# --- favicons --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "ext"),
    [
        (ICO, "ico"),
        (PNG, "png"),
        (b"GIF89a....", "gif"),
        (b"\xff\xd8\xff\xe0....", "jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b'  <svg xmlns="http://www.w3.org/2000/svg"/>', "svg"),
        (b"<!doctype html><html>Not found</html>", None),
        (b"", None),
    ],
)
def test_image_ext(data: bytes, ext: str | None) -> None:
    assert favicon.image_ext(data) == ext


@pytest.fixture
def server() -> Iterator[str]:
    """http://127.0.0.1:<port> serving a few fixed paths."""
    pages = {"/png": PNG, "/html": b"<html>oops</html>", "/huge": PNG + b"\x00" * favicon.MAX_BYTES}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = pages.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_args: object) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_a_real_image_is_cached_under_its_own_extension(server: str, tmp_path: Path) -> None:
    written = favicon.fetch("example.com", tmp_path, url=server + "/png")
    assert written == tmp_path / "example.com.png"
    assert written.read_bytes() == PNG
    assert sites.cached_icon("https://example.com/path", tmp_path) == written
    assert not sites.needs_fetch("example.com", tmp_path)
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("path", ["/html", "/missing", "/huge"])
def test_anything_else_leaves_a_marker_so_it_is_never_tried_again(
    server: str, tmp_path: Path, path: str
) -> None:
    written = favicon.fetch("example.com", tmp_path, url=server + path)
    assert written == tmp_path / "example.com.none"
    assert sites.cached_icon("example.com", tmp_path) is None
    assert not sites.needs_fetch("example.com", tmp_path)


def test_unreachable_also_leaves_a_marker(tmp_path: Path) -> None:
    written = favicon.fetch("example.com", tmp_path, url="http://127.0.0.1:9/favicon.ico")
    assert written.suffix == ".none"


def test_host_file_names_are_safe() -> None:
    assert favicon.file_stem("LocalHost:8080") == "localhost_8080"
    assert favicon.file_stem("a/../b") == "a_.._b"


def test_needs_fetch(tmp_path: Path) -> None:
    assert sites.needs_fetch("github.com", tmp_path)
    assert not sites.needs_fetch("not a url at all://", tmp_path)


def test_the_cache_fetches_in_a_child_process_once(qtbot: QtBot, tmp_path: Path) -> None:
    """End to end, still offline: https on a local port that is closed fails fast, which is
    enough to see the child run, write its marker and report back."""
    cache = sites.FaviconCache(tmp_path)
    site = LauncherSite(url="https://127.0.0.1:9")
    with qtbot.waitSignal(cache.updated, timeout=20_000):
        cache.ensure([site])
    assert (tmp_path / "127.0.0.1.none").exists()
    cache.ensure([site])  # tried once: no second process
    assert not cache._running


def test_the_ui_side_never_imports_the_network_stack() -> None:
    script = (
        "import sys, dragshift.launcher.sites, dragshift.ui.launcher_wheel;"
        "print(','.join(m for m in ('ssl', 'urllib.request', 'http.client') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
