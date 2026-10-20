from pathlib import Path

import pytest

from dragshift.engine import registry
from dragshift.engine.jobs import JobManager


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
