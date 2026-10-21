import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dragshift.engine import registry
from dragshift.engine.jobs import JobManager

EVERYONE = "*S-1-1-0"  # the well-known SID, so icacls needs no account name


@pytest.fixture
def make_read_only() -> Iterator[Callable[[Path], Path]]:
    """Deny everyone "create files" and "create folders" in a folder (FR-5.3's "not writable",
    like C:\\Program Files for a normal user). The deny is lifted again after the test."""
    denied: list[Path] = []

    def make(folder: Path) -> Path:
        rights = f"{EVERYONE}:(WD,AD)"  # write data / add file, append data / add subdirectory
        subprocess.run(["icacls", str(folder), "/deny", rights], check=True, capture_output=True)
        denied.append(folder)
        try:
            (folder / "probe").touch()
        except PermissionError:
            return folder
        (folder / "probe").unlink()
        pytest.skip("the icacls deny did not take effect here")

    yield make
    for folder in denied:
        subprocess.run(
            ["icacls", str(folder), "/remove:d", EVERYONE], check=True, capture_output=True
        )


@pytest.fixture
def downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """%USERPROFILE%\\Downloads in a temp folder, for this process and the workers it spawns."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    return home / "Downloads"


@pytest.fixture(autouse=True)
def cancel_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep cancel flags out of the real %LOCALAPPDATA%\\DragShift."""
    folder = tmp_path_factory.mktemp("cancel")
    monkeypatch.setattr(JobManager, "CANCEL_DIR", folder)
    return folder


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty registry that does not auto-load the real Op modules."""
    monkeypatch.setattr(registry, "_OPS", {})
    monkeypatch.setattr(registry, "_loaded", True)
