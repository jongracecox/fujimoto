from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    pass


def get_worktree_root() -> Path:
    raw = os.environ.get("CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT")
    if not raw:
        raise ConfigError(
            "CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT is not set.\n"
            "Set it to the directory where worktrees should be created, e.g.:\n"
            "  export CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT=~/git/worktrees/"
        )
    root = Path(raw).expanduser().resolve()
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


def build_worktree_path(project_name: str, title: str) -> Path:
    root = get_worktree_root()
    today = date.today().strftime("%Y%m%d")
    dir_name = f"{today}-{slugify(title)}"
    return root / project_name / dir_name


def get_project_worktrees_dir(project_name: str) -> Path:
    return get_worktree_root() / project_name
