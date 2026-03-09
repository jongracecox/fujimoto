from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class TmuxError(Exception):
    pass


def check_tmux_installed() -> None:
    if not shutil.which("tmux"):
        raise TmuxError("tmux is not installed. Install it with: brew install tmux")


def session_name(project_name: str, worktree_dir_name: str) -> str:
    return f"{project_name}/{worktree_dir_name}"


def session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def create_session(name: str, working_dir: Path) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(working_dir)],
        check=True,
    )
    options = {
        "prefix": "C-a",
        "status-right": '"Detach: ^A D | Scroll: ^A [ | Kill: ^A X"',
        "status-style": "bg=colour235,fg=colour248",
        "status-right-length": "60",
    }
    for key, value in options.items():
        subprocess.run(
            ["tmux", "set-option", "-t", name, key, value],
            check=True,
        )
    subprocess.run(
        ["tmux", "unbind-key", "-t", name, "C-b"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "bind-key", "-t", name, "C-a", "send-prefix"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", name, "claude", "Enter"],
        check=True,
    )


def attach_session(name: str) -> None:
    print()
    print("╭─────────────────────────────────────────────╮")
    print("│  Attaching to tmux session                  │")
    print("│                                             │")
    print("│  Ctrl+A D  — Detach (leave running)         │")
    print("│  Ctrl+A [  — Scroll mode                    │")
    print("│  Ctrl+A X  — Kill pane                      │")
    print("╰─────────────────────────────────────────────╯")
    print()
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def launch_claude_in_tmux(project_name: str, worktree_path: Path) -> None:
    check_tmux_installed()
    name = session_name(project_name, worktree_path.name)
    if session_exists(name):
        attach_session(name)
    else:
        create_session(name, worktree_path)
        attach_session(name)
