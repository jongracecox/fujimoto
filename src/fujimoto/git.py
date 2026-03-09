from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _run(args: list[str], cwd: Path | str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise GitError(e.stderr.strip() or str(e)) from e
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH")


def get_repo_root(cwd: Path | str | None = None) -> Path:
    return Path(_run(["rev-parse", "--show-toplevel"], cwd=cwd))


def get_project_name(cwd: Path | str | None = None) -> str:
    return get_repo_root(cwd).name


def get_current_branch(cwd: Path | str | None = None) -> str:
    return _run(["branch", "--show-current"], cwd=cwd)


def get_default_branch(cwd: Path | str | None = None) -> str:
    try:
        ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
        return ref.split("/")[-1]
    except GitError:
        pass
    for candidate in ("main", "master"):
        try:
            _run(["rev-parse", "--verify", candidate], cwd=cwd)
            return candidate
        except GitError:
            continue
    return "main"


def create_worktree(
    path: Path, base_branch: str, new_branch: str, cwd: Path | str | None = None
) -> None:
    if path.exists():
        raise GitError(f"Directory already exists: {path}")
    _run(["worktree", "add", "-b", new_branch, str(path), base_branch], cwd=cwd)


def remove_worktree(path: Path, cwd: Path | str | None = None) -> None:
    """Remove a git worktree. Uses --force to handle dirty worktrees."""
    _run(["worktree", "remove", "--force", str(path)], cwd=cwd)


def get_unpushed_commits(branch: str, cwd: Path | str | None = None) -> list[str]:
    """Return list of commit summaries on branch not yet pushed to origin."""
    try:
        output = _run(["log", f"origin/{branch}..{branch}", "--oneline"], cwd=cwd)
    except GitError:
        # No remote tracking branch — all commits are "unpushed"
        try:
            merge_base = get_merge_base(branch, cwd=cwd)
            output = _run(["log", f"{merge_base}..{branch}", "--oneline"], cwd=cwd)
        except GitError:
            return []
    if not output:
        return []
    return output.splitlines()


def get_merge_base(branch: str, cwd: Path | str | None = None) -> str:
    """Find the merge base between a branch and the default branch."""
    default = get_default_branch(cwd=cwd)
    return _run(["merge-base", default, branch], cwd=cwd)


def is_branch_merged(branch: str, into: str, cwd: Path | str | None = None) -> bool:
    """Check if branch has been merged into another branch."""
    try:
        _run(["merge-base", "--is-ancestor", branch, into], cwd=cwd)
        return True
    except GitError:
        return False


def has_remote_branch(branch: str, cwd: Path | str | None = None) -> bool:
    """Check if a branch exists on the remote."""
    try:
        output = _run(["ls-remote", "--heads", "origin", branch], cwd=cwd)
        return bool(output)
    except GitError:
        return False


def push_branch(branch: str, cwd: Path | str | None = None) -> None:
    """Push a branch to origin."""
    _run(["push", "-u", "origin", branch], cwd=cwd)


def delete_branch(
    branch: str, remote: bool = False, cwd: Path | str | None = None
) -> None:
    """Delete a branch locally, and optionally from origin."""
    _run(["branch", "-D", branch], cwd=cwd)
    if remote:
        try:
            _run(["push", "origin", "--delete", branch], cwd=cwd)
        except GitError:
            pass  # Remote branch may not exist


def cherry_pick_branch(branch: str, onto: str, cwd: Path | str | None = None) -> None:
    """Cherry-pick all commits from branch onto another branch.

    Finds the merge-base and cherry-picks the range.
    """
    merge_base = _run(["merge-base", onto, branch], cwd=cwd)
    _run(["checkout", onto], cwd=cwd)
    _run(["cherry-pick", f"{merge_base}..{branch}"], cwd=cwd)
