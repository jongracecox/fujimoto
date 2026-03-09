from __future__ import annotations

import sys

import questionary

from claude_worktree.config import (
    ConfigError,
    build_worktree_path,
    get_project_worktrees_dir,
    slugify,
)
from claude_worktree.git import (
    GitError,
    create_worktree,
    get_current_branch,
    get_default_branch,
    get_project_name,
)
from claude_worktree.tmux import TmuxError, launch_claude_in_tmux


def main() -> None:
    try:
        _run_interactive()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except GitError as e:
        print(f"Git error: {e}", file=sys.stderr)
        sys.exit(1)
    except TmuxError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


def _run_interactive() -> None:
    project_name = get_project_name()
    current_branch = get_current_branch()
    default_branch = get_default_branch()

    action = questionary.select(
        "What would you like to do?",
        choices=[
            "Create a new worktree",
            "Use an existing worktree",
        ],
    ).ask()

    if action is None:
        return

    if action == "Create a new worktree":
        _create_new(project_name, current_branch, default_branch)
    else:
        _use_existing(project_name)


def _create_new(project_name: str, current_branch: str, default_branch: str) -> None:
    title = questionary.text(
        "Worktree title:",
        validate=lambda val: len(val.strip()) > 0 or "Title cannot be empty",
    ).ask()

    if title is None:
        return

    if current_branch == default_branch:
        base_branch = default_branch
    else:
        choice = questionary.select(
            "Base branch:",
            choices=[
                f"Current branch ({current_branch})",
                f"Default branch ({default_branch})",
            ],
        ).ask()

        if choice is None:
            return

        base_branch = current_branch if choice.startswith("Current") else default_branch

    worktree_path = build_worktree_path(project_name, title)
    new_branch = f"worktree/{slugify(title)}"

    print(f"\nCreating worktree at {worktree_path}")
    print(f"Branch: {new_branch} (from {base_branch})")

    create_worktree(worktree_path, base_branch, new_branch)
    launch_claude_in_tmux(project_name, worktree_path)


def _use_existing(project_name: str) -> None:
    project_dir = get_project_worktrees_dir(project_name)

    if not project_dir.exists():
        print("No existing worktrees found.")
        return

    worktrees = sorted(
        [d for d in project_dir.iterdir() if d.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )

    if not worktrees:
        print("No existing worktrees found.")
        return

    choices = [d.name for d in worktrees]
    selected = questionary.select(
        "Select worktree:",
        choices=choices,
    ).ask()

    if selected is None:
        return

    selected_path = project_dir / selected
    launch_claude_in_tmux(project_name, selected_path)
