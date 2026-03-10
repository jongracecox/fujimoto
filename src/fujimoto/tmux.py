from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


class TmuxError(Exception):
    pass


def set_terminal_title(title: str) -> None:
    """Set the terminal window/tab title via OSC escape sequence.

    Works in iTerm2 and most modern terminals. Silently ignored otherwise.
    """
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except OSError:
        pass


def is_tmux_installed() -> bool:
    return shutil.which("tmux") is not None


def install_tmux() -> None:
    """Install tmux via brew. Raises TmuxError on failure."""
    if not shutil.which("brew"):
        raise TmuxError("brew is not installed. Install tmux manually.")
    result = subprocess.run(["brew", "install", "tmux"])
    if result.returncode != 0:
        raise TmuxError("Failed to install tmux via brew")
    if not shutil.which("tmux"):
        raise TmuxError("tmux was installed but not found on PATH")


def list_project_sessions(project_name: str) -> list[str]:
    """Return tmux session names that belong to the given project."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    prefix = f"{project_name}/"
    return [s for s in result.stdout.strip().splitlines() if s.startswith(prefix)]


def session_name(project_name: str, worktree_dir_name: str) -> str:
    """Build a tmux session name from project and worktree directory.

    >>> session_name("qsic-data", "20260309-fix-tests")
    'qsic-data/20260309-fix-tests'
    """
    return f"{project_name}/{worktree_dir_name}"


def session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def rename_session(old_name: str, new_name: str) -> None:
    """Rename a tmux session."""
    result = subprocess.run(
        ["tmux", "rename-session", "-t", old_name, new_name],
        capture_output=True,
    )
    if result.returncode != 0:
        raise TmuxError(f"Failed to rename session: {old_name}")


def kill_session(name: str) -> None:
    """Kill a tmux session by name."""
    result = subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True,
    )
    if result.returncode != 0:
        raise TmuxError(f"Failed to kill session: {name}")


def _configure_session(name: str) -> None:
    """Apply standard tmux configuration to a session."""
    options: dict[str, str] = {
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


def create_session(
    name: str,
    working_dir: Path,
    system_prompt: str | None = None,
) -> None:
    claude_cmd = "claude"
    if system_prompt:
        escaped = system_prompt.replace("'", "'\\''")
        claude_cmd = f"claude --append-system-prompt '{escaped}'"
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(working_dir),
            claude_cmd,
        ],
        check=True,
    )
    _configure_session(name)


def create_session_with_command(name: str, working_dir: Path, command: str) -> None:
    """Create a tmux session and run an arbitrary command instead of claude."""
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(working_dir),
            command,
        ],
        check=True,
    )
    _configure_session(name)


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
    subprocess.run(["tmux", "attach-session", "-t", name])


def launch_claude_in_tmux(
    project_name: str,
    working_dir: Path,
    tmux_name: str | None = None,
    system_prompt: str | None = None,
) -> None:
    name = tmux_name or session_name(project_name, working_dir.name)
    if session_exists(name):
        attach_session(name)
    else:
        create_session(name, working_dir, system_prompt=system_prompt)
        attach_session(name)
