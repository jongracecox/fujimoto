"""Which sessions the user still considers open.

Fujimoto is the only thing that ever changes a session's *intent*. A session
the user terminated through fujimoto is forgotten; a session that disappeared
any other way — an out-of-band ``tmux kill-session``, a closed terminal window,
an ``exit`` in the pane, a tmux crash, a host restart — keeps its record and is
shown as *stopped*, ready to resume.

That single rule is the whole design: there is no boot-time detection and no
reconciliation pass. A record's presence means "open"; its absence means
"closed", which is also what a session fujimoto has never launched looks like.
Terminating therefore just deletes the record, and the store stays small
without needing to age anything out.

State lives in ``~/.cache/fujimoto/sessions.json``, keyed by tmux session name
so worktree, direct and ad hoc sessions are all covered uniformly — and so a
record survives its worktree being deleted. Reads and writes degrade
gracefully: a missing file, unreadable cache or corrupt JSON yields an empty
state rather than an error, mirroring `settings.py` and `version_check.py`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from fujimoto import debug


def _state_path() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "sessions.json"


@dataclass
class SessionRecord:
    """A session the user still considers open."""

    cwd: str
    # Everything but the working directory is optional so a record written by a
    # newer (or older) fujimoto still loads instead of being dropped.
    project: str = ""
    session_type: str = ""
    branch: str = ""
    claude_session_id: str | None = None
    last_seen: str = ""

    @property
    def path(self) -> Path:
        return Path(self.cwd)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_state() -> dict[str, SessionRecord]:
    """Read the open-session records, tolerating a missing or corrupt file."""
    path = _state_path()
    if not path.exists():
        debug.log_once(
            "session-state-load",
            "session_state.load",
            path=debug.rp(path),
            found=False,
        )
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        debug.log(
            "session_state.load",
            path=debug.rp(path),
            found=True,
            error=type(exc).__name__,
        )
        return {}
    if not isinstance(data, dict):
        debug.log(
            "session_state.load",
            path=debug.rp(path),
            found=True,
            error="not-an-object",
        )
        return {}

    records: dict[str, SessionRecord] = {}
    skipped = 0
    fields = {"cwd", "project", "session_type", "branch", "claude_session_id"}
    for name, raw in data.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("cwd"), str):
            # A malformed record is silently dropped, which otherwise looks
            # exactly like a session that was never launched.
            skipped += 1
            debug.log("session_state.skipped", session=debug.rv(name))
            continue
        kwargs = {k: v for k, v in raw.items() if k in fields}
        records[name] = SessionRecord(
            **kwargs,
            last_seen=raw.get("last_seen") or "",
        )
    debug.log_once(
        "session-state-load",
        "session_state.load",
        path=debug.rp(path),
        found=True,
        records=len(records),
        skipped=skipped,
        sessions=",".join(debug.rv(n) for n in sorted(records)),
    )
    return records


def save_state(state: dict[str, SessionRecord]) -> None:
    """Persist the open-session records, swallowing filesystem errors."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({name: asdict(rec) for name, rec in state.items()}, indent=2)
        )
    except OSError as exc:
        debug.log("session_state.save", path=debug.rp(path), error=type(exc).__name__)
        return
    debug.log("session_state.save", path=debug.rp(path), records=len(state))


def mark_open(
    tmux_name: str,
    *,
    cwd: Path,
    project: str,
    session_type: str,
    branch: str = "",
    claude_session_id: str | None = None,
) -> None:
    """Record that a session is open. Called on every launch and reconnect."""
    state = load_state()
    existing = state.get(tmux_name)
    # A reconnect knows nothing new about the conversation, so don't let it
    # blank out an id recorded when the session was first launched.
    if claude_session_id is None and existing is not None:
        claude_session_id = existing.claude_session_id
    state[tmux_name] = SessionRecord(
        cwd=str(cwd),
        project=project,
        session_type=session_type,
        branch=branch,
        claude_session_id=claude_session_id,
        last_seen=_now(),
    )
    debug.log(
        "session_state.mark_open",
        session=debug.rv(tmux_name),
        cwd=debug.rp(cwd),
        project=debug.rv(project),
        session_type=session_type,
        branch=debug.rref(branch),
        claude_session=claude_session_id or "none",
        new_record=existing is None,
    )
    save_state(state)


def mark_closed(tmux_name: str) -> None:
    """Forget a session. The only path by which a session stops being open."""
    state = load_state()
    removed = state.pop(tmux_name, None) is not None
    debug.log("session_state.mark_closed", session=debug.rv(tmux_name), removed=removed)
    if removed:
        save_state(state)


def touch(tmux_name: str, claude_session_id: str | None = None) -> None:
    """Refresh a record without changing its intent (used when stopping)."""
    state = load_state()
    record = state.get(tmux_name)
    if record is None:
        debug.log("session_state.touch", session=debug.rv(tmux_name), found=False)
        return
    debug.log(
        "session_state.touch",
        session=debug.rv(tmux_name),
        found=True,
        claude_session=claude_session_id or "unchanged",
    )
    record.last_seen = _now()
    if claude_session_id is not None:
        record.claude_session_id = claude_session_id
    save_state(state)


def rename(old_name: str, new_name: str) -> None:
    """Follow a tmux session rename so its record isn't orphaned."""
    state = load_state()
    record = state.pop(old_name, None)
    debug.log(
        "session_state.rename",
        old=debug.rv(old_name),
        new=debug.rv(new_name),
        found=record is not None,
    )
    if record is None:
        return
    state[new_name] = record
    save_state(state)


def prune() -> dict[str, SessionRecord]:
    """Drop records whose working directory is gone, and return what remains.

    A deleted worktree (or an ad hoc temp dir cleared by a reboot) can never be
    resumed, so its record is dead weight.
    """
    state = load_state()
    live = {name: rec for name, rec in state.items() if rec.path.exists()}
    dropped = [name for name in state if name not in live]
    for name in dropped:
        # The usual cause of a session vanishing from the home screen.
        debug.log(
            "session_state.pruned",
            session=debug.rv(name),
            cwd=debug.rp(state[name].cwd),
        )
    debug.log_once(
        "session-state-prune",
        "session_state.prune",
        records=len(state),
        live=len(live),
        dropped=len(dropped),
    )
    if len(live) != len(state):
        save_state(live)
    return live
