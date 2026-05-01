"""Open a directory in Visual Studio Code."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _has_vscode() -> bool:
    """Check if the ``code`` CLI is available on PATH."""
    return shutil.which("code") is not None


def open_vscode(directory: Path) -> None:
    """Open *directory* in VS Code.

    Raises ``OSError`` if the ``code`` CLI is not found.
    """
    if not _has_vscode():
        msg = (
            "'code' CLI not found on PATH. On macOS, install it from VS Code: "
            "Cmd+Shift+P → 'Shell Command: Install code command in PATH'. "
            "On Linux, install VS Code via your package manager (the 'code' "
            "command is included)."
        )
        raise OSError(msg)

    subprocess.run(["code", str(directory)], check=True)
