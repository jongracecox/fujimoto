"""Tests for fujimoto.monitor."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from fujimoto.claude import ClaudeSession, EntryType, SessionState, StopReason
from fujimoto.monitor import (
    SessionMonitor,
    _poll_once,
    _send_notification,
    _state_display,
)


def _make_session(
    path: Path,
    state: SessionState = SessionState.WORKING,
    session_id: str = "abc123",
) -> ClaudeSession:
    """Create a minimal ClaudeSession for testing."""
    from datetime import datetime, timezone

    return ClaudeSession(
        jsonl_path=path / f"{session_id}.jsonl",
        session_id=session_id,
        state=state,
        last_entry_type=EntryType.ASSISTANT,
        stop_reason=StopReason.TOOL_USE
        if state == SessionState.WAITING_FOR_TOOL_APPROVAL
        else None,
        cwd=path,
        git_branch="main",
        last_activity=datetime.now(tz=timezone.utc),
    )


class TestStateDisplay:
    def test_tool_approval(self) -> None:
        assert (
            _state_display(SessionState.WAITING_FOR_TOOL_APPROVAL)
            == "Needs tool approval"
        )

    def test_waiting_for_user(self) -> None:
        assert _state_display(SessionState.WAITING_FOR_USER) == "Waiting for input"

    def test_other_state(self) -> None:
        assert _state_display(SessionState.WORKING) == "working"


class TestSendNotification:
    @patch("fujimoto.monitor.subprocess.run")
    def test_calls_osascript(self, mock_run: MagicMock) -> None:
        _send_notification("Test Title", "Test message")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"
        assert args[1] == "-e"
        assert "Test Title" in args[2]
        assert "Test message" in args[2]
        assert 'sound name "default"' in args[2]


class TestPollOnce:
    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_no_notification_on_initial_same_state(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        session = _make_session(path, SessionState.WORKING)
        mock_get.return_value = [session]

        snapshot = {str(path): ("abc123", SessionState.WORKING)}
        new_snapshot = _poll_once([path], snapshot, None)

        mock_notify.assert_not_called()
        assert new_snapshot[str(path)] == ("abc123", SessionState.WORKING)

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_notifies_on_tool_approval_transition(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        session = _make_session(path, SessionState.WAITING_FOR_TOOL_APPROVAL)
        mock_get.return_value = [session]

        snapshot = {str(path): ("abc123", SessionState.WORKING)}
        _poll_once([path], snapshot, None)

        mock_notify.assert_called_once()
        title, message = mock_notify.call_args[0]
        assert "project" in title
        assert "tool approval" in message.lower()

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_skips_attached_path(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        session = _make_session(path, SessionState.WAITING_FOR_TOOL_APPROVAL)
        mock_get.return_value = [session]

        snapshot = {str(path): ("abc123", SessionState.WORKING)}
        _poll_once([path], snapshot, attached_path=path)

        mock_notify.assert_not_called()

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_no_notification_for_non_notify_state(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        session = _make_session(path, SessionState.WAITING_FOR_USER)
        mock_get.return_value = [session]

        snapshot = {str(path): ("abc123", SessionState.WORKING)}
        _poll_once([path], snapshot, None)

        mock_notify.assert_not_called()

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_handles_empty_sessions(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        mock_get.return_value = []

        new_snapshot = _poll_once([path], {}, None)

        mock_notify.assert_not_called()
        assert str(path) not in new_snapshot

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_notifies_on_new_session_in_notify_state(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")
        session = _make_session(path, SessionState.WAITING_FOR_TOOL_APPROVAL)
        mock_get.return_value = [session]

        # Empty snapshot — first time seeing this session
        _poll_once([path], {}, None)

        mock_notify.assert_called_once()


class TestSessionMonitor:
    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_start_and_stop(self, mock_notify: MagicMock, mock_get: MagicMock) -> None:
        mock_get.return_value = []
        monitor = SessionMonitor(paths=[Path("/tmp/test")], interval=0.1)
        monitor.start()
        time.sleep(0.3)
        monitor.stop()

        assert monitor._thread is None

    @patch("fujimoto.monitor.get_sessions_for_path")
    @patch("fujimoto.monitor._send_notification")
    def test_detects_state_change(
        self, mock_notify: MagicMock, mock_get: MagicMock
    ) -> None:
        path = Path("/tmp/project")

        # First call (initial snapshot): working. Subsequent calls: tool approval.
        working = _make_session(path, SessionState.WORKING)
        approval = _make_session(path, SessionState.WAITING_FOR_TOOL_APPROVAL)
        # After the explicit sequence, return approval forever
        call_count = 0
        sequence = [
            [working],  # initial snapshot build
            [approval],  # first poll tick
        ]

        def get_side_effect(*args: object, **kwargs: object) -> list[ClaudeSession]:
            nonlocal call_count
            if call_count < len(sequence):
                result = sequence[call_count]
                call_count += 1
                return result
            return [approval]

        mock_get.side_effect = get_side_effect

        monitor = SessionMonitor(paths=[path], interval=0.1)
        monitor.start()
        time.sleep(0.4)
        monitor.stop()

        mock_notify.assert_called_once()

    def test_start_is_idempotent(self) -> None:
        with patch("fujimoto.monitor.get_sessions_for_path", return_value=[]):
            monitor = SessionMonitor(paths=[], interval=0.1)
            monitor.start()
            thread = monitor._thread
            monitor.start()  # second start should be a no-op
            assert monitor._thread is thread
            monitor.stop()
