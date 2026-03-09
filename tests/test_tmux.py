from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from fujimoto.tmux import (
    TmuxError,
    attach_session,
    create_session,
    create_session_with_command,
    install_tmux,
    is_tmux_installed,
    kill_session,
    launch_claude_in_tmux,
    list_project_sessions,
    session_exists,
    session_name,
)


class TestSessionName:
    def test_format(self) -> None:
        assert session_name("my-project", "20260309-fix") == "my-project/20260309-fix"

    def test_with_special_chars(self) -> None:
        assert session_name("proj", "a-b-c") == "proj/a-b-c"


class TestIsTmuxInstalled:
    def test_returns_true_when_found(self) -> None:
        with patch("fujimoto.tmux.shutil.which", return_value="/usr/bin/tmux"):
            assert is_tmux_installed() is True

    def test_returns_false_when_missing(self) -> None:
        with patch("fujimoto.tmux.shutil.which", return_value=None):
            assert is_tmux_installed() is False


class TestInstallTmux:
    def test_raises_when_brew_missing(self) -> None:
        with patch("fujimoto.tmux.shutil.which", return_value=None):
            with pytest.raises(TmuxError, match="brew is not installed"):
                install_tmux()

    def test_raises_on_brew_failure(self) -> None:
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "brew":
                return "/opt/homebrew/bin/brew"
            return None

        with (
            patch("fujimoto.tmux.shutil.which", side_effect=which_side_effect),
            patch(
                "fujimoto.tmux.subprocess.run",
                return_value=MagicMock(returncode=1),
            ),
        ):
            with pytest.raises(TmuxError, match="Failed to install"):
                install_tmux()

    def test_raises_when_not_on_path_after_install(self) -> None:
        call_count = 0

        def which_side_effect(cmd: str) -> str | None:
            nonlocal call_count
            if cmd == "brew":
                return "/opt/homebrew/bin/brew"
            call_count += 1
            if call_count <= 1:
                return None  # Before install
            return None  # Still not found after install

        with (
            patch("fujimoto.tmux.shutil.which", side_effect=which_side_effect),
            patch(
                "fujimoto.tmux.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
        ):
            with pytest.raises(TmuxError, match="not found on PATH"):
                install_tmux()


class TestListProjectSessions:
    def test_filters_by_project(self) -> None:
        mock_result = MagicMock(
            returncode=0,
            stdout="my-proj/20260309-fix\nother/thing\nmy-proj/20260308-test\n",
        )
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            result = list_project_sessions("my-proj")
            assert result == ["my-proj/20260309-fix", "my-proj/20260308-test"]

    def test_returns_empty_on_failure(self) -> None:
        mock_result = MagicMock(returncode=1)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            assert list_project_sessions("proj") == []

    def test_returns_empty_when_no_matches(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="other/session\n")
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            assert list_project_sessions("my-proj") == []


class TestSessionExists:
    def test_returns_true_on_success(self) -> None:
        mock_result = MagicMock(returncode=0)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            assert session_exists("my-proj/test") is True

    def test_returns_false_on_failure(self) -> None:
        mock_result = MagicMock(returncode=1)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            assert session_exists("my-proj/test") is False


class TestKillSession:
    def test_kills_session(self) -> None:
        mock_result = MagicMock(returncode=0)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            kill_session("my-proj/test")

    def test_raises_on_failure(self) -> None:
        mock_result = MagicMock(returncode=1)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            with pytest.raises(TmuxError, match="Failed to kill"):
                kill_session("my-proj/test")


class TestCreateSession:
    def test_creates_session_and_configures(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            create_session("proj/test", tmp_path)

            calls = mock_run.call_args_list
            # First call: new-session with claude as the session command
            assert calls[0] == call(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "proj/test",
                    "-c",
                    str(tmp_path),
                    "claude",
                ],
                check=True,
            )


class TestCreateSessionWithCommand:
    def test_creates_session_with_custom_command(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            create_session_with_command("proj/pr-test", tmp_path, "echo hello")

            calls = mock_run.call_args_list
            assert calls[0] == call(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "proj/pr-test",
                    "-c",
                    str(tmp_path),
                    "echo hello",
                ],
                check=True,
            )


class TestAttachSession:
    def test_calls_subprocess_run(self) -> None:
        with patch("fujimoto.tmux.subprocess.run") as mock_run:
            attach_session("proj/test")
            mock_run.assert_called_once_with(
                ["tmux", "attach-session", "-t", "proj/test"]
            )


class TestLaunchClaudeInTmux:
    def test_attaches_when_session_exists(self, tmp_path: Path) -> None:
        with (
            patch("fujimoto.tmux.session_exists", return_value=True),
            patch("fujimoto.tmux.attach_session") as mock_attach,
        ):
            launch_claude_in_tmux("proj", tmp_path / "20260309-test")
            mock_attach.assert_called_once_with("proj/20260309-test")

    def test_creates_and_attaches_when_no_session(self, tmp_path: Path) -> None:
        with (
            patch("fujimoto.tmux.session_exists", return_value=False),
            patch("fujimoto.tmux.create_session") as mock_create,
            patch("fujimoto.tmux.attach_session") as mock_attach,
        ):
            wt_path = tmp_path / "20260309-test"
            launch_claude_in_tmux("proj", wt_path)
            mock_create.assert_called_once_with("proj/20260309-test", wt_path)
            mock_attach.assert_called_once_with("proj/20260309-test")

    def test_uses_explicit_tmux_name(self, tmp_path: Path) -> None:
        with (
            patch("fujimoto.tmux.session_exists", return_value=True),
            patch("fujimoto.tmux.attach_session") as mock_attach,
        ):
            launch_claude_in_tmux("proj", tmp_path, "proj/direct-1")
            mock_attach.assert_called_once_with("proj/direct-1")
