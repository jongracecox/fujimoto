from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import pytest

from textual.widgets import Collapsible, Input, Label, ListItem, ListView, Static

from fujimoto.claude import (
    ClaudeSession,
    ContentMode,
    SessionState,
    Snippet,
    TranscriptMessage,
)
from fujimoto.claude.log_parser import EntryType, StopReason
from types import SimpleNamespace

from fujimoto.cli import (
    CSS,
    ICON_EYES,
    ICON_FORK,
    ICON_GEAR,
    ICON_ORANGE_CIRCLE,
    ICON_SHIELD,
    LaunchTarget,
    SessionApp,
    SessionInfo,
    _build_fork_system_prompt,
    _claude_state_label,
    _format_prompt_lines,
    _friendly_key_label,
    SNIPPET_MATCH_STYLE,
    _fit_snippet,
    _get_claude_sessions,
    _relative_time,
    _render_snippet,
    _pair_results,
    _tool_run_title,
    _tool_summary,
    main,
)
from fujimoto import session_state
from fujimoto.config import ConfigError
from fujimoto.git import GitError
from fujimoto.tmux import TmuxError


# -- Helpers --


def _patch_git_info(
    project: str = "test-proj",
    current: str = "feat/test",
    default: str = "main",
    sessions: list[str] | None = None,
    worktrees: list[Path] | None = None,
    projects: list[Path] | None = None,
    claude_sessions_fn: object | None = None,
    open_sessions: dict[str, object] | None = None,
    worktree_root: Path | None = None,
    session_paths: dict[str, Path] | None = None,
):
    """Return a context manager that patches git/tmux info for TUI tests."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        nonlocal worktree_root
        if worktrees is not None:
            # We need the root to exist for iterdir. A caller that needs to
            # know the paths up front (to pair them with `session_paths`)
            # supplies the root itself.
            if worktree_root is None:
                import tempfile

                _tmpdir = tempfile.mkdtemp()
                worktree_root = Path(_tmpdir) / project
            worktree_root.mkdir(parents=True, exist_ok=True)
            for wt in worktrees:
                (worktree_root / wt.name).mkdir(exist_ok=True)

        with (
            patch("fujimoto.cli.is_tmux_installed", return_value=True),
            patch("fujimoto.cli.get_project_name", return_value=project),
            patch(
                "fujimoto.cli.get_repo_root",
                return_value=Path("/fake/repo"),
            ),
            patch("fujimoto.cli.get_current_branch", return_value=current),
            patch("fujimoto.cli.get_default_branch", return_value=default),
            patch(
                "fujimoto.cli.list_project_sessions",
                return_value=sessions or [],
            ),
            patch(
                "fujimoto.cli.get_project_worktrees_dir",
                return_value=worktree_root or Path("/nonexistent"),
            ),
            patch("fujimoto.cli.session_name", side_effect=lambda p, d: f"{p}/{d}"),
            # Never shell out to a real tmux from a test: an unpatched lookup
            # would make every direct row fall back to the project root.
            patch(
                "fujimoto.cli.get_session_path",
                side_effect=lambda name: (session_paths or {}).get(name),
            ),
            patch(
                "fujimoto.cli.list_projects",
                return_value=projects or [],
            ),
            patch(
                "fujimoto.cli.get_sessions_for_path",
                side_effect=claude_sessions_fn or (lambda _path: []),
            ),
            patch(
                "fujimoto.cli.check_for_update",
                return_value=(None, False),
            ),
            patch(
                "fujimoto.session_state.prune",
                return_value=open_sessions or {},
            ),
            patch(
                "fujimoto.cli.load_settings",
                return_value=__import__(
                    "fujimoto.settings", fromlist=["Settings"]
                ).Settings(quick_terminal_enabled=False),
            ),
        ):
            yield

    return _ctx()


# -- main() tests --


@pytest.fixture(autouse=True)
def _clean_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["fujimoto"])


@pytest.fixture(autouse=True)
def _isolate_session_state(tmp_path: Path):
    """Keep the TUI tests off the real ~/.cache/fujimoto/sessions.json."""
    with patch(
        "fujimoto.session_state._state_path",
        return_value=tmp_path / "session-state" / "sessions.json",
    ):
        yield


class TestPaneSubcommand:
    def test_vscode_invokes_open_vscode_with_session_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["fujimoto", "pane", "vscode", "--session", "proj/test"]
        )
        with (
            patch("fujimoto.cli.get_session_path", return_value=Path("/tmp/wt")),
            patch("fujimoto.cli.open_vscode") as mock_code,
        ):
            main()
            mock_code.assert_called_once_with(Path("/tmp/wt"))

    def test_terminal_invokes_open_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["fujimoto", "pane", "terminal", "--session", "proj/test"]
        )
        with (
            patch("fujimoto.cli.get_session_path", return_value=Path("/tmp/wt")),
            patch("fujimoto.cli.open_terminal") as mock_term,
        ):
            main()
            mock_term.assert_called_once_with(Path("/tmp/wt"))

    def test_missing_session_path_surfaces_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["fujimoto", "pane", "vscode", "--session", "ghost"]
        )
        with (
            patch("fujimoto.cli.get_session_path", return_value=None),
            patch("fujimoto.cli.display_message") as mock_msg,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        mock_msg.assert_called_once()
        assert "ghost" in mock_msg.call_args.args[1]

    def test_oserror_surfaces_via_display_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["fujimoto", "pane", "vscode", "--session", "proj/test"]
        )
        with (
            patch("fujimoto.cli.get_session_path", return_value=Path("/tmp/wt")),
            patch("fujimoto.cli.open_vscode", side_effect=OSError("code missing")),
            patch("fujimoto.cli.display_message") as mock_msg,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        assert "code missing" in mock_msg.call_args.args[1]


class TestDebugFlags:
    @pytest.fixture(autouse=True)
    def _isolate_debug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto import debug

        monkeypatch.setenv(debug.LOG_DIR_ENV, str(tmp_path / "logs"))
        debug.disable()
        yield
        debug.disable()

    def _log_files(self, tmp_path: Path) -> list[Path]:
        return sorted((tmp_path / "logs").glob("*.log"))

    def test_debug_writes_a_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        app = SessionApp()
        app._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", return_value=app),
            patch.object(app, "run"),
        ):
            main()
        logs = self._log_files(tmp_path)
        assert len(logs) == 1
        text = logs[0].read_text()
        assert "===== fujimoto / system" in text
        assert "prerequisites issues=0" in text
        assert "tui.exit reason=quit" in text
        assert "shutdown" in text

    def test_no_flag_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto"])
        app = SessionApp()
        app._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", return_value=app),
            patch.object(app, "run"),
        ):
            main()
        assert not (tmp_path / "logs").exists()

    def test_debug_redacted_hides_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug-redacted"])
        app = SessionApp()
        app._launch_target = LaunchTarget(
            "secretproj",
            Path("/tmp/wt/secretproj-worktree"),
            "secretproj/20260827-thing",
            "worktree",
            None,
        )
        second = SessionApp()
        second._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app, second]),
            patch.object(app, "run"),
            patch.object(second, "run"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._build_system_prompt", return_value="p"),
            patch("fujimoto.cli._session_terminal_title", return_value="t"),
        ):
            main()
        text = self._log_files(tmp_path)[0].read_text()
        assert "launch.target" in text
        assert "secretproj" not in text
        assert "20260827-thing" not in text
        assert "[REDACTED-" in text

    def test_debug_logs_launch_target_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        app = SessionApp()
        app._launch_target = LaunchTarget(
            "proj",
            Path("/tmp/wt/proj-worktree"),
            "proj/20260827-thing",
            "worktree",
            None,
        )
        second = SessionApp()
        second._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app, second]),
            patch.object(app, "run"),
            patch.object(second, "run"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._build_system_prompt", return_value="p"),
            patch("fujimoto.cli._session_terminal_title", return_value="t"),
        ):
            main()
        text = self._log_files(tmp_path)[0].read_text()
        assert "session_type=worktree" in text
        assert "proj/20260827-thing" in text

    def test_debug_logs_aborted_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        app = SessionApp()
        app._launch_target = LaunchTarget(
            "proj", Path("/tmp/wt"), "proj/wt", "worktree", None
        )
        second = SessionApp()
        second._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app, second]),
            patch.object(app, "run"),
            patch.object(second, "run"),
            patch("fujimoto.cli._apply_worktree_config", return_value=False),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._session_terminal_title", return_value="t"),
            patch("fujimoto.cli._build_system_prompt", return_value="p"),
        ):
            main()
        mock_launch.assert_not_called()
        assert "launch.aborted reason=project-config" in (
            self._log_files(tmp_path)[0].read_text()
        )

    def test_debug_logs_prerequisite_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=["no tmux"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        assert 'prerequisite_issue detail="no tmux"' in (
            self._log_files(tmp_path)[0].read_text()
        )

    def test_debug_logs_fatal_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=GitError("not a repo")),
            pytest.raises(SystemExit),
        ):
            main()
        text = self._log_files(tmp_path)[0].read_text()
        assert "fatal exception=GitError message=not a repo" in text
        assert "    ! Traceback" in text

    def test_debug_logs_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 130
        assert "interrupted" in self._log_files(tmp_path)[0].read_text()

    def test_debug_with_pane_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["fujimoto", "--debug", "pane", "vscode", "--session", "proj/test"],
        )
        with (
            patch("fujimoto.cli.get_session_path", return_value=Path("/tmp/wt")),
            patch("fujimoto.cli.open_vscode"),
        ):
            main()
        assert "pane.command action=vscode" in (
            self._log_files(tmp_path)[0].read_text()
        )

    def test_unopenable_log_dir_is_reported_not_fatal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import debug

        monkeypatch.setattr("sys.argv", ["fujimoto", "--debug"])
        monkeypatch.setattr(
            debug, "enable", lambda **kw: (_ for _ in ()).throw(OSError("read-only"))
        )
        app = SessionApp()
        app._launch_target = None
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", return_value=app),
            patch.object(app, "run"),
        ):
            main()
        assert "could not open debug log" in capsys.readouterr().err


class TestDebugHomeInventory:
    @pytest.fixture(autouse=True)
    def _debug_on(self, tmp_path: Path) -> None:
        from fujimoto import debug

        debug.enable(redact=False, log_dir=tmp_path / "logs")
        yield
        debug.disable()

    def _log_text(self, tmp_path: Path) -> str:
        return next((tmp_path / "logs").glob("*.log")).read_text()

    @pytest.mark.asyncio
    async def test_home_inventory_is_logged(self, tmp_path: Path) -> None:
        app = SessionApp()
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            worktrees=[Path("20260827-thing")],
            projects=[Path("/fake/other")],
        ):
            async with app.run_test():
                pass
        text = self._log_text(tmp_path)
        assert "tui.git_info cwd=none project=test-proj" in text
        assert "tui.home items=" in text
        assert "tui.worktree path=" in text
        assert "tui.item id=" in text
        assert "20260827-thing" in text

    @pytest.mark.asyncio
    async def test_home_inventory_redacts_names(self, tmp_path: Path) -> None:
        from fujimoto import debug

        debug.disable()
        debug.enable(redact=True, log_dir=tmp_path / "logs")
        app = SessionApp()
        with _patch_git_info(
            project="secretproj",
            sessions=["secretproj/direct-1"],
            worktrees=[Path("20260827-thing")],
        ):
            async with app.run_test():
                pass
        text = self._log_text(tmp_path)
        assert "tui.item id=" in text
        assert "secretproj" not in text
        assert "20260827-thing" not in text

    @pytest.mark.asyncio
    async def test_large_inventory_is_capped_and_summarised(
        self, tmp_path: Path
    ) -> None:
        from fujimoto import debug

        worktrees = [Path(f"2026082{i % 10}-thing-{i}") for i in range(25)]
        app = SessionApp()
        with _patch_git_info(worktrees=worktrees):
            async with app.run_test():
                pass
        text = self._log_text(tmp_path)
        assert text.count("tui.worktree path=") == debug.DEFAULT_SERIES_CAP
        assert "tui.worktree_summary not_logged=15" in text
        # Emitted once, not once per home render (it used to repeat verbatim).
        assert text.count("tui.worktree_summary") == 1
        # The close-time `series.summarised` line is covered in test_debug.py;
        # here the logger is still open, so it has not been flushed yet.

    @pytest.mark.asyncio
    async def test_selection_is_logged(self, tmp_path: Path) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
        assert "tui.selected list=home id=action-create" in self._log_text(tmp_path)


class TestCreateConfigFlag:
    def test_creates_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--create-config"])
        with patch("fujimoto.cli.get_repo_root", return_value=tmp_path):
            main()
        assert (tmp_path / ".fujimoto.yaml").exists()

    def test_refuses_overwrite(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".fujimoto.yaml").write_text("existing")
        monkeypatch.setattr("sys.argv", ["fujimoto", "--create-config"])
        with (
            patch("fujimoto.cli.get_repo_root", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        assert (tmp_path / ".fujimoto.yaml").read_text() == "existing"

    def test_not_a_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["fujimoto", "--create-config"])
        with (
            patch("fujimoto.cli.get_repo_root", side_effect=GitError("not a git repo")),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1


class TestShowError:
    @pytest.mark.asyncio
    async def test_renders_message_with_brackets(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                # Brackets would be parsed as markup if not escaped.
                await app._show_error("boom [type=int, input_value=1]")
                text = app.query_one("#main").query("Static")[0].render().plain
                assert "[type=int, input_value=1]" in text


class TestConfigErrorDialog:
    @pytest.mark.asyncio
    async def test_dialog_shown_for_invalid_config(self) -> None:
        from fujimoto.cli import ConfigErrorDialog
        from fujimoto.config import ConfigError

        # Message contains brackets (like pydantic's validation output) which
        # must not be parsed as console markup.
        msg = "Invalid .fujimoto.yaml:\nwhen [type=enum, input_value=1, input_type=int]"
        with _patch_git_info():
            with patch(
                "fujimoto.cli.load_project_config",
                side_effect=ConfigError(msg),
            ):
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    assert isinstance(app.screen, ConfigErrorDialog)

    @pytest.mark.asyncio
    async def test_dialog_dismissed_with_ok(self) -> None:
        from fujimoto.cli import ConfigErrorDialog
        from fujimoto.config import ConfigError

        with _patch_git_info():
            with patch(
                "fujimoto.cli.load_project_config",
                side_effect=ConfigError("bad yaml"),
            ):
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    assert isinstance(app.screen, ConfigErrorDialog)
                    await pilot.click("#ce-ok")
                    await pilot.pause()
                    assert not isinstance(app.screen, ConfigErrorDialog)

    @pytest.mark.asyncio
    async def test_no_dialog_for_valid_config(self) -> None:
        from fujimoto.cli import ConfigErrorDialog

        with _patch_git_info():
            with patch("fujimoto.cli.load_project_config"):
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    assert not isinstance(app.screen, ConfigErrorDialog)

    @pytest.mark.asyncio
    async def test_invalid_config_does_not_block_launch(self) -> None:
        # The dialog informs but must not prevent opening a session — you may
        # need to launch one to fix the file.
        from types import SimpleNamespace

        from fujimoto.config import ConfigError

        with _patch_git_info():
            with patch(
                "fujimoto.cli.load_project_config",
                side_effect=ConfigError("bad yaml"),
            ):
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._selected_session = SessionInfo(
                        name="wt",
                        session_type="worktree",
                        project="p",
                        path=Path("/tmp/wt"),
                        tmux_session="p/wt",
                        is_active=True,
                        branch="b",
                    )
                    event = SimpleNamespace(item=SimpleNamespace(id="sa-connect"))
                    await app.on_session_action_selected(event)  # type: ignore[arg-type]
                    assert app._launch_target is not None


class TestResolveWorktreeSource:
    def test_prefers_meta_source_root(self, tmp_path: Path) -> None:
        from fujimoto.cli import _resolve_worktree_source

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch(
                "fujimoto.cli.read_session_meta",
                return_value={"source_root": "/main/repo"},
            ),
            patch(
                "fujimoto.cli.get_main_worktree_root",
                return_value=Path("/main/repo"),
            ),
        ):
            assert _resolve_worktree_source(wt) == Path("/main/repo")

    def test_falls_back_to_git(self, tmp_path: Path) -> None:
        from fujimoto.cli import _resolve_worktree_source

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli.read_session_meta", return_value={}),
            patch(
                "fujimoto.cli.get_main_worktree_root",
                return_value=Path("/derived/repo"),
            ),
        ):
            assert _resolve_worktree_source(wt) == Path("/derived/repo")

    def test_none_for_main_repo(self, tmp_path: Path) -> None:
        from fujimoto.cli import _resolve_worktree_source

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli.read_session_meta", return_value={}),
            patch("fujimoto.cli.get_main_worktree_root", return_value=wt.resolve()),
        ):
            assert _resolve_worktree_source(wt) is None

    def test_none_when_not_a_repo(self, tmp_path: Path) -> None:
        from fujimoto.cli import _resolve_worktree_source

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli.read_session_meta", return_value={}),
            patch(
                "fujimoto.cli.get_main_worktree_root",
                side_effect=GitError("not a repo"),
            ),
        ):
            assert _resolve_worktree_source(wt) is None


class TestApplyWorktreeConfig:
    def test_non_worktree_proceeds(self, tmp_path: Path) -> None:
        from fujimoto.cli import _apply_worktree_config

        with patch("fujimoto.cli._resolve_worktree_source", return_value=None):
            assert _apply_worktree_config(tmp_path) is True

    def test_first_time_uses_create_and_marks(self, tmp_path: Path) -> None:
        from fujimoto.cli import _apply_worktree_config
        from fujimoto.project_config import ApplyResult, Trigger

        wt = tmp_path / "wt"
        wt.mkdir()
        source = tmp_path / "main"
        source.mkdir()
        with (
            patch("fujimoto.cli._resolve_worktree_source", return_value=source),
            patch("fujimoto.cli.config_once_applied", return_value=False),
            patch("fujimoto.cli.load_project_config") as mock_load,
            patch(
                "fujimoto.cli.apply_project_config", return_value=ApplyResult()
            ) as mock_apply,
            patch("fujimoto.cli.mark_config_once_applied") as mock_mark,
        ):
            assert _apply_worktree_config(wt) is True
            # Config is read from the source (main clone), not the worktree.
            mock_load.assert_called_once_with(source)
            assert mock_apply.call_args.kwargs["trigger"] is Trigger.CREATE
            assert mock_apply.call_args.kwargs["source_root"] == source
            assert mock_apply.call_args.kwargs["worktree_root"] == wt
            mock_mark.assert_called_once_with(wt)

    def test_subsequent_uses_launch_no_mark(self, tmp_path: Path) -> None:
        from fujimoto.cli import _apply_worktree_config
        from fujimoto.project_config import ApplyResult, Trigger

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli._resolve_worktree_source", return_value=tmp_path),
            patch("fujimoto.cli.config_once_applied", return_value=True),
            patch("fujimoto.cli.load_project_config"),
            patch(
                "fujimoto.cli.apply_project_config", return_value=ApplyResult()
            ) as mock_apply,
            patch("fujimoto.cli.mark_config_once_applied") as mock_mark,
        ):
            assert _apply_worktree_config(wt) is True
            assert mock_apply.call_args.kwargs["trigger"] is Trigger.LAUNCH
            mock_mark.assert_not_called()

    def test_init_error_abort_returns_false(self, tmp_path: Path) -> None:
        from fujimoto.cli import _apply_worktree_config
        from fujimoto.project_config import ApplyResult, OnError, ProjectConfig

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli._resolve_worktree_source", return_value=tmp_path),
            patch("fujimoto.cli.config_once_applied", return_value=False),
            patch(
                "fujimoto.cli.load_project_config",
                return_value=ProjectConfig(on_error=OnError.ABORT),
            ),
            patch(
                "fujimoto.cli.apply_project_config",
                return_value=ApplyResult(init_error="boom"),
            ),
            patch("fujimoto.cli._pause_for_key") as mock_pause,
            patch("fujimoto.cli.mark_config_once_applied") as mock_mark,
        ):
            assert _apply_worktree_config(wt) is False
            mock_pause.assert_called_once()
            mock_mark.assert_not_called()

    def test_init_error_continue_returns_true_and_marks(self, tmp_path: Path) -> None:
        from fujimoto.cli import _apply_worktree_config
        from fujimoto.project_config import ApplyResult, OnError, ProjectConfig

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli._resolve_worktree_source", return_value=tmp_path),
            patch("fujimoto.cli.config_once_applied", return_value=False),
            patch(
                "fujimoto.cli.load_project_config",
                return_value=ProjectConfig(on_error=OnError.CONTINUE),
            ),
            patch(
                "fujimoto.cli.apply_project_config",
                return_value=ApplyResult(init_error="boom"),
            ),
            patch("fujimoto.cli._pause_for_key"),
            patch("fujimoto.cli.mark_config_once_applied") as mock_mark,
        ):
            assert _apply_worktree_config(wt) is True
            mock_mark.assert_called_once_with(wt)

    def test_config_error_skips_and_proceeds(self, tmp_path: Path) -> None:
        # Malformed config is surfaced on the home screen; at launch it is just
        # skipped (no pause, no apply) and the session still opens.
        from fujimoto.cli import _apply_worktree_config
        from fujimoto.config import ConfigError

        wt = tmp_path / "wt"
        wt.mkdir()
        with (
            patch("fujimoto.cli._resolve_worktree_source", return_value=tmp_path),
            patch("fujimoto.cli.config_once_applied", return_value=False),
            patch(
                "fujimoto.cli.load_project_config",
                side_effect=ConfigError("bad yaml"),
            ),
            patch("fujimoto.cli.apply_project_config") as mock_apply,
            patch("fujimoto.cli._pause_for_key") as mock_pause,
        ):
            assert _apply_worktree_config(wt) is True
            mock_apply.assert_not_called()
            mock_pause.assert_not_called()


class TestPauseForKey:
    def test_reads_single_key_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import termios
        import tty

        from fujimoto import cli

        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.sys.stdin, "fileno", lambda: 0)
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: [])
        monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
        monkeypatch.setattr(tty, "setraw", lambda fd: None)
        reads: list[int] = []
        monkeypatch.setattr(cli.sys.stdin, "read", lambda n: (reads.append(n), "x")[1])
        cli._pause_for_key("prompt")
        assert reads == [1]

    def test_noop_when_not_a_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto import cli

        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        # Should return without touching stdin.read
        cli._pause_for_key("prompt")


class TestMain:
    def test_aborts_launch_when_config_returns_false(self) -> None:
        app1 = SessionApp.__new__(SessionApp)
        app1._launch_target = LaunchTarget(
            "proj", Path("/tmp/test"), None, "worktree", None
        )
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli._apply_worktree_config", return_value=False),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._build_system_prompt", return_value="test"),
            patch("fujimoto.cli._session_terminal_title", return_value="t"),
        ):
            main()
            mock_launch.assert_not_called()

    def test_exits_on_config_error(self) -> None:
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp") as mock_app_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_app_cls.side_effect = ConfigError("test error")
            main()
        assert exc_info.value.code == 1

    def test_exits_on_git_error(self) -> None:
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp") as mock_app_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_app_cls.side_effect = GitError("not a git repo")
            main()
        assert exc_info.value.code == 1

    def test_exits_on_tmux_error(self) -> None:
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp") as mock_app_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_app_cls.side_effect = TmuxError("tmux missing")
            main()
        assert exc_info.value.code == 1

    def test_exits_early_on_prerequisite_failure(self) -> None:
        with (
            patch(
                "fujimoto.cli._check_prerequisites",
                return_value=["FUJIMOTO_WORKTREE_ROOT is not set."],
            ),
            patch("fujimoto.cli.SessionApp") as mock_app_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        mock_app_cls.assert_not_called()

    def test_exits_on_keyboard_interrupt(self) -> None:
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp") as mock_app_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_app_cls.side_effect = KeyboardInterrupt
            main()
        assert exc_info.value.code == 130

    def test_launches_tmux_then_loops_back(self) -> None:
        # First iteration: launch target set -> attach tmux
        # Second iteration: no target -> exit loop
        app1 = SessionApp.__new__(SessionApp)
        app1._launch_target = LaunchTarget(
            "proj", Path("/tmp/test"), None, "worktree", None
        )
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch(
                "fujimoto.cli._apply_worktree_config", return_value=True
            ) as mock_apply,
            patch("fujimoto.cli._build_system_prompt", return_value="test") as mock_sp,
            patch("fujimoto.cli._session_terminal_title", return_value="test-title"),
        ):
            main()
            mock_sp.assert_called_once_with("worktree", "proj", Path("/tmp/test"))
            mock_apply.assert_called_once_with(Path("/tmp/test"))
            mock_launch.assert_called_once_with(
                "proj",
                Path("/tmp/test"),
                None,
                system_prompt="test",
                resume_session_id=None,
                fork_session=False,
            )

    def test_no_launch_when_target_not_set(self) -> None:
        mock_app = SessionApp.__new__(SessionApp)
        mock_app._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", return_value=mock_app),
            patch.object(mock_app, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
        ):
            main()
            mock_launch.assert_not_called()

    def test_launches_with_tmux_name(self) -> None:
        app1 = SessionApp.__new__(SessionApp)
        app1._launch_target = LaunchTarget(
            "proj",
            Path("/tmp/repo"),
            "proj/direct-1",
            "direct",
            None,
        )
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli._build_system_prompt", return_value="test"),
            patch("fujimoto.cli._session_terminal_title", return_value="test-title"),
        ):
            main()
            mock_launch.assert_called_once_with(
                "proj",
                Path("/tmp/repo"),
                "proj/direct-1",
                system_prompt="test",
                resume_session_id=None,
                fork_session=False,
            )


class TestSessionTerminalTitle:
    def test_default_template_worktree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.delenv("FUJIMOTO_WINDOW_TITLE", raising=False)
        with patch("fujimoto.cli.get_current_branch", return_value="main"):
            title = _session_terminal_title(
                "myproj", None, Path("/tmp/myproj/20260101-foo"), "worktree"
            )
        assert title == f"{ICON_WIZARD} fujimoto - myproj - 20260101-foo"

    def test_custom_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "{session_type}:{branch}")
        with patch("fujimoto.cli.get_current_branch", return_value="feature-x"):
            title = _session_terminal_title("proj", None, Path("/tmp/wt"), "worktree")
        assert title == f"{ICON_WIZARD} fujimoto - worktree:feature-x"

    def test_empty_template_returns_prefix_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "")
        with patch("fujimoto.cli.get_current_branch", return_value=""):
            title = _session_terminal_title("proj", None, Path("/tmp/wt"), "worktree")
        assert title == f"{ICON_WIZARD} fujimoto"

    def test_unknown_placeholder_renders_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "{nonexistent}{git_project}")
        with patch("fujimoto.cli.get_current_branch", return_value=""):
            title = _session_terminal_title("proj", None, Path("/tmp/wt"), "worktree")
        assert title == f"{ICON_WIZARD} fujimoto - proj"

    def test_branch_empty_on_git_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "branch={branch}")
        with patch("fujimoto.cli.get_current_branch", side_effect=GitError("no repo")):
            title = _session_terminal_title("proj", None, Path("/tmp/wt"), "adhoc")
        assert title == f"{ICON_WIZARD} fujimoto - branch="

    def test_adhoc_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv(
            "FUJIMOTO_WINDOW_TITLE",
            "{session_type} {worktree_name} {git_project_dir}",
        )
        with patch("fujimoto.cli.get_current_branch", side_effect=GitError("no")):
            title = _session_terminal_title(
                "", "adhoc-1", Path("/tmp/fujimoto-adhoc-xyz"), "adhoc"
            )
        assert title == f"{ICON_WIZARD} fujimoto - adhoc fujimoto-adhoc-xyz"

    def test_direct_session_git_project_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "{git_project_dir}")
        with patch("fujimoto.cli.get_current_branch", return_value="main"):
            title = _session_terminal_title("proj", None, Path("/repos/proj"), "direct")
        assert title == f"{ICON_WIZARD} fujimoto - /repos/proj"

    def test_tmux_name_falls_back_to_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_terminal_title

        monkeypatch.setenv("FUJIMOTO_WINDOW_TITLE", "{tmux_name}")
        with patch("fujimoto.cli.get_current_branch", return_value=""):
            title = _session_terminal_title("proj", None, Path("/tmp/wt"), "worktree")
        assert title == f"{ICON_WIZARD} fujimoto - proj/wt"


class TestSessionManagerTitle:
    def test_includes_project(self) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_manager_title

        assert _session_manager_title("myproj") == f"{ICON_WIZARD} fujimoto - myproj"

    def test_empty_project_returns_prefix_only(self) -> None:
        from fujimoto.cli import ICON_WIZARD, _session_manager_title

        assert _session_manager_title("") == f"{ICON_WIZARD} fujimoto"


class TestBuildSystemPrompt:
    def test_worktree_prompt_includes_base_branch(self, tmp_path: Path) -> None:
        from fujimoto.cli import _build_system_prompt
        from fujimoto.config import store_session_meta

        store_session_meta(tmp_path, "main")
        prompt = _build_system_prompt("worktree", "myproj", tmp_path)
        assert "worktree" in prompt
        assert "myproj" in prompt
        assert "main" in prompt
        assert "Focus your work on this worktree's branch" in prompt

    def test_worktree_prompt_without_meta(self, tmp_path: Path) -> None:
        from fujimoto.cli import _build_system_prompt

        prompt = _build_system_prompt("worktree", "myproj", tmp_path)
        assert "unknown" in prompt

    def test_direct_prompt(self, tmp_path: Path) -> None:
        from fujimoto.cli import _build_system_prompt

        prompt = _build_system_prompt("direct", "myproj", tmp_path)
        assert "direct" in prompt
        assert "myproj" in prompt
        assert "not an isolated worktree" in prompt

    def test_adhoc_prompt(self, tmp_path: Path) -> None:
        from fujimoto.cli import _build_system_prompt

        prompt = _build_system_prompt("adhoc", "adhoc", tmp_path)
        assert "ad hoc" in prompt
        assert "not in a git project" in prompt
        assert "temporary directory" in prompt


# -- TUI tests --


class TestHomeSearch:
    @pytest.mark.asyncio
    async def test_slash_reveals_and_focuses_search(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test() as pilot:
                search = app.query_one("#home-search", Input)
                assert not search.display
                await pilot.press("slash")
                assert search.display
                assert app._searching
                assert app.focused is not None
                assert app.focused.id == "home-search"

    @pytest.mark.asyncio
    async def test_slash_is_typed_not_rebound_while_searching(self) -> None:
        """Once the box has focus, `/` is literal text rather than the binding."""
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.press("slash")
                assert app.query_one("#home-search", Input).value == "/"

    @pytest.mark.asyncio
    async def test_live_filter_is_case_insensitive(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "LOGGING":
                    await pilot.press(key)
                await pilot.pause()
                assert set(app._session_map) == {"wt-20260308-add-logging"}
                assert not app.query("#wt-20260309-fix-tests")

    @pytest.mark.asyncio
    async def test_filter_matches_branch_name(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "worktree":
                    await pilot.press(key)
                await pilot.pause()
                assert set(app._session_map) == {"wt-20260309-fix-tests"}

    @pytest.mark.asyncio
    async def test_filter_covers_active_and_inactive(self, tmp_path: Path) -> None:
        """A query spanning both sections keeps a match in each."""
        active = tmp_path / "20260309-alpha"
        inactive = tmp_path / "20260308-alpha"
        other = tmp_path / "20260307-beta"
        with _patch_git_info(
            sessions=["test-proj/20260309-alpha"],
            worktrees=[active, inactive, other],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "alpha":
                    await pilot.press(key)
                await pilot.pause()
                assert set(app._session_map) == {
                    "wt-20260309-alpha",
                    "wt-20260308-alpha",
                }
                assert app._session_map["wt-20260309-alpha"].is_active
                assert not app._session_map["wt-20260308-alpha"].is_active

    @pytest.mark.asyncio
    async def test_filter_hides_actions_and_settings(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1], projects=[Path("/git/other")]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.press("f")
                await pilot.pause()
                assert not app.query("#action-create")
                assert not app.query("#action-switch-project")
                assert not app.query("#action-toggle-quick-terminal")

    @pytest.mark.asyncio
    async def test_no_matches_shows_placeholder(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "zzz":
                    await pilot.press(key)
                await pilot.pause()
                assert app._session_map == {}
                home_list = app.query_one("#home-list", ListView)
                assert len(home_list) == 1
                assert home_list.children[0].disabled

    @pytest.mark.asyncio
    async def test_enter_applies_filter_and_focuses_list(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "logging":
                    await pilot.press(key)
                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is not None
                assert app.focused.id == "home-list"
                assert app._search_query == "logging"
                assert set(app._session_map) == {"wt-20260308-add-logging"}

    @pytest.mark.asyncio
    async def test_highlight_skips_separator_rows(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.press("f")
                await pilot.pause()
                home_list = app.query_one("#home-list", ListView)
                assert home_list.index is not None
                assert not home_list.children[home_list.index].disabled

    @pytest.mark.asyncio
    async def test_arrows_in_search_box_move_list(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-alpha"
        wt2 = tmp_path / "20260308-alpha"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "alpha":
                    await pilot.press(key)
                await pilot.pause()
                home_list = app.query_one("#home-list", ListView)
                start = home_list.index
                await pilot.press("down")
                assert home_list.index != start
                assert not home_list.children[home_list.index or 0].disabled
                await pilot.press("up")
                assert home_list.index == start

    @pytest.mark.asyncio
    async def test_escape_in_search_box_clears_filter(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "logging":
                    await pilot.press(key)
                await pilot.press("escape")
                await pilot.pause()
                assert not app._searching
                assert app._search_query == ""
                assert not app.query_one("#home-search", Input).display
                assert len(app._session_map) == 2
                assert app.focused is not None
                assert app.focused.id == "home-list"
                assert app._launch_target is None
                assert app.is_running

    @pytest.mark.asyncio
    async def test_escape_on_filtered_list_clears_before_quitting(
        self, tmp_path: Path
    ) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.press("f")
                await pilot.press("enter")
                await pilot.press("escape")
                await pilot.pause()
                assert app._search_query == ""
                assert app.is_running
                await pilot.press("escape")
                await pilot.pause()
                assert not app.is_running

    @pytest.mark.asyncio
    async def test_filter_excludes_direct_and_previous_sessions(self) -> None:
        """Direct sessions and previous Claude sessions are filtered too."""
        cs = _make_claude_session(session_id="session-1111-aaaa", git_branch="main")
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=lambda _path: [cs],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                assert "ds-test-proj--direct-1" in app._session_map
                await pilot.press("slash")
                for key in "nomatch":
                    await pilot.press(key)
                await pilot.pause()
                assert app._session_map == {}

    @pytest.mark.asyncio
    async def test_filter_excludes_non_matching_active_worktree(
        self, tmp_path: Path
    ) -> None:
        active = tmp_path / "20260309-alpha"
        inactive = tmp_path / "20260308-beta"
        with _patch_git_info(
            sessions=["test-proj/20260309-alpha"],
            worktrees=[active, inactive],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "beta":
                    await pilot.press(key)
                await pilot.pause()
                assert set(app._session_map) == {"wt-20260308-beta"}

    @pytest.mark.asyncio
    async def test_search_survives_a_trip_through_session_actions(
        self, tmp_path: Path
    ) -> None:
        """Coming back to a re-rendered home keeps the filter and the focus."""
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "logging":
                    await pilot.press(key)
                await pilot.press("enter")
                await pilot.press("enter")
                await pilot.pause()
                await app._show_home()
                await pilot.pause()
                assert app._search_query == "logging"
                assert app.query_one("#home-search", Input).display
                assert app.focused is not None
                assert app.focused.id == "home-search"
                assert set(app._session_map) == {"wt-20260308-add-logging"}

    @pytest.mark.asyncio
    async def test_search_helpers_are_noops_off_home(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # into the create form
                await pilot.pause()
                assert not app.query("#home-list")
                await app.action_search()
                assert not app._searching
                await app._refresh_home_list()  # no list to refresh

    @pytest.mark.asyncio
    async def test_selecting_filtered_session_opens_actions(
        self, tmp_path: Path
    ) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                for key in "logging":
                    await pilot.press(key)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert app._selected_session is not None
                assert app._selected_session.name == "20260308-add-logging"


class TestSessionAppHome:
    @pytest.mark.asyncio
    async def test_home_shows_create_option(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                assert app.query_one("#action-create")
                assert app.query_one("#action-direct")
                assert app.query_one("#home-list")

    @pytest.mark.asyncio
    async def test_home_shows_existing_worktrees(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test():
                assert len(app._session_map) == 2

    @pytest.mark.asyncio
    async def test_home_shows_active_indicator(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(
            sessions=["test-proj/20260309-fix-tests"],
            worktrees=[wt1],
        ):
            app = SessionApp()
            async with app.run_test():
                assert "test-proj/20260309-fix-tests" in app._active_sessions

    @pytest.mark.asyncio
    async def test_home_shows_inactive_with_black_circle(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        with _patch_git_info(worktrees=[wt1]):
            app = SessionApp()
            async with app.run_test():
                session = app._session_map["wt-20260309-fix-tests"]
                assert not session.is_active

    @pytest.mark.asyncio
    async def test_home_no_worktrees(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                assert len(app._session_map) == 0

    @pytest.mark.asyncio
    async def test_subtitle_shows_project(self) -> None:
        with _patch_git_info(project="my-project"):
            app = SessionApp()
            async with app.run_test():
                assert app.sub_title == "my-project"

    @pytest.mark.asyncio
    async def test_quit_binding(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("q")
                assert app._launch_target is None

    @pytest.mark.asyncio
    async def test_escape_on_home_exits(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("escape")
                assert app._launch_target is None

    @pytest.mark.asyncio
    async def test_direct_sessions_shown_in_active(self, tmp_path: Path) -> None:
        """Direct sessions (tmux sessions without matching worktrees) appear."""
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test():
                assert "ds-test-proj--direct-1" in app._session_map
                session = app._session_map["ds-test-proj--direct-1"]
                assert session.session_type == "direct"
                assert session.is_active


class TestHomeRefresh:
    """`r` re-runs discovery and rebuilds the home rows in place."""

    @pytest.mark.asyncio
    async def test_refresh_picks_up_a_new_session(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                assert "ds-test-proj--direct-1" not in app._session_map
                with patch(
                    "fujimoto.cli.list_project_sessions",
                    return_value=["test-proj/direct-1"],
                ):
                    await pilot.press("r")
                    await pilot.pause()
                assert "ds-test-proj--direct-1" in app._session_map

    @pytest.mark.asyncio
    async def test_refresh_drops_a_gone_session(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                assert "ds-test-proj--direct-1" in app._session_map
                with patch("fujimoto.cli.list_project_sessions", return_value=[]):
                    await pilot.press("r")
                    await pilot.pause()
                assert "ds-test-proj--direct-1" not in app._session_map

    @pytest.mark.asyncio
    async def test_refresh_keeps_the_highlighted_row(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                target = "wt-20260308-add-logging"
                for index, item in enumerate(home_list.children):
                    if item.id == target:
                        home_list.index = index
                await pilot.pause()
                await pilot.press("r")
                await pilot.pause()
                highlighted = app.query_one("#home-list", ListView).highlighted_child
                assert highlighted is not None
                assert highlighted.id == target

    @pytest.mark.asyncio
    async def test_refresh_keeps_the_filter(self, tmp_path: Path) -> None:
        wt1 = tmp_path / "20260309-fix-tests"
        wt2 = tmp_path / "20260308-add-logging"
        with _patch_git_info(worktrees=[wt1, wt2]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.pause()
                app.query_one("#home-search", Input).value = "logging"
                await pilot.pause()
                assert app._search_query == "logging"
                await app.action_refresh()
                await pilot.pause()
                assert app._search_query == "logging"
                assert "wt-20260308-add-logging" in app._session_map
                assert "wt-20260309-fix-tests" not in app._session_map

    @pytest.mark.asyncio
    async def test_refresh_is_a_noop_off_the_home_screen(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await app._show_session_search()
                await pilot.pause()
                with patch("fujimoto.cli.get_project_name") as get_name:
                    await app.action_refresh()
                    assert not get_name.called


class TestSessionAppDirectSession:
    @pytest.mark.asyncio
    async def test_launch_direct_session(self) -> None:
        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.get_next_direct_session_name",
                return_value="test-proj/direct-1",
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("down")  # Move to "New session in..."
                await pilot.press("enter")
                await pilot.pause()
                # Now shows a title form with default name
                assert len(app.query("#direct-title-input")) > 0
                # Submit with default value to launch
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[2] == "test-proj/direct-1"

    @pytest.mark.asyncio
    async def test_launch_direct_session_custom_name(self) -> None:
        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.get_next_direct_session_name",
                return_value="test-proj/direct-1",
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # Clear default and type custom name
                title_input = app.query_one("#direct-title-input", Input)
                title_input.value = ""
                await pilot.press(*"my task")
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[2] == "test-proj/my-task"


class TestDirectSessionLocation:
    """A `direct-N` row reports where tmux says it runs, not the repo root."""

    @staticmethod
    def _worktree_session(wt: Path) -> ClaudeSession:
        return ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.WORKING,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.TOOL_USE,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title="In a worktree",
            first_prompt="Fix the bug",
        )

    @pytest.mark.asyncio
    async def test_direct_row_in_worktree_uses_worktree_path_and_state(
        self, tmp_path: Path
    ) -> None:
        wt_root = tmp_path / "worktrees" / "test-proj"
        wt = wt_root / "20260309-test"
        cs = self._worktree_session(wt)
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            worktrees=[wt],
            worktree_root=wt_root,
            session_paths={"test-proj/direct-1": wt},
            claude_sessions_fn=lambda path: [cs] if path == wt else [],
        ):
            app = SessionApp()
            async with app.run_test():
                info = app._session_map["ds-test-proj--direct-1"]
                assert info.path == wt
                # State comes from the worktree's transcript, not the root's
                assert info.claude_session_id == cs.session_id
                assert info.claude_state == SessionState.WORKING
                label = app.query_one("#ds-test-proj--direct-1").query_one(Label)
                assert "test-proj/20260309-test" in str(label.render())

    @pytest.mark.asyncio
    async def test_direct_row_falls_back_to_session_record(
        self, tmp_path: Path
    ) -> None:
        """tmux can't answer (session gone mid-render) → use the record."""
        recorded = tmp_path / "somewhere"
        record = session_state.SessionRecord(cwd=str(recorded))
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            open_sessions={"test-proj/direct-1": record},
        ):
            app = SessionApp()
            async with app.run_test():
                assert app._session_map["ds-test-proj--direct-1"].path == recorded

    @pytest.mark.asyncio
    async def test_direct_row_ignores_relative_record_cwd(self) -> None:
        """A relative `cwd` is the old bug's own output — not a location."""
        record = session_state.SessionRecord(cwd=".")
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            open_sessions={"test-proj/direct-1": record},
        ):
            app = SessionApp()
            async with app.run_test():
                info = app._session_map["ds-test-proj--direct-1"]
                assert info.path == Path("/fake/repo")

    @pytest.mark.asyncio
    async def test_direct_session_cwd_is_memoized(self) -> None:
        with (
            _patch_git_info(sessions=["test-proj/direct-1"]),
            patch("fujimoto.cli.get_session_path", return_value=None) as get_path,
        ):
            app = SessionApp()
            async with app.run_test():
                calls = get_path.call_count
                app._direct_session_cwd("test-proj/direct-1")
                assert get_path.call_count == calls


class TestResumeTarget:
    """Resuming names the tmux session after the transcript's directory."""

    @staticmethod
    def _session(wt: Path) -> ClaudeSession:
        return ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title="In a worktree",
            first_prompt="Fix the bug",
        )

    @pytest.mark.asyncio
    async def test_transcript_in_idle_worktree_reuses_worktree_name(
        self, tmp_path: Path
    ) -> None:
        wt_root = tmp_path / "worktrees" / "test-proj"
        wt = wt_root / "20260309-test"
        with _patch_git_info(worktrees=[wt], worktree_root=wt_root):
            app = SessionApp()
            async with app.run_test():
                assert app._resume_target("test-proj", wt) == (
                    "test-proj/20260309-test",
                    "worktree",
                )

    @pytest.mark.asyncio
    async def test_transcript_in_busy_worktree_falls_back_to_direct(
        self, tmp_path: Path
    ) -> None:
        wt_root = tmp_path / "worktrees" / "test-proj"
        wt = wt_root / "20260309-test"
        with (
            _patch_git_info(
                worktrees=[wt],
                worktree_root=wt_root,
                sessions=["test-proj/20260309-test"],
            ),
            patch(
                "fujimoto.cli.get_next_direct_session_name",
                return_value="test-proj/direct-1",
            ),
        ):
            app = SessionApp()
            async with app.run_test():
                assert app._resume_target("test-proj", wt) == (
                    "test-proj/direct-1",
                    "direct",
                )

    @pytest.mark.asyncio
    async def test_resuming_a_claude_row_lands_in_its_worktree(
        self, tmp_path: Path
    ) -> None:
        """The regression: a transcript row whose cwd is a worktree.

        It used to launch as `direct-N`, which detached the session from the
        worktree and let the worktree be resumed again alongside it.
        """
        wt_root = tmp_path / "worktrees" / "test-proj"
        wt = wt_root / "20260309-test"
        cs = self._session(wt)
        with _patch_git_info(
            worktrees=[wt],
            worktree_root=wt_root,
            # Reached from the project root's listing, as a search hit is
            claude_sessions_fn=lambda path: [cs] if path == Path("/fake/repo") else [],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == f"cs-{cs.session_id}":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # "Resume" is the first action for a claude row
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[2] == "test-proj/20260309-test"
                assert app._launch_target[3] == "worktree"
                assert app._launch_target[1] == wt


class TestSessionAppAdhocSession:
    @pytest.mark.asyncio
    async def test_launch_adhoc_session(self) -> None:
        with (
            _patch_git_info(),
            patch("fujimoto.cli.list_all_sessions", return_value=[]),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to "Ad hoc session" (3rd item, index 2)
                await pilot.press("down", "down")
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                target = app._launch_target
                assert target.project == "adhoc"
                assert target.session_type == "adhoc"
                assert target.tmux_name == "adhoc-1"
                assert target.resume_session_id is None
                assert target.forked_from_session_id is None
                assert target.working_dir.exists()

    @pytest.mark.asyncio
    async def test_launch_adhoc_increments_name(self) -> None:
        with (
            _patch_git_info(),
            patch("fujimoto.cli.list_all_sessions", return_value=["adhoc-1"]),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("down", "down")
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[2] == "adhoc-2"

    @pytest.mark.asyncio
    async def test_home_shows_adhoc_option(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                assert app.query_one("#action-adhoc")


class TestSessionAppSessionActions:
    @pytest.mark.asyncio
    async def test_shows_submenu_for_active_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate past create options and separator to active worktree
                for _ in range(10):
                    await pilot.press("down")
                # Find the worktree item and select it
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#session-actions")) > 0

    @pytest.mark.asyncio
    async def test_connect_exits_with_target(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # "Connect" is the first option
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None

    @pytest.mark.asyncio
    async def test_terminate_kills_session(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]),
            patch("fujimoto.cli.kill_session") as mock_kill,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-terminate":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                mock_kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_inactive_worktree_shows_launch(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#session-actions")) > 0
                # No previous sessions → Resume hidden, Launch is first
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None

    @pytest.mark.asyncio
    async def test_cancel_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Navigate to cancel (last item)
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_resume_picker_shown_for_active_worktree(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        fake_session = ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title=None,
            first_prompt=None,
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _path: [fake_session],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-resume-picker" in action_ids

    @pytest.mark.asyncio
    async def test_resume_picker_shown_for_inactive_worktree(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        fake_session = ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title=None,
            first_prompt=None,
        )
        with _patch_git_info(
            worktrees=[wt],
            claude_sessions_fn=lambda _path: [fake_session],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-resume-picker" in action_ids

    @pytest.mark.asyncio
    async def test_resume_picker_hidden_when_no_previous_sessions(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-resume-picker" not in action_ids

    @pytest.mark.asyncio
    async def test_resume_picker_sets_launch_target(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        fake_session = ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title="My test session",
            first_prompt="Add resume session picker to worktree menu",
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _path: [fake_session],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # With a single previous session, the picker is skipped and the
                # session launches directly.
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-resume-picker":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[4] == fake_session.session_id
                # Active worktree → direct-N name, not the worktree name
                assert app._launch_target[2].startswith("test-proj/direct-")
                # Working dir comes from cs.cwd, not session.path
                assert app._launch_target[1] == fake_session.cwd

    @pytest.mark.asyncio
    async def test_resume_picker_inactive_worktree_uses_worktree_session_name(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        fake_session = ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title="My test session",
            first_prompt="Fix the bug",
        )
        # No active sessions → worktree is inactive
        with _patch_git_info(
            worktrees=[wt],
            claude_sessions_fn=lambda _path: [fake_session],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # "Resume previous session" is the first option for inactive worktrees.
                # Single previous session → auto-launch, no picker.
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[4] == fake_session.session_id
                # Inactive worktree → reuse the worktree's session name
                assert app._launch_target[2] == "test-proj/20260309-test"
                # Working dir is cs.cwd
                assert app._launch_target[1] == fake_session.cwd

    @pytest.mark.asyncio
    async def test_resume_picker_cancel_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        fake_session = ClaudeSession(
            jsonl_path=wt / "session.jsonl",
            session_id="abc12345-def6-7890-abcd-ef1234567890",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc),
            title=None,
            first_prompt=None,
        )
        fake_session_2 = ClaudeSession(
            jsonl_path=wt / "session-2.jsonl",
            session_id="bcd23456-ef78-9012-bcde-f23456789012",
            state=SessionState.IDLE,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=StopReason.END_TURN,
            cwd=wt,
            git_branch="worktree/20260309-test",
            last_activity=datetime(2026, 3, 8, 12, 0, 0, tzinfo=timezone.utc),
            title=None,
            first_prompt=None,
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _path: [fake_session, fake_session_2],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-resume-picker":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Two sessions → picker shown
                assert len(app.query("#resume-picker")) > 0
                # Navigate to cancel (past both sessions) and press enter
                picker = app.query_one("#resume-picker", ListView)
                for i, item in enumerate(picker.children):
                    if item.id == "rp-cancel":
                        picker.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_direct_session_has_no_finish(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Should show session-actions but no "Finish" option
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-finish" not in action_ids
                assert "sa-connect" in action_ids
                assert "sa-terminate" in action_ids


class TestSessionAppOpenTerminal:
    @pytest.mark.asyncio
    async def test_open_terminal_in_session_actions(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-terminal" in action_ids

    @pytest.mark.asyncio
    async def test_open_terminal_shows_mode_submenu(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-terminal":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                mode_list = app.query_one("#terminal-mode-list", ListView)
                ids = [c.id for c in mode_list.children]
                assert ids == ["term-this", "term-window", "term-cancel"]

    async def _navigate_to_terminal_mode(self, pilot: object, app: SessionApp) -> None:
        home_list = app.query_one("#home-list", ListView)
        for i, item in enumerate(home_list.children):
            if item.id == "ds-test-proj--direct-1":
                home_list.index = i
                break
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        actions = app.query_one("#session-actions", ListView)
        for i, item in enumerate(actions.children):
            if item.id == "sa-terminal":
                actions.index = i
                break
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_term_window_calls_open_terminal_and_returns_to_actions(
        self,
    ) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._navigate_to_terminal_mode(pilot, app)
                mode_list = app.query_one("#terminal-mode-list", ListView)
                for i, item in enumerate(mode_list.children):
                    if item.id == "term-window":
                        mode_list.index = i
                        break
                with patch("fujimoto.cli.open_terminal") as mock_open:
                    await pilot.press("enter")
                    await pilot.pause()
                    mock_open.assert_called_once()
                    # Returned to the session actions menu, not home.
                    assert len(app.query("#session-actions")) > 0
                    assert len(app.query("#terminal-mode-list")) == 0

    @pytest.mark.asyncio
    async def test_term_window_error_shows_error(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._navigate_to_terminal_mode(pilot, app)
                mode_list = app.query_one("#terminal-mode-list", ListView)
                for i, item in enumerate(mode_list.children):
                    if item.id == "term-window":
                        mode_list.index = i
                        break
                with patch(
                    "fujimoto.cli.open_terminal",
                    side_effect=OSError("osascript not found"),
                ):
                    await pilot.press("enter")
                    await pilot.pause()
                    assert len(app.query("#terminal-mode-list")) == 0
                    assert len(app.query("#home-list")) == 0

    @pytest.mark.asyncio
    async def test_term_this_runs_shell_subprocess_and_returns_to_actions(
        self,
    ) -> None:
        import contextlib

        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._navigate_to_terminal_mode(pilot, app)
                mode_list = app.query_one("#terminal-mode-list", ListView)
                for i, item in enumerate(mode_list.children):
                    if item.id == "term-this":
                        mode_list.index = i
                        break
                with (
                    patch("fujimoto.cli.subprocess.run", return_value=None) as mock_run,
                    patch.object(
                        SessionApp, "suspend", return_value=contextlib.nullcontext()
                    ),
                    patch.dict("os.environ", {"SHELL": "/bin/zsh"}),
                ):
                    await pilot.press("enter")
                    await pilot.pause()
                    mock_run.assert_called_once()
                    args, kwargs = mock_run.call_args
                    assert args[0] == ["/bin/zsh"]
                    assert kwargs["cwd"] is not None
                assert len(app.query("#session-actions")) > 0
                assert app._launch_target is None

    @pytest.mark.asyncio
    async def test_term_this_shell_error_shows_error(self) -> None:
        import contextlib

        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._navigate_to_terminal_mode(pilot, app)
                mode_list = app.query_one("#terminal-mode-list", ListView)
                for i, item in enumerate(mode_list.children):
                    if item.id == "term-this":
                        mode_list.index = i
                        break
                with (
                    patch(
                        "fujimoto.cli.subprocess.run",
                        side_effect=OSError("shell missing"),
                    ),
                    patch.object(
                        SessionApp, "suspend", return_value=contextlib.nullcontext()
                    ),
                ):
                    await pilot.press("enter")
                    await pilot.pause()
                    assert len(app.query("#session-actions")) == 0
                    assert len(app.query("#terminal-mode-list")) == 0

    @pytest.mark.asyncio
    async def test_term_cancel_returns_to_session_actions(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._navigate_to_terminal_mode(pilot, app)
                mode_list = app.query_one("#terminal-mode-list", ListView)
                for i, item in enumerate(mode_list.children):
                    if item.id == "term-cancel":
                        mode_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#session-actions")) > 0
                assert len(app.query("#terminal-mode-list")) == 0


class TestSessionAppOpenVscode:
    @pytest.mark.asyncio
    async def test_open_vscode_in_session_actions(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                action_ids = [child.id for child in actions.children]
                assert "sa-vscode" in action_ids

    @pytest.mark.asyncio
    async def test_open_vscode_calls_open_vscode(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-vscode":
                        actions.index = i
                        break
                with patch("fujimoto.cli.open_vscode") as mock_open:
                    await pilot.press("enter")
                    await pilot.pause()
                    mock_open.assert_called_once()
                    # Should stay on session actions menu
                    assert len(app.query("#session-actions")) > 0

    @pytest.mark.asyncio
    async def test_open_vscode_error_shows_error(self) -> None:
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "ds-test-proj--direct-1":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-vscode":
                        actions.index = i
                        break
                with patch(
                    "fujimoto.cli.open_vscode",
                    side_effect=OSError("'code' CLI not found"),
                ):
                    await pilot.press("enter")
                    await pilot.pause()
                    # Error view: no session-actions, no home-list
                    assert len(app.query("#session-actions")) == 0
                    assert len(app.query("#home-list")) == 0


class TestSessionAppRename:
    @pytest.mark.asyncio
    async def test_rename_shows_input(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Navigate to Rename option
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-rename":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#rename-input")) > 0

    @pytest.mark.asyncio
    async def test_rename_calls_tmux_rename(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]),
            patch("fujimoto.cli.rename_session") as mock_rename,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-rename":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Clear and type new name
                rename_input = app.query_one("#rename-input", Input)
                rename_input.value = "new-name"
                await pilot.press("enter")
                await pilot.pause()
                mock_rename.assert_called_once_with(
                    "test-proj/20260309-test", "test-proj/new-name"
                )
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_rename_same_name_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]),
            patch("fujimoto.cli.rename_session") as mock_rename,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-rename":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Submit with same name (default value)
                await pilot.press("enter")
                await pilot.pause()
                mock_rename.assert_not_called()
                assert len(app.query("#home-list")) > 0


class TestSessionAppFinishFlow:
    @pytest.mark.asyncio
    async def test_finish_shows_options_for_unmerged(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=["abc fix"]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Navigate to "Finish"
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#finish-list")) > 0
                finish_list = app.query_one("#finish-list", ListView)
                action_ids = [child.id for child in finish_list.children]
                assert "finish-pr" in action_ids
                assert "finish-cherry-pick" in action_ids
                assert "finish-discard" in action_ids

    @pytest.mark.asyncio
    async def test_finish_shows_delete_for_merged(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=[]),
            patch("fujimoto.cli.is_branch_merged", return_value=True),
            patch("fujimoto.cli.has_remote_branch", return_value=True),
            patch("fujimoto.cli.read_session_meta", return_value={}),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                finish_list = app.query_one("#finish-list", ListView)
                action_ids = [child.id for child in finish_list.children]
                assert "finish-delete" in action_ids
                assert "finish-delete-remote" in action_ids

    @pytest.mark.asyncio
    async def test_discard_shows_confirmation(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=["abc fix"]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Select "Discard & Delete"
                finish_list = app.query_one("#finish-list", ListView)
                for i, item in enumerate(finish_list.children):
                    if item.id == "finish-discard":
                        finish_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#confirm-list")) > 0

    @pytest.mark.asyncio
    async def test_confirm_delete_removes_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=[]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
            patch("fujimoto.cli.remove_worktree") as mock_remove,
            patch("fujimoto.cli.delete_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                finish_list = app.query_one("#finish-list", ListView)
                for i, item in enumerate(finish_list.children):
                    if item.id == "finish-discard":
                        finish_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Confirm delete
                await pilot.press("enter")
                await pilot.pause()
                mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_cancel_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=[]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                finish_list = app.query_one("#finish-list", ListView)
                for i, item in enumerate(finish_list.children):
                    if item.id == "finish-discard":
                        finish_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Cancel
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_push_and_pr(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=["abc fix"]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
            patch("fujimoto.cli.push_branch") as mock_push,
            patch("fujimoto.cli.create_session_with_command") as mock_pr_session,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Select "Push & Create PR"
                await pilot.press("enter")
                await pilot.pause()
                mock_push.assert_called_once()
                mock_pr_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_cherry_pick_and_delete(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=["abc fix"]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch(
                "fujimoto.cli.read_session_meta",
                return_value={"base_branch": "main"},
            ),
            patch("fujimoto.cli.cherry_pick_branch") as mock_cherry,
            patch("fujimoto.cli.remove_worktree") as mock_remove,
            patch("fujimoto.cli.delete_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Select "Cherry-pick to main"
                finish_list = app.query_one("#finish-list", ListView)
                for i, item in enumerate(finish_list.children):
                    if item.id == "finish-cherry-pick":
                        finish_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                mock_cherry.assert_called_once()
                mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_finish_cancel_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli.get_unpushed_commits", return_value=[]),
            patch("fujimoto.cli.is_branch_merged", return_value=False),
            patch("fujimoto.cli.has_remote_branch", return_value=False),
            patch("fujimoto.cli.read_session_meta", return_value={}),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, item in enumerate(home_list.children):
                    if item.id == "wt-20260309-test":
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                for i, item in enumerate(actions.children):
                    if item.id == "sa-finish":
                        actions.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Navigate to Cancel
                finish_list = app.query_one("#finish-list", ListView)
                for i, item in enumerate(finish_list.children):
                    if item.id == "finish-cancel":
                        finish_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#home-list")) > 0


class TestSessionAppCreateFlow:
    @pytest.mark.asyncio
    async def test_navigate_to_create_form(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Create new"
                await pilot.pause()
                assert len(app.query("#title-input")) > 0

    @pytest.mark.asyncio
    async def test_create_form_empty_title_stays(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Create new"
                await pilot.pause()
                await pilot.press("enter")  # Submit empty title
                await pilot.pause()
                # Should still be on create form
                assert len(app.query("#title-input")) > 0

    @pytest.mark.asyncio
    async def test_escape_from_create_returns_home(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Create new"
                await pilot.pause()
                await pilot.press("escape")  # Go back
                await pilot.pause()
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_create_always_shows_branch_select(self) -> None:
        with _patch_git_info(current="main", default="main"):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Create new"
                await pilot.pause()
                await pilot.press(*"test-title")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#branch-list")) > 0

    @pytest.mark.asyncio
    async def test_branch_select_always_shows_three_options(self) -> None:
        with _patch_git_info(current="feat/test", default="main"):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Create new"
                await pilot.pause()
                await pilot.press(*"test-title")
                await pilot.press("enter")
                await pilot.pause()
                branch_list = app.query_one("#branch-list", ListView)
                # Default, current, another = 3 items
                assert len(branch_list) == 3

    @pytest.mark.asyncio
    async def test_branch_select_shows_current_even_when_on_default(self) -> None:
        with _patch_git_info(current="main", default="main"):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"test-title")
                await pilot.press("enter")
                await pilot.pause()
                branch_list = app.query_one("#branch-list", ListView)
                # Default, current, another = always 3 items
                assert len(branch_list) == 3

    @pytest.mark.asyncio
    async def test_branch_select_default_with_fetch(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
            patch("fujimoto.cli.fetch_branch") as mock_fetch,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                await pilot.press("enter")  # Select default branch (first option)
                await pilot.pause()
                mock_fetch.assert_called_once_with("main", cwd=None)
                mock_create.assert_called_once()
                # Worktree starts from origin/main, not local main
                assert mock_create.call_args[0][1] == "origin/main"
                assert app._base_branch == "main"
                assert app._start_point == "origin/main"

    @pytest.mark.asyncio
    async def test_branch_select_default_fetch_failure_continues(
        self, tmp_path: Path
    ) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
            patch(
                "fujimoto.cli.fetch_branch",
                side_effect=GitError("no remote"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                # Should still create despite fetch failure, using local branch
                mock_create.assert_called_once()
                assert mock_create.call_args[0][1] == "main"
                assert app._start_point == ""

    @pytest.mark.asyncio
    async def test_branch_select_current(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                await pilot.press("down")  # Move to current branch (second option)
                await pilot.press("enter")
                await pilot.pause()
                mock_create.assert_called_once()
                assert app._base_branch == "feat/test"

    @pytest.mark.asyncio
    async def test_branch_select_other_shows_picker(self) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.list_branches",
                return_value=[
                    "develop",
                    "feat/test",
                    "main",
                    "worktree/20260309-old",
                ],
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                # Move to "Another branch…" (3rd option)
                await pilot.press("down", "down")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#branch-picker-list")) > 0
                # worktree/ branches should be excluded
                branch_list = app.query_one("#branch-picker-list", ListView)
                assert len(branch_list) == 3

    @pytest.mark.asyncio
    async def test_branch_picker_select(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.list_branches",
                return_value=["develop", "feat/test", "main"],
            ),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                await pilot.press("down", "down")  # Another branch
                await pilot.press("enter")
                await pilot.pause()
                # Select "develop" (first in list)
                branch_list = app.query_one("#branch-picker-list", ListView)
                branch_list.index = 0
                await pilot.press("enter")
                await pilot.pause()
                mock_create.assert_called_once()
                assert app._base_branch == "develop"

    @pytest.mark.asyncio
    async def test_branch_picker_filter(self) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.list_branches",
                return_value=["develop", "feat/test", "main", "release/1.0"],
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                await pilot.press("down", "down")  # Another branch
                await pilot.press("enter")
                await pilot.pause()
                # Type filter
                await pilot.press(*"dev")
                await pilot.pause()
                branch_list = app.query_one("#branch-picker-list", ListView)
                assert len(branch_list) == 1

    @pytest.mark.asyncio
    async def test_branch_picker_submit_filter(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="feat/test", default="main"),
            patch(
                "fujimoto.cli.list_branches",
                return_value=["develop", "feat/test", "main"],
            ),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("down", "down")  # Another branch
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"dev")
                await pilot.press("enter")  # Submit filter selects highlighted
                await pilot.pause()
                mock_create.assert_called_once()
                assert app._base_branch == "develop"

    @pytest.mark.asyncio
    async def test_create_stores_session_meta(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="main", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree"),
            patch("fujimoto.cli.store_session_meta") as mock_meta,
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                mock_meta.assert_called_once_with(
                    tmp_path / "new-wt",
                    "main",
                    source_root=Path("/fake/repo"),
                    forked_from_session_id=None,
                    forked_from_worktree=None,
                )

    @pytest.mark.asyncio
    async def test_create_defers_config_to_launch(self, tmp_path: Path) -> None:
        # Project config is applied in main() before launch, not in the create
        # flow — so creating a worktree must not call apply_project_config.
        with (
            _patch_git_info(current="main", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch("fujimoto.cli.create_worktree"),
            patch("fujimoto.cli.store_session_meta"),
            patch("fujimoto.cli.fetch_branch"),
            patch("fujimoto.cli.apply_project_config") as mock_apply,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                mock_apply.assert_not_called()
                assert app._launch_target is not None
                assert app._launch_target[3] == "worktree"


def _fake_claude_session(
    wt: Path,
    session_id: str = "abc12345-def6-7890-abcd-ef1234567890",
    title: str = "Parent session",
    minute: int = 0,
) -> ClaudeSession:
    return ClaudeSession(
        jsonl_path=wt / f"{session_id}.jsonl",
        session_id=session_id,
        state=SessionState.IDLE,
        last_entry_type=EntryType.ASSISTANT,
        stop_reason=StopReason.END_TURN,
        cwd=wt,
        git_branch=f"worktree/{wt.name}",
        last_activity=datetime(2026, 3, 9, 12, minute, 0, tzinfo=timezone.utc),
        title=title,
        first_prompt="Do the thing",
    )


class TestBuildForkSystemPrompt:
    def test_names_both_worktrees_and_the_caveat(self) -> None:
        prompt = _build_fork_system_prompt(
            "test-proj",
            Path("/wt/20260309-fork"),
            Path("/wt/20260101-parent"),
            "worktree/20260101-parent",
        )
        assert "/wt/20260101-parent" in prompt
        assert "/wt/20260309-fork" in prompt
        assert "worktree/20260309-fork" in prompt
        assert "worktree/20260101-parent" in prompt
        assert "uncommitted" in prompt

    def test_survives_unknown_parent(self) -> None:
        prompt = _build_fork_system_prompt("test-proj", Path("/wt/fork"), None, "main")
        assert "different git worktree" in prompt
        assert "None" not in prompt


class TestForkMenuItem:
    """Fork needs a conversation to inherit and a branch to base the tree on."""

    @staticmethod
    def _action_ids(app: SessionApp) -> list[str]:
        return [i.id for i in app.query_one("#session-actions", ListView).children]

    async def _open_actions(self, app: SessionApp, pilot, item_id: str) -> None:
        home_list = app.query_one("#home-list", ListView)
        for i, item in enumerate(home_list.children):
            if item.id == item_id:
                home_list.index = i
                break
        await pilot.press("enter")
        await pilot.pause()

    @pytest.mark.asyncio
    async def test_second_item_for_active_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                ids = self._action_ids(app)
                assert ids[0] == "sa-connect"
                assert ids[1] == "sa-fork"

    @pytest.mark.asyncio
    async def test_second_item_for_inactive_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                ids = self._action_ids(app)
                assert ids[0] == "sa-resume-picker"
                assert ids[1] == "sa-fork"

    @pytest.mark.asyncio
    async def test_offered_for_direct_session(self, tmp_path: Path) -> None:
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=lambda _p: [_fake_claude_session(tmp_path)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "ds-test-proj--direct-1")
                assert "sa-fork" in self._action_ids(app)

    @pytest.mark.asyncio
    async def test_absent_without_previous_sessions(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                assert "sa-fork" not in self._action_ids(app)

    @pytest.mark.asyncio
    async def test_absent_for_claude_session_row(self, tmp_path: Path) -> None:
        cs = _fake_claude_session(tmp_path)
        with _patch_git_info(claude_sessions_fn=lambda _p: [cs]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, f"cs-{cs.session_id}")
                assert "sa-fork" not in self._action_ids(app)


class TestForkFlow:
    async def _start_fork(self, app: SessionApp, pilot, item_id: str) -> None:
        home_list = app.query_one("#home-list", ListView)
        for i, item in enumerate(home_list.children):
            if item.id == item_id:
                home_list.index = i
                break
        await pilot.press("enter")
        await pilot.pause()
        actions = app.query_one("#session-actions", ListView)
        for i, item in enumerate(actions.children):
            if item.id == "sa-fork":
                actions.index = i
                break
        await pilot.press("enter")
        await pilot.pause()

    @staticmethod
    async def _pick(app: SessionApp, pilot, list_id: str, item_id: str) -> None:
        lv = app.query_one(list_id, ListView)
        for i, item in enumerate(lv.children):
            if item.id == item_id:
                lv.index = i
                break
        await pilot.press("enter")
        await pilot.pause()

    @pytest.mark.asyncio
    async def test_forks_off_parent_branch(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        parent = _fake_claude_session(wt)
        new_wt = tmp_path / "20260309-fork"
        with (
            _patch_git_info(
                sessions=["test-proj/20260309-test"],
                worktrees=[wt],
                claude_sessions_fn=lambda _p: [parent],
            ),
            patch("fujimoto.cli.build_worktree_path", return_value=new_wt),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta") as mock_meta,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                assert len(app.query("#fork-title-input")) > 0
                await pilot.press(*"fork")
                await pilot.press("enter")
                await pilot.pause()
                # Parent branch is the default (first) option.
                assert len(app.query("#fork-branch-list")) > 0
                await self._pick(app, pilot, "#fork-branch-list", "fork-branch-parent")

            mock_create.assert_called_once_with(
                new_wt,
                "worktree/20260309-test",
                "worktree/20260309-fork",
                cwd=None,
            )
            # The worktree dir the TUI listed, not the fake session's cwd.
            parent_path = app._fork_parent_path
            assert parent_path is not None
            assert parent_path.name == "20260309-test"
            mock_meta.assert_called_once_with(
                new_wt,
                "worktree/20260309-test",
                source_root=Path("/fake/repo"),
                forked_from_session_id=parent.session_id,
                forked_from_worktree=parent_path,
            )
            target = app._launch_target
            assert target is not None
            assert target.working_dir == new_wt
            assert target.session_type == "worktree"
            assert target.forked_from_session_id == parent.session_id
            assert target.forked_from_worktree == parent_path
            # A fork is not a resume-in-place.
            assert target.resume_session_id is None

    @pytest.mark.asyncio
    async def test_forks_off_parents_base_branch(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        parent = _fake_claude_session(wt)
        new_wt = tmp_path / "20260309-fork"
        with (
            _patch_git_info(
                sessions=["test-proj/20260309-test"],
                worktrees=[wt],
                claude_sessions_fn=lambda _p: [parent],
            ),
            patch("fujimoto.cli.build_worktree_path", return_value=new_wt),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
            patch(
                "fujimoto.cli.read_session_meta",
                return_value={"base_branch": "develop"},
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await pilot.press(*"fork")
                await pilot.press("enter")
                await pilot.pause()
                await self._pick(app, pilot, "#fork-branch-list", "fork-branch-base")

            assert mock_create.call_args.args[1] == "develop"

    @pytest.mark.asyncio
    async def test_picker_shown_for_multiple_sessions(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        older = _fake_claude_session(wt, session_id="older-1", title="Older", minute=0)
        newer = _fake_claude_session(wt, session_id="newer-2", title="Newer", minute=5)
        new_wt = tmp_path / "20260309-fork"
        with (
            _patch_git_info(
                sessions=["test-proj/20260309-test"],
                worktrees=[wt],
                claude_sessions_fn=lambda _p: [newer, older],
            ),
            patch("fujimoto.cli.build_worktree_path", return_value=new_wt),
            patch("fujimoto.cli.create_worktree"),
            patch("fujimoto.cli.store_session_meta"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await pilot.press(*"fork")
                await pilot.press("enter")
                await pilot.pause()
                await self._pick(app, pilot, "#fork-branch-list", "fork-branch-parent")
                # Two candidates → pick which conversation to fork.
                assert len(app.query("#fork-picker")) > 0
                await self._pick(app, pilot, "#fork-picker", "fp-1")

            target = app._launch_target
            assert target is not None
            assert target.forked_from_session_id == "older-1"

    @pytest.mark.asyncio
    async def test_picker_cancel_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        sessions = [
            _fake_claude_session(wt, session_id="a-1", minute=1),
            _fake_claude_session(wt, session_id="b-2", minute=2),
        ]
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: sessions,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await pilot.press(*"fork")
                await pilot.press("enter")
                await pilot.pause()
                await self._pick(app, pilot, "#fork-branch-list", "fork-branch-parent")
                await self._pick(app, pilot, "#fork-picker", "fp-cancel")
                assert len(app.query("#home-list")) > 0
                assert app._launch_target is None

    @pytest.mark.asyncio
    async def test_empty_title_stays_on_form(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#fork-title-input")) > 0

    @pytest.mark.asyncio
    async def test_branch_picker_offers_worktree_branches(self, tmp_path: Path) -> None:
        # Plain create hides worktree/* as base candidates; a fork wants them.
        wt = tmp_path / "20260309-test"
        new_wt = tmp_path / "20260309-fork"
        with (
            _patch_git_info(
                sessions=["test-proj/20260309-test"],
                worktrees=[wt],
                claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
            ),
            patch(
                "fujimoto.cli.list_branches",
                return_value=["main", "worktree/20260309-test"],
            ),
            patch("fujimoto.cli.build_worktree_path", return_value=new_wt),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await pilot.press(*"fork")
                await pilot.press("enter")
                await pilot.pause()
                await self._pick(app, pilot, "#fork-branch-list", "fork-branch-other")
                assert set(app._branch_picker_names.values()) == {
                    "main",
                    "worktree/20260309-test",
                }
                await self._pick(app, pilot, "#branch-picker-list", "bp-1")

            assert mock_create.call_args.args[1] == "worktree/20260309-test"

    @pytest.mark.asyncio
    async def test_plain_create_clears_fork_state(self, tmp_path: Path) -> None:
        # A cancelled fork must not turn the next plain create into a fork.
        wt = tmp_path / "20260309-test"
        new_wt = tmp_path / "20260309-plain"
        with (
            _patch_git_info(
                sessions=["test-proj/20260309-test"],
                worktrees=[wt],
                claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
            ),
            patch("fujimoto.cli.build_worktree_path", return_value=new_wt),
            patch("fujimoto.cli.create_worktree"),
            patch("fujimoto.cli.store_session_meta") as mock_meta,
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._start_fork(app, pilot, "wt-20260309-test")
                await app._show_home()
                await pilot.pause()
                await self._pick(app, pilot, "#home-list", "action-create")
                await pilot.press(*"plain")
                await pilot.press("enter")
                await pilot.pause()
                await self._pick(app, pilot, "#branch-list", "branch-current")

            assert mock_meta.call_args.kwargs["forked_from_session_id"] is None
            assert app._launch_target is not None
            assert app._launch_target.forked_from_session_id is None


class TestPendingFork:
    """`Ctrl-A f` detaches and hands the fork to the TUI."""

    @pytest.mark.asyncio
    async def test_opens_fork_flow_for_the_requesting_worktree(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                # The worktree dir the TUI actually listed.
                listed = app._session_map["wt-20260309-test"].path
                app._pending_fork = listed
                await app._open_pending_fork()
                await pilot.pause()
                assert len(app.query("#fork-title-input")) > 0
                assert app._forking is True
                assert app._fork_parent_path == listed
                # Consumed, so a later detach doesn't re-trigger it.
                assert app._pending_fork is None

    @pytest.mark.asyncio
    async def test_no_pending_fork_stays_on_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await app._open_pending_fork()
                await pilot.pause()
                assert len(app.query("#home-list")) > 0
                assert len(app.query("#fork-title-input")) == 0

    @pytest.mark.asyncio
    async def test_unknown_path_stays_on_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                app._pending_fork = tmp_path / "somewhere-else"
                await app._open_pending_fork()
                await pilot.pause()
                assert len(app.query("#home-list")) > 0
                assert len(app.query("#fork-title-input")) == 0

    @pytest.mark.asyncio
    async def test_errors_when_no_conversation_to_fork(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                app._pending_fork = app._session_map["wt-20260309-test"].path
                await app._open_pending_fork()
                await pilot.pause()
                assert len(app.query("#fork-title-input")) == 0

    def test_constructor_accepts_pending_fork(self, tmp_path: Path) -> None:
        assert SessionApp()._pending_fork is None
        assert SessionApp(pending_fork=tmp_path)._pending_fork == tmp_path


class TestMainPendingFork:
    def _app(self, target: LaunchTarget | None) -> SessionApp:
        app = SessionApp.__new__(SessionApp)
        app._launch_target = target
        return app

    def test_pending_fork_passed_to_next_app(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        app1 = self._app(LaunchTarget("proj", wt, "proj/20260309-test", "worktree"))
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]) as mock_cls,
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value="fork") as mock_take,
        ):
            main()
        mock_take.assert_called_once_with("proj/20260309-test")
        # First app opens normally; the one after the detach opens on the fork.
        assert mock_cls.call_args_list[0].kwargs["pending_fork"] is None
        assert mock_cls.call_args_list[1].kwargs["pending_fork"] == wt

    def test_no_pending_action_leaves_next_app_clean(self, tmp_path: Path) -> None:
        app1 = self._app(LaunchTarget("proj", tmp_path / "wt", "proj/wt", "worktree"))
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]) as mock_cls,
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value=None),
        ):
            main()
        assert mock_cls.call_args_list[1].kwargs["pending_fork"] is None

    def test_derives_session_name_when_not_given(self, tmp_path: Path) -> None:
        # tmux_name is None for a freshly created worktree; the flag is keyed by
        # the name launch_claude_in_tmux would have derived.
        app1 = self._app(
            LaunchTarget("proj", tmp_path / "20260309-new", None, "worktree")
        )
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.session_name", return_value="proj/20260309-new"),
            patch("fujimoto.cli.take_pending_action", return_value=None) as mock_take,
        ):
            main()
        mock_take.assert_called_once_with("proj/20260309-new")


class TestForkHomeMarker:
    @pytest.mark.asyncio
    async def test_fork_worktree_gets_marker(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-fork"
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli._is_fork_worktree", return_value=True),
        ):
            app = SessionApp()
            async with app.run_test():
                item = app.query_one("#wt-20260309-fork", ListItem)
                label = item.query_one(Label)
                assert ICON_FORK in str(label.content)
                assert app._session_map["wt-20260309-fork"].is_fork is True

    @pytest.mark.asyncio
    async def test_plain_worktree_has_no_marker(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-plain"
        with _patch_git_info(worktrees=[wt]):
            app = SessionApp()
            async with app.run_test():
                item = app.query_one("#wt-20260309-plain", ListItem)
                label = item.query_one(Label)
                assert ICON_FORK not in str(label.content)
                assert app._session_map["wt-20260309-plain"].is_fork is False


class TestIsForkWorktree:
    def test_true_when_recorded(self, tmp_path: Path) -> None:
        from fujimoto.cli import _is_fork_worktree
        from fujimoto.config import store_session_meta as real_store

        real_store(tmp_path, "main", forked_from_session_id="abc")
        assert _is_fork_worktree(tmp_path) is True

    def test_false_for_plain_worktree(self, tmp_path: Path) -> None:
        from fujimoto.cli import _is_fork_worktree
        from fujimoto.config import store_session_meta as real_store

        real_store(tmp_path, "main")
        assert _is_fork_worktree(tmp_path) is False

    def test_false_without_meta(self, tmp_path: Path) -> None:
        from fujimoto.cli import _is_fork_worktree

        assert _is_fork_worktree(tmp_path) is False


class TestSessionAppConflict:
    @pytest.mark.asyncio
    async def test_shows_conflict_when_path_exists(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing-wt"
        existing.mkdir()
        with (
            _patch_git_info(current="main", default="main"),
            patch("fujimoto.cli.build_worktree_path", return_value=existing),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")  # Submit title
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                assert len(app.query("#conflict-list")) > 0

    @pytest.mark.asyncio
    async def test_conflict_connect_exits(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing-wt"
        existing.mkdir()
        with (
            _patch_git_info(current="main", default="main"),
            patch("fujimoto.cli.build_worktree_path", return_value=existing),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                await pilot.press("enter")  # Connect to existing
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[1] == existing

    @pytest.mark.asyncio
    async def test_conflict_suffix_creates_new(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing-wt"
        existing.mkdir()
        with (
            _patch_git_info(current="main", default="main"),
            patch("fujimoto.cli.build_worktree_path", return_value=existing),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                await pilot.press("down")  # Move to "Create new with suffix"
                await pilot.press("enter")
                await pilot.pause()
                mock_create.assert_called_once()
                called_path = mock_create.call_args[0][0]
                assert called_path.name == "existing-wt-2"


class TestSessionAppErrors:
    @pytest.mark.asyncio
    async def test_shows_error_on_git_failure(self) -> None:
        with (
            patch("fujimoto.cli.is_tmux_installed", return_value=True),
            patch(
                "fujimoto.cli.get_project_name",
                side_effect=GitError("not a repo"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#main").render()

    @pytest.mark.asyncio
    async def test_shows_error_on_config_error(self) -> None:
        with (
            patch("fujimoto.cli.is_tmux_installed", return_value=True),
            patch(
                "fujimoto.cli.get_project_name",
                side_effect=ConfigError("env not set"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()

    @pytest.mark.asyncio
    async def test_create_worktree_config_error(self) -> None:
        with (
            _patch_git_info(current="main", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                side_effect=ConfigError("FUJIMOTO_WORKTREE_ROOT is not set."),
            ),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                # Should show error in TUI, not crash
                assert app._launch_target is None
                main = app.query_one("#main")
                text = main.query("Static")[0].render().plain
                assert "FUJIMOTO_WORKTREE_ROOT" in text

    @pytest.mark.asyncio
    async def test_create_worktree_git_error(self, tmp_path: Path) -> None:
        with (
            _patch_git_info(current="main", default="main"),
            patch(
                "fujimoto.cli.build_worktree_path",
                return_value=tmp_path / "new-wt",
            ),
            patch(
                "fujimoto.cli.create_worktree",
                side_effect=GitError("branch already exists"),
            ),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                # Should show error, not crash
                assert app._launch_target is None


class TestSessionAppTmuxInstall:
    @pytest.mark.asyncio
    async def test_shows_install_prompt_when_missing(self) -> None:
        with patch("fujimoto.cli.is_tmux_installed", return_value=False):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert len(app.query("#tmux-install-list")) > 0

    @pytest.mark.asyncio
    async def test_quit_from_install_prompt(self) -> None:
        with patch("fujimoto.cli.is_tmux_installed", return_value=False):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("down")  # Move to "Quit"
                await pilot.press("enter")
                await pilot.pause()

    @pytest.mark.asyncio
    async def test_install_success_shows_home(self) -> None:
        installed = False

        def fake_is_installed() -> bool:
            return installed

        with (
            patch("fujimoto.cli.is_tmux_installed", side_effect=fake_is_installed),
            patch("fujimoto.cli.install_tmux") as mock_install,
            patch("fujimoto.cli.get_project_name", return_value="proj"),
            patch("fujimoto.cli.get_repo_root", return_value=Path("/fake/repo")),
            patch("fujimoto.cli.get_current_branch", return_value="main"),
            patch("fujimoto.cli.get_default_branch", return_value="main"),
            patch("fujimoto.cli.list_project_sessions", return_value=[]),
            patch(
                "fujimoto.cli.get_project_worktrees_dir",
                return_value=Path("/nonexistent"),
            ),
            patch("fujimoto.cli.get_sessions_for_path", return_value=[]),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()

                def do_install() -> None:
                    nonlocal installed
                    installed = True

                mock_install.side_effect = do_install
                await pilot.press("enter")  # Select "Install with brew"
                await pilot.pause()
                mock_install.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_failure_shows_error(self) -> None:
        with (
            patch("fujimoto.cli.is_tmux_installed", return_value=False),
            patch(
                "fujimoto.cli.install_tmux",
                side_effect=TmuxError("brew failed"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Select "Install with brew"
                await pilot.pause()


class TestSessionAppProjectSwitch:
    @pytest.mark.asyncio
    async def test_switch_project_shown_when_projects_available(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "other-repo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test():
                assert len(app.query("#action-switch-project")) > 0

    @pytest.mark.asyncio
    async def test_switch_project_hidden_when_no_projects(self) -> None:
        with _patch_git_info(projects=[]):
            app = SessionApp()
            async with app.run_test():
                assert len(app.query("#action-switch-project")) == 0

    @pytest.mark.asyncio
    async def test_navigate_to_project_select(self, tmp_path: Path) -> None:
        proj = tmp_path / "other-repo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to switch project (last item)
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#project-list")) > 0
                assert len(app.query("#project-filter")) > 0

    @pytest.mark.asyncio
    async def test_filter_narrows_project_list(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "bravo"
        proj_b.mkdir()
        proj_c = tmp_path / "charlie"
        proj_c.mkdir()
        with _patch_git_info(projects=[proj_a, proj_b, proj_c]):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to switch project
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # All three projects visible
                project_list = app.query_one("#project-list", ListView)
                assert len(project_list) == 3
                # Type to filter
                await pilot.press(*"bra")
                await pilot.pause()
                assert len(project_list) == 1

    @pytest.mark.asyncio
    async def test_filter_then_enter_selects(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "bravo"
        proj_b.mkdir()
        with (
            _patch_git_info(projects=[proj_a, proj_b]),
            patch(
                "fujimoto.cli.get_project_worktrees_dir",
                return_value=Path("/nonexistent"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to switch project
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # Filter to "bravo"
                await pilot.press(*"bravo")
                await pilot.pause()
                # Enter selects directly from the filter input
                await pilot.press("enter")
                await pilot.pause()
                assert app._project_cwd == proj_b

    @pytest.mark.asyncio
    async def test_arrow_down_moves_list_highlight(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "bravo"
        proj_b.mkdir()
        proj_c = tmp_path / "charlie"
        proj_c.mkdir()
        with _patch_git_info(projects=[proj_a, proj_b, proj_c]):
            app = SessionApp()
            async with app.run_test() as pilot:
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                project_list = app.query_one("#project-list", ListView)
                assert project_list.index == 0
                # Arrow down moves highlight while focus stays on filter
                await pilot.press("down")
                await pilot.pause()
                assert project_list.index == 1
                assert app.focused.id == "project-filter"
                await pilot.press("down")
                await pilot.pause()
                assert project_list.index == 2

    @pytest.mark.asyncio
    async def test_arrow_up_moves_list_highlight(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "bravo"
        proj_b.mkdir()
        with _patch_git_info(projects=[proj_a, proj_b]):
            app = SessionApp()
            async with app.run_test() as pilot:
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                project_list = app.query_one("#project-list", ListView)
                # Move down then back up
                await pilot.press("down")
                await pilot.pause()
                assert project_list.index == 1
                await pilot.press("up")
                await pilot.pause()
                assert project_list.index == 0

    @pytest.mark.asyncio
    async def test_ghost_text_shown_for_startswith_match(self, tmp_path: Path) -> None:
        proj = tmp_path / "bravo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test() as pilot:
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"bra")
                await pilot.pause()
                filter_input = app.query_one("#project-filter", Input)
                assert filter_input._suggestion == "bravo"

    @pytest.mark.asyncio
    async def test_tab_autocompletes_suggestion(self, tmp_path: Path) -> None:
        proj = tmp_path / "bravo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test() as pilot:
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press(*"bra")
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()
                filter_input = app.query_one("#project-filter", Input)
                assert filter_input.value == "bravo"

    @pytest.mark.asyncio
    async def test_arrow_then_enter_selects(self, tmp_path: Path) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "bravo"
        proj_b.mkdir()
        with (
            _patch_git_info(projects=[proj_a, proj_b]),
            patch(
                "fujimoto.cli.get_project_worktrees_dir",
                return_value=Path("/nonexistent"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # Arrow down to bravo, then enter
                await pilot.press("down")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert app._project_cwd == proj_b

    @pytest.mark.asyncio
    async def test_select_project_reinitializes(self, tmp_path: Path) -> None:
        proj1 = tmp_path / "repo-a"
        proj1.mkdir()
        proj2 = tmp_path / "repo-b"
        proj2.mkdir()
        with (
            _patch_git_info(projects=[proj1, proj2]),
            patch(
                "fujimoto.cli.get_project_worktrees_dir",
                return_value=Path("/nonexistent"),
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to switch project
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # Enter selects the first (highlighted) project
                await pilot.press("enter")
                await pilot.pause()
                # Should reinitialize and show home
                assert app._project_cwd == proj1

    @pytest.mark.asyncio
    async def test_project_select_error_shows_error(self, tmp_path: Path) -> None:
        proj = tmp_path / "bad-repo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to switch project
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                # Mock git failure for next init, then Enter selects
                with patch(
                    "fujimoto.cli.get_project_name",
                    side_effect=GitError("not a repo"),
                ):
                    await pilot.press("enter")
                    await pilot.pause()
                    # Should not crash


class TestSessionAppConflictSuffix:
    @pytest.mark.asyncio
    async def test_suffix_increments_past_existing(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing-wt"
        existing.mkdir()
        # Also create -2 so it needs to go to -3
        (tmp_path / "existing-wt-2").mkdir()
        with (
            _patch_git_info(current="main", default="main"),
            patch("fujimoto.cli.build_worktree_path", return_value=existing),
            patch("fujimoto.cli.create_worktree") as mock_create,
            patch("fujimoto.cli.store_session_meta"),
            patch("fujimoto.cli.fetch_branch"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")  # Create new
                await pilot.pause()
                await pilot.press(*"title")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")  # Select default branch
                await pilot.pause()
                await pilot.press("down")  # Move to "Create new with suffix"
                await pilot.press("enter")
                await pilot.pause()
                mock_create.assert_called_once()
                called_path = mock_create.call_args[0][0]
                assert called_path.name == "existing-wt-3"


class TestSessionAppEscapeFromNested:
    @pytest.mark.asyncio
    async def test_escape_from_project_select_returns_home(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "repo"
        proj.mkdir()
        with _patch_git_info(projects=[proj]):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to project select
                for _ in range(10):
                    await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#project-list")) > 0
                # Escape back
                await pilot.press("escape")
                await pilot.pause()
                assert len(app.query("#home-list")) > 0


def _make_claude_session(
    session_id: str = "abc12345-def6-7890",
    state: SessionState = SessionState.IDLE,
    cwd: Path = Path("/fake/repo"),
    git_branch: str | None = "main",
    last_activity: datetime | None = None,
) -> ClaudeSession:
    return ClaudeSession(
        jsonl_path=Path(f"/fake/.claude/projects/test/{session_id}.jsonl"),
        session_id=session_id,
        state=state,
        last_entry_type=EntryType.ASSISTANT,
        stop_reason=StopReason.END_TURN,
        cwd=cwd,
        git_branch=git_branch,
        last_activity=last_activity or datetime.now(tz=timezone.utc),
    )


class TestClaudeStateLabel:
    def test_waiting_for_user(self) -> None:
        label = _claude_state_label(SessionState.WAITING_FOR_USER)
        assert ICON_EYES in label
        assert "awaiting input" in label

    def test_waiting_for_tool_approval(self) -> None:
        label = _claude_state_label(SessionState.WAITING_FOR_TOOL_APPROVAL)
        assert ICON_SHIELD in label
        assert "approve tool" in label

    def test_processing(self) -> None:
        label = _claude_state_label(SessionState.WORKING)
        assert ICON_GEAR in label
        assert "working" in label

    def test_unknown(self) -> None:
        assert _claude_state_label(SessionState.UNKNOWN) == ""


class TestRelativeTime:
    def test_just_now(self) -> None:
        now = datetime.now(tz=timezone.utc)
        assert _relative_time(now) == "just now"

    def test_minutes_ago(self) -> None:
        from datetime import timedelta

        dt = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        assert _relative_time(dt) == "5m ago"

    def test_hours_ago(self) -> None:
        from datetime import timedelta

        dt = datetime.now(tz=timezone.utc) - timedelta(hours=3)
        assert _relative_time(dt) == "3h ago"

    def test_days_ago(self) -> None:
        from datetime import timedelta

        dt = datetime.now(tz=timezone.utc) - timedelta(days=7)
        assert _relative_time(dt) == "7d ago"

    def test_months_ago(self) -> None:
        from datetime import timedelta

        dt = datetime.now(tz=timezone.utc) - timedelta(days=60)
        assert _relative_time(dt) == "2mo ago"


class TestFormatPromptLines:
    def test_single_line_unchanged(self) -> None:
        assert _format_prompt_lines("hello world", 80) == ["hello world"]

    def test_two_lines_returned_as_is(self) -> None:
        assert _format_prompt_lines("line one\nline two", 80) == [
            "line one",
            "line two",
        ]

    def test_three_lines_all_shown(self) -> None:
        result = _format_prompt_lines("a\nb\nc", 80)
        assert result == ["a", "b", "…", "c"]

    def test_four_lines_shows_first_two_ellipsis_last(self) -> None:
        result = _format_prompt_lines("a\nb\nc\nd", 80)
        assert result == ["a", "b", "…", "d"]

    def test_long_single_line_word_wrapped(self) -> None:
        # A long line with spaces should be word-wrapped into multiple display lines
        words = " ".join(["word"] * 20)  # "word word word ..." — well over 20 chars
        result = _format_prompt_lines(words, 20)
        assert len(result) > 1
        for ln in result:
            assert len(ln) <= 20

    def test_long_line_no_spaces_word_wrapped(self) -> None:
        # textwrap breaks long words by default — 100 chars at width 20 → 5 chunks
        result = _format_prompt_lines("a" * 100, 20)
        # 5 chunks → first 2 + ellipsis + last
        assert result == ["a" * 20, "a" * 20, "…", "a" * 20]

    def test_empty_lines_skipped(self) -> None:
        assert _format_prompt_lines("a\n\nb", 80) == ["a", "b"]

    def test_exact_width_not_truncated(self) -> None:
        text = "a" * 20
        assert _format_prompt_lines(text, 20) == [text]


class TestGetClaudeSessions:
    def test_returns_empty_when_no_sessions(self) -> None:
        with patch("fujimoto.cli.get_sessions_for_path", return_value=[]):
            path_to_latest, root_sessions = _get_claude_sessions(Path("/fake/repo"), [])
        assert path_to_latest == {}
        assert root_sessions == []

    def test_returns_root_sessions(self) -> None:
        cs = _make_claude_session()
        with patch("fujimoto.cli.get_sessions_for_path", return_value=[cs]):
            path_to_latest, root_sessions = _get_claude_sessions(Path("/fake/repo"), [])
        assert str(Path("/fake/repo")) in path_to_latest
        assert root_sessions == [cs]

    def test_returns_worktree_sessions(self) -> None:
        wt_path = Path("/fake/worktrees/wt1")
        cs = _make_claude_session(cwd=wt_path)

        def fake_sessions(path: Path) -> list[ClaudeSession]:
            if path == wt_path:
                return [cs]
            return []

        with patch("fujimoto.cli.get_sessions_for_path", side_effect=fake_sessions):
            path_to_latest, root_sessions = _get_claude_sessions(
                Path("/fake/repo"), [wt_path]
            )
        assert str(wt_path) in path_to_latest
        assert root_sessions == []

    def test_none_project_root(self) -> None:
        with patch("fujimoto.cli.get_sessions_for_path", return_value=[]):
            path_to_latest, root_sessions = _get_claude_sessions(None, [])
        assert path_to_latest == {}
        assert root_sessions == []


class TestClaudeSessionsOnHome:
    @pytest.mark.asyncio
    async def test_active_session_shows_claude_state(self, tmp_path: Path) -> None:
        cs = _make_claude_session(state=SessionState.WAITING_FOR_USER)

        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=lambda _path: [cs],
        ):
            app = SessionApp()
            async with app.run_test():
                session = app._session_map["ds-test-proj--direct-1"]
                assert session.claude_state == SessionState.WAITING_FOR_USER
                assert session.claude_session_id == cs.session_id

    @pytest.mark.asyncio
    async def test_inactive_worktree_shows_claude_state(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-fix"
        cs = _make_claude_session(
            state=SessionState.WORKING,
            cwd=wt,
        )

        def fake_sessions(path: Path) -> list[ClaudeSession]:
            # Match on dir name since _patch_git_info creates a temp copy
            if path.name == "20260309-fix":
                return [cs]
            return []

        with _patch_git_info(
            worktrees=[wt],
            claude_sessions_fn=fake_sessions,
        ):
            app = SessionApp()
            async with app.run_test():
                session = app._session_map["wt-20260309-fix"]
                assert session.claude_state == SessionState.WORKING

    @pytest.mark.asyncio
    async def test_previous_claude_sessions_shown(self) -> None:
        cs1 = _make_claude_session(session_id="session-1111-aaaa")
        cs2 = _make_claude_session(session_id="session-2222-bbbb")

        with _patch_git_info(
            claude_sessions_fn=lambda _path: [cs1, cs2],
        ):
            app = SessionApp()
            async with app.run_test():
                assert "cs-session-" in str(list(app._session_map.keys()))
                # Both sessions should be in the map (no active tmux to claim them)
                claude_sessions = [
                    s for s in app._session_map.values() if s.session_type == "claude"
                ]
                assert len(claude_sessions) == 2

    @pytest.mark.asyncio
    async def test_claimed_session_not_in_previous(self) -> None:
        """The latest Claude session for an active direct tmux session is claimed."""
        cs1 = _make_claude_session(session_id="session-1111-aaaa")
        cs2 = _make_claude_session(session_id="session-2222-bbbb")

        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=lambda _path: [cs1, cs2],
        ):
            app = SessionApp()
            async with app.run_test():
                # cs1 is the latest and should be claimed by the active session
                direct = app._session_map["ds-test-proj--direct-1"]
                assert direct.claude_session_id == cs1.session_id
                # Only cs2 should appear as a previous Claude session
                claude_sessions = [
                    s for s in app._session_map.values() if s.session_type == "claude"
                ]
                assert len(claude_sessions) == 1
                assert claude_sessions[0].claude_session_id == cs2.session_id

    @pytest.mark.asyncio
    async def test_previous_sessions_limited_to_five(self) -> None:
        sessions = [
            _make_claude_session(session_id=f"sess-{i:04d}-aaaa") for i in range(10)
        ]
        with _patch_git_info(
            claude_sessions_fn=lambda _path: sessions,
        ):
            app = SessionApp()
            async with app.run_test():
                claude_sessions = [
                    s for s in app._session_map.values() if s.session_type == "claude"
                ]
                assert len(claude_sessions) == 5


class TestClaudeSessionActions:
    @pytest.mark.asyncio
    async def test_resume_action_shown_for_claude_session(self) -> None:
        cs = _make_claude_session()
        with _patch_git_info(
            claude_sessions_fn=lambda _path: [cs],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate to the claude session item
                home_list = app.query_one("#home-list", ListView)
                # Find the claude session item index
                for i, child in enumerate(home_list.children):
                    if child.id and child.id.startswith("cs-"):
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Should show session actions with Resume
                assert len(app.query("#sa-resume")) > 0
                # Should NOT show Rename
                assert len(app.query("#sa-rename")) == 0

    @pytest.mark.asyncio
    async def test_resume_sets_launch_target(self) -> None:
        cs = _make_claude_session()
        with (
            _patch_git_info(
                claude_sessions_fn=lambda _path: [cs],
            ),
            patch(
                "fujimoto.cli.get_next_direct_session_name",
                return_value="test-proj/direct-1",
            ),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                for i, child in enumerate(home_list.children):
                    if child.id and child.id.startswith("cs-"):
                        home_list.index = i
                        break
                await pilot.press("enter")
                await pilot.pause()
                # Select Resume
                await pilot.press("enter")
                await pilot.pause()
                assert app._launch_target is not None
                assert app._launch_target[4] == cs.session_id
                assert app._launch_target[2] == "test-proj/direct-1"


class TestPolling:
    @pytest.mark.asyncio
    async def test_poll_timer_starts_on_home(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                assert app._poll_timer is not None

    @pytest.mark.asyncio
    async def test_poll_timer_stops_on_navigate_away(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                assert app._poll_timer is not None
                # Navigate to create form
                await pilot.press("enter")
                await pilot.pause()
                assert app._poll_timer is None

    @pytest.mark.asyncio
    async def test_poll_updates_label_on_state_change(self) -> None:
        cs = _make_claude_session(state=SessionState.WORKING)
        call_count = 0

        def counting_sessions(path: Path) -> list[ClaudeSession]:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [cs]
            return [_make_claude_session(state=SessionState.WAITING_FOR_USER)]

        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=counting_sessions,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                session = app._session_map["ds-test-proj--direct-1"]
                assert session.claude_state == SessionState.WORKING
                # Trigger a poll — should update label in-place
                await app._poll_session_states()
                await pilot.pause()
                session = app._session_map["ds-test-proj--direct-1"]
                assert session.claude_state == SessionState.WAITING_FOR_USER
                # Home list should still exist (no rebuild)
                assert len(app.query("#home-list")) > 0

    @pytest.mark.asyncio
    async def test_poll_no_update_when_unchanged(self) -> None:
        cs = _make_claude_session(state=SessionState.WAITING_FOR_USER)
        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=lambda _path: [cs],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                home_list.index = 1
                await app._poll_session_states()
                await pilot.pause()
                # Index preserved — no rebuild
                home_list = app.query_one("#home-list", ListView)
                assert home_list.index == 1

    @pytest.mark.asyncio
    async def test_poll_preserves_selection(self) -> None:
        call_count = 0

        def changing_sessions(path: Path) -> list[ClaudeSession]:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [_make_claude_session(state=SessionState.WORKING)]
            return [_make_claude_session(state=SessionState.WAITING_FOR_USER)]

        with _patch_git_info(
            sessions=["test-proj/direct-1"],
            claude_sessions_fn=changing_sessions,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                home_list = app.query_one("#home-list", ListView)
                home_list.index = 1
                await app._poll_session_states()
                await pilot.pause()
                # In-place update should not change selection
                home_list = app.query_one("#home-list", ListView)
                assert home_list.index == 1

    @pytest.mark.asyncio
    async def test_poll_skipped_when_not_on_home(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                # Navigate away from home
                await pilot.press("enter")
                await pilot.pause()
                # Poll should be a no-op (no #home-list)
                await app._poll_session_states()
                # Should not crash


class TestMainResume:
    def test_resume_skips_system_prompt(self) -> None:
        app1 = SessionApp.__new__(SessionApp)
        app1._launch_target = LaunchTarget(
            "proj",
            Path("/tmp/repo"),
            "proj/direct-1",
            "direct",
            "resume-session-id",
        )
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
        ):
            main()
            mock_launch.assert_called_once_with(
                "proj",
                Path("/tmp/repo"),
                "proj/direct-1",
                system_prompt=None,
                resume_session_id="resume-session-id",
                fork_session=False,
            )

    def test_fork_resumes_parent_with_fork_flag(self, tmp_path: Path) -> None:
        app1 = SessionApp.__new__(SessionApp)
        app1._launch_target = LaunchTarget(
            "proj",
            tmp_path / "20260309-fork",
            "proj/20260309-fork",
            "worktree",
            forked_from_session_id="parent-session-id",
            forked_from_worktree=tmp_path / "20260101-parent",
        )
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch(
                "fujimoto.cli.read_session_meta",
                return_value={"base_branch": "worktree/20260101-parent"},
            ),
        ):
            main()

        kwargs = mock_launch.call_args.kwargs
        # A fork resumes the PARENT conversation, with --fork-session so the
        # parent's transcript is left untouched.
        assert kwargs["resume_session_id"] == "parent-session-id"
        assert kwargs["fork_session"] is True
        # Unlike a plain resume, a fork does get a system prompt — it has moved
        # to a different worktree and needs to be told.
        prompt = kwargs["system_prompt"]
        assert prompt is not None
        assert str(tmp_path / "20260101-parent") in prompt
        assert str(tmp_path / "20260309-fork") in prompt
        assert "worktree/20260101-parent" in prompt


# -- Update banner tests --


class TestUpdateBanner:
    @pytest.mark.asyncio
    async def test_banner_shows_when_update_version_set(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                app._update_banner_version = "9.9.9"
                await app._show_home()
                await pilot.pause()
                banner = app.query("#update-banner")
                assert len(banner) == 1

    @pytest.mark.asyncio
    async def test_no_banner_when_version_not_set(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                assert app._update_banner_version is None
                assert len(app.query("#update-banner")) == 0

    @pytest.mark.asyncio
    async def test_dismiss_clears_banner_and_persists(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                app._update_banner_version = "9.9.9"
                await app._show_home()
                await pilot.pause()
                with patch("fujimoto.cli.dismiss_update_version") as mock_dismiss:
                    await app.action_dismiss_update()
                    await pilot.pause()
                    mock_dismiss.assert_called_once_with("9.9.9")
                assert app._update_banner_version is None
                assert len(app.query("#update-banner")) == 0

    @pytest.mark.asyncio
    async def test_dismiss_noop_when_not_on_home(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                app._update_banner_version = "9.9.9"
                app._on_home = False
                with patch("fujimoto.cli.dismiss_update_version") as mock_dismiss:
                    await app.action_dismiss_update()
                    mock_dismiss.assert_not_called()
                assert app._update_banner_version == "9.9.9"

    @pytest.mark.asyncio
    async def test_version_label_rendered(self) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test():
                label = app.query_one("#version-label")
                rendered = str(label.render())
                assert rendered.startswith("fujimoto v")


class TestQuickTerminalPromptAndToggle:
    @pytest.mark.asyncio
    async def test_first_launch_shows_modal_when_setting_unset(self) -> None:
        from fujimoto.cli import QuickTerminalPrompt
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, QuickTerminalPrompt)
                # Home is rendered underneath so the toggle row exists.
                assert len(app.query("#home-list")) == 1

    @pytest.mark.asyncio
    async def test_first_launch_skips_modal_when_env_disables_key(self) -> None:
        from fujimoto.cli import QuickTerminalPrompt
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value=""),
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert not isinstance(app.screen, QuickTerminalPrompt)
                assert len(app.query("#home-list")) == 1

    @pytest.mark.asyncio
    async def test_modal_yes_saves_true_and_enables_binding(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                mock_save.assert_called_once_with(Settings(quick_terminal_enabled=True))
                mock_enable.assert_called_once()

    @pytest.mark.asyncio
    async def test_modal_no_saves_false_and_skips_binding(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("n")
                await pilot.pause()
                mock_save.assert_called_once_with(
                    Settings(quick_terminal_enabled=False)
                )
                mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_modal_escape_saves_false(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                mock_save.assert_called_once_with(
                    Settings(quick_terminal_enabled=False)
                )
                mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_home_toggle_off_to_on(self) -> None:
        from fujimoto.settings import Settings

        load_calls: list[Settings] = []

        def fake_load() -> Settings:
            # First call: in on_mount → False (skip prompt).
            # Subsequent: reflect current toggle state.
            if not load_calls:
                load_calls.append(Settings(quick_terminal_enabled=False))
                return load_calls[-1]
            return load_calls[-1]

        with (
            _patch_git_info(),
            patch("fujimoto.cli.load_settings", side_effect=fake_load),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
            patch("fujimoto.cli.disable_quick_terminal_binding") as mock_disable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._toggle_quick_terminal()
                await pilot.pause()
                mock_save.assert_called_once_with(Settings(quick_terminal_enabled=True))
                mock_enable.assert_called_once()
                mock_disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_home_toggle_on_to_off(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=True),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
            patch("fujimoto.cli.disable_quick_terminal_binding") as mock_disable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._toggle_quick_terminal()
                await pilot.pause()
                mock_save.assert_called_once_with(
                    Settings(quick_terminal_enabled=False)
                )
                mock_disable.assert_called_once()
                mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_home_toggle_noop_when_env_disables(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=None),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value=""),
            patch("fujimoto.cli.save_settings") as mock_save,
            patch("fujimoto.cli.enable_quick_terminal_binding") as mock_enable,
            patch("fujimoto.cli.disable_quick_terminal_binding") as mock_disable,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._toggle_quick_terminal()
                mock_save.assert_not_called()
                mock_enable.assert_not_called()
                mock_disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_home_renders_settings_row(self) -> None:
        from fujimoto.settings import Settings

        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.load_settings",
                return_value=Settings(quick_terminal_enabled=True),
            ),
            patch("fujimoto.cli.quick_terminal_key", return_value="C-`"),
        ):
            app = SessionApp()
            async with app.run_test():
                assert len(app.query("#action-toggle-quick-terminal")) == 1


class TestFriendlyKeyLabel:
    def test_ctrl_backtick(self) -> None:
        assert _friendly_key_label("C-`") == "Ctrl+`"

    def test_ctrl_letter_preserves_case(self) -> None:
        assert _friendly_key_label("C-f") == "Ctrl+f"

    def test_meta_alt(self) -> None:
        assert _friendly_key_label("M-x") == "Alt+x"

    def test_shift(self) -> None:
        assert _friendly_key_label("S-Tab") == "Shift+Tab"

    def test_combination(self) -> None:
        assert _friendly_key_label("C-M-x") == "Ctrl+Alt+x"

    def test_no_prefix_returned_as_is(self) -> None:
        assert _friendly_key_label("Space") == "Space"


# -- Stopped sessions (remembered across a restart) --


def _list_text(app: SessionApp) -> str:
    """All text currently rendered in the home list."""
    parts: list[str] = []
    for widget in app.query("#home-list").first(ListView).query(Label):
        parts.append(str(widget.render()))
    for widget in app.query("#home-list").first(ListView).query(Static):
        parts.append(str(widget.render()))
    return "\n".join(parts)


def _record(
    tmp_path: Path,
    name: str = "wt-a",
    project: str = "test-proj",
    session_type: str = "worktree",
    claude_session_id: str | None = None,
):
    """Build an open-session record whose directory actually exists."""
    from fujimoto.session_state import SessionRecord

    cwd = tmp_path / name
    cwd.mkdir(exist_ok=True)
    return SessionRecord(
        cwd=str(cwd),
        project=project,
        session_type=session_type,
        branch=f"worktree/{name}" if session_type == "worktree" else "feat/test",
        claude_session_id=claude_session_id,
    )


class TestStoppedSessions:
    @pytest.mark.asyncio
    async def test_renders_orange_in_sessions_section(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                info = app._session_map["wt-wt-a"]
                assert info.is_stopped is True
                assert info.is_active is False
                label = app._build_session_label(info, "")
                assert label.startswith(ICON_ORANGE_CIRCLE)
                text = _list_text(app)
                assert "───── sessions ─────" in text

    @pytest.mark.asyncio
    async def test_running_session_is_not_stopped(self, tmp_path: Path) -> None:
        # The record exists but tmux still has the session: it is simply live.
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(
            sessions=["test-proj/wt-a"],
            worktrees=[Path("wt-a")],
            open_sessions=records,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app._session_map["wt-wt-a"].is_active is True
                assert app._session_map["wt-wt-a"].is_stopped is False
                assert "Restore" not in _list_text(app)

    @pytest.mark.asyncio
    async def test_stopped_worktree_leaves_inactive_section(
        self, tmp_path: Path
    ) -> None:
        # A worktree with an open record must appear once, as stopped — not
        # again under "inactive worktrees".
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(worktrees=[Path("wt-a")], open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "inactive worktrees" not in _list_text(app)
                assert app._session_map["wt-wt-a"].is_stopped is True

    @pytest.mark.asyncio
    async def test_untracked_worktree_stays_inactive(self, tmp_path: Path) -> None:
        # No record at all means the user never had it open, or terminated it.
        # Either way it is dim — this is what keeps an upgrade from turning
        # every existing worktree orange.
        with _patch_git_info(worktrees=[Path("wt-a")], open_sessions={}):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "inactive worktrees" in _list_text(app)
                assert app._session_map["wt-wt-a"].is_stopped is False

    @pytest.mark.asyncio
    async def test_other_projects_records_are_ignored(self, tmp_path: Path) -> None:
        records = {"other/wt-a": _record(tmp_path, project="other")}
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app._stopped_records() == {}
                assert "Restore" not in _list_text(app)

    @pytest.mark.asyncio
    async def test_stopped_direct_session_renders(self, tmp_path: Path) -> None:
        records = {
            "test-proj/direct-1": _record(
                tmp_path, name="direct-1", session_type="direct"
            )
        }
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                info = app._session_map["ds-test-proj--direct-1"]
                assert info.is_stopped is True
                assert info.session_type == "direct"

    @pytest.mark.asyncio
    async def test_search_filters_stopped_rows(self, tmp_path: Path) -> None:
        records = {
            "test-proj/wt-a": _record(tmp_path, name="wt-a"),
            "test-proj/wt-b": _record(tmp_path, name="wt-b"),
        }
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app._search_query = "wt-b"
                await app._refresh_home_list()
                await pilot.pause()
                assert "wt-b" in _list_text(app)
                assert "wt-a" not in _list_text(app)


class TestRestoreStoppedSessions:
    @pytest.mark.asyncio
    async def test_row_shows_count_and_pluralises(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "Restore 1 stopped session" in _list_text(app)
                assert "sessions" not in _list_text(app).split("Restore 1")[1][:20]

        records["test-proj/wt-b"] = _record(tmp_path, name="wt-b")
        with _patch_git_info(open_sessions=records):
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "Restore 2 stopped sessions" in _list_text(app)

    @pytest.mark.asyncio
    async def test_creates_detached_sessions_resuming_conversations(
        self, tmp_path: Path
    ) -> None:
        records = {"test-proj/wt-a": _record(tmp_path, claude_session_id="rec-id")}
        cs = _fake_claude_session(tmp_path / "wt-a", session_id="live-id")
        with _patch_git_info(open_sessions=records, claude_sessions_fn=lambda _p: [cs]):
            with patch("fujimoto.cli.create_session") as create:
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await app._restore_stopped_sessions()
        create.assert_called_once()
        args, kwargs = create.call_args
        assert args[0] == "test-proj/wt-a"
        # The live transcript wins over the id recorded at launch time.
        assert kwargs["resume_session_id"] == "live-id"
        assert kwargs["system_prompt"] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_recorded_id(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path, claude_session_id="rec-id")}
        with _patch_git_info(open_sessions=records):
            with patch("fujimoto.cli.create_session") as create:
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await app._restore_stopped_sessions()
        assert create.call_args.kwargs["resume_session_id"] == "rec-id"

    @pytest.mark.asyncio
    async def test_no_conversation_launches_fresh(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(open_sessions=records):
            with patch("fujimoto.cli.create_session") as create:
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await app._restore_stopped_sessions()
        assert create.call_args.kwargs["resume_session_id"] is None
        assert create.call_args.kwargs["system_prompt"] is not None

    @pytest.mark.asyncio
    async def test_selecting_the_row_restores(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(open_sessions=records):
            with patch("fujimoto.cli.create_session") as create:
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    event = SimpleNamespace(item=SimpleNamespace(id="action-restore"))
                    await app.on_home_selected(event)  # type: ignore[arg-type]
        create.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_is_surfaced(self, tmp_path: Path) -> None:
        records = {"test-proj/wt-a": _record(tmp_path)}
        with _patch_git_info(open_sessions=records):
            with patch("fujimoto.cli.create_session", side_effect=RuntimeError("nope")):
                app = SessionApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await app._restore_stopped_sessions()
                    await pilot.pause()
                    text = " ".join(
                        str(w.render())
                        for w in app.query("#main").first().query(Static)
                    )
                    assert "Could not restore" in text


class TestStopAndTerminate:
    def _session(self, tmp_path: Path, active: bool = True, stopped: bool = False):
        return SessionInfo(
            name="wt-a",
            session_type="worktree",
            project="test-proj",
            path=tmp_path / "wt-a",
            tmux_session="test-proj/wt-a",
            is_active=active,
            is_stopped=stopped,
            branch="worktree/wt-a",
            claude_session_id="cid",
        )

    @pytest.mark.asyncio
    async def test_active_session_offers_both(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._show_session_actions(self._session(tmp_path))
                await pilot.pause()
                ids = [
                    i.id for i in app.query("#session-actions").first(ListView).children
                ]
                assert "sa-stop" in ids
                assert "sa-terminate" in ids

    @pytest.mark.asyncio
    async def test_stopped_session_offers_terminate_only(self, tmp_path: Path) -> None:
        # There is nothing left to stop, but the user still needs a way to
        # dismiss an orange row without launching it first.
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._show_session_actions(
                    self._session(tmp_path, active=False, stopped=True)
                )
                await pilot.pause()
                ids = [
                    i.id for i in app.query("#session-actions").first(ListView).children
                ]
                assert "sa-stop" not in ids
                assert "sa-terminate" in ids
                info = " ".join(
                    str(w.render()) for w in app.query("#main").first().query(Static)
                )
                assert "stopped" in info

    @pytest.mark.asyncio
    async def test_inactive_worktree_offers_neither(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._show_session_actions(self._session(tmp_path, active=False))
                await pilot.pause()
                ids = [
                    i.id for i in app.query("#session-actions").first(ListView).children
                ]
                assert "sa-stop" not in ids
                assert "sa-terminate" not in ids

    @pytest.mark.asyncio
    async def test_stop_keeps_record_open(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                with patch("fujimoto.cli.kill_session") as kill:
                    await app._end_session(self._session(tmp_path), terminate=False)
                kill.assert_called_once_with("test-proj/wt-a")
                state = session_state.load_state()
                assert "test-proj/wt-a" in state
                assert state["test-proj/wt-a"].claude_session_id == "cid"

    @pytest.mark.asyncio
    async def test_terminate_forgets_record(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                with patch("fujimoto.cli.kill_session") as kill:
                    await app._end_session(self._session(tmp_path), terminate=True)
                kill.assert_called_once()
                assert session_state.load_state() == {}

    @pytest.mark.asyncio
    async def test_terminating_a_stopped_session_kills_nothing(
        self, tmp_path: Path
    ) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("fujimoto.cli.kill_session") as kill:
                    await app._end_session(
                        self._session(tmp_path, active=False, stopped=True),
                        terminate=True,
                    )
                kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_failure_on_a_live_session_is_surfaced(
        self, tmp_path: Path
    ) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                with (
                    patch("fujimoto.cli.kill_session", side_effect=TmuxError("boom")),
                    patch("fujimoto.cli.session_exists", return_value=True),
                ):
                    await app._end_session(self._session(tmp_path), terminate=True)
                await pilot.pause()
                text = " ".join(
                    str(w.render()) for w in app.query("#main").first().query(Static)
                )
                assert "boom" in text
                # Marking a still-running session closed would hide it.
                assert "test-proj/wt-a" in session_state.load_state()

    @pytest.mark.asyncio
    async def test_kill_failure_on_a_vanished_session_proceeds(
        self, tmp_path: Path
    ) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                # It died between being listed and being acted on — which is
                # the state the kill was aiming for anyway.
                with (
                    patch("fujimoto.cli.kill_session", side_effect=TmuxError("gone")),
                    patch("fujimoto.cli.session_exists", return_value=False),
                ):
                    await app._end_session(self._session(tmp_path), terminate=True)
                assert session_state.load_state() == {}

    @pytest.mark.asyncio
    async def test_menu_items_route_to_one_handler(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app._selected_session = self._session(tmp_path)
                with patch.object(app, "_end_session") as end:
                    for item_id, terminate in (
                        ("sa-stop", False),
                        ("sa-terminate", True),
                    ):
                        event = SimpleNamespace(item=SimpleNamespace(id=item_id))
                        await app.on_session_action_selected(event)  # type: ignore[arg-type]
                        assert end.call_args.kwargs["terminate"] is terminate


class TestTerminatePrompt:
    def _target(self, tmp_path: Path) -> LaunchTarget:
        return LaunchTarget(
            "test-proj", tmp_path / "wt-a", "test-proj/wt-a", "worktree"
        )

    @pytest.mark.asyncio
    async def test_opens_on_mount_with_terminate_default(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp(pending_close=self._target(tmp_path))
            async with app.run_test() as pilot:
                await pilot.pause()
                prompt = app.query("#terminate-prompt").first(ListView)
                ids = [i.id for i in prompt.children]
                assert ids == ["tp-terminate", "tp-stop", "tp-cancel"]
                # Enter therefore terminates, matching the confirm it replaces.
                assert prompt.index == 0

    @pytest.mark.asyncio
    async def test_mentions_the_stop_shortcut(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp(pending_close=self._target(tmp_path))
            async with app.run_test() as pilot:
                await pilot.pause()
                text = " ".join(
                    str(w.render()) for w in app.query("#main").first().query(Static)
                )
                assert "s stops without asking" in text

    @pytest.mark.asyncio
    async def test_terminate_forgets_record(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        (tmp_path / "wt-a").mkdir()
        with _patch_git_info():
            app = SessionApp(pending_close=self._target(tmp_path))
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path / "wt-a",
                    project="test-proj",
                    session_type="worktree",
                )
                with patch("fujimoto.cli.kill_session"):
                    event = SimpleNamespace(item=SimpleNamespace(id="tp-terminate"))
                    await app.on_terminate_prompt_selected(event)  # type: ignore[arg-type]
                assert session_state.load_state() == {}

    @pytest.mark.asyncio
    async def test_stop_keeps_record(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        (tmp_path / "wt-a").mkdir()
        with _patch_git_info():
            app = SessionApp(pending_close=self._target(tmp_path))
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path / "wt-a",
                    project="test-proj",
                    session_type="worktree",
                )
                with patch("fujimoto.cli.kill_session") as kill:
                    event = SimpleNamespace(item=SimpleNamespace(id="tp-stop"))
                    await app.on_terminate_prompt_selected(event)  # type: ignore[arg-type]
                kill.assert_called_once_with("test-proj/wt-a")
                assert "test-proj/wt-a" in session_state.load_state()

    @pytest.mark.asyncio
    async def test_cancel_reattaches(self, tmp_path: Path) -> None:
        target = self._target(tmp_path)
        with _patch_git_info():
            app = SessionApp(pending_close=target)
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("fujimoto.cli.kill_session") as kill:
                    event = SimpleNamespace(item=SimpleNamespace(id="tp-cancel"))
                    await app.on_terminate_prompt_selected(event)  # type: ignore[arg-type]
                # Cancel must cost nothing: nothing killed, session re-attached.
                kill.assert_not_called()
                assert app._launch_target == target

    @pytest.mark.asyncio
    async def test_no_pending_close_opens_home(self, tmp_path: Path) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert not app.query("#terminate-prompt")
                assert app.query("#home-list")


class TestMainPendingStopAndClose:
    def _app(self, target: LaunchTarget | None) -> SessionApp:
        app = SessionApp.__new__(SessionApp)
        app._launch_target = target
        return app

    def test_records_session_as_open_before_attaching(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        wt = tmp_path / "wt-a"
        wt.mkdir()
        app1 = self._app(LaunchTarget("proj", wt, "proj/wt-a", "worktree"))
        app2 = self._app(None)
        seen: list[dict] = []
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            # The record must exist before the attach blocks, or a host that
            # dies mid-session leaves nothing to restore.
            patch(
                "fujimoto.cli.launch_claude_in_tmux",
                side_effect=lambda *a, **k: seen.append(session_state.load_state()),
            ),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value=None),
        ):
            main()
        assert "proj/wt-a" in seen[0]

    def test_stop_kills_session_without_prompting(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        wt = tmp_path / "wt-a"
        wt.mkdir()
        app1 = self._app(LaunchTarget("proj", wt, "proj/wt-a", "worktree"))
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]) as mock_cls,
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value="stop"),
            patch("fujimoto.cli.kill_session") as kill,
        ):
            main()
        kill.assert_called_once_with("proj/wt-a")
        # No prompt, and the record stays open so it comes back orange.
        assert mock_cls.call_args_list[1].kwargs["pending_close"] is None
        assert "proj/wt-a" in session_state.load_state()

    def test_stop_tolerates_a_session_already_gone(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt-a"
        wt.mkdir()
        app1 = self._app(LaunchTarget("proj", wt, "proj/wt-a", "worktree"))
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value="stop"),
            patch("fujimoto.cli.kill_session", side_effect=TmuxError("gone")),
        ):
            main()  # must not raise

    def test_close_hands_the_prompt_to_the_next_app(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt-a"
        wt.mkdir()
        app1 = self._app(LaunchTarget("proj", wt, "proj/wt-a", "worktree"))
        app2 = self._app(None)
        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]) as mock_cls,
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux"),
            patch("fujimoto.cli._apply_worktree_config", return_value=True),
            patch("fujimoto.cli.take_pending_action", return_value="close"),
            patch("fujimoto.cli.kill_session") as kill,
        ):
            main()
        # The TUI asks first; nothing is killed by main().
        kill.assert_not_called()
        pending = mock_cls.call_args_list[1].kwargs["pending_close"]
        assert pending == LaunchTarget("proj", wt, "proj/wt-a", "worktree")


class TestSessionStateBookkeeping:
    @pytest.mark.asyncio
    async def test_rename_follows_the_record(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/old",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                app._selected_session = SessionInfo(
                    name="old",
                    session_type="worktree",
                    project="test-proj",
                    path=tmp_path,
                    tmux_session="test-proj/old",
                    is_active=True,
                    branch="worktree/old",
                )
                with patch("fujimoto.cli.rename_session"):
                    event = SimpleNamespace(value="new")
                    await app.on_rename_submitted(event)  # type: ignore[arg-type]
                # A renamed session must not be orphaned as a stale record.
                assert set(session_state.load_state()) == {"test-proj/new"}

    @pytest.mark.asyncio
    async def test_deleting_a_worktree_forgets_the_record(self, tmp_path: Path) -> None:
        from fujimoto import session_state

        with _patch_git_info():
            app = SessionApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                session_state.mark_open(
                    "test-proj/wt-a",
                    cwd=tmp_path,
                    project="test-proj",
                    session_type="worktree",
                )
                session = SessionInfo(
                    name="wt-a",
                    session_type="worktree",
                    project="test-proj",
                    path=tmp_path,
                    tmux_session="test-proj/wt-a",
                    is_active=True,
                    branch="worktree/wt-a",
                )
                with (
                    patch("fujimoto.cli.kill_session"),
                    patch("fujimoto.cli.remove_worktree"),
                    patch("fujimoto.cli.delete_branch"),
                ):
                    await app._do_delete_worktree(session, remove_remote=False)
                # Otherwise it would come back as a stopped row pointing at a
                # directory that no longer exists.
                assert session_state.load_state() == {}


class TestHomeListHeight:
    """The session list fills the available screen height (no fixed cap)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("rows", "expected"), [(30, 25), (60, 55)])
    async def test_list_grows_with_terminal_height(
        self, rows: int, expected: int
    ) -> None:
        with _patch_git_info():
            app = SessionApp()
            async with app.run_test(size=(100, rows)) as pilot:
                await pilot.pause()
                assert app.query_one("#home-list", ListView).size.height == expected


# -- Transcript search (`s`) --


_SNIPPET_TEXT = "a snippet with the needle in it"
_SNIPPET_SPAN = (
    _SNIPPET_TEXT.index("needle"),
    _SNIPPET_TEXT.index("needle") + len("needle"),
)


def _make_hit(
    session_id: str = "abcdef12",
    *,
    matches: int = 2,
    cwd: str = "/repo/wt",
    snippets: tuple[object, ...] | None = None,
):
    """A SearchHit with just enough of a ClaudeSession to render a row."""
    from fujimoto.claude.search import SearchHit

    return SearchHit(
        session=ClaudeSession(
            jsonl_path=Path(f"/logs/{session_id}.jsonl"),
            session_id=session_id,
            state=SessionState.IDLE,
            last_entry_type=EntryType.USER,
            stop_reason=None,
            cwd=Path(cwd),
            git_branch="worktree/x",
            last_activity=datetime.now(tz=timezone.utc),
        ),
        match_count=matches,
        snippets=snippets
        if snippets is not None
        else (Snippet(text=_SNIPPET_TEXT, spans=(_SNIPPET_SPAN,)),),
    )


def _write_transcript(directory: Path, name: str, text: str) -> Path:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"{name}.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "/repo",
                "gitBranch": "main",
                "timestamp": "2026-08-26T10:00:00Z",
                "message": {"content": text},
            }
        )
        + "\n"
    )
    return log


class TestTranscriptSearchView:
    @pytest.mark.asyncio
    async def test_s_opens_the_search_view(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                assert app._on_search is True
                assert len(app.query("#search-input")) == 1
                assert len(app.query("#search-results")) == 1
                assert app.focused is not None
                assert app.focused.id == "search-input"

    @pytest.mark.asyncio
    async def test_s_is_ignored_away_from_the_home_screen(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await app._show_create_form()
                await pilot.press("s")
                await pilot.pause()
                assert app._on_search is False

    @pytest.mark.asyncio
    async def test_escape_returns_to_the_home_screen(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_go_back()
                await pilot.pause()
                assert app._on_search is False
                assert len(app.query("#home-list")) == 1

    @pytest.mark.asyncio
    async def test_query_survives_leaving_and_reopening(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "needle"
                await pilot.pause()
                await app.action_go_back()
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                assert app.query_one("#search-input", Input).value == "needle"

    @pytest.mark.asyncio
    async def test_status_line_shows_the_default_modes(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                status = str(app.query_one("#search-status", Static).render())
                assert "literal" in status
                assert "message text" in status
                assert app._transcript_mode is ContentMode.TEXT


class TestTranscriptSearchModeToggles:
    @pytest.mark.asyncio
    async def test_ctrl_r_toggles_regex(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_toggle_search_regex()
                await pilot.pause()
                assert app._transcript_regex is True
                assert "regex" in str(app.query_one("#search-status", Static).render())
                await app.action_toggle_search_regex()
                await pilot.pause()
                assert app._transcript_regex is False

    @pytest.mark.asyncio
    async def test_ctrl_t_toggles_content_mode(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_toggle_search_mode()
                await pilot.pause()
                assert app._transcript_mode is ContentMode.RAW
                assert "raw" in str(app.query_one("#search-status", Static).render())
                await app.action_toggle_search_mode()
                await pilot.pause()
                assert app._transcript_mode is ContentMode.TEXT

    @pytest.mark.asyncio
    async def test_content_mode_sticks_across_reopening_the_view(self) -> None:
        """`ctrl+t` is a preference for the session, not for one visit."""
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_toggle_search_mode()
                await app.action_go_back()
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                assert app._transcript_mode is ContentMode.RAW

    @pytest.mark.asyncio
    async def test_toggles_are_ignored_off_the_search_view(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test():
                await app.action_toggle_search_regex()
                await app.action_toggle_search_mode()
                assert app._transcript_regex is False
                assert app._transcript_mode is ContentMode.TEXT

    @pytest.mark.asyncio
    async def test_toggling_clears_stale_results(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                assert len(app.query_one("#search-results", ListView)) == 1
                await app.action_toggle_search_regex()
                await pilot.pause()
                assert app._search_hits == []
                assert len(app.query_one("#search-results", ListView)) == 0


class TestTranscriptSearchBatches:
    @pytest.mark.asyncio
    async def test_batch_appends_rows_and_reports_progress(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(
                    app._search_token, 10, 40, (_make_hit("aaa"), _make_hit("bbb"))
                )
                results = app.query_one("#search-results", ListView)
                assert len(results) == 2
                status = str(app.query_one("#search-status", Static).render())
                assert "10/40" in status
                assert "2 found" in status
                # First row is highlighted so Enter works straight away.
                assert results.index == 0

    @pytest.mark.asyncio
    async def test_final_batch_reports_a_summary(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 7, 7, (_make_hit(),))
                status = str(app.query_one("#search-status", Static).render())
                assert "1 session of 7 matched" in status

    @pytest.mark.asyncio
    async def test_no_transcripts_is_reported(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 0, 0, ())
                status = str(app.query_one("#search-status", Static).render())
                assert "no transcripts" in status

    @pytest.mark.asyncio
    async def test_stale_batches_are_dropped(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token - 1, 5, 5, (_make_hit(),))
                assert len(app.query_one("#search-results", ListView)) == 0

    @pytest.mark.asyncio
    async def test_batches_are_dropped_after_leaving_the_view(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                token = app._search_token
                await app.action_go_back()
                await pilot.pause()
                app._apply_search_batch(token, 5, 5, (_make_hit(),))
                assert app._search_hits == []

    @pytest.mark.asyncio
    async def test_row_shows_match_count_branch_and_snippet(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(
                    app._search_token, 1, 1, (_make_hit("abcdef12", matches=3),)
                )
                await pilot.pause()
                item = app.query_one("#search-results", ListView).children[0]
                text = " ".join(str(label.render()) for label in item.query(Label))
                assert "wt" in text
                assert "3 matches" in text
                assert "worktree/x" in text
                assert "needle" in text

    @pytest.mark.asyncio
    async def test_single_match_is_not_pluralised(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(
                    app._search_token, 1, 1, (_make_hit(matches=1),)
                )
                await pilot.pause()
                item = app.query_one("#search-results", ListView).children[0]
                text = " ".join(str(label.render()) for label in item.query(Label))
                assert "1 match" in text
                assert "1 matches" not in text

    @pytest.mark.asyncio
    async def test_long_snippets_are_truncated_to_the_panel_width(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                long_hit = _make_hit(
                    matches=1,
                    snippets=(Snippet(text="z" * 500, spans=((0, 1),)),),
                )
                app._apply_search_batch(app._search_token, 1, 1, (long_hit,))
                await pilot.pause()
                item = app.query_one("#search-results", ListView).children[0]
                snippet_label = str(item.query(Label)[1].render())
                assert snippet_label.endswith("…")
                assert len(snippet_label) < 500


class TestTranscriptSearchQuery:
    @pytest.mark.asyncio
    async def test_short_query_does_not_start_a_scan(self) -> None:
        app = SessionApp()
        with (
            _patch_git_info(),
            patch("fujimoto.cli.claude_search.list_session_logs") as mock_list,
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "n"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                mock_list.assert_not_called()
                status = str(app.query_one("#search-status", Static).render())
                assert "at least 2 characters" in status

    @pytest.mark.asyncio
    async def test_keystrokes_are_debounced_into_one_scan(self) -> None:
        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.claude_search.list_session_logs", return_value=[]
            ) as mock_list,
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                search_input = app.query_one("#search-input", Input)
                for value in ("ne", "nee", "need", "needle"):
                    search_input.value = value
                    await pilot.pause()
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert mock_list.call_count == 1

    @pytest.mark.asyncio
    async def test_invalid_regex_is_reported_without_crashing(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._transcript_regex = True
                app.query_one("#search-input", Input).value = "(unclosed"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                status = str(app.query_one("#search-status", Static).render())
                assert "invalid regex" in status

    @pytest.mark.asyncio
    async def test_enter_moves_focus_to_the_results(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                app.query_one("#search-input", Input).focus()
                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is not None
                assert app.focused.id == "search-results"


class TestTranscriptSearchEndToEnd:
    @pytest.mark.asyncio
    async def test_matching_transcript_becomes_a_result_row(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_transcript(projects / "-fake-repo", "sess-a", "the needle is here")
        _write_transcript(projects / "-fake-repo", "sess-b", "only hay in this one")

        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ),
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "needle"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert len(app._search_hits) == 1
                assert app._search_hits[0].session.session_id == "sess-a"
                status = str(app.query_one("#search-status", Static).render())
                assert "1 session of 2 matched" in status

    @pytest.mark.asyncio
    async def test_default_mode_excludes_json_scaffolding_and_ctrl_t_finds_it(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_transcript(projects / "-fake-repo", "sess-a", "nothing relevant")

        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ),
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                # `gitBranch` exists only as a JSON key, so the default
                # message-text mode must not match it.
                app.query_one("#search-input", Input).value = "gitBranch"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app._search_hits == []

                # ctrl+t drops to raw, which sees the scaffolding.
                await app.action_toggle_search_mode()
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert len(app._search_hits) == 1

    @pytest.mark.asyncio
    async def test_worktree_transcripts_are_searched_too(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        worktree = tmp_path / "wt" / "20260826-thing"
        _write_transcript(
            projects / str(worktree).replace("/", "-"), "wt-sess", "needle in worktree"
        )

        app = SessionApp()
        with (
            _patch_git_info(worktrees=[worktree]),
            patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ),
        ):
            async with app.run_test() as pilot:
                # `_patch_git_info` materializes worktrees under its own root,
                # so point the app at the path the transcript was written for.
                app._existing_worktrees = [worktree]
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "needle"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert [h.session.session_id for h in app._search_hits] == ["wt-sess"]


class TestTranscriptSearchSelection:
    @pytest.mark.asyncio
    async def test_selecting_a_result_opens_the_session_actions_menu(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit("abc123"),))
                results = app.query_one("#search-results", ListView)
                results.focus()
                results.index = 0
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.query("#session-actions")) == 1
                assert app._actions_from_search is True
                assert app._selected_session is not None
                assert app._selected_session.claude_session_id == "abc123"
                assert app._selected_session.session_type == "claude"
                assert app._selected_session.path == Path("/repo/wt")

    @pytest.mark.asyncio
    async def test_cancel_returns_to_the_results(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                await app._show_session_actions(
                    app._search_result_map["sr-0"], from_search=True
                )
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                actions.index = [
                    i
                    for i, item in enumerate(actions.children)
                    if item.id == "sa-cancel"
                ][0]
                await pilot.press("enter")
                await pilot.pause()
                assert app._on_search is True
                # The hits are re-rendered rather than rescanned.
                assert len(app.query_one("#search-results", ListView)) == 1
                status = str(app.query_one("#search-status", Static).render())
                assert "1 session matched" in status

    @pytest.mark.asyncio
    async def test_escape_from_actions_returns_to_the_results(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                await app._show_session_actions(
                    app._search_result_map["sr-0"], from_search=True
                )
                await pilot.pause()
                await app.action_go_back()
                await pilot.pause()
                assert app._on_search is True
                assert len(app.query_one("#search-results", ListView)) == 1

    @pytest.mark.asyncio
    async def test_actions_opened_from_home_still_return_home(self) -> None:
        app = SessionApp()
        session = SessionInfo(
            name="direct-1",
            session_type="direct",
            project="test-proj",
            path=Path("/fake/repo"),
            tmux_session="test-proj/direct-1",
            is_active=True,
            branch="feat/test",
        )
        with _patch_git_info(sessions=["test-proj/direct-1"]):
            async with app.run_test() as pilot:
                await app._show_session_actions(session)
                await pilot.pause()
                assert app._actions_from_search is False
                await app.action_go_back()
                await pilot.pause()
                assert len(app.query("#home-list")) == 1

    @pytest.mark.asyncio
    async def test_resuming_a_result_launches_with_its_session_id(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit("xyz789"),))
                await app._show_session_actions(
                    app._search_result_map["sr-0"], from_search=True
                )
                await pilot.pause()
                actions = app.query_one("#session-actions", ListView)
                actions.index = [
                    i
                    for i, item in enumerate(actions.children)
                    if item.id == "sa-resume"
                ][0]
                await pilot.press("enter")
                await pilot.pause()
        assert app._launch_target is not None
        assert app._launch_target.resume_session_id == "xyz789"
        assert app._launch_target.working_dir == Path("/repo/wt")


class TestHomeFilterCaching:
    @pytest.mark.asyncio
    async def test_filter_keystrokes_do_not_reparse_transcripts(self) -> None:
        """The `/` filter must not re-read every JSONL log per keystroke."""
        calls: list[Path] = []

        def _sessions_for(path: Path) -> list[ClaudeSession]:
            calls.append(path)
            return []

        app = SessionApp()
        with _patch_git_info(claude_sessions_fn=_sessions_for):
            async with app.run_test() as pilot:
                await pilot.pause()
                app._stop_polling()  # keep the 3s poller out of the count
                baseline = len(calls)
                await pilot.press("slash")
                await pilot.pause()
                search = app.query_one("#home-search", Input)
                for value in ("a", "ab", "abc"):
                    search.value = value
                    await pilot.pause()
                assert len(calls) == baseline

    @pytest.mark.asyncio
    async def test_reopening_home_refetches_transcripts(self) -> None:
        calls: list[Path] = []

        def _sessions_for(path: Path) -> list[ClaudeSession]:
            calls.append(path)
            return []

        app = SessionApp()
        with _patch_git_info(claude_sessions_fn=_sessions_for):
            async with app.run_test() as pilot:
                await pilot.pause()
                app._stop_polling()
                baseline = len(calls)
                await app._show_create_form()
                await app._show_home()
                app._stop_polling()
                await pilot.pause()
                assert len(calls) > baseline

    @pytest.mark.asyncio
    async def test_poller_refreshes_the_render_cache(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._claude_cache = None
                await app._poll_session_states()
                assert app._claude_cache is not None

    @pytest.mark.asyncio
    async def test_fork_marker_is_read_once_per_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260826-thing"
        wt.mkdir()
        app = SessionApp()
        with (
            _patch_git_info(worktrees=[wt]),
            patch("fujimoto.cli._is_fork_worktree", return_value=False) as mock_fork,
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                app._stop_polling()
                baseline = mock_fork.call_count
                assert baseline >= 1
                await app._refresh_home_list()
                await app._refresh_home_list()
                await pilot.pause()
                assert mock_fork.call_count == baseline


class TestTranscriptSearchGuards:
    @pytest.mark.asyncio
    async def test_action_is_a_no_op_off_the_home_screen(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await app._show_create_form()
                await pilot.pause()
                await app.action_session_search()
                await pilot.pause()
                assert app._on_search is False
                assert len(app.query("#search-input")) == 0

    @pytest.mark.asyncio
    async def test_restore_does_not_rescan_the_same_query(self) -> None:
        """Remounting the box with a preserved query must not wipe the hits."""
        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.cli.claude_search.list_session_logs", return_value=[]
            ) as mock_list,
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._transcript_query = "needle"
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                await pilot.pause()
                await app._show_session_search(restore=True)
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert len(app._search_hits) == 1
                mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_arriving_after_the_list_is_gone_is_ignored(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                # Still "on search" as far as the flags go, but the widget has
                # been removed — as happens between a teardown and its batch.
                await app.query_one("#search-results").remove()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                assert app._search_hits == []

    @pytest.mark.asyncio
    async def test_stale_regex_error_is_not_shown(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._search_failed(app._search_token - 1, "invalid regex: nope")
                status = str(app.query_one("#search-status", Static).render())
                assert "invalid regex" not in status


# -- Snippet highlighting --


def _styled(content) -> list[tuple[str, str]]:
    """(text, style) pairs of a Content, in reading order."""
    return [(content.plain[sp.start : sp.end], sp.style) for sp in content.spans]


class TestRenderSnippet:
    def test_match_is_highlighted_and_context_is_dimmed(self) -> None:
        content = _render_snippet(Snippet(text="say needle now", spans=((4, 10),)), 80)
        assert content.plain == "say needle now"
        assert _styled(content) == [
            ("say ", "dim"),
            ("needle", SNIPPET_MATCH_STYLE),
            (" now", "dim"),
        ]

    def test_every_span_is_highlighted(self) -> None:
        content = _render_snippet(
            Snippet(text="needle and needle", spans=((0, 6), (11, 17))), 80
        )
        highlighted = [
            t for t, style in _styled(content) if style == SNIPPET_MATCH_STYLE
        ]
        assert highlighted == ["needle", "needle"]

    def test_match_at_the_start_emits_no_leading_context(self) -> None:
        content = _render_snippet(Snippet(text="needle b", spans=((0, 6),)), 80)
        assert _styled(content)[0] == ("needle", SNIPPET_MATCH_STYLE)

    def test_match_at_the_end_emits_no_trailing_context(self) -> None:
        content = _render_snippet(Snippet(text="a needle", spans=((2, 8),)), 80)
        assert _styled(content)[-1] == ("needle", SNIPPET_MATCH_STYLE)

    def test_snippet_with_no_spans_is_all_dim(self) -> None:
        content = _render_snippet(Snippet(text="no match here", spans=()), 80)
        assert _styled(content) == [("no match here", "dim")]

    def test_empty_snippet_renders_nothing(self) -> None:
        assert _render_snippet(Snippet(text="", spans=()), 80).plain == ""

    def test_text_is_never_parsed_as_markup(self) -> None:
        """Transcript bytes are full of brackets; none of it may become a tag."""
        raw = '{"a": [needle]}'
        content = _render_snippet(Snippet(text=raw, spans=((7, 13),)), 80)
        assert content.plain == raw
        assert ("needle", SNIPPET_MATCH_STYLE) in _styled(content)

    def test_a_fragment_ending_in_a_bracket_keeps_its_styling(self) -> None:
        """The failure mode markup splicing has: `[` swallows the closing tag."""
        content = _render_snippet(Snippet(text="x[needle", spans=((2, 8),)), 80)
        assert content.plain == "x[needle"
        assert _styled(content) == [("x[", "dim"), ("needle", SNIPPET_MATCH_STYLE)]

    def test_a_fragment_ending_in_a_backslash_keeps_its_styling(self) -> None:
        """The other markup-splicing failure mode: `\\` escapes the closing tag."""
        content = _render_snippet(Snippet(text="path\\ needle", spans=((6, 12),)), 80)
        assert content.plain == "path\\ needle"
        assert ("needle", SNIPPET_MATCH_STYLE) in _styled(content)


class TestFitSnippet:
    def test_short_snippet_is_untouched(self) -> None:
        snippet = Snippet(text="a needle b", spans=((2, 8),))
        assert _fit_snippet(snippet, 80) == ("a needle b", ((2, 8),))

    def test_match_beyond_the_width_is_kept_on_screen(self) -> None:
        """A plain right-truncation would cut the match off entirely."""
        text = "x" * 100 + "needle" + "y" * 100
        fitted, spans = _fit_snippet(Snippet(text=text, spans=((100, 106),)), 40)
        assert len(fitted) <= 40
        assert len(spans) == 1
        start, end = spans[0]
        assert fitted[start:end] == "needle"

    def test_truncation_marks_both_elided_ends(self) -> None:
        text = "x" * 100 + "needle" + "y" * 100
        fitted, _ = _fit_snippet(Snippet(text=text, spans=((100, 106),)), 40)
        assert fitted.startswith("…")
        assert fitted.endswith("…")

    def test_match_near_the_start_keeps_the_left_edge(self) -> None:
        text = "needle" + "y" * 200
        fitted, spans = _fit_snippet(Snippet(text=text, spans=((0, 6),)), 40)
        assert not fitted.startswith("…")
        assert fitted.endswith("…")
        assert fitted[spans[0][0] : spans[0][1]] == "needle"

    def test_match_near_the_end_keeps_the_right_edge(self) -> None:
        text = "x" * 200 + "needle"
        fitted, spans = _fit_snippet(Snippet(text=text, spans=((200, 206),)), 40)
        assert fitted.startswith("…")
        assert not fitted.endswith("…")
        assert fitted[spans[0][0] : spans[0][1]] == "needle"

    def test_spans_scrolled_out_of_the_window_are_dropped(self) -> None:
        text = "needle" + "-" * 300 + "needle"
        fitted, spans = _fit_snippet(Snippet(text=text, spans=((0, 6), (306, 312))), 40)
        assert len(spans) == 1
        assert fitted[spans[0][0] : spans[0][1]] == "needle"

    def test_no_spans_still_truncates(self) -> None:
        fitted, spans = _fit_snippet(Snippet(text="z" * 200, spans=()), 40)
        assert len(fitted) <= 40
        assert spans == ()

    @pytest.mark.parametrize("width", [10, 20, 40, 80, 120])
    def test_every_span_remains_inside_the_fitted_text(self, width: int) -> None:
        """Whatever the window, a span must never index past the text."""
        import re

        text = "".join("needle" if i % 7 == 0 else "-" * 6 for i in range(40))
        spans = tuple((m.start(), m.end()) for m in re.finditer("needle", text))
        fitted, fitted_spans = _fit_snippet(Snippet(text=text, spans=spans), width)
        assert len(fitted) <= width
        for start, end in fitted_spans:
            assert 0 <= start < end <= len(fitted)
            assert fitted[start:end] == "needle"

    @pytest.mark.parametrize("width", [10, 20, 40, 80])
    def test_the_first_match_is_always_visible(self, width: int) -> None:
        text = "-" * 500 + "needle" + "-" * 500
        fitted, spans = _fit_snippet(Snippet(text=text, spans=((500, 506),)), width)
        assert spans, f"first match lost at width {width}"
        assert fitted[spans[0][0] : spans[0][1]] == "needle"


class TestHighlightedResultRows:
    @pytest.mark.asyncio
    async def test_result_row_highlights_the_match(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                await pilot.pause()
                item = app.query_one("#search-results", ListView).children[0]
                content = item.query(Label)[1].render()
                assert content.plain == _SNIPPET_TEXT
                # Resolve through a real render so the assertion covers the
                # theme lookup of `$warning`, not just the style string.
                segments = [
                    (seg.text, seg.style)
                    for seg in Console().render(content)
                    if seg.text.strip()
                ]
                highlighted = [t for t, style in segments if style and style.bold]
                assert highlighted == ["needle"]
                for text, style in segments:
                    if text == "needle":
                        # The highlight must not inherit the surrounding dim,
                        # which would wash its colour out.
                        assert not style.dim
                        assert style.color is not None
                    else:
                        assert style.dim

    @pytest.mark.asyncio
    async def test_end_to_end_scan_highlights_the_typed_term(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_transcript(projects / "-fake-repo", "sess-a", "the NeEdLe is here")

        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ),
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "needle"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert len(app._search_hits) == 1
                snippet = app._search_hits[0].snippets[0]
                start, end = snippet.spans[0]
                # Matching is case-insensitive, so the span covers the text as
                # it was written, not as it was typed.
                assert snippet.text[start:end] == "NeEdLe"


class TestTranscriptSearchCaseToggle:
    @pytest.mark.asyncio
    async def test_default_is_case_insensitive(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                assert app._transcript_case_sensitive is False
                assert "ignore case" in str(
                    app.query_one("#search-status", Static).render()
                )

    @pytest.mark.asyncio
    async def test_ctrl_i_toggles_case_sensitivity(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await pilot.press("ctrl+i")
                await pilot.pause()
                assert app._transcript_case_sensitive is True
                assert "match case" in str(
                    app.query_one("#search-status", Static).render()
                )
                await pilot.press("ctrl+i")
                await pilot.pause()
                assert app._transcript_case_sensitive is False

    @pytest.mark.asyncio
    async def test_tab_does_not_toggle_case(self) -> None:
        """Ctrl+I shares a byte with Tab; a Tab must not flip the mode."""
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()
                assert app._transcript_case_sensitive is False

    @pytest.mark.asyncio
    async def test_toggle_is_ignored_off_the_search_view(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test():
                await app.action_toggle_search_case()
                assert app._transcript_case_sensitive is False

    @pytest.mark.asyncio
    async def test_toggling_case_clears_stale_results(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app._apply_search_batch(app._search_token, 1, 1, (_make_hit(),))
                await pilot.pause()
                await app.action_toggle_search_case()
                await pilot.pause()
                assert app._search_hits == []

    @pytest.mark.asyncio
    async def test_case_choice_sticks_across_reopening_the_view(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_toggle_search_case()
                await app.action_go_back()
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                assert app._transcript_case_sensitive is True

    @pytest.mark.asyncio
    async def test_all_three_modes_are_shown_together(self) -> None:
        app = SessionApp()
        with _patch_git_info():
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                await app.action_toggle_search_regex()
                await app.action_toggle_search_mode()
                await app.action_toggle_search_case()
                await pilot.pause()
                status = str(app.query_one("#search-status", Static).render())
                assert "regex" in status
                assert "raw" in status
                assert "match case" in status

    @pytest.mark.asyncio
    async def test_case_sensitive_scan_excludes_a_differently_cased_hit(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_transcript(projects / "-fake-repo", "sess-a", "the NeEdLe is here")

        app = SessionApp()
        with (
            _patch_git_info(),
            patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ),
        ):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                app.query_one("#search-input", Input).value = "needle"
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert len(app._search_hits) == 1

                await app.action_toggle_search_case()
                await pilot.pause(app.SEARCH_DEBOUNCE * 2)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app._search_hits == []


def _write_log(path: Path, entries: list[dict]) -> Path:
    """Write a minimal Claude JSONL log and return its path."""
    import json

    lines = []
    for entry in entries:
        entry.setdefault("timestamp", "2026-03-09T12:00:00.000Z")
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines))
    return path


def _log_session(
    log: Path, cwd: Path, session_id: str = "abc12345", minute: int = 0
) -> ClaudeSession:
    return ClaudeSession(
        jsonl_path=log,
        session_id=session_id,
        state=SessionState.IDLE,
        last_entry_type=EntryType.ASSISTANT,
        stop_reason=StopReason.END_TURN,
        cwd=cwd,
        git_branch="worktree/20260309-test",
        last_activity=datetime(2026, 3, 9, 12, minute, 0, tzinfo=timezone.utc),
        title="A chat",
        first_prompt="Do the thing",
    )


class TestSessionLogViewer:
    """Reading a transcript back without starting Claude."""

    @staticmethod
    def _action_ids(app: SessionApp) -> list[str]:
        return [i.id for i in app.query_one("#session-actions", ListView).children]

    async def _open_actions(self, app: SessionApp, pilot, item_id: str) -> None:
        home_list = app.query_one("#home-list", ListView)
        for i, item in enumerate(home_list.children):
            if item.id == item_id:
                home_list.index = i
                break
        await pilot.press("enter")
        await pilot.pause()

    async def _select(self, app: SessionApp, pilot, list_id: str, item_id: str) -> None:
        lst = app.query_one(list_id, ListView)
        for i, item in enumerate(lst.children):
            if item.id == item_id:
                lst.index = i
                break
        await pilot.press("enter")
        await pilot.pause()

    @staticmethod
    def _body_texts(app: SessionApp) -> list[str]:
        """Prose bodies only — a collapsible's body is nested, not a child."""
        return [str(w.visual) for w in app.query("#log-panel > .log-body")]

    @staticmethod
    def _tool_body_texts(app: SessionApp) -> list[str]:
        return [str(w.visual) for w in app.query("Collapsible .log-body")]

    @pytest.mark.asyncio
    async def test_menu_item_sits_between_resume_and_terminal(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_fake_claude_session(wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                ids = self._action_ids(app)
                assert ids.index("sa-resume-picker") < ids.index("sa-viewlog")
                assert ids.index("sa-viewlog") < ids.index("sa-terminal")

    @pytest.mark.asyncio
    async def test_absent_without_previous_sessions(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(sessions=["test-proj/20260309-test"], worktrees=[wt]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                assert "sa-viewlog" not in self._action_ids(app)

    @pytest.mark.asyncio
    async def test_single_transcript_opens_directly(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [
                {"type": "user", "message": {"content": "Do the thing [brackets]"}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "On it."}]},
                },
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                assert len(app.query("#log-panel")) == 1
                assert len(app.query("#log-picker")) == 0
                # Markup in message bodies is escaped, not interpreted.
                assert self._body_texts(app) == ["Do the thing [brackets]", "On it."]
                # Nothing was launched — this is a read-only view.
                assert app._launch_target is None

    @pytest.mark.asyncio
    async def test_escape_returns_home(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "hi"}}]
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                await pilot.press("escape")
                await pilot.pause()
                assert len(app.query("#home-list")) == 1

    @pytest.mark.asyncio
    async def test_empty_transcript_says_so(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(tmp_path / "s.jsonl", [{"type": "file-history-snapshot"}])
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                rendered = " ".join(
                    str(w.visual) for w in app.query("#log-panel Static")
                )
                assert "no messages" in rendered

    @pytest.mark.asyncio
    async def test_unreadable_log_shows_error(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(tmp_path / "gone.jsonl", wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                assert len(app.query("#log-panel")) == 0
                assert "Error:" in " ".join(
                    str(w.visual) for w in app.query("#main > Static")
                )

    @pytest.mark.asyncio
    async def test_multiple_transcripts_show_picker(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        first = _write_log(
            tmp_path / "a.jsonl", [{"type": "user", "message": {"content": "first"}}]
        )
        second = _write_log(
            tmp_path / "b.jsonl", [{"type": "user", "message": {"content": "second"}}]
        )
        candidates = [
            _log_session(first, wt, "aaa11111", minute=1),
            _log_session(second, wt, "bbb22222", minute=0),
        ]
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: candidates,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                assert len(app.query("#log-picker")) == 1
                await self._select(app, pilot, "#log-picker", "lp-1")
                assert self._body_texts(app) == ["second"]

    @pytest.mark.asyncio
    async def test_picker_cancel_returns_to_actions(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "a.jsonl", [{"type": "user", "message": {"content": "hi"}}]
        )
        candidates = [
            _log_session(log, wt, "aaa11111", minute=1),
            _log_session(log, wt, "bbb22222", minute=0),
        ]
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: candidates,
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                await self._select(app, pilot, "#log-picker", "lp-cancel")
                assert "sa-viewlog" in self._action_ids(app)

    @pytest.mark.asyncio
    async def test_claude_row_opens_its_own_transcript(self, tmp_path: Path) -> None:
        log = _write_log(
            tmp_path / "s.jsonl",
            [{"type": "user", "message": {"content": "from the claude row"}}],
        )
        cs = _log_session(log, tmp_path, "ccc33333")
        with _patch_git_info(claude_sessions_fn=lambda _p: [cs]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, f"cs-{cs.session_id}")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                assert self._body_texts(app) == ["from the claude row"]

    @pytest.mark.asyncio
    async def test_claude_row_without_matching_log_errors(self, tmp_path: Path) -> None:
        log = _write_log(
            tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "hi"}}]
        )
        cs = _log_session(log, tmp_path, "ccc33333")
        with _patch_git_info(claude_sessions_fn=lambda _p: [cs]):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, f"cs-{cs.session_id}")
                # The row's transcript disappears between listing and viewing.
                with patch("fujimoto.cli.get_sessions_for_path", return_value=[]):
                    await self._select(app, pilot, "#session-actions", "sa-viewlog")
                assert "Session log not found" in " ".join(
                    str(w.visual) for w in app.query("#main > Static")
                )

    @pytest.mark.asyncio
    async def test_tool_calls_collapse_to_one_line(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "On it."},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "grep -rn needle src/"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": "a.txt\nb.txt\nc.txt"}
                        ]
                    },
                },
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")

                collapsibles = list(app.query(Collapsible))
                # The result is folded into its call, so there is one row, not two.
                assert len(collapsibles) == 1
                # Folded away by default — the prose is what the viewer is for.
                assert collapsibles[0].collapsed is True
                assert collapsibles[0].title == "⚒ Bash  command: grep -rn needle src/"
                # Only the prose is rendered as an ordinary body row.
                assert self._body_texts(app) == ["On it."]
                # Opening the call shows its arguments and then its output.
                assert self._tool_body_texts(app) == [
                    "command: grep -rn needle src/",
                    "a.txt\nb.txt\nc.txt",
                ]

    @pytest.mark.asyncio
    async def test_expanded_tool_body_drops_the_repeated_name(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ]
                    },
                }
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_actions(app, pilot, "wt-20260309-test")
                await self._select(app, pilot, "#session-actions", "sa-viewlog")
                # Tab reaches the collapsible's title, Enter opens it — the
                # viewer stays usable without a mouse.
                await pilot.press("tab")
                await pilot.press("enter")
                await pilot.pause()
                assert app.query(Collapsible).first().collapsed is False
                # The title already says "Bash"; the body carries the arguments.
                assert self._tool_body_texts(app) == ["command: ls"]

    def test_result_summary_is_singular_for_one_line(self) -> None:
        msg = TranscriptMessage(
            "tool_result", "just one", datetime(2026, 3, 9, tzinfo=timezone.utc)
        )
        assert _tool_summary(msg).startswith("↳ Result (1 line)")

    def test_tool_summary_without_arguments_is_just_the_name(self) -> None:
        msg = TranscriptMessage(
            "tool_use", "Read", datetime(2026, 3, 9, tzinfo=timezone.utc)
        )
        assert _tool_summary(msg) == "⚒ Read"

    def test_claude_label_is_purple(self) -> None:
        # The role label's colour is a deliberate choice, not a theme default.
        assert ".log-assistant {\n    color: #a78bfa;\n}" in CSS

    @staticmethod
    def _tool_run(count: int, names: list[str] | None = None) -> list[dict]:
        """A run of `count` tool calls, each followed by its result."""
        names = names or ["Bash"]
        entries: list[dict] = []
        for i in range(count):
            entries.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": names[i % len(names)],
                                "input": {"command": f"step {i}"},
                            }
                        ]
                    },
                }
            )
            entries.append(
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": f"out {i}"}]
                    },
                }
            )
        return entries

    async def _open_log(self, app: SessionApp, pilot) -> None:
        await self._open_actions(app, pilot, "wt-20260309-test")
        await self._select(app, pilot, "#session-actions", "sa-viewlog")

    @pytest.mark.asyncio
    async def test_a_run_of_calls_folds_into_one_row(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [{"type": "user", "message": {"content": "go"}}]
            + self._tool_run(10, ["Bash", "Read", "Edit"])
            + [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                }
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_log(app, pilot)

                # Twenty tool messages, but only one row at the top level.
                runs = list(app.query(".log-tool-run"))
                assert len(runs) == 1
                run = runs[0]
                assert isinstance(run, Collapsible)
                assert run.collapsed is True
                assert run.title == "⚒ 10 tool calls  Bash, Read, Edit"
                # The prose either side is untouched.
                assert self._body_texts(app) == ["go", "Done."]

                # Opening the run reveals the calls, each still folded.
                run.collapsed = False
                await pilot.pause()
                nested = list(run.query(".log-tool"))
                # Ten calls, not ten calls plus ten results.
                assert len(nested) == 10
                assert all(c.collapsed for c in nested)

    @pytest.mark.asyncio
    async def test_a_lone_call_is_not_wrapped(self, tmp_path: Path) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(tmp_path / "s.jsonl", self._tool_run(1))
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_log(app, pilot)
                # Wrapping a single row in another would only add a keystroke.
                assert len(app.query(".log-tool-run")) == 0
                assert len(app.query(".log-tool")) == 1

    @pytest.mark.asyncio
    async def test_runs_are_split_by_the_prose_between_them(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            self._tool_run(2)
            + [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Halfway."}]},
                }
            ]
            + self._tool_run(3),
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_log(app, pilot)
                runs = list(app.query(".log-tool-run"))
                assert [r.title for r in runs] == [
                    "⚒ 2 tool calls  Bash",
                    "⚒ 3 tool calls  Bash",
                ]

    def test_run_title_caps_the_tool_names_it_lists(self) -> None:
        ts = datetime(2026, 3, 9, tzinfo=timezone.utc)
        run = [
            TranscriptMessage("tool_use", name, ts)
            for name in ("Bash", "Read", "Edit", "Grep", "Write", "Glob")
        ]
        assert _tool_run_title(run) == "⚒ 6 tool calls  Bash, Read, Edit, Grep, +2 more"

    @pytest.mark.asyncio
    async def test_parallel_calls_pair_with_their_own_results(
        self, tmp_path: Path
    ) -> None:
        # Parallel calls arrive as several tool_use blocks followed by several
        # results, so position alone would pair each call with the wrong reply.
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Read",
                                "input": {"path": "a.py"},
                            },
                            {
                                "type": "tool_use",
                                "id": "t2",
                                "name": "Grep",
                                "input": {"pattern": "needle"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t2",
                                "content": "grep output",
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "read output",
                            },
                        ]
                    },
                },
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_log(app, pilot)
                calls = list(app.query(".log-tool-run")[0].query(".log-tool"))
                assert [c.title for c in calls] == [
                    "⚒ Read  path: a.py",
                    "⚒ Grep  pattern: needle",
                ]
                # Results are matched by tool_use_id, not by arrival order.
                assert [str(w.visual) for w in calls[0].query(".log-body")] == [
                    "path: a.py",
                    "read output",
                ]
                assert [str(w.visual) for w in calls[1].query(".log-body")] == [
                    "pattern: needle",
                    "grep output",
                ]

    @pytest.mark.asyncio
    async def test_an_unclaimed_result_still_gets_a_row(self, tmp_path: Path) -> None:
        # A transcript can open mid-flight, with a reply whose call was never
        # recorded. Dropping it would silently lose transcript content.
        wt = tmp_path / "20260309-test"
        log = _write_log(
            tmp_path / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": "orphaned output"}
                        ]
                    },
                }
            ],
        )
        with _patch_git_info(
            sessions=["test-proj/20260309-test"],
            worktrees=[wt],
            claude_sessions_fn=lambda _p: [_log_session(log, wt)],
        ):
            app = SessionApp()
            async with app.run_test() as pilot:
                await self._open_log(app, pilot)
                rows = list(app.query(".log-tool"))
                assert len(rows) == 1
                assert rows[0].title.startswith("↳ Result (1 line)")
                assert self._tool_body_texts(app) == ["orphaned output"]

    def test_results_pair_positionally_when_a_log_has_no_ids(self) -> None:
        ts = datetime(2026, 3, 9, tzinfo=timezone.utc)
        run = [
            TranscriptMessage("tool_use", "Bash\ncommand: one", ts),
            TranscriptMessage("tool_result", "first out", ts),
            TranscriptMessage("tool_use", "Bash\ncommand: two", ts),
            TranscriptMessage("tool_result", "second out", ts),
        ]
        pairs = _pair_results(run)
        assert [(call.text.splitlines()[1], result.text) for call, result in pairs] == [
            ("command: one", "first out"),
            ("command: two", "second out"),
        ]

    def test_a_call_with_no_result_pairs_with_none(self) -> None:
        ts = datetime(2026, 3, 9, tzinfo=timezone.utc)
        run = [TranscriptMessage("tool_use", "Bash\ncommand: hung", ts, "t1")]
        assert _pair_results(run) == [(run[0], None)]
