"""platform/dwm.py: what Windows reports about appearance (PRD §8.2, §9.6, NFR-8)."""

import pytest

from dragshift.platform import dwm


def test_argb_colorization_is_read_as_rgb() -> None:
    assert dwm.rgb_from_argb(0xC40078D4) == (0x00, 0x78, 0xD4)


def test_abgr_registry_accent_is_read_as_rgb() -> None:
    assert dwm.rgb_from_abgr(0xFFD47800) == (0x00, 0x78, 0xD4)


def test_accent_prefers_the_registry_then_dwm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dwm, "_registry_accent", lambda: (1, 2, 3))
    monkeypatch.setattr(dwm, "_dwm_colorization", lambda: (4, 5, 6))
    assert dwm.accent_rgb() == (1, 2, 3)
    monkeypatch.setattr(dwm, "_registry_accent", lambda: None)
    assert dwm.accent_rgb() == (4, 5, 6)
    monkeypatch.setattr(dwm, "_dwm_colorization", lambda: None)
    assert dwm.accent_rgb() is None


def test_light_theme_follows_the_registry_value(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {"AppsUseLightTheme": 0}
    monkeypatch.setattr(dwm, "_read_dword", lambda _key, name: values.get(name))
    assert dwm.apps_use_light_theme() is False
    values["AppsUseLightTheme"] = 1
    assert dwm.apps_use_light_theme() is True
    values.clear()
    assert dwm.apps_use_light_theme() is None


def test_real_calls_return_well_formed_values() -> None:
    """Read-only queries of this machine's settings; any answer is fine, a crash is not."""
    accent = dwm.accent_rgb()
    assert accent is None or (len(accent) == 3 and all(0 <= c <= 255 for c in accent))
    assert dwm.apps_use_light_theme() in (True, False, None)
    assert isinstance(dwm.animations_enabled(), bool)
