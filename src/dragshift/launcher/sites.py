"""Sites wheel slots (FR-3.4): ``{label, url}``, opened in the default browser, with a favicon.

The favicon is fetched once per site, by a short-lived child process
(``dragshift.launcher.favicon``), so ``urllib`` and ``ssl`` never load into the UI process
(NFR-1). The result is cached in ``%APPDATA%\\DragShift\\icons``. A site whose fetch failed
gets a ``<host>.none`` marker and is not tried again: that fetch is the only network access
DragShift makes (NFR-5).
"""

import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QProcess, Signal

from dragshift.config import LauncherSite, default_config_path
from dragshift.launcher.favicon import NO_ICON, file_stem

log = logging.getLogger(__name__)

SCHEMES = ("http", "https")
ICON_EXTS = ("ico", "png", "gif", "jpg", "bmp", "webp", "svg")  # as written by favicon.py


def icons_dir() -> Path:
    return default_config_path().parent / "icons"


def normalize_url(url: str) -> str | None:
    """``github.com`` -> ``https://github.com``. None for anything but a web address."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in SCHEMES or not parts.hostname or "@" in parts.netloc:
        return None  # "@": "javascript:x@evil.com" would read as a user name at evil.com
    try:
        parts.port  # noqa: B018 - raises for "javascript:alert(1)" read as host:port
    except ValueError:
        return None
    return url


def host_of(url: str) -> str | None:
    normal = normalize_url(url)
    return urlsplit(normal).hostname if normal else None


def label_of(site: LauncherSite) -> str:
    if site.label.strip():
        return site.label.strip()
    host = host_of(site.url) or site.url.strip()
    return host.removeprefix("www.")


def launch_site(site: LauncherSite) -> str | None:
    """Open the site in the default browser. Returns None on success, else a reason."""
    url = normalize_url(site.url)
    if url is None:
        return f"not a web address: {site.url!r}"
    try:
        os.startfile(url)
    except OSError as e:
        return str(e)
    return None


def cached_icon(url: str, folder: Path) -> Path | None:
    """The cached favicon of the site at ``url``, if one was fetched."""
    host = host_of(url)
    if host is None:
        return None
    stem = file_stem(host)
    for ext in ICON_EXTS:
        path = folder / f"{stem}.{ext}"
        if path.is_file():
            return path
    return None


def needs_fetch(url: str, folder: Path) -> bool:
    """True if this site has neither an icon nor a failed attempt on record."""
    host = host_of(url)
    if host is None:
        return False
    return (
        cached_icon(url, folder) is None and not (folder / f"{file_stem(host)}.{NO_ICON}").exists()
    )


class FaviconCache(QObject):
    """Starts one child process per site that still needs its favicon."""

    updated = Signal()  # a fetch finished: icons may have changed

    def __init__(self, folder: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.folder = folder or icons_dir()
        self._running: dict[str, QProcess] = {}  # host -> its fetch

    def ensure(self, sites: Iterable[LauncherSite]) -> None:
        for site in sites:
            host = host_of(site.url)
            if host and host not in self._running and needs_fetch(site.url, self.folder):
                self._start(host)

    def _start(self, host: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        proc = QProcess(self)
        args = ["-m", "dragshift.launcher.favicon", host, str(self.folder)]
        proc.finished.connect(lambda *_: self._done(host))
        self._running[host] = proc
        log.info("fetching the favicon of %s (once)", host)
        proc.start(sys.executable, args)

    def _done(self, host: str) -> None:
        # The finished QProcess stays a child of this object (one per site, ever). Deleting it
        # here with deleteLater crashed Qt later, when this object itself was destroyed.
        self._running.pop(host, None)
        self.updated.emit()

    def wait(self, ms: int = 15_000) -> None:
        """Block until every running fetch is done (for tests and shutdown)."""
        for proc in list(self._running.values()):
            proc.waitForFinished(ms)
