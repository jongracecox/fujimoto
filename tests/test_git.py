from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_worktree.git import (
    GitError,
    _run,
    create_worktree,
    get_current_branch,
    get_default_branch,
    get_project_name,
    get_repo_root,
)


class TestRun:
    def test_returns_stdout_stripped(self) -> None:
        result = _run(["rev-parse", "--is-inside-work-tree"])
        assert result == "true"

    def test_raises_git_error_on_failure(self) -> None:
        with pytest.raises(GitError):
            _run(["rev-parse", "--verify", "nonexistent-ref-abc123"])

    def test_raises_git_error_when_not_installed(self) -> None:
        with patch("claude_worktree.git.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(GitError, match="not installed"):
                _run(["status"])


class TestGetRepoRoot:
    def test_returns_path(self) -> None:
        root = get_repo_root()
        assert root.is_dir()
        assert (root / ".git").exists()

    def test_raises_outside_repo(self, tmp_path: Path) -> None:
        with patch(
            "claude_worktree.git.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                128, "git", stderr="not a git repository"
            ),
        ):
            with pytest.raises(GitError):
                get_repo_root()


class TestGetProjectName:
    def test_returns_basename(self) -> None:
        name = get_project_name()
        assert isinstance(name, str)
        assert len(name) > 0
        assert "/" not in name


class TestGetCurrentBranch:
    def test_returns_string(self) -> None:
        branch = get_current_branch()
        assert isinstance(branch, str)


class TestGetDefaultBranch:
    def test_returns_string(self) -> None:
        branch = get_default_branch()
        assert isinstance(branch, str)
        assert len(branch) > 0

    def test_uses_symbolic_ref_when_available(self) -> None:
        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return "refs/remotes/origin/develop"
            raise GitError("not found")

        with patch("claude_worktree.git._run", side_effect=mock_run):
            assert get_default_branch() == "develop"

    def test_falls_back_to_main_when_no_remote(self) -> None:
        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            raise GitError("not found")

        with patch("claude_worktree.git._run", side_effect=mock_run):
            result = get_default_branch()
            assert result == "main"

    def test_detects_main_branch(self) -> None:
        call_count = 0

        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                raise GitError("not found")
            if args == ["rev-parse", "--verify", "main"]:
                return "abc123"
            raise GitError("not found")

        with patch("claude_worktree.git._run", side_effect=mock_run):
            result = get_default_branch()
            assert result == "main"


class TestCreateWorktree:
    def test_raises_if_path_exists(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing"
        existing.mkdir()
        with pytest.raises(GitError, match="already exists"):
            create_worktree(existing, "main", "test-branch")

    def test_calls_git_worktree_add(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "new-worktree"
        with patch("claude_worktree.git._run") as mock_run:
            create_worktree(worktree_path, "main", "my-branch")
            mock_run.assert_called_once_with(
                ["worktree", "add", "-b", "my-branch", str(worktree_path), "main"],
                cwd=None,
            )

    def test_calls_git_worktree_add_with_cwd(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "new-worktree"
        project_dir = tmp_path / "my-repo"
        with patch("claude_worktree.git._run") as mock_run:
            create_worktree(worktree_path, "main", "my-branch", cwd=project_dir)
            mock_run.assert_called_once_with(
                ["worktree", "add", "-b", "my-branch", str(worktree_path), "main"],
                cwd=project_dir,
            )
