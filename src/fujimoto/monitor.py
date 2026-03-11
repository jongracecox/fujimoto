"""Background monitor for Claude session state changes.

Polls Claude JSONL logs and sends macOS notifications when sessions
transition to states that need user attention (e.g. tool approval).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from fujimoto.claude import (
    ClaudeSession,
    SessionState,
    get_sessions_for_path,
)

ICON_WIZARD = "\U0001f9d9\U0001f3fd\u200d\u2642\ufe0f"


class NotifierError(Exception):
    pass


def is_notifier_installed() -> bool:
    """Check if terminal-notifier is on PATH."""
    return shutil.which("terminal-notifier") is not None


def notifications_skipped() -> bool:
    """Check if notifications are disabled via environment variable."""
    return os.environ.get("FUJIMOTO_SKIP_NOTIFICATIONS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def install_notifier() -> None:
    """Install terminal-notifier via brew. Raises NotifierError on failure."""
    if not shutil.which("brew"):
        raise NotifierError(
            "brew is not installed. Install terminal-notifier manually."
        )
    result = subprocess.run(["brew", "install", "terminal-notifier"])
    if result.returncode != 0:
        raise NotifierError("Failed to install terminal-notifier via brew")
    if not shutil.which("terminal-notifier"):
        raise NotifierError("terminal-notifier was installed but not found on PATH")


def _send_notification(title: str, message: str) -> None:
    """Send a macOS notification via terminal-notifier.

    Launched via ``Popen`` so it doesn't block the monitor thread.
    """
    subprocess.Popen(
        [
            "terminal-notifier",
            "-title",
            f"{ICON_WIZARD} {title}",
            "-message",
            message,
            "-sound",
            "default",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _state_display(state: SessionState) -> str:
    """Human-readable label for a session state."""
    return {
        SessionState.WAITING_FOR_TOOL_APPROVAL: "Needs tool approval",
        SessionState.WAITING_FOR_USER: "Waiting for input",
    }.get(state, state.value)


# States that warrant a notification
_NOTIFY_STATES = frozenset(
    {
        SessionState.WAITING_FOR_TOOL_APPROVAL,
    }
)


def _poll_once(
    paths: list[Path],
    snapshot: dict[str, tuple[str, SessionState]],
    attached_path: Path | None,
) -> dict[str, tuple[str, SessionState]]:
    """Poll all paths and notify on interesting state transitions.

    Returns the new snapshot.
    """
    new_snapshot: dict[str, tuple[str, SessionState]] = {}

    for path in paths:
        sessions = get_sessions_for_path(path)
        if not sessions:
            continue
        latest: ClaudeSession = sessions[0]
        new_snapshot[str(path)] = (latest.session_id, latest.state)

        # Skip the session the user is currently looking at
        if attached_path and path == attached_path:
            continue

        old = snapshot.get(str(path))
        if old and old == (latest.session_id, latest.state):
            continue

        # State changed — check if it's interesting
        if latest.state in _NOTIFY_STATES:
            session_label = path.name
            message = _state_display(latest.state)
            if latest.pending_tool_summary:
                message = f"{message}\n{latest.pending_tool_summary}"
            _send_notification(
                f"fujimoto — {session_label}",
                message,
            )

    return new_snapshot


class SessionMonitor:
    """Background thread that monitors Claude sessions for state changes."""

    def __init__(
        self,
        paths: list[Path],
        attached_path: Path | None = None,
        interval: float = 3.0,
    ) -> None:
        self._paths = paths
        self._attached_path = attached_path
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background monitor thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitor and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        snapshot: dict[str, tuple[str, SessionState]] = {}

        # Build initial snapshot without notifying
        for path in self._paths:
            sessions = get_sessions_for_path(path)
            if sessions:
                snapshot[str(path)] = (sessions[0].session_id, sessions[0].state)

        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break
            snapshot = _poll_once(self._paths, snapshot, self._attached_path)
