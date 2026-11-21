"""Cross-process locking and atomic replacement for fujimoto's JSON stores.

`sessions.json` and the launch-options cache are each one file that several
fujimoto processes read, change and write back — the TUI, and any number of
`fujimoto chat`/`new`/`stop` runs started by agents. Without a lock, two
overlapping writers lose one of their changes. `locked()` serialises the
read-change-write; `atomic_write()` makes every write a rename, so a reader
(which never locks) sees the old file or the new one, never half of either.

The lock is `fcntl.flock` on a sidecar `<file>.lock`. The kernel releases it
when its holder exits, however that happens, so a crash cannot leave a stale
lock and nothing has to detect one. While held, the lock file names its
holder — pid, command and when it was taken — purely for diagnosis: a
timeout logs it, and `cat` shows it.

A lock that cannot be had within `timeout` is *not* an error: the caller
proceeds unlocked (the pre-lock behaviour) and the event is logged with the
holder's details. Bookkeeping must never freeze the TUI.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fujimoto import debug

DEFAULT_TIMEOUT = 2.0
_POLL = 0.02


def lock_path(path: Path) -> Path:
    """The sidecar lock file for `path`.

    >>> lock_path(Path("/c/sessions.json"))
    PosixPath('/c/sessions.json.lock')
    """
    return path.with_name(f"{path.name}.lock")


def _holder_text() -> str:
    command = " ".join(
        [Path(sys.argv[0]).name if sys.argv else "?", *sys.argv[1:2]]
    ).strip()
    return json.dumps(
        {
            "pid": os.getpid(),
            "command": command,
            "since": datetime.now(tz=timezone.utc).isoformat(),
        }
    )


def read_holder(path: Path) -> dict[str, object] | None:
    """Who holds `path`'s lock, as last written; None if free or unreadable."""
    try:
        text = lock_path(path).read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        holder = json.loads(text)
    except ValueError:
        return None
    return holder if isinstance(holder, dict) else None


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[bool]:
    """Hold an exclusive lock for `path` for the duration of the block.

    Yields True when the lock was taken, False when the block runs unlocked
    because it timed out or the lock file could not be opened.
    """
    target = lock_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        debug.log(
            "filelock.unavailable", path=debug.rp(target), error=type(exc).__name__
        )
        yield False
        return

    started = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    break
                time.sleep(_POLL)
        waited = time.monotonic() - started
        if acquired:
            os.ftruncate(fd, 0)
            os.pwrite(fd, _holder_text().encode(), 0)
            if waited >= _POLL:
                debug.log(
                    "filelock.waited", path=debug.rp(target), seconds=round(waited, 3)
                )
        else:
            holder = read_holder(path) or {}
            debug.log(
                "filelock.timeout",
                path=debug.rp(target),
                seconds=round(waited, 3),
                held_by_pid=holder.get("pid", "unknown"),
                held_by=debug.rv(str(holder.get("command", "unknown"))),
                held_since=holder.get("since", "unknown"),
            )
        yield acquired
    finally:
        if acquired:
            # Clear the holder so a free lock reads as free.
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` via a temp file and a rename.

    Raises OSError like `Path.write_text`; the temp file never survives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
