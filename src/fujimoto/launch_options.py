"""Extra `claude` CLI arguments a session is launched with.

A session's options are resolved from the first of these that has a value:

1. **Saved for the session** — whatever the user last entered in the launch
   options dialog. Stored where the session's lifetime is:
   - a worktree keeps them in `.fujimoto/meta.json`, so they survive
     terminate, crash and reboot for as long as the worktree exists;
   - any other session keeps them on its `session_state` record (covers
     stop, park and recovery) and, when the Claude conversation id is known,
     in `~/.cache/fujimoto/launch_options.json` keyed by that id — which is
     what brings them back when a terminated session's transcript is resumed.
2. **Inherited from the parent** — a fork starts with its parent's options.
3. **The project default** — `claude_args` in `.fujimoto.yaml`.
4. Nothing.

An override is only stored when it differs from the project default, so a
session that never asked for anything different keeps following
`.fujimoto.yaml` as it changes.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fujimoto import debug

from . import session_state
from .config import (
    ConfigError,
    has_session_meta,
    read_claude_args,
    write_claude_args,
)
from .git import GitError, get_main_worktree_root
from .project_config import load_project_config

Args = tuple[str, ...]


class LaunchOptionsError(ValueError):
    """Raised when typed launch options cannot be split into arguments."""


class Source(StrEnum):
    """Where a session's launch options came from."""

    SESSION = "session"
    PARENT = "parent"
    PROJECT = "project"
    NONE = "none"

    @property
    def label(self) -> str:
        return {
            Source.SESSION: "saved for this session",
            Source.PARENT: "inherited from the parent session",
            Source.PROJECT: "project default from .fujimoto.yaml",
            Source.NONE: "none",
        }[self]


@dataclass(frozen=True)
class Resolved:
    args: Args
    source: Source


def parse(text: str) -> Args:
    """Split typed options the way a shell would.

    >>> parse("--plugin-dir ./plugins --model 'opus'")
    ('--plugin-dir', './plugins', '--model', 'opus')
    >>> parse("")
    ()
    """
    try:
        return tuple(shlex.split(text))
    except ValueError as exc:
        raise LaunchOptionsError(str(exc)) from exc


def render(args: Args) -> str:
    """The inverse of `parse`, for pre-filling the dialog.

    >>> render(("--plugin-dir", "my plugins"))
    "--plugin-dir 'my plugins'"
    """
    return shlex.join(args)


def _cache_path() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "launch_options.json"


def _load_cache() -> dict[str, list[str]]:
    path = _cache_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        debug.log("launch_options.cache_load", error=type(exc).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, list) and all(isinstance(a, str) for a in v)
    }


def _save_cache(cache: dict[str, list[str]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2))
    except OSError as exc:
        debug.log("launch_options.cache_save", error=type(exc).__name__)


def remember_for_conversation(claude_session_id: str, args: Args | None) -> None:
    """Key options by Claude conversation id, so a resume finds them again."""
    cache = _load_cache()
    if args is None:
        if cache.pop(claude_session_id, None) is None:
            return
    else:
        cache[claude_session_id] = list(args)
    _save_cache(cache)
    debug.log(
        "launch_options.remember",
        claude_session=claude_session_id,
        cleared=args is None,
    )


def _is_worktree(working_dir: Path) -> bool:
    """Whether fujimoto created this directory as a worktree (it has meta)."""
    return has_session_meta(working_dir)


def saved_args(
    working_dir: Path,
    tmux_name: str | None = None,
    claude_session_id: str | None = None,
) -> Args | None:
    """Options saved for a session, or None if it never had an override."""
    worktree_args = read_claude_args(working_dir)
    if worktree_args is not None:
        return tuple(worktree_args)
    if tmux_name:
        record = session_state.load_state().get(tmux_name)
        if record is not None and record.claude_args is not None:
            return tuple(record.claude_args)
    if claude_session_id:
        cached = _load_cache().get(claude_session_id)
        if cached is not None:
            return tuple(cached)
    return None


def project_default(working_dir: Path) -> Args:
    """`claude_args` from the project's `.fujimoto.yaml`, read from the main clone.

    A malformed config is already reported on the home screen, so here it just
    contributes nothing.
    """
    try:
        root = get_main_worktree_root(working_dir)
        return tuple(load_project_config(root).claude_args)
    except (GitError, ConfigError, OSError):
        return ()


def resolve(
    working_dir: Path,
    tmux_name: str | None = None,
    claude_session_id: str | None = None,
    *,
    parent_dir: Path | None = None,
    parent_session_id: str | None = None,
) -> Resolved:
    """The options a launch should use when the user does not pick any."""
    saved = saved_args(working_dir, tmux_name, claude_session_id)
    if saved is not None:
        resolved = Resolved(saved, Source.SESSION)
    else:
        inherited = (
            saved_args(parent_dir, None, parent_session_id)
            if parent_dir is not None
            else None
        )
        if inherited is not None:
            resolved = Resolved(inherited, Source.PARENT)
        else:
            default = project_default(working_dir)
            resolved = Resolved(default, Source.PROJECT if default else Source.NONE)
    debug.log(
        "launch_options.resolve",
        cwd=debug.rp(working_dir),
        session=debug.rv(tmux_name),
        source=resolved.source.value,
        count=len(resolved.args),
        args=debug.rargs(resolved.args),
    )
    return resolved


def save(
    working_dir: Path,
    tmux_name: str,
    claude_session_id: str | None,
    args: Args,
) -> None:
    """Store `args` as the session's options for every later launch.

    Matching the project default clears the override instead, so the session
    goes back to following `.fujimoto.yaml`.
    """
    value: Args | None = None if args == project_default(working_dir) else args
    if _is_worktree(working_dir):
        write_claude_args(working_dir, None if value is None else list(value))
        where = "worktree-meta"
    else:
        session_state.set_claude_args(tmux_name, None if value is None else list(value))
        if claude_session_id:
            remember_for_conversation(claude_session_id, value)
        where = "session-record"
    debug.log(
        "launch_options.save",
        cwd=debug.rp(working_dir),
        session=debug.rv(tmux_name),
        where=where,
        cleared=value is None,
        count=len(args),
        args=debug.rargs(args),
    )
