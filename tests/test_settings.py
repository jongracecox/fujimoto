from __future__ import annotations

from pathlib import Path

import pytest

from fujimoto.settings import Settings, load_settings, save_settings


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_load_settings_missing_file(tmp_home: Path) -> None:
    settings = load_settings()
    assert settings == Settings(quick_terminal_enabled=None)


def test_load_settings_corrupt_json(tmp_home: Path) -> None:
    cache = tmp_home / ".cache" / "fujimoto" / "settings.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_load_settings_non_bool_value(tmp_home: Path) -> None:
    cache = tmp_home / ".cache" / "fujimoto" / "settings.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"quick_terminal_enabled": "yes"}')
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_save_and_load_roundtrip_true(tmp_home: Path) -> None:
    save_settings(Settings(quick_terminal_enabled=True))
    assert load_settings() == Settings(quick_terminal_enabled=True)


def test_save_and_load_roundtrip_false(tmp_home: Path) -> None:
    save_settings(Settings(quick_terminal_enabled=False))
    assert load_settings() == Settings(quick_terminal_enabled=False)


def test_save_settings_creates_parent_dir(tmp_home: Path) -> None:
    save_settings(Settings(quick_terminal_enabled=True))
    assert (tmp_home / ".cache" / "fujimoto" / "settings.json").exists()


def test_save_settings_swallows_oserror(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a: object, **kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _raise)
    save_settings(Settings(quick_terminal_enabled=True))
