from __future__ import annotations

import json
from pathlib import Path

import pytest

from fujimoto.claude import trust


def _state(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(payload))
    return path


class TestIsTrusted:
    def test_trusted(self, tmp_path: Path) -> None:
        d = tmp_path / "chats"
        d.mkdir()
        state = _state(
            tmp_path, {"projects": {str(d.resolve()): {"hasTrustDialogAccepted": True}}}
        )
        assert trust.is_trusted(d, state) is True

    def test_not_trusted(self, tmp_path: Path) -> None:
        state = _state(
            tmp_path,
            {"projects": {"/other": {"hasTrustDialogAccepted": True}, "/x": {}}},
        )
        assert trust.is_trusted(Path("/x"), state) is False

    @pytest.mark.parametrize("payload", [[], {"projects": []}, {}])
    def test_unexpected_shape_is_unknown(self, tmp_path: Path, payload: object) -> None:
        assert trust.is_trusted(Path("/x"), _state(tmp_path, payload)) is None

    def test_missing_or_corrupt_is_unknown(self, tmp_path: Path) -> None:
        assert trust.is_trusted(Path("/x"), tmp_path / "missing.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{nope")
        assert trust.is_trusted(Path("/x"), bad) is None


class TestStateFile:
    def test_follows_claude_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert trust.get_claude_state_file() == tmp_path / ".claude.json"

    def test_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert trust.get_claude_state_file() == Path.home() / ".claude.json"
