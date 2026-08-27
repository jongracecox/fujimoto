from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fujimoto import debug


def _settings_path() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "settings.json"


@dataclass
class Settings:
    quick_terminal_enabled: bool | None = None


def load_settings() -> Settings:
    path = _settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Settings()
    raw = data.get("quick_terminal_enabled")
    enabled: bool | None = raw if isinstance(raw, bool) else None
    debug.log_once(
        "settings",
        "settings.loaded",
        path=debug.rp(path),
        quick_terminal_enabled=enabled,
    )
    return Settings(quick_terminal_enabled=enabled)


def save_settings(settings: Settings) -> None:
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"quick_terminal_enabled": settings.quick_terminal_enabled})
        )
        debug.log(
            "settings.saved",
            path=debug.rp(path),
            quick_terminal_enabled=settings.quick_terminal_enabled,
        )
    except OSError:
        pass
