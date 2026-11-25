from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global config at a temp dir for every test.

    The real `~/.config/fujimoto/config.yaml` may set a default model, which
    would otherwise appear in every launch's claude arguments.
    """
    root = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    return root


@pytest.fixture(autouse=True)
def _isolated_version_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the update-check cache at a temp file for every test.

    New sessions read it to decide whether to offer an upgrade, so the real
    `~/.cache/fujimoto/version_check.json` would leak into launch prompts.
    """
    path = tmp_path / "version-cache" / "version_check.json"
    monkeypatch.setattr("fujimoto.version_check._cache_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _outside_cmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if outside cmux.

    Run inside a cmux pane, `CMUX_SURFACE_ID` would make attach tests call
    the real cmux CLI and rebind the developer's own pane.
    """
    for key in ("CMUX_SURFACE_ID", "CMUX_PANEL_ID", "CMUX_HISTORY_FILE"):
        monkeypatch.delenv(key, raising=False)
