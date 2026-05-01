"""Tests for fujimoto.terminal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.terminal import (
    _applescript_quote,
    _format_args,
    _has_iterm,
    _open_iterm,
    _open_linux_terminal,
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


class TestFormatArgs:
    def test_substitutes_placeholder(self) -> None:
        assert _format_args(["--cwd", "{dir}"], Path("/tmp/x")) == ["--cwd", "/tmp/x"]

    def test_substitutes_inline(self) -> None:
        assert _format_args(["--working-directory={dir}"], Path("/tmp/x")) == [
            "--working-directory=/tmp/x"
        ]

    def test_appends_when_no_placeholder(self) -> None:
        assert _format_args(["-e", "bash"], Path("/tmp/x")) == ["-e", "bash", "/tmp/x"]


class TestOpenTerminalMacOS:
    @patch("fujimoto.terminal.sys.platform", "darwin")
    @patch("fujimoto.terminal.subprocess.run")
    @patch("fujimoto.terminal._has_iterm", return_value=True)
    def test_uses_iterm_when_available(self, _has: object, mock_run: object) -> None:
        open_terminal(Path("/tmp/test"))
        args = mock_run.call_args  # type: ignore[union-attr]
        assert args[0][0][0] == "osascript"

    @patch("fujimoto.terminal.sys.platform", "darwin")
    @patch("fujimoto.terminal.subprocess.run")
    @patch("fujimoto.terminal._has_iterm", return_value=False)
    def test_falls_back_to_terminal_app(self, _has: object, mock_run: object) -> None:
        open_terminal(Path("/tmp/test"))
        mock_run.assert_called_once_with(  # type: ignore[union-attr]
            ["open", "-a", "Terminal", "/tmp/test"], check=True
        )


class TestOpenLinuxTerminal:
    @patch("fujimoto.terminal.subprocess.Popen")
    @patch("fujimoto.terminal.shutil.which")
    def test_uses_first_found_terminal(
        self, mock_which: object, mock_popen: object
    ) -> None:
        # Only gnome-terminal is on PATH (after x-terminal-emulator misses)
        def which(cmd: str) -> str | None:
            return "/usr/bin/gnome-terminal" if cmd == "gnome-terminal" else None

        mock_which.side_effect = which  # type: ignore[union-attr]
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FUJIMOTO_TERMINAL", None)
            _open_linux_terminal(Path("/tmp/x"))

        cmd = mock_popen.call_args[0][0]  # type: ignore[union-attr]
        assert cmd[0] == "gnome-terminal"
        assert "--working-directory=/tmp/x" in cmd

    @patch("fujimoto.terminal.subprocess.Popen")
    @patch("fujimoto.terminal.shutil.which", return_value="/usr/bin/alacritty")
    def test_uses_custom_env_var(self, _which: object, mock_popen: object) -> None:
        with patch.dict(
            "os.environ",
            {"FUJIMOTO_TERMINAL": "alacritty --working-directory {dir}"},
        ):
            _open_linux_terminal(Path("/tmp/x"))

        cmd = mock_popen.call_args[0][0]  # type: ignore[union-attr]
        assert cmd == ["alacritty", "--working-directory", "/tmp/x"]

    @patch("fujimoto.terminal.subprocess.Popen")
    @patch("fujimoto.terminal.shutil.which", return_value="/usr/bin/myterm")
    def test_env_var_appends_dir_without_placeholder(
        self, _which: object, mock_popen: object
    ) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_TERMINAL": "myterm -e bash"}):
            _open_linux_terminal(Path("/tmp/x"))

        cmd = mock_popen.call_args[0][0]  # type: ignore[union-attr]
        assert cmd == ["myterm", "-e", "bash", "/tmp/x"]

    @patch("fujimoto.terminal.shutil.which", return_value=None)
    def test_env_var_missing_executable_raises(self, _which: object) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_TERMINAL": "nope --foo"}):
            with pytest.raises(OSError, match="not on PATH"):
                _open_linux_terminal(Path("/tmp/x"))

    @patch("fujimoto.terminal.shutil.which", return_value=None)
    def test_no_terminal_found_raises(self, _which: object) -> None:
        import os

        os.environ.pop("FUJIMOTO_TERMINAL", None)
        with pytest.raises(OSError, match="No supported terminal"):
            _open_linux_terminal(Path("/tmp/x"))


class TestOpenTerminalDispatch:
    @patch("fujimoto.terminal.sys.platform", "linux")
    @patch("fujimoto.terminal._open_linux_terminal")
    def test_linux_dispatches_to_linux_handler(self, mock_handler: object) -> None:
        open_terminal(Path("/tmp/x"))
        mock_handler.assert_called_once_with(Path("/tmp/x"))  # type: ignore[union-attr]

    @patch("fujimoto.terminal.sys.platform", "win32")
    def test_unsupported_platform_raises(self) -> None:
        with pytest.raises(OSError, match="not supported on platform"):
            open_terminal(Path("/tmp/x"))
