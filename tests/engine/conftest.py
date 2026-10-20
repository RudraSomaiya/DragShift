import pytest

from dragshift.engine import registry


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty registry that does not auto-load the real Op modules."""
    monkeypatch.setattr(registry, "_OPS", {})
    monkeypatch.setattr(registry, "_loaded", True)
