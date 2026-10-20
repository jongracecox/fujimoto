"""Which Claude Code processes are running right now, from Claude's own registry.

Claude Code writes one `<pid>.json` per running interactive process under
`$CLAUDE_CONFIG_DIR/sessions/` (default `~/.claude/sessions/`), recording the
conversation id it is writing (`sessionId`), its `cwd`, its peer address
(`name`, what `SendMessage` takes), a `status` (`busy`/`idle`) and, when it
runs inside tmux, the pane it runs in (`tmux`, `<session>:@<window>.%<pane>`).

That is a far more exact answer to "is this transcript live?" than tmux alone,
which only knows that *something* runs in a directory. It is also undocumented,
so every field is read defensively: a file that is unreadable, not a dict, or
lacks a `sessionId` is skipped, and a process that has exited without removing
its file (a crash, a `kill -9`) is detected with a pid liveness check rather
than trusted. A registry that has vanished altogether reads as empty, and the
caller falls back to fujimoto's own tmux/session-state view.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fujimoto import debug
from fujimoto.claude.log_parser import get_claude_projects_dir

# `<session>:@<window>.%<pane>`. tmux forbids `:` in a session name, so the
# last `:@` is the boundary.
_TMUX_PANE = re.compile(r"^(?P<session>.+):@\d+\.%\d+$")


@dataclass(frozen=True)
class LiveSession:
    """One running Claude Code process, as its registry file describes it."""

    pid: int
    session_id: str
    cwd: Path | None
    peer_name: str | None
    status: str | None
    tmux_session: str | None


def get_claude_sessions_dir() -> Path:
    """The registry directory, alongside `projects/` in Claude's config tree.

    >>> get_claude_sessions_dir().name
    'sessions'
    """
    return get_claude_projects_dir().parent / "sessions"


def tmux_session_from_pane(value: object) -> str | None:
    """The tmux session name in a registry `tmux` field, or None.

    >>> tmux_session_from_pane("proj/20261020-x:@12.%34")
    'proj/20261020-x'
    >>> tmux_session_from_pane("") is None
    True
    >>> tmux_session_from_pane("not a pane") is None
    True
    """
    if not isinstance(value, str):
        return None
    match = _TMUX_PANE.match(value)
    return match.group("session") if match else None


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists.

    `PermissionError` means it exists but belongs to someone else, which still
    counts — the question is whether the registry file is stale.

    >>> pid_alive(os.getpid())
    True
    >>> pid_alive(0)
    False
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_entry(path: Path) -> LiveSession | None:
    """One registry file as a `LiveSession`, or None when it isn't one."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    session_id = data.get("sessionId")
    if not isinstance(pid, int) or not isinstance(session_id, str) or not session_id:
        return None
    cwd = _optional_str(data.get("cwd"))
    return LiveSession(
        pid=pid,
        session_id=session_id,
        cwd=Path(cwd) if cwd else None,
        peer_name=_optional_str(data.get("name")),
        status=_optional_str(data.get("status")),
        tmux_session=tmux_session_from_pane(data.get("tmux")),
    )


def list_live_sessions(sessions_dir: Path | None = None) -> list[LiveSession]:
    """Every registered Claude process that is still running."""
    directory = sessions_dir or get_claude_sessions_dir()
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:  # pragma: no cover - glob on an unreadable dir
        files = []
    live: list[LiveSession] = []
    unreadable = 0
    stale = 0
    for path in files:
        entry = _parse_entry(path)
        if entry is None:
            unreadable += 1
        elif not pid_alive(entry.pid):
            stale += 1
        else:
            live.append(entry)
    # Both skip counts matter: `unreadable` climbing is what a registry format
    # change looks like, and `stale` explains a session that reads as stopped.
    debug.log(
        "claude.live_sessions",
        dir=debug.rp(directory),
        exists=directory.is_dir(),
        files=len(files),
        live=len(live),
        stale=stale,
        unreadable=unreadable,
    )
    return live
