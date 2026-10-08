from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import launch_options, session_state
from fujimoto.config import ConfigError, read_claude_args, store_session_meta
from fujimoto.git import GitError
from fujimoto.launch_options import LaunchOptionsError, Source


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    with (
        patch(
            "fujimoto.session_state._state_path",
            return_value=tmp_path / "cache" / "sessions.json",
        ),
        patch(
            "fujimoto.launch_options._cache_path",
            return_value=tmp_path / "cache" / "launch_options.json",
        ),
        # No real git: every directory reads as having no project default
        # unless a test says otherwise.
        patch(
            "fujimoto.launch_options.get_main_worktree_root",
            side_effect=GitError("not a repo"),
        ),
    ):
        yield


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    wt = tmp_path / name
    wt.mkdir()
    store_session_meta(wt, "main")
    return wt


def _project_default(args: list[str]):
    """Patch the project's `.fujimoto.yaml` default to `args`."""
    return patch("fujimoto.launch_options.project_default", return_value=tuple(args))


class TestParse:
    def test_splits_like_a_shell(self) -> None:
        assert launch_options.parse('--a "b c" d') == ("--a", "b c", "d")

    def test_unbalanced_quote_raises(self) -> None:
        with pytest.raises(LaunchOptionsError):
            launch_options.parse("--a 'b")

    def test_render_round_trips(self) -> None:
        args = ("--plugin-dir", "a b", "--x=$HOME")
        assert launch_options.parse(launch_options.render(args)) == args

    def test_source_labels(self) -> None:
        assert all(s.label for s in Source)


class TestResolve:
    def test_nothing_anywhere(self, tmp_path: Path) -> None:
        resolved = launch_options.resolve(tmp_path, "p/x")
        assert resolved == launch_options.Resolved((), Source.NONE)

    def test_project_default(self, tmp_path: Path) -> None:
        with _project_default(["--a"]):
            resolved = launch_options.resolve(tmp_path, "p/x")
        assert resolved == launch_options.Resolved(("--a",), Source.PROJECT)

    def test_worktree_override_beats_project(self, tmp_path: Path) -> None:
        wt = _worktree(tmp_path)
        launch_options.save(wt, "p/wt", None, ("--b",))
        with _project_default(["--a"]):
            resolved = launch_options.resolve(wt, "p/wt")
        assert resolved == launch_options.Resolved(("--b",), Source.SESSION)

    def test_explicit_empty_is_an_override(self, tmp_path: Path) -> None:
        wt = _worktree(tmp_path)
        with _project_default(["--a"]):
            launch_options.save(wt, "p/wt", None, ())
            resolved = launch_options.resolve(wt, "p/wt")
        assert resolved == launch_options.Resolved((), Source.SESSION)

    def test_parent_inherited(self, tmp_path: Path) -> None:
        parent = _worktree(tmp_path, "parent")
        child = _worktree(tmp_path, "child")
        launch_options.save(parent, "p/parent", None, ("--b",))
        resolved = launch_options.resolve(child, "p/child", parent_dir=parent)
        assert resolved == launch_options.Resolved(("--b",), Source.PARENT)

    def test_parent_without_options_falls_through(self, tmp_path: Path) -> None:
        parent = _worktree(tmp_path, "parent")
        child = _worktree(tmp_path, "child")
        resolved = launch_options.resolve(child, "p/child", parent_dir=parent)
        assert resolved.source is Source.NONE


class TestSave:
    def test_matching_the_project_default_clears(self, tmp_path: Path) -> None:
        wt = _worktree(tmp_path)
        launch_options.save(wt, "p/wt", None, ("--b",))
        with _project_default(["--a"]):
            launch_options.save(wt, "p/wt", None, ("--a",))
        assert read_claude_args(wt) is None

    def test_non_worktree_uses_record_and_conversation(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "p/direct-1", cwd=tmp_path, project="p", session_type="direct"
        )
        launch_options.save(tmp_path, "p/direct-1", "conv", ("--a",))
        assert session_state.load_state()["p/direct-1"].claude_args == ["--a"]
        assert launch_options.saved_args(tmp_path, None, "conv") == ("--a",)
        assert launch_options.saved_args(tmp_path, "p/direct-1") == ("--a",)
        # The record survives a relaunch.
        session_state.mark_open(
            "p/direct-1", cwd=tmp_path, project="p", session_type="direct"
        )
        assert launch_options.saved_args(tmp_path, "p/direct-1") == ("--a",)

    def test_non_worktree_never_grows_meta(self, tmp_path: Path) -> None:
        launch_options.save(tmp_path, "p/direct-1", None, ("--a",))
        assert not (tmp_path / ".fujimoto").exists()


class TestConversationCache:
    def test_clear(self, tmp_path: Path) -> None:
        launch_options.remember_for_conversation("c", ("--a",))
        launch_options.remember_for_conversation("c", None)
        assert launch_options.saved_args(tmp_path, None, "c") is None

    def test_clear_missing_is_a_no_op(self, tmp_path: Path) -> None:
        launch_options.remember_for_conversation("c", None)
        assert not (tmp_path / "cache" / "launch_options.json").exists()

    @pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"c": [1]}'])
    def test_corrupt_cache_reads_empty(self, tmp_path: Path, content: str) -> None:
        cache = tmp_path / "cache" / "launch_options.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(content)
        assert launch_options.saved_args(tmp_path, None, "c") is None

    def test_unwritable_cache_is_swallowed(self, tmp_path: Path) -> None:
        blocker = tmp_path / "cache"
        blocker.write_text("a file where the directory should be")
        launch_options.remember_for_conversation("c", ("--a",))  # no raise

    def test_unreadable_cache_is_swallowed(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache" / "launch_options.json"
        cache.mkdir(parents=True)  # a directory: read_text raises OSError
        assert launch_options.saved_args(tmp_path, None, "c") is None


class TestProjectDefault:
    def test_reads_main_clone_config(self, tmp_path: Path) -> None:
        (tmp_path / ".fujimoto.yaml").write_text("claude_args: --a b\n")
        with patch(
            "fujimoto.launch_options.get_main_worktree_root", return_value=tmp_path
        ):
            assert launch_options.project_default(tmp_path / "wt") == ("--a", "b")

    def test_malformed_config_contributes_nothing(self, tmp_path: Path) -> None:
        with (
            patch(
                "fujimoto.launch_options.get_main_worktree_root",
                return_value=tmp_path,
            ),
            patch(
                "fujimoto.launch_options.load_project_config",
                side_effect=ConfigError("bad"),
            ),
        ):
            assert launch_options.project_default(tmp_path) == ()


# Captured at import, before the autouse fixture replaces it.
_REAL_CACHE_PATH = launch_options._cache_path


def test_cache_lives_beside_the_session_state() -> None:
    with patch("pathlib.Path.home", return_value=Path("/h")):
        assert _REAL_CACHE_PATH() == Path("/h/.cache/fujimoto/launch_options.json")
