from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from textual.widgets import Input, ListView

from fujimoto.claude import ClaudeSession, SessionState
from fujimoto.claude.log_parser import EntryType, StopReason
from fujimoto.cli import (
    ICON_EYES,
    ICON_GEAR,
    ICON_SHIELD,
    SessionApp,
    _claude_state_label,
    _get_claude_sessions,
    _relative_time,
    main,
)
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
):
    """Return a context manager that patches git/tmux info for TUI tests."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        worktree_root = None
        if worktrees is not None:
            # We need the root to exist for iterdir
            import tempfile

            _tmpdir = tempfile.mkdtemp()
            worktree_root = Path(_tmpdir) / project
            worktree_root.mkdir(parents=True)
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
            patch(
                "fujimoto.cli.list_projects",
                return_value=projects or [],
            ),
            patch(
                "fujimoto.cli.get_sessions_for_path",
                side_effect=claude_sessions_fn or (lambda _path: []),
            ),
        ):
            yield

    return _ctx()


# -- main() tests --


class TestMain:
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
        app1._launch_target = ("proj", Path("/tmp/test"), None, "worktree", None)
        app2 = SessionApp.__new__(SessionApp)
        app2._launch_target = None

        with (
            patch("fujimoto.cli._check_prerequisites", return_value=[]),
            patch("fujimoto.cli.SessionApp", side_effect=[app1, app2]),
            patch.object(app1, "run"),
            patch.object(app2, "run"),
            patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
            patch("fujimoto.cli._build_system_prompt", return_value="test") as mock_sp,
            patch("fujimoto.cli._session_terminal_title", return_value="test-title"),
        ):
            main()
            mock_sp.assert_called_once_with("worktree", "proj", Path("/tmp/test"))
            mock_launch.assert_called_once_with(
                "proj",
                Path("/tmp/test"),
                None,
                system_prompt="test",
                resume_session_id=None,
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
        app1._launch_target = (
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
            )


class TestBuildSystemPrompt:
    def test_worktree_prompt_includes_base_branch(self, tmp_path: Path) -> None:
        from fujimoto.cli import _build_system_prompt
        from fujimoto.config import store_session_meta

        store_session_meta(tmp_path, "main")
        prompt = _build_system_prompt("worktree", "myproj", tmp_path)
        assert "worktree" in prompt
        assert "myproj" in prompt
        assert "main" in prompt
        assert "Do not push" in prompt

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
                project, working_dir, tmux_name, session_type, resume_id = (
                    app._launch_target
                )
                assert project == "adhoc"
                assert session_type == "adhoc"
                assert tmux_name == "adhoc-1"
                assert resume_id is None
                assert working_dir.exists()

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
                # "Terminate" is the second option
                await pilot.press("down")
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
                # First option for inactive is "Launch"
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
    async def test_open_terminal_calls_open_terminal(self) -> None:
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
                with patch("fujimoto.cli.open_terminal") as mock_open:
                    await pilot.press("enter")
                    await pilot.pause()
                    mock_open.assert_called_once()
                    # Should stay on session actions menu
                    assert len(app.query("#session-actions")) > 0

    @pytest.mark.asyncio
    async def test_open_terminal_error_shows_error(self) -> None:
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
                with patch(
                    "fujimoto.cli.open_terminal",
                    side_effect=OSError("osascript not found"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch") as mock_fetch,
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
                assert app._base_branch == "main"

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
                "fujimoto.cli.fetch_and_rebase_branch",
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
                # Should still create despite fetch failure
                mock_create.assert_called_once()

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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
                mock_meta.assert_called_once_with(tmp_path / "new-wt", "main")


class TestSessionAppConflict:
    @pytest.mark.asyncio
    async def test_shows_conflict_when_path_exists(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing-wt"
        existing.mkdir()
        with (
            _patch_git_info(current="main", default="main"),
            patch("fujimoto.cli.build_worktree_path", return_value=existing),
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
            patch("fujimoto.cli.fetch_and_rebase_branch"),
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
        app1._launch_target = (
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
        ):
            main()
            mock_launch.assert_called_once_with(
                "proj",
                Path("/tmp/repo"),
                "proj/direct-1",
                system_prompt=None,
                resume_session_id="resume-session-id",
            )
