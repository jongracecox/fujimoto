from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.claude.log_parser import (
    ClaudeLogError,
    ClaudeSession,
    EntryType,
    SessionState,
    StopReason,
    encode_project_path,
    get_claude_projects_dir,
    get_sessions_for_path,
    parse_session,
)


def _make_entry(
    type: str = "assistant",
    stop_reason: str | None = "end_turn",
    session_id: str = "test-uuid",
    cwd: str = "/test",
    git_branch: str | None = "main",
    timestamp: str = "2026-03-09T12:00:00.000Z",
    is_sidechain: bool = False,
) -> str:
    """Build a JSON line for a Claude session log entry."""
    entry: dict = {
        "type": type,
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
    }
    if git_branch is not None:
        entry["gitBranch"] = git_branch
    if is_sidechain:
        entry["isSidechain"] = True
    if type == "assistant" and stop_reason is not None:
        entry["message"] = {"stop_reason": stop_reason}
    return json.dumps(entry)


class TestEncodeProjectPath:
    def test_standard_path(self) -> None:
        assert (
            encode_project_path(Path("/Users/alice/git/myproject"))
            == "-Users-alice-git-myproject"
        )

    def test_worktree_path(self) -> None:
        result = encode_project_path(
            Path("/Users/alice/git/worktrees/proj/20260309-fix")
        )
        assert result == "-Users-alice-git-worktrees-proj-20260309-fix"

    def test_trailing_slash(self) -> None:
        assert encode_project_path(Path("/tmp/test/")) == "-tmp-test"


class TestGetClaudeProjectsDir:
    def test_returns_expected_path(self) -> None:
        with patch(
            "fujimoto.claude.log_parser.Path.home", return_value=Path("/mock/home")
        ):
            result = get_claude_projects_dir()
            assert result == Path("/mock/home/.claude/projects")


class TestParseSession:
    def test_end_turn_is_waiting_for_user(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text(_make_entry(type="assistant", stop_reason="end_turn") + "\n")

        session = parse_session(log)

        assert session.session_id == "abc123"
        assert session.state == SessionState.WAITING_FOR_USER
        assert session.last_entry_type == EntryType.ASSISTANT
        assert session.stop_reason == StopReason.END_TURN

    def test_tool_use_is_processing(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text(_make_entry(type="assistant", stop_reason="tool_use") + "\n")

        session = parse_session(log)

        assert session.state == SessionState.PROCESSING
        assert session.stop_reason == StopReason.TOOL_USE

    def test_last_user_entry_is_processing(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(type="assistant", stop_reason="end_turn"),
            _make_entry(type="user", stop_reason=None),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.PROCESSING
        assert session.last_entry_type == EntryType.USER
        assert session.stop_reason is None

    def test_only_non_meaningful_entries_is_unknown(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text(_make_entry(type="system", stop_reason=None) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.UNKNOWN
        assert session.last_entry_type == EntryType.SYSTEM

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text("")

        with pytest.raises(ClaudeLogError, match="Empty session log"):
            parse_session(log)

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            "not valid json",
            _make_entry(type="assistant", stop_reason="end_turn"),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER

    def test_sidechain_entries_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(
                type="assistant",
                stop_reason="end_turn",
                timestamp="2026-03-09T11:00:00.000Z",
            ),
            _make_entry(
                type="user", is_sidechain=True, timestamp="2026-03-09T12:00:00.000Z"
            ),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        # The sidechain user entry should be ignored, so last meaningful is the assistant
        assert session.state == SessionState.WAITING_FOR_USER
        assert session.last_entry_type == EntryType.ASSISTANT

    def test_metadata_extracted(self, tmp_path: Path) -> None:
        log = tmp_path / "session-42.jsonl"
        log.write_text(
            _make_entry(
                type="assistant",
                stop_reason="end_turn",
                cwd="/my/project",
                git_branch="feature/cool",
                timestamp="2026-03-09T15:30:00.000Z",
            )
            + "\n"
        )

        session = parse_session(log)

        assert session.session_id == "session-42"
        assert session.cwd == Path("/my/project")
        assert session.git_branch == "feature/cool"
        assert session.last_activity == datetime(
            2026, 3, 9, 15, 30, tzinfo=timezone.utc
        )
        assert session.jsonl_path == log

    def test_unknown_entry_type_raises(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        entry = json.dumps({"type": "banana", "cwd": "/test", "timestamp": ""})
        log.write_text(entry + "\n")

        with pytest.raises(ClaudeLogError, match="Unknown entry type"):
            parse_session(log)

    def test_unknown_stop_reason_raises(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        entry = json.dumps(
            {
                "type": "assistant",
                "cwd": "/test",
                "timestamp": "",
                "message": {"stop_reason": "exploded"},
            }
        )
        log.write_text(entry + "\n")

        with pytest.raises(ClaudeLogError, match="Unknown stop_reason"):
            parse_session(log)

    def test_assistant_without_stop_reason_is_unknown(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        entry = json.dumps(
            {
                "type": "assistant",
                "cwd": "/test",
                "timestamp": "",
            }
        )
        log.write_text(entry + "\n")

        session = parse_session(log)

        assert session.state == SessionState.UNKNOWN
        assert session.stop_reason is None


class TestGetSessionsForPath:
    def test_multiple_sessions_sorted_by_activity(self, tmp_path: Path) -> None:
        encoded = "-test-project"
        session_dir = tmp_path / "projects" / encoded
        session_dir.mkdir(parents=True)

        (session_dir / "older.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T10:00:00.000Z") + "\n"
        )
        (session_dir / "newer.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T14:00:00.000Z") + "\n"
        )

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            sessions = get_sessions_for_path(Path("/test/project"))

        assert len(sessions) == 2
        assert sessions[0].session_id == "newer"
        assert sessions[1].session_id == "older"

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            sessions = get_sessions_for_path(Path("/does/not/exist"))

        assert sessions == []

    def test_no_jsonl_files_returns_empty(self, tmp_path: Path) -> None:
        encoded = "-test-project"
        session_dir = tmp_path / "projects" / encoded
        session_dir.mkdir(parents=True)
        (session_dir / "readme.txt").write_text("not a log")

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            sessions = get_sessions_for_path(Path("/test/project"))

        assert sessions == []

    def test_path_encoding_applied(self, tmp_path: Path) -> None:
        # Verify the path is encoded before looking up the directory
        encoded = "-Users-alice-git-myproject"
        session_dir = tmp_path / "projects" / encoded
        session_dir.mkdir(parents=True)
        (session_dir / "s1.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T12:00:00.000Z") + "\n"
        )

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            sessions = get_sessions_for_path(Path("/Users/alice/git/myproject"))

        assert len(sessions) == 1

    def test_parse_errors_skipped(self, tmp_path: Path) -> None:
        encoded = "-test-project"
        session_dir = tmp_path / "projects" / encoded
        session_dir.mkdir(parents=True)

        (session_dir / "good.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T12:00:00.000Z") + "\n"
        )
        (session_dir / "bad.jsonl").write_text("")  # Empty — will raise

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            sessions = get_sessions_for_path(Path("/test/project"))

        assert len(sessions) == 1
        assert sessions[0].session_id == "good"


class TestClaudeSessionIsActive:
    def _build_session(self, state: SessionState) -> ClaudeSession:
        return ClaudeSession(
            jsonl_path=Path("/fake.jsonl"),
            session_id="test",
            state=state,
            last_entry_type=EntryType.ASSISTANT,
            stop_reason=None,
            cwd=Path("/test"),
            git_branch=None,
            last_activity=datetime.now(tz=timezone.utc),
        )

    def test_waiting_for_user_is_active(self) -> None:
        assert self._build_session(SessionState.WAITING_FOR_USER).is_active is True

    def test_processing_is_active(self) -> None:
        assert self._build_session(SessionState.PROCESSING).is_active is True

    def test_unknown_is_not_active(self) -> None:
        assert self._build_session(SessionState.UNKNOWN).is_active is False
