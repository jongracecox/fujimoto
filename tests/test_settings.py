from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fujimoto import settings as settings_mod
from fujimoto.global_config import global_config_path
from fujimoto.settings import Settings, load_settings, save_settings


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _legacy(tmp_home: Path) -> Path:
    path = tmp_home / ".cache" / "fujimoto" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _config() -> dict[str, object]:
    return yaml.safe_load(global_config_path().read_text()) or {}


def test_load_settings_missing_file(tmp_home: Path) -> None:
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_load_settings_corrupt_legacy_json(tmp_home: Path) -> None:
    _legacy(tmp_home).write_text("{not json")
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_load_settings_non_bool_legacy_value(tmp_home: Path) -> None:
    _legacy(tmp_home).write_text('{"quick_terminal_enabled": "yes"}')
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_load_settings_legacy_not_a_mapping(tmp_home: Path) -> None:
    _legacy(tmp_home).write_text("[true]")
    assert load_settings() == Settings(quick_terminal_enabled=None)


@pytest.mark.parametrize("value", [True, False])
def test_save_and_load_roundtrip(tmp_home: Path, value: bool) -> None:
    save_settings(Settings(quick_terminal_enabled=value))
    assert load_settings() == Settings(quick_terminal_enabled=value)
    assert _config() == {"quick_terminal": value}


def test_save_writes_global_config_not_cache(tmp_home: Path) -> None:
    save_settings(Settings(quick_terminal_enabled=True))
    assert global_config_path().exists()
    assert not (tmp_home / ".cache" / "fujimoto" / "settings.json").exists()


def test_save_none_removes_the_key(tmp_home: Path) -> None:
    save_settings(Settings(quick_terminal_enabled=True))
    save_settings(Settings(quick_terminal_enabled=None))
    assert "quick_terminal" not in _config()


def test_save_keeps_other_keys_and_comments(tmp_home: Path) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("# my notes\nmodel: opus\n")
    save_settings(Settings(quick_terminal_enabled=False))
    text = path.read_text()
    assert "# my notes" in text
    assert _config() == {"model": "opus", "quick_terminal": False}


def test_legacy_value_is_migrated_once(tmp_home: Path) -> None:
    legacy = _legacy(tmp_home)
    legacy.write_text(json.dumps({"quick_terminal_enabled": True}))
    assert load_settings() == Settings(quick_terminal_enabled=True)
    assert _config() == {"quick_terminal": True}
    # The legacy file is left for an older fujimoto running alongside.
    assert legacy.exists()
    # The config wins from then on.
    save_settings(Settings(quick_terminal_enabled=False))
    assert load_settings() == Settings(quick_terminal_enabled=False)


def test_config_value_beats_legacy(tmp_home: Path) -> None:
    _legacy(tmp_home).write_text(json.dumps({"quick_terminal_enabled": True}))
    path = global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("quick_terminal: false\n")
    assert load_settings() == Settings(quick_terminal_enabled=False)


def test_invalid_global_config_reads_as_never_asked(tmp_home: Path) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("bogus_key: 1\n")
    assert load_settings() == Settings(quick_terminal_enabled=None)


def test_migration_failure_still_returns_legacy_value(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy(tmp_home).write_text(json.dumps({"quick_terminal_enabled": True}))

    def _raise(*a: object, **kw: object) -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(settings_mod, "update_global_key", _raise)
    assert load_settings() == Settings(quick_terminal_enabled=True)


def test_save_settings_swallows_oserror(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a: object, **kw: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(settings_mod, "update_global_key", _raise)
    save_settings(Settings(quick_terminal_enabled=True))
