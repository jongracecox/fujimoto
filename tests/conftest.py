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
