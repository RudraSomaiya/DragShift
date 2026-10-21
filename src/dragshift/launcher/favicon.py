"""Fetch one site's favicon into the icon cache (FR-3.4). Runs as a child process:

    python -m dragshift.launcher.favicon <host> <folder>

Writes ``<host>.<ext>`` when ``https://<host>/favicon.ico`` is a real image, else the marker
``<host>.none`` so the site is never tried again. Exits 0 either way.
"""

import re
import sys
from pathlib import Path

NO_ICON = "none"  # marker extension: tried once, nothing usable
TIMEOUT_S = 5
MAX_BYTES = 512 * 1024  # a favicon bigger than this is not a favicon

# Leading bytes -> file extension. Only real images are kept, not an HTML error page.
_MAGIC = (
    (b"\x00\x00\x01\x00", "ico"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"\xff\xd8\xff", "jpg"),
    (b"BM", "bmp"),
)


def file_stem(host: str) -> str:
    """A host as a safe file name (``localhost:8080`` -> ``localhost_8080``)."""
    return re.sub(r"[^a-z0-9.-]", "_", host.lower())


def image_ext(data: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:256].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:1024].lower()):
        return "svg"
    return None


def fetch(host: str, folder: Path, url: str | None = None) -> Path:
    """Fetch ``url`` (default ``https://<host>/favicon.ico``) and cache it. Returns the file
    written: the icon, or the ``.none`` marker."""
    import urllib.request  # here, not at the top: sites.py imports this module in the UI

    stem = file_stem(host)
    url = url or f"https://{host}/favicon.ico"
    data = b""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "DragShift"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = response.read(MAX_BYTES + 1)
    except (OSError, ValueError):  # offline, DNS, TLS, HTTP 404, timeout, a bad URL
        data = b""
    ext = image_ext(data) if 0 < len(data) <= MAX_BYTES else None
    folder.mkdir(parents=True, exist_ok=True)
    if ext is None:
        marker = folder / f"{stem}.{NO_ICON}"
        marker.touch()
        return marker
    target = folder / f"{stem}.{ext}"
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    part.replace(target)
    return target


def main(argv: list[str]) -> int:
    host, folder = argv[1], Path(argv[2])
    fetch(host, folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
