"""Open a new terminal window in a given directory."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


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


def open_terminal(directory: Path) -> None:
    """Open a new terminal window in the given directory.

    Uses iTerm2 if installed, otherwise falls back to Terminal.app.
    Requires macOS (osascript / open).
    """
    if not shutil.which("osascript"):
        msg = "osascript not found — this feature requires macOS"
        raise OSError(msg)

    if _has_iterm():
        _open_iterm(directory)
    else:
        _open_terminal_app(directory)
