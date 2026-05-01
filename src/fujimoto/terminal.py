"""Open a new terminal window in a given directory.

Cross-platform: macOS uses iTerm2 (or Terminal.app fallback). Linux uses the
``FUJIMOTO_TERMINAL`` env var if set, otherwise auto-detects a common terminal
emulator on PATH.

The ``FUJIMOTO_TERMINAL`` env var is shell-quoted and may contain ``{dir}`` as
a placeholder for the working directory. If ``{dir}`` is absent, the directory
is appended as the final argument. Examples:

    FUJIMOTO_TERMINAL="alacritty --working-directory {dir}"
    FUJIMOTO_TERMINAL="kitty --directory {dir}"
    FUJIMOTO_TERMINAL="wezterm start --cwd {dir}"
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# (executable, args-with-{dir}-placeholder). First match on PATH wins.
_LINUX_TERMINALS: list[tuple[str, list[str]]] = [
    ("x-terminal-emulator", ["--working-directory={dir}"]),
    ("gnome-terminal", ["--working-directory={dir}"]),
    ("konsole", ["--workdir", "{dir}"]),
    ("xfce4-terminal", ["--working-directory={dir}"]),
    ("tilix", ["--working-directory={dir}"]),
    ("terminator", ["--working-directory={dir}"]),
    ("kitty", ["--directory", "{dir}"]),
    ("alacritty", ["--working-directory", "{dir}"]),
    ("wezterm", ["start", "--cwd", "{dir}"]),
    ("foot", ["--working-directory={dir}"]),
    ("xterm", ["-e", "cd {dir} && exec ${{SHELL:-/bin/sh}}"]),
]


def _has_iterm() -> bool:
    """Check if iTerm2 is installed."""
    return Path("/Applications/iTerm.app").exists()


def _open_iterm(directory: Path) -> None:
    """Open a new iTerm2 window in the given directory using AppleScript."""
    script = f"""
    tell application "iTerm"
        activate
        set newWindow to (create window with default profile)
        tell current session of newWindow
            write text "cd {_applescript_quote(str(directory))}"
        end tell
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=True)


def _open_terminal_app(directory: Path) -> None:
    """Open a new Terminal.app window in the given directory."""
    subprocess.run(["open", "-a", "Terminal", str(directory)], check=True)


def _applescript_quote(s: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _format_args(args: list[str], directory: Path) -> list[str]:
    """Substitute ``{dir}`` in each arg; append directory if no placeholder."""
    dir_str = str(directory)
    if any("{dir}" in a for a in args):
        return [a.replace("{dir}", dir_str) for a in args]
    return [*args, dir_str]


def _open_linux_terminal(directory: Path) -> None:
    """Open a Linux terminal emulator in the given directory.

    Uses ``FUJIMOTO_TERMINAL`` if set, else probes a list of common terminals.
    """
    custom = os.environ.get("FUJIMOTO_TERMINAL", "").strip()
    if custom:
        parts = shlex.split(custom)
        if not parts:
            msg = "FUJIMOTO_TERMINAL is set but empty"
            raise OSError(msg)
        executable = parts[0]
        if not shutil.which(executable):
            msg = f"FUJIMOTO_TERMINAL refers to '{executable}' but it is not on PATH"
            raise OSError(msg)
        cmd = [executable, *_format_args(parts[1:], directory)]
        subprocess.Popen(cmd, start_new_session=True)
        return

    for executable, args in _LINUX_TERMINALS:
        if shutil.which(executable):
            cmd = [executable, *_format_args(args, directory)]
            subprocess.Popen(cmd, start_new_session=True)
            return

    msg = (
        "No supported terminal emulator found on PATH. Set FUJIMOTO_TERMINAL "
        "to your terminal command (e.g. 'alacritty --working-directory {dir}')."
    )
    raise OSError(msg)


def open_terminal(directory: Path) -> None:
    """Open a new terminal window in the given directory.

    macOS: iTerm2 if installed, otherwise Terminal.app.
    Linux: ``FUJIMOTO_TERMINAL`` env var, or auto-detected terminal emulator.
    """
    if sys.platform == "darwin":
        if _has_iterm():
            _open_iterm(directory)
        else:
            _open_terminal_app(directory)
        return

    if sys.platform.startswith("linux"):
        _open_linux_terminal(directory)
        return

    msg = f"Opening a terminal is not supported on platform '{sys.platform}'"
    raise OSError(msg)
