"""Tests for fujimoto.terminal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.terminal import (
    _applescript_quote,
    _has_iterm,
    _open_iterm,
    _open_terminal_app,
    open_terminal,
)


class TestApplescriptQuote:
    def test_plain_string(self) -> None:
        assert _applescript_quote("/Users/foo/bar") == "/Users/foo/bar"

    def test_backslash(self) -> None:
        assert _applescript_quote("a\\b") == "a\\\\b"

    def test_double_quote(self) -> None:
        assert _applescript_quote('a"b') == 'a\\"b'

    def test_both(self) -> None:
        assert _applescript_quote('a\\"b') == 'a\\\\\\"b'


class TestHasIterm:
    @patch("fujimoto.terminal.Path.exists", return_value=True)
    def test_installed(self, _mock: object) -> None:
        assert _has_iterm() is True

    @patch("fujimoto.terminal.Path.exists", return_value=False)
    def test_not_installed(self, _mock: object) -> None:
        assert _has_iterm() is False


class TestOpenIterm:
    @patch("fujimoto.terminal.subprocess.run")
    def test_calls_osascript(self, mock_run: object) -> None:
        _open_iterm(Path("/tmp/test"))

        assert mock_run.call_count == 1  # type: ignore[union-attr]
        args = mock_run.call_args  # type: ignore[union-attr]
        assert args[0][0][0] == "osascript"
        assert args[0][0][1] == "-e"
        assert "/tmp/test" in args[0][0][2]
        assert args[1]["check"] is True


class TestOpenTerminalApp:
    @patch("fujimoto.terminal.subprocess.run")
    def test_calls_open(self, mock_run: object) -> None:
        _open_terminal_app(Path("/tmp/test"))

        mock_run.assert_called_once_with(  # type: ignore[union-attr]
            ["open", "-a", "Terminal", "/tmp/test"], check=True
        )


class TestOpenTerminal:
    @patch("fujimoto.terminal.shutil.which", return_value=None)
    def test_raises_on_non_macos(self, _mock: object) -> None:
        with pytest.raises(OSError, match="osascript not found"):
            open_terminal(Path("/tmp/test"))

    @patch("fujimoto.terminal.subprocess.run")
    @patch("fujimoto.terminal._has_iterm", return_value=True)
    @patch("fujimoto.terminal.shutil.which", return_value="/usr/bin/osascript")
    def test_uses_iterm_when_available(
        self, _which: object, _has: object, mock_run: object
    ) -> None:
        open_terminal(Path("/tmp/test"))
        args = mock_run.call_args  # type: ignore[union-attr]
        assert args[0][0][0] == "osascript"

    @patch("fujimoto.terminal.subprocess.run")
    @patch("fujimoto.terminal._has_iterm", return_value=False)
    @patch("fujimoto.terminal.shutil.which", return_value="/usr/bin/osascript")
    def test_falls_back_to_terminal_app(
        self, _which: object, _has: object, mock_run: object
    ) -> None:
        open_terminal(Path("/tmp/test"))
        mock_run.assert_called_once_with(  # type: ignore[union-attr]
            ["open", "-a", "Terminal", "/tmp/test"], check=True
        )
