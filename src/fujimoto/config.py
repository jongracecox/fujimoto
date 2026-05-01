from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    pass


def get_git_projects_root() -> Path | None:
    """Read FUJIMOTO_GIT_ROOT env var. Returns None if unset."""
    raw = os.environ.get("FUJIMOTO_GIT_ROOT")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def list_projects() -> list[Path]:
    """List git repositories under the git projects root.

    Returns directories that contain a .git subdirectory, sorted by name.
    Returns an empty list if the env var is unset or the directory doesn't exist.
    """
    root = get_git_projects_root()
    if root is None or not root.is_dir():
        return []
    return sorted(
        [d for d in root.iterdir() if d.is_dir() and (d / ".git").exists()],
        key=lambda p: p.name,
    )


def get_worktree_root(project_root: Path | None = None) -> Path:
    """Resolve the directory where worktrees should be created.

    If FUJIMOTO_WORKTREE_ROOT is set, use it. Otherwise fall back to
    `<project_root>/.fujimoto/worktrees/`, ensuring the `.fujimoto` directory
    is gitignored. Raises ConfigError only if neither is available.
    """
    raw = os.environ.get("FUJIMOTO_WORKTREE_ROOT")
    if raw:
        root = Path(raw).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    if project_root is None:
        raise ConfigError(
            "FUJIMOTO_WORKTREE_ROOT is not set and no project root was provided."
        )
    _ensure_meta_dir(project_root)
    root = project_root / META_DIR / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(title: str) -> str:
    """Lowercase and replace non-alphanumeric characters with hyphens.

    >>> slugify("Fix Unit Tests")
    'fix-unit-tests'
    >>> slugify("  Hello World!! 123  ")
    'hello-world-123'
    >>> slugify("already-slugged")
    'already-slugged'
    >>> slugify("UPPER")
    'upper'
    >>> slugify("a---b")
    'a-b'
    >>> slugify("---leading-and-trailing---")
    'leading-and-trailing'
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def build_worktree_path(
    project_name: str, title: str, project_root: Path | None = None
) -> Path:
    today = date.today().strftime("%Y%m%d")
    dir_name = f"{today}-{slugify(title)}"
    return get_project_worktrees_dir(project_name, project_root) / dir_name


def get_project_worktrees_dir(
    project_name: str, project_root: Path | None = None
) -> Path:
    """Directory holding all worktrees for `project_name`.

    With FUJIMOTO_WORKTREE_ROOT set: `{root}/{project_name}`.
    With the in-project fallback: `<project_root>/.fujimoto/worktrees/`
    (no extra project layer — the directory already lives inside the project).
    """
    if os.environ.get("FUJIMOTO_WORKTREE_ROOT"):
        return get_worktree_root() / project_name
    return get_worktree_root(project_root)


META_DIR = ".fujimoto"
META_FILENAME = "meta.json"


def _get_meta_dir(worktree_path: Path) -> Path:
    return worktree_path / META_DIR


def _ensure_meta_dir(worktree_path: Path) -> Path:
    meta_dir = _get_meta_dir(worktree_path)
    meta_dir.mkdir(exist_ok=True)
    gitignore = meta_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return meta_dir


def store_session_meta(worktree_path: Path, base_branch: str) -> None:
    """Write session metadata to a JSON file in the worktree directory."""
    meta = {"base_branch": base_branch}
    meta_dir = _ensure_meta_dir(worktree_path)
    meta_path = meta_dir / META_FILENAME
    meta_path.write_text(json.dumps(meta))


def read_session_meta(worktree_path: Path) -> dict[str, str]:
    """Read session metadata from the worktree directory."""
    meta_path = _get_meta_dir(worktree_path) / META_FILENAME
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def get_next_direct_session_name(project_name: str, active_sessions: set[str]) -> str:
    """Compute the next direct-N session name for a project."""
    prefix = f"{project_name}/direct-"
    n = 1
    while f"{prefix}{n}" in active_sessions:
        n += 1
    return f"{prefix}{n}"


def get_next_adhoc_session_name(active_sessions: set[str]) -> str:
    """Compute the next adhoc-N tmux session name."""
    n = 1
    while f"adhoc-{n}" in active_sessions:
        n += 1
    return f"adhoc-{n}"
