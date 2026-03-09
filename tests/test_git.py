from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.git import (
    GitError,
    _run,
    cherry_pick_branch,
    create_worktree,
    delete_branch,
    get_current_branch,
    get_default_branch,
    get_merge_base,
    get_project_name,
    get_repo_root,
    get_unpushed_commits,
    has_remote_branch,
    is_branch_merged,
    push_branch,
    remove_worktree,
)


class TestRun:
    def test_returns_stdout_stripped(self) -> None:
        result = _run(["rev-parse", "--is-inside-work-tree"])
        assert result == "true"

    def test_raises_git_error_on_failure(self) -> None:
        with pytest.raises(GitError):
            _run(["rev-parse", "--verify", "nonexistent-ref-abc123"])

    def test_raises_git_error_when_not_installed(self) -> None:
        with patch("fujimoto.git.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(GitError, match="not installed"):
                _run(["status"])


class TestGetRepoRoot:
    def test_returns_path(self) -> None:
        root = get_repo_root()
        assert root.is_dir()
        assert (root / ".git").exists()

    def test_raises_outside_repo(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.git.subprocess.run",
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

        with patch("fujimoto.git._run", side_effect=mock_run):
            assert get_default_branch() == "develop"

    def test_falls_back_to_main_when_no_remote(self) -> None:
        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            raise GitError("not found")

        with patch("fujimoto.git._run", side_effect=mock_run):
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

        with patch("fujimoto.git._run", side_effect=mock_run):
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
        with patch("fujimoto.git._run") as mock_run:
            create_worktree(worktree_path, "main", "my-branch")
            mock_run.assert_called_once_with(
                ["worktree", "add", "-b", "my-branch", str(worktree_path), "main"],
                cwd=None,
            )

    def test_calls_git_worktree_add_with_cwd(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "new-worktree"
        project_dir = tmp_path / "my-repo"
        with patch("fujimoto.git._run") as mock_run:
            create_worktree(worktree_path, "main", "my-branch", cwd=project_dir)
            mock_run.assert_called_once_with(
                ["worktree", "add", "-b", "my-branch", str(worktree_path), "main"],
                cwd=project_dir,
            )


class TestRemoveWorktree:
    def test_calls_git_worktree_remove(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "my-worktree"
        with patch("fujimoto.git._run") as mock_run:
            remove_worktree(wt_path)
            mock_run.assert_called_once_with(
                ["worktree", "remove", "--force", str(wt_path)], cwd=None
            )

    def test_passes_cwd(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "my-worktree"
        repo = tmp_path / "repo"
        with patch("fujimoto.git._run") as mock_run:
            remove_worktree(wt_path, cwd=repo)
            mock_run.assert_called_once_with(
                ["worktree", "remove", "--force", str(wt_path)], cwd=repo
            )


class TestGetUnpushedCommits:
    def test_returns_commits_when_remote_exists(self) -> None:
        with patch(
            "fujimoto.git._run", return_value="abc123 fix bug\ndef456 add feature"
        ):
            result = get_unpushed_commits("my-branch")
            assert len(result) == 2

    def test_returns_empty_when_up_to_date(self) -> None:
        with patch("fujimoto.git._run", return_value=""):
            result = get_unpushed_commits("my-branch")
            assert result == []

    def test_falls_back_to_merge_base_when_no_remote(self) -> None:
        call_count = 0

        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if "origin/" in str(args):
                raise GitError("no remote")
            if args[0] == "merge-base":
                return "abc123"
            if args[0] == "log":
                return "def456 some commit"
            # For get_default_branch
            if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                raise GitError("no remote")
            if args == ["rev-parse", "--verify", "main"]:
                return "abc123"
            raise GitError("not found")

        with patch("fujimoto.git._run", side_effect=mock_run):
            result = get_unpushed_commits("my-branch")
            assert len(result) == 1


class TestGetMergeBase:
    def test_returns_commit_hash(self) -> None:
        with patch("fujimoto.git._run", return_value="abc123") as mock_run:
            result = get_merge_base("my-branch")
            assert result == "abc123"
            # Should call merge-base with default branch
            assert mock_run.call_count >= 1


class TestIsBranchMerged:
    def test_returns_true_when_merged(self) -> None:
        with patch("fujimoto.git._run", return_value=""):
            assert is_branch_merged("feature", "main") is True

    def test_returns_false_when_not_merged(self) -> None:
        with patch("fujimoto.git._run", side_effect=GitError("not ancestor")):
            assert is_branch_merged("feature", "main") is False


class TestHasRemoteBranch:
    def test_returns_true_when_exists(self) -> None:
        with patch("fujimoto.git._run", return_value="abc123\trefs/heads/my-branch"):
            assert has_remote_branch("my-branch") is True

    def test_returns_false_when_missing(self) -> None:
        with patch("fujimoto.git._run", return_value=""):
            assert has_remote_branch("my-branch") is False

    def test_returns_false_on_error(self) -> None:
        with patch("fujimoto.git._run", side_effect=GitError("no remote")):
            assert has_remote_branch("my-branch") is False


class TestPushBranch:
    def test_calls_git_push(self) -> None:
        with patch("fujimoto.git._run") as mock_run:
            push_branch("my-branch")
            mock_run.assert_called_once_with(
                ["push", "-u", "origin", "my-branch"], cwd=None
            )


class TestDeleteBranch:
    def test_deletes_local_only(self) -> None:
        with patch("fujimoto.git._run") as mock_run:
            delete_branch("my-branch")
            mock_run.assert_called_once_with(["branch", "-D", "my-branch"], cwd=None)

    def test_deletes_local_and_remote(self) -> None:
        with patch("fujimoto.git._run") as mock_run:
            delete_branch("my-branch", remote=True)
            assert mock_run.call_count == 2
            mock_run.assert_any_call(["branch", "-D", "my-branch"], cwd=None)
            mock_run.assert_any_call(
                ["push", "origin", "--delete", "my-branch"], cwd=None
            )

    def test_ignores_remote_error(self) -> None:
        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            if "push" in args:
                raise GitError("remote not found")
            return ""

        with patch("fujimoto.git._run", side_effect=mock_run):
            delete_branch("my-branch", remote=True)  # Should not raise


class TestCherryPickBranch:
    def test_calls_correct_sequence(self) -> None:
        calls: list[list[str]] = []

        def mock_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[0] == "merge-base":
                return "abc123"
            return ""

        with patch("fujimoto.git._run", side_effect=mock_run):
            cherry_pick_branch("feature", "main")
            assert calls[0] == ["merge-base", "main", "feature"]
            assert calls[1] == ["checkout", "main"]
            assert calls[2] == ["cherry-pick", "abc123..feature"]
