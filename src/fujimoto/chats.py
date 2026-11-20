"""Chat sessions' own metadata: one small JSON file per chat.

A worktree outlives a terminated session because it has a directory (and a
`.fujimoto/meta.json`). A chat has no directory of its own — it runs in the
project root, a worktree or the shared scratch directory — so this file is
its equivalent: `~/.cache/fujimoto/chats/<project>/<chat name>.json`.

It is written when the chat is launched and kept when it is terminated
(`closed` is stamped instead), which is what lets a finished chat stay on the
home screen's *previous sessions* list until the user removes it. Whether a
chat is *open* is still answered by `session_state` alone; this file only
says the chat exists.

One file per chat rather than one shared file, so two fujimoto processes
acting on different chats never write the same file, and each write is a
temp-file-then-rename so a reader never sees half of one. Missing, unreadable
or malformed files read as "no such chat", never as an error.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from fujimoto import debug


@dataclass
class ChatMeta:
    """What a chat needs to be listed and resumed after it has closed."""

    # The tmux session name, `<project>/<slug>` — the chat's identity.
    name: str
    project: str
    cwd: str
    claude_session_id: str | None = None
    parent_session: str | None = None
    created: str = ""
    # ISO-8601 when the chat was terminated; empty while it is open.
    closed: str = ""

    @property
    def path(self) -> Path:
        return Path(self.cwd)

    @property
    def slug(self) -> str:
        return self.name.rsplit("/", 1)[-1]


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def chats_dir(project: str) -> Path:
    """Where a project's chat metadata files live (not created here)."""
    return Path.home() / ".cache" / "fujimoto" / "chats" / project


def _meta_path(name: str, project: str) -> Path:
    return chats_dir(project) / f"{name.rsplit('/', 1)[-1]}.json"


def _write(meta: ChatMeta) -> None:
    """Atomically replace the chat's file, swallowing filesystem errors."""
    target = _meta_path(meta.name, meta.project)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(meta), handle, indent=2)
        os.replace(tmp, target)
    except OSError as exc:
        debug.log("chats.write", chat=debug.rv(meta.name), error=type(exc).__name__)


def _read(path: Path) -> ChatMeta | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        debug.log("chats.skipped", path=debug.rp(path), error=type(exc).__name__)
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(k), str) and raw.get(k) for k in ("name", "project", "cwd")
    ):
        debug.log("chats.skipped", path=debug.rp(path), error="malformed")
        return None

    def opt(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    return ChatMeta(
        name=raw["name"],
        project=raw["project"],
        cwd=raw["cwd"],
        claude_session_id=opt("claude_session_id"),
        parent_session=opt("parent_session"),
        created=opt("created") or "",
        closed=opt("closed") or "",
    )


def get(name: str, project: str) -> ChatMeta | None:
    path = _meta_path(name, project)
    return _read(path) if path.exists() else None


def list_chats(project: str) -> list[ChatMeta]:
    """Every chat recorded for `project`, open or closed."""
    directory = chats_dir(project)
    if not directory.is_dir():
        return []
    chats = [m for p in sorted(directory.glob("*.json")) if (m := _read(p))]
    debug.log_once(
        f"chats-list-{project}",
        "chats.list",
        project=debug.rv(project),
        chats=len(chats),
        closed=sum(bool(c.closed) for c in chats),
    )
    return chats


def find_by_conversation(claude_session_id: str) -> ChatMeta | None:
    """The chat, in any project, whose recorded conversation this is."""
    root = chats_dir("_").parent
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/*.json")):
        meta = _read(path)
        if meta is not None and meta.claude_session_id == claude_session_id:
            return meta
    return None


def record_open(
    name: str,
    *,
    project: str,
    cwd: Path,
    claude_session_id: str | None,
    parent_session: str | None = None,
) -> None:
    """Note a chat launch: create its file, or reopen a closed one.

    Like `session_state.mark_open`, a launch that knows no new id or parent
    keeps the recorded ones.
    """
    existing = get(name, project)
    meta = ChatMeta(
        name=name,
        project=project,
        cwd=str(cwd),
        claude_session_id=claude_session_id
        or (existing.claude_session_id if existing else None),
        parent_session=parent_session
        or (existing.parent_session if existing else None),
        created=existing.created if existing and existing.created else _now(),
    )
    _write(meta)
    debug.log(
        "chats.open",
        chat=debug.rv(name),
        new=existing is None,
        reopened=bool(existing and existing.closed),
    )


def mark_closed(name: str, project: str, claude_session_id: str | None) -> bool:
    """Stamp a terminated chat closed, keeping it listed. False if unknown."""
    meta = get(name, project)
    if meta is None:
        debug.log("chats.close", chat=debug.rv(name), found=False)
        return False
    if claude_session_id:
        meta.claude_session_id = claude_session_id
    meta.closed = _now()
    _write(meta)
    debug.log("chats.close", chat=debug.rv(name), found=True)
    return True


def remove(name: str, project: str) -> None:
    """Forget a chat entirely (its transcript is untouched)."""
    path = _meta_path(name, project)
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as exc:
        debug.log("chats.remove", chat=debug.rv(name), error=type(exc).__name__)
        return
    debug.log("chats.remove", chat=debug.rv(name), removed=removed)


def rename(old: str, new: str, project: str) -> None:
    """Follow a chat's rename; a no-op for a session that is not a chat."""
    meta = get(old, project)
    if meta is None:
        return
    meta.name = new
    _write(meta)
    remove(old, project)
    debug.log("chats.rename", old=debug.rv(old), new=debug.rv(new))


def prune(project: str) -> list[ChatMeta]:
    """Drop chats whose directory is gone, and return the rest.

    A chat in a deleted worktree can never be resumed in place, so its entry
    is dead weight — the same rule `session_state.prune` applies to records.
    """
    kept: list[ChatMeta] = []
    for meta in list_chats(project):
        if meta.path.is_dir():
            kept.append(meta)
            continue
        remove(meta.name, project)
        debug.log("chats.pruned", chat=debug.rv(meta.name), cwd=debug.rp(meta.cwd))
    return kept
