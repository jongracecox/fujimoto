"""Tests for fujimoto.monitor."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fujimoto.claude import ClaudeSession, EntryType, SessionState, StopReason
from fujimoto.monitor import (
    NotifierError,
    SessionMonitor,
    _poll_once,
    _send_notification,
    _state_display,
    install_notifier,
    is_notifier_installed,
    notifications_skipped,
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


class TestIsNotifierInstalled:
    @patch(
        "fujimoto.monitor.shutil.which", return_value="/usr/local/bin/terminal-notifier"
    )
    def test_installed(self, _mock: object) -> None:
        assert is_notifier_installed() is True

    @patch("fujimoto.monitor.shutil.which", return_value=None)
    def test_not_installed(self, _mock: object) -> None:
        assert is_notifier_installed() is False


class TestInstallNotifier:
    @patch(
        "fujimoto.monitor.shutil.which",
        side_effect=["/usr/local/bin/brew", "/usr/local/bin/terminal-notifier"],
    )
    @patch("fujimoto.monitor.subprocess.run")
    def test_successful_install(self, mock_run: MagicMock, _which: object) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        install_notifier()
        mock_run.assert_called_once_with(["brew", "install", "terminal-notifier"])

    @patch("fujimoto.monitor.shutil.which", return_value=None)
    def test_raises_without_brew(self, _mock: object) -> None:
        with pytest.raises(NotifierError, match="brew is not installed"):
            install_notifier()

    @patch("fujimoto.monitor.shutil.which", return_value="/usr/local/bin/brew")
    @patch("fujimoto.monitor.subprocess.run")
    def test_raises_on_brew_failure(self, mock_run: MagicMock, _which: object) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        with pytest.raises(NotifierError, match="Failed to install"):
            install_notifier()

    @patch("fujimoto.monitor.shutil.which", side_effect=["/usr/local/bin/brew", None])
    @patch("fujimoto.monitor.subprocess.run")
    def test_raises_when_not_on_path_after_install(
        self, mock_run: MagicMock, _which: object
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(NotifierError, match="not found on PATH"):
            install_notifier()


class TestNotificationsSkipped:
    @patch.dict("os.environ", {"FUJIMOTO_SKIP_NOTIFICATIONS": "1"})
    def test_skipped_with_1(self) -> None:
        assert notifications_skipped() is True

    @patch.dict("os.environ", {"FUJIMOTO_SKIP_NOTIFICATIONS": "true"})
    def test_skipped_with_true(self) -> None:
        assert notifications_skipped() is True

    @patch.dict("os.environ", {"FUJIMOTO_SKIP_NOTIFICATIONS": "yes"})
    def test_skipped_with_yes(self) -> None:
        assert notifications_skipped() is True

    @patch.dict("os.environ", {"FUJIMOTO_SKIP_NOTIFICATIONS": "TRUE"})
    def test_skipped_case_insensitive(self) -> None:
        assert notifications_skipped() is True

    @patch.dict("os.environ", {"FUJIMOTO_SKIP_NOTIFICATIONS": ""})
    def test_not_skipped_with_empty(self) -> None:
        assert notifications_skipped() is False

    @patch.dict("os.environ", {}, clear=True)
    def test_not_skipped_when_unset(self) -> None:
        assert notifications_skipped() is False


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
    @patch("fujimoto.monitor.subprocess.Popen")
    def test_calls_terminal_notifier(self, mock_popen: MagicMock) -> None:
        _send_notification("Test Title", "Test message")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "terminal-notifier"
        assert "-title" in args
        assert "Test Title" in args[args.index("-title") + 1]
        assert "-message" in args
        assert args[args.index("-message") + 1] == "Test message"


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
