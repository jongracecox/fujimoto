from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from fujimoto.tmux import (
    TmuxError,
    _configure_session,
    _ensure_extended_keys,
    _meta_key_label,
    attach_session,
    create_session,
    create_session_with_command,
    display_message,
    get_session_path,
    install_tmux,
    is_tmux_installed,
    kill_session,
    launch_claude_in_tmux,
    list_project_sessions,
    rename_session,
    session_exists,
    session_name,
    set_terminal_title,
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
        with (
            patch("fujimoto.tmux.sys.platform", "darwin"),
            patch("fujimoto.tmux.shutil.which", return_value=None),
        ):
            with pytest.raises(TmuxError, match="brew is not installed"):
                install_tmux()

    def test_raises_on_brew_failure(self) -> None:
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "brew":
                return "/opt/homebrew/bin/brew"
            return None

        with (
            patch("fujimoto.tmux.sys.platform", "darwin"),
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
            patch("fujimoto.tmux.sys.platform", "darwin"),
            patch("fujimoto.tmux.shutil.which", side_effect=which_side_effect),
            patch(
                "fujimoto.tmux.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
        ):
            with pytest.raises(TmuxError, match="not found on PATH"):
                install_tmux()

    def test_linux_raises_with_apt_hint(self) -> None:
        def which_side_effect(cmd: str) -> str | None:
            return "/usr/bin/apt-get" if cmd == "apt-get" else None

        with (
            patch("fujimoto.tmux.sys.platform", "linux"),
            patch("fujimoto.tmux.shutil.which", side_effect=which_side_effect),
        ):
            with pytest.raises(TmuxError, match="apt-get install"):
                install_tmux()

    def test_linux_raises_with_pacman_hint(self) -> None:
        def which_side_effect(cmd: str) -> str | None:
            return "/usr/bin/pacman" if cmd == "pacman" else None

        with (
            patch("fujimoto.tmux.sys.platform", "linux"),
            patch("fujimoto.tmux.shutil.which", side_effect=which_side_effect),
        ):
            with pytest.raises(TmuxError, match="pacman -S"):
                install_tmux()

    def test_linux_raises_with_generic_hint_when_no_pkg_manager(self) -> None:
        with (
            patch("fujimoto.tmux.sys.platform", "linux"),
            patch("fujimoto.tmux.shutil.which", return_value=None),
        ):
            with pytest.raises(TmuxError, match="package manager"):
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


class TestRenameSession:
    def test_renames_session(self) -> None:
        mock_result = MagicMock(returncode=0)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=mock_result
        ) as mock_run:
            rename_session("proj/old-name", "proj/new-name")
            mock_run.assert_called_once_with(
                ["tmux", "rename-session", "-t", "proj/old-name", "proj/new-name"],
                capture_output=True,
            )

    def test_raises_on_failure(self) -> None:
        mock_result = MagicMock(returncode=1)
        with patch("fujimoto.tmux.subprocess.run", return_value=mock_result):
            with pytest.raises(TmuxError, match="Failed to rename"):
                rename_session("proj/old", "proj/new")


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

    def test_creates_session_with_system_prompt(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            create_session("proj/test", tmp_path, system_prompt="You are in a worktree")

            calls = mock_run.call_args_list
            assert calls[0] == call(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "proj/test",
                    "-c",
                    str(tmp_path),
                    "claude --append-system-prompt 'You are in a worktree'",
                ],
                check=True,
            )


class TestConfigureSession:
    def test_installs_fujimoto_key_table_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FUJIMOTO_META_KEY", raising=False)
        monkeypatch.delenv("FUJIMOTO_TMUX_PREFIX", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            _configure_session("proj/test")

            cmds = [c.args[0] for c in mock_run.call_args_list]
            # Status hint mentions fujimoto chord with new default ^A
            status_cmd = next(
                c
                for c in cmds
                if c[:3] == ["tmux", "set-option", "-t"] and c[4] == "status-right"
            )
            assert "Fujimoto: ^A t/T/w/v/d/x/[" in status_cmd[5]
            assert "^A t toggles" in status_cmd[5]
            assert "help: ^A ?" in status_cmd[5]
            # fujimoto-table bindings present (server-global, no -t)
            table_keys = [
                c[4] for c in cmds if c[:4] == ["tmux", "bind-key", "-T", "fujimoto"]
            ]
            assert set(table_keys) == {"t", "T", "v", "w", "d", "x", "[", "?"}
            # Root C-a switches to fujimoto table (server-global, no -t)
            assert any(
                c[:5] == ["tmux", "bind-key", "-n", "C-a", "switch-client"]
                for c in cmds
            )

    def test_default_prefix_is_c_b(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FUJIMOTO_META_KEY", raising=False)
        monkeypatch.delenv("FUJIMOTO_TMUX_PREFIX", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            _configure_session("proj/test")
            cmds = [c.args[0] for c in mock_run.call_args_list]
            # prefix is set to C-b
            assert any(
                c[:3] == ["tmux", "set-option", "-t"]
                and c[4] == "prefix"
                and c[5] == "C-b"
                for c in cmds
            )
            # send-prefix bound to C-b
            assert any(
                c[:5] == ["tmux", "bind-key", "-t", "proj/test", "C-b"]
                and c[5] == "send-prefix"
                for c in cmds
            )
            # default prefix matches DEFAULT_PREFIX_KEY -> no unbind C-b
            assert not any(
                c[:3] == ["tmux", "unbind-key", "-t"] and c[4] == "C-b" for c in cmds
            )

    def test_custom_prefix_unbinds_default_c_b(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_META_KEY", "C-f")
        monkeypatch.setenv("FUJIMOTO_TMUX_PREFIX", "C-a")
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            _configure_session("proj/test")
            cmds = [c.args[0] for c in mock_run.call_args_list]
            # prefix set to C-a
            assert any(
                c[:3] == ["tmux", "set-option", "-t"]
                and c[4] == "prefix"
                and c[5] == "C-a"
                for c in cmds
            )
            # Old C-b prefix unbound
            assert any(
                c[:3] == ["tmux", "unbind-key", "-t"] and c[4] == "C-b" for c in cmds
            )
            # send-prefix bound on C-a
            assert any(
                c[:5] == ["tmux", "bind-key", "-t", "proj/test", "C-a"]
                and c[5] == "send-prefix"
                for c in cmds
            )

    def test_collision_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FUJIMOTO_META_KEY", "C-a")
        monkeypatch.setenv("FUJIMOTO_TMUX_PREFIX", "C-a")
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ):
            with pytest.raises(TmuxError, match="both set to 'C-a'"):
                _configure_session("proj/test")

    def test_disabled_when_meta_key_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_META_KEY", "")
        monkeypatch.delenv("FUJIMOTO_TMUX_PREFIX", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            _configure_session("proj/test")

            cmds = [c.args[0] for c in mock_run.call_args_list]
            assert not any(
                c[:4] == ["tmux", "bind-key", "-T", "fujimoto"] for c in cmds
            )
            assert not any(
                c[:2] == ["tmux", "bind-key"] and "switch-client" in c for c in cmds
            )
            # Status hint omits the chord prefix and uses configured prefix label
            status_cmd = next(
                c
                for c in cmds
                if c[:3] == ["tmux", "set-option", "-t"] and c[4] == "status-right"
            )
            assert "Fujimoto" not in status_cmd[5]
            assert "^B D" in status_cmd[5]

    def test_custom_meta_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FUJIMOTO_META_KEY", "M-f")
        monkeypatch.delenv("FUJIMOTO_TMUX_PREFIX", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            _configure_session("proj/test")

            cmds = [c.args[0] for c in mock_run.call_args_list]
            assert any(
                c[:5] == ["tmux", "bind-key", "-n", "M-f", "switch-client"]
                for c in cmds
            )


class TestMetaKeyLabel:
    def test_ctrl_key(self) -> None:
        assert _meta_key_label("C-f") == "^F"

    def test_other_key(self) -> None:
        assert _meta_key_label("M-f") == "M-f"


class TestGetSessionPath:
    def test_returns_path_on_success(self) -> None:
        with patch(
            "fujimoto.tmux.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="/tmp/abc\n"),
        ):
            assert get_session_path("proj/test") == Path("/tmp/abc")

    def test_returns_none_on_failure(self) -> None:
        with patch(
            "fujimoto.tmux.subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert get_session_path("proj/test") is None


class TestDisplayMessage:
    def test_invokes_tmux_display_message(self) -> None:
        with patch("fujimoto.tmux.subprocess.run") as mock_run:
            display_message("proj/test", "hello")
            mock_run.assert_called_once_with(
                ["tmux", "display-message", "-t", "proj/test", "hello"],
                capture_output=True,
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
            mock_create.assert_called_once_with(
                "proj/20260309-test",
                wt_path,
                system_prompt=None,
                resume_session_id=None,
            )
            mock_attach.assert_called_once_with("proj/20260309-test")

    def test_uses_explicit_tmux_name(self, tmp_path: Path) -> None:
        with (
            patch("fujimoto.tmux.session_exists", return_value=True),
            patch("fujimoto.tmux.attach_session") as mock_attach,
        ):
            launch_claude_in_tmux("proj", tmp_path, "proj/direct-1")
            mock_attach.assert_called_once_with("proj/direct-1")


class TestEnsureExtendedKeys:
    def test_sets_global_extended_keys_and_appends_extkeys(self) -> None:
        def run_side_effect(args: list[str], **kwargs: object) -> MagicMock:
            if args[:3] == ["tmux", "show-options", "-s"]:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0)

        with patch(
            "fujimoto.tmux.subprocess.run", side_effect=run_side_effect
        ) as mock_run:
            _ensure_extended_keys()

            calls = mock_run.call_args_list
            assert (
                call(
                    ["tmux", "set-option", "-g", "extended-keys", "always"],
                    check=True,
                )
                in calls
            )
            assert (
                call(
                    [
                        "tmux",
                        "set-option",
                        "-s",
                        "-a",
                        "terminal-features",
                        "xterm*:extkeys",
                    ],
                    check=True,
                )
                in calls
            )

    def test_skips_append_when_extkeys_already_present(self) -> None:
        def run_side_effect(args: list[str], **kwargs: object) -> MagicMock:
            if args[:3] == ["tmux", "show-options", "-s"]:
                return MagicMock(
                    returncode=0,
                    stdout="terminal-features[0] xterm*:extkeys\n",
                )
            return MagicMock(returncode=0)

        with patch(
            "fujimoto.tmux.subprocess.run", side_effect=run_side_effect
        ) as mock_run:
            _ensure_extended_keys()

            # Should NOT have the append call
            for c in mock_run.call_args_list:
                if c[0][0][:4] == ["tmux", "set-option", "-s", "-a"]:
                    pytest.fail("Should not append extkeys when already present")


class TestSetTerminalTitle:
    def test_writes_osc_sequence(self) -> None:
        with patch("fujimoto.tmux.sys.stdout") as mock_stdout:
            set_terminal_title("hello")
            mock_stdout.write.assert_called_once_with("\033]0;hello\007")
            mock_stdout.flush.assert_called_once()

    def test_handles_oserror_gracefully(self) -> None:
        with patch("fujimoto.tmux.sys.stdout") as mock_stdout:
            mock_stdout.write.side_effect = OSError("broken pipe")
            # Should not raise
            set_terminal_title("hello")

    def test_empty_title(self) -> None:
        with patch("fujimoto.tmux.sys.stdout") as mock_stdout:
            set_terminal_title("")
            mock_stdout.write.assert_called_once_with("\033]0;\007")


class TestQuickTerminalBinding:
    def test_quick_terminal_key_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.tmux import quick_terminal_key

        monkeypatch.delenv("FUJIMOTO_QUICK_TERMINAL_KEY", raising=False)
        assert quick_terminal_key() == "C-`"

    def test_quick_terminal_key_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.tmux import quick_terminal_key

        monkeypatch.setenv("FUJIMOTO_QUICK_TERMINAL_KEY", "C-Space")
        assert quick_terminal_key() == "C-Space"

    def test_quick_terminal_key_empty_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.tmux import quick_terminal_key

        monkeypatch.setenv("FUJIMOTO_QUICK_TERMINAL_KEY", "")
        assert quick_terminal_key() == ""

    def test_enable_runs_bind_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.tmux import enable_quick_terminal_binding

        monkeypatch.delenv("FUJIMOTO_QUICK_TERMINAL_KEY", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            enable_quick_terminal_binding()
            args = mock_run.call_args[0][0]
            assert args[:4] == ["tmux", "bind-key", "-n", "C-`"]
            assert "if-shell" in args
            assert any("split-window -v -l 30%" in a for a in args)
            assert any("select-pane -t :.+" in a for a in args)

    def test_enable_noop_when_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.tmux import enable_quick_terminal_binding

        monkeypatch.setenv("FUJIMOTO_QUICK_TERMINAL_KEY", "")
        with patch("fujimoto.tmux.subprocess.run") as mock_run:
            enable_quick_terminal_binding()
            mock_run.assert_not_called()

    def test_disable_runs_unbind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.tmux import disable_quick_terminal_binding

        monkeypatch.delenv("FUJIMOTO_QUICK_TERMINAL_KEY", raising=False)
        with patch(
            "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run:
            disable_quick_terminal_binding()
            mock_run.assert_called_once_with(
                ["tmux", "unbind-key", "-n", "C-`"], capture_output=True
            )

    def test_disable_noop_when_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.tmux import disable_quick_terminal_binding

        monkeypatch.setenv("FUJIMOTO_QUICK_TERMINAL_KEY", "")
        with patch("fujimoto.tmux.subprocess.run") as mock_run:
            disable_quick_terminal_binding()
            mock_run.assert_not_called()

    def test_apply_quick_terminal_setting_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.settings import Settings
        from fujimoto.tmux import _apply_quick_terminal_setting

        monkeypatch.delenv("FUJIMOTO_QUICK_TERMINAL_KEY", raising=False)
        with (
            patch(
                "fujimoto.settings.load_settings",
                return_value=Settings(quick_terminal_enabled=True),
            ),
            patch(
                "fujimoto.tmux.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            _apply_quick_terminal_setting()
            assert mock_run.called

    def test_apply_quick_terminal_setting_when_disabled(self) -> None:
        from fujimoto.settings import Settings
        from fujimoto.tmux import _apply_quick_terminal_setting

        with (
            patch(
                "fujimoto.settings.load_settings",
                return_value=Settings(quick_terminal_enabled=False),
            ),
            patch("fujimoto.tmux.subprocess.run") as mock_run,
        ):
            _apply_quick_terminal_setting()
            mock_run.assert_not_called()
