"""Whether Claude Code already trusts a directory (read-only).

Claude Code asks "do you trust this folder?" the first time it starts in a
directory that is not inside a trusted git repository, and records the answer
in its state file (`~/.claude.json`, or `.claude.json` under
`$CLAUDE_CONFIG_DIR`) as `projects[<path>].hasTrustDialogAccepted`. There is
no command to pre-trust a directory, and fujimoto never writes that file —
Claude Code rewrites it constantly — so this only *reads* it, to warn that a
session started without anyone watching will sit at the prompt.

The format is undocumented: every read is defensive, and "can't tell" is
`None`, never an error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fujimoto import debug


def get_claude_state_file() -> Path:
    """Claude Code's state file, which holds per-directory trust.

    >>> get_claude_state_file().name
    '.claude.json'
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def is_trusted(path: Path, state_file: Path | None = None) -> bool | None:
    """True/False if the state file says, None if it can't be read."""
    source = state_file or get_claude_state_file()
    try:
        data = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        debug.log("claude.trust", path=debug.rp(path), error=type(exc).__name__)
        return None
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        debug.log("claude.trust", path=debug.rp(path), error="no-projects")
        return None
    candidates = {str(path)}
    try:
        candidates.add(str(path.resolve()))
    except OSError:  # pragma: no cover - resolve() rarely fails
        pass
    trusted = any(
        isinstance(entry := projects.get(key), dict)
        and entry.get("hasTrustDialogAccepted") is True
        for key in candidates
    )
    debug.log("claude.trust", path=debug.rp(path), trusted=trusted)
    return trusted
