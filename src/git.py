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


def get_repo_root() -> Path:
    return Path(_run(["rev-parse", "--show-toplevel"]))


def get_project_name() -> str:
    return get_repo_root().name


def get_current_branch() -> str:
    return _run(["branch", "--show-current"])


def get_default_branch() -> str:
    try:
        ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"])
        return ref.split("/")[-1]
    except GitError:
        pass
    for candidate in ("main", "master"):
        try:
            _run(["rev-parse", "--verify", candidate])
            return candidate
        except GitError:
            continue
    return "main"


def create_worktree(path: Path, base_branch: str, new_branch: str) -> None:
    if path.exists():
        raise GitError(f"Directory already exists: {path}")
    _run(["worktree", "add", "-b", new_branch, str(path), base_branch])
