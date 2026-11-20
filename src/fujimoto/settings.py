"""User preferences the TUI reads and writes.

These live in the global config (`~/.config/fujimoto/config.yaml`, see
`global_config`). They used to be in `~/.cache/fujimoto/settings.json`; a value
found only there is copied into the config the first time it is read, and the
old file is left alone so an older fujimoto running alongside still sees it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fujimoto import debug

from .config import ConfigError
from .global_config import load_global_config, update_global_key


def _settings_path() -> Path:
    """The legacy settings file, read only to migrate from it."""
    return Path.home() / ".cache" / "fujimoto" / "settings.json"


@dataclass
class Settings:
    quick_terminal_enabled: bool | None = None


def _legacy_quick_terminal() -> bool | None:
    path = _settings_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("quick_terminal_enabled") if isinstance(data, dict) else None
    return raw if isinstance(raw, bool) else None


def load_settings() -> Settings:
    try:
        enabled = load_global_config().quick_terminal
    except ConfigError:
        # Already reported when the TUI opens; treat as "never asked" here.
        return Settings()
    if enabled is None:
        legacy = _legacy_quick_terminal()
        if legacy is not None:
            enabled = legacy
            try:
                update_global_key("quick_terminal", legacy)
                debug.log("settings.migrated", quick_terminal=legacy)
            except (ConfigError, OSError) as exc:
                debug.log("settings.migrate_failed", error=type(exc).__name__)
    debug.log_once("settings", "settings.loaded", quick_terminal_enabled=enabled)
    return Settings(quick_terminal_enabled=enabled)


def save_settings(settings: Settings) -> None:
    try:
        if settings.quick_terminal_enabled is None:
            update_global_key("quick_terminal", None, remove=True)
        else:
            update_global_key("quick_terminal", settings.quick_terminal_enabled)
        debug.log(
            "settings.saved",
            quick_terminal_enabled=settings.quick_terminal_enabled,
        )
    except (ConfigError, OSError) as exc:
        debug.log("settings.save_failed", error=type(exc).__name__)
