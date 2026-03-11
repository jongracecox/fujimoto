"""Tests for fujimoto.vscode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.vscode import _has_vscode, open_vscode


class TestHasVscode:
    @patch("fujimoto.vscode.shutil.which", return_value="/usr/local/bin/code")
    def test_found(self, _mock: object) -> None:
        assert _has_vscode() is True

    @patch("fujimoto.vscode.shutil.which", return_value=None)
    def test_not_found(self, _mock: object) -> None:
        assert _has_vscode() is False


class TestOpenVscode:
    @patch("fujimoto.vscode.shutil.which", return_value=None)
    def test_raises_when_code_not_found(self, _mock: object) -> None:
        with pytest.raises(OSError, match="'code' CLI not found"):
            open_vscode(Path("/tmp/test"))

    @patch("fujimoto.vscode.subprocess.run")
    @patch("fujimoto.vscode.shutil.which", return_value="/usr/local/bin/code")
    def test_calls_code(self, _which: object, mock_run: object) -> None:
        open_vscode(Path("/tmp/test"))
        mock_run.assert_called_once_with(  # type: ignore[union-attr]
            ["code", "/tmp/test"], check=True
        )
