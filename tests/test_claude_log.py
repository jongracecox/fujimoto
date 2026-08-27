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
    read_transcript,
    session_dirs_for_path,
)


def _make_entry(
    type: str = "assistant",
    stop_reason: str | None = "end_turn",
    session_id: str = "test-uuid",
    cwd: str = "/test",
    git_branch: str | None = "main",
    timestamp: str = "2026-03-09T12:00:00.000Z",
    is_sidechain: bool = False,
    text: str | None = None,
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
    if type == "assistant":
        message: dict = {}
        if stop_reason is not None:
            message["stop_reason"] = stop_reason
        if text is not None:
            message["content"] = [{"type": "text", "text": text}]
        if message:
            entry["message"] = message
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

    def test_dots_become_hyphens(self) -> None:
        # Claude munges dots as well as slashes, so `/.` becomes `--`. This is
        # what made every worktree under the default `<repo>/.fujimoto/
        # worktrees/` root invisible to the transcript lookup.
        assert (
            encode_project_path(Path("/repo/.fujimoto/worktrees/20260309-fix"))
            == "-repo--fujimoto-worktrees-20260309-fix"
        )

    def test_dot_inside_a_name(self) -> None:
        assert encode_project_path(Path("/git/site.com")) == "-git-site-com"


class TestGetClaudeProjectsDir:
    def test_returns_expected_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch(
            "fujimoto.claude.log_parser.Path.home", return_value=Path("/mock/home")
        ):
            result = get_claude_projects_dir()
            assert result == Path("/mock/home/.claude/projects")

    def test_honours_claude_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere/claude")
        assert get_claude_projects_dir() == Path("/elsewhere/claude/projects")

    def test_config_dir_tilde_is_expanded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cc")
        assert get_claude_projects_dir() == Path.home() / "cc" / "projects"

    def test_blank_config_dir_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        with patch(
            "fujimoto.claude.log_parser.Path.home", return_value=Path("/mock/home")
        ):
            assert get_claude_projects_dir() == Path("/mock/home/.claude/projects")


class TestParseSession:
    def test_end_turn_is_waiting_for_user(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text(
            _make_entry(type="assistant", stop_reason="end_turn", text="Done.") + "\n"
        )

        session = parse_session(log)

        assert session.session_id == "abc123"
        assert session.state == SessionState.WAITING_FOR_USER
        assert session.last_entry_type == EntryType.ASSISTANT
        assert session.stop_reason == StopReason.END_TURN

    def test_end_turn_with_question_is_waiting_for_user(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        log.write_text(
            _make_entry(
                type="assistant",
                stop_reason="end_turn",
                text="Should I proceed?",
            )
            + "\n"
        )

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER
        assert session.stop_reason == StopReason.END_TURN

    def test_end_turn_no_content_is_waiting_for_user(self, tmp_path: Path) -> None:
        """end_turn with no message content (e.g. minimal log entry) → waiting."""
        log = tmp_path / "abc123.jsonl"
        log.write_text(_make_entry(type="assistant", stop_reason="end_turn") + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER

    def test_tool_use_without_result_is_waiting_for_approval(
        self, tmp_path: Path
    ) -> None:
        """tool_use with no following tool_result → pending approval."""
        log = tmp_path / "abc123.jsonl"
        log.write_text(_make_entry(type="assistant", stop_reason="tool_use") + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_TOOL_APPROVAL
        assert session.stop_reason == StopReason.TOOL_USE

    def test_tool_use_with_result_is_working(self, tmp_path: Path) -> None:
        """tool_use followed by a tool_result → actively working."""
        log = tmp_path / "abc123.jsonl"
        tool_result_entry = json.dumps(
            {
                "type": "user",
                "cwd": "/test",
                "timestamp": "2026-03-09T12:00:01.000Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
                    ]
                },
            }
        )
        lines = [
            _make_entry(type="assistant", stop_reason="tool_use"),
            tool_result_entry,
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WORKING
        assert session.last_entry_type == EntryType.USER

    def test_last_user_entry_is_working(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(type="assistant", stop_reason="end_turn", text="Done."),
            _make_entry(type="user", stop_reason=None),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WORKING
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
            _make_entry(type="assistant", stop_reason="end_turn", text="Done."),
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
                text="All done.",
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
                text="Here you go.",
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

    def test_unknown_entry_type_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            json.dumps({"type": "banana", "cwd": "/test", "timestamp": ""}),
            _make_entry(type="assistant", stop_reason="end_turn", text="Done."),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER

    def test_only_unknown_entry_types_raises(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        entry = json.dumps({"type": "banana", "cwd": "/test", "timestamp": ""})
        log.write_text(entry + "\n")

        with pytest.raises(ClaudeLogError, match="No parseable entries"):
            parse_session(log)

    def test_unknown_stop_reason_is_waiting_for_user(self, tmp_path: Path) -> None:
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

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER
        assert session.stop_reason is None

    def test_no_stop_reason_is_waiting_for_user(self, tmp_path: Path) -> None:
        """An interrupted/canceled response (Esc) has stop_reason=None → waiting."""
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

        assert session.state == SessionState.WAITING_FOR_USER
        assert session.stop_reason is None

    def test_last_prompt_makes_session_idle(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(
                type="assistant",
                stop_reason="end_turn",
                text="Want me to continue?",
            ),
            json.dumps({"type": "last-prompt"}),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.IDLE
        assert session.is_active is False

    def test_canceled_response_with_last_prompt_is_idle(self, tmp_path: Path) -> None:
        """Esc cancel followed by session exit → idle."""
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(type="user", stop_reason=None),
            json.dumps({"type": "assistant", "cwd": "/test", "timestamp": ""}),
            json.dumps({"type": "last-prompt"}),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.IDLE

    def test_queue_operation_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "abc123.jsonl"
        lines = [
            _make_entry(type="assistant", stop_reason="end_turn", text="Finished."),
            json.dumps({"type": "queue-operation", "cwd": "/test", "timestamp": ""}),
        ]
        log.write_text("\n".join(lines) + "\n")

        session = parse_session(log)

        assert session.state == SessionState.WAITING_FOR_USER


class TestGetSessionsForPath:
    def test_multiple_sessions_sorted_by_activity(self, tmp_path: Path) -> None:
        encoded = "-test-project"
        session_dir = tmp_path / "projects" / encoded
        session_dir.mkdir(parents=True)

        (session_dir / "older.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T10:00:00.000Z", text="Done.") + "\n"
        )
        (session_dir / "newer.jsonl").write_text(
            _make_entry(timestamp="2026-03-09T14:00:00.000Z", text="Done.") + "\n"
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
            _make_entry(timestamp="2026-03-09T12:00:00.000Z", text="Done.") + "\n"
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
            _make_entry(timestamp="2026-03-09T12:00:00.000Z", text="Done.") + "\n"
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

    def test_waiting_for_tool_approval_is_active(self) -> None:
        assert (
            self._build_session(SessionState.WAITING_FOR_TOOL_APPROVAL).is_active
            is True
        )

    def test_working_is_active(self) -> None:
        assert self._build_session(SessionState.WORKING).is_active is True

    def test_idle_is_not_active(self) -> None:
        assert self._build_session(SessionState.IDLE).is_active is False

    def test_unknown_is_not_active(self) -> None:
        assert self._build_session(SessionState.UNKNOWN).is_active is False


def test_parse_session_skips_non_object_json_lines(tmp_path):
    """A valid JSON line that isn't an object must not crash the parse."""
    log = tmp_path / "a.jsonl"
    log.write_text(
        "[1, 2, 3]\n"
        '"a bare string"\n'
        + json.dumps(
            {
                "type": "user",
                "cwd": "/repo",
                "timestamp": "2026-08-26T10:00:00Z",
                "message": {"content": "hello"},
            }
        )
        + "\n"
    )
    session = parse_session(log)
    assert session.state == SessionState.WORKING
    assert session.cwd == Path("/repo")


def _line(entry: dict) -> str:
    entry.setdefault("timestamp", "2026-03-09T12:00:00.000Z")
    return json.dumps(entry)


class TestReadTranscript:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ClaudeLogError):
            read_transcript(tmp_path / "missing.jsonl")

    def test_empty_file_returns_no_messages(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text("")
        assert read_transcript(log) == []

    def test_plain_user_and_assistant_text(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            "\n".join(
                [
                    _line({"type": "user", "message": {"content": "  hello  "}}),
                    _line(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "hi there"}]
                            },
                        }
                    ),
                ]
            )
        )
        messages = read_transcript(log)
        assert [(m.role, m.text) for m in messages] == [
            ("user", "hello"),
            ("assistant", "hi there"),
        ]
        assert messages[0].timestamp == datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)

    def test_skips_sidechain_meta_blank_and_unknown_entries(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            "\n".join(
                [
                    "",
                    "not json",
                    _line({"type": "last-prompt"}),
                    _line({"type": "file-history-snapshot"}),
                    _line(
                        {
                            "type": "user",
                            "isSidechain": True,
                            "message": {"content": "sub-agent"},
                        }
                    ),
                    _line(
                        {"type": "user", "isMeta": True, "message": {"content": "meta"}}
                    ),
                    _line({"type": "user", "message": {"content": "   "}}),
                    _line({"type": "user", "message": {"content": None}}),
                    _line({"type": "user", "message": {"content": "kept"}}),
                ]
            )
        )
        assert [m.text for m in read_transcript(log)] == ["kept"]

    def test_thinking_tool_use_and_tool_result(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            "\n".join(
                [
                    _line(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    "not-a-dict",
                                    {"type": "thinking", "thinking": " pondering "},
                                    {"type": "thinking", "thinking": "  "},
                                    {"type": "text", "text": "  "},
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {"command": "ls"},
                                    },
                                    {"type": "unknown-block"},
                                ]
                            },
                        }
                    ),
                    _line(
                        {
                            "type": "user",
                            "message": {
                                "content": [
                                    {"type": "tool_result", "content": "file.txt"}
                                ]
                            },
                        }
                    ),
                ]
            )
        )
        assert [(m.role, m.text) for m in read_transcript(log)] == [
            ("thinking", "pondering"),
            ("tool_use", "Bash\ncommand: ls"),
            ("tool_result", "file.txt"),
        ]

    def test_tool_use_without_input_shows_name_only(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            _line(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "input": "not-a-dict"}]
                    },
                }
            )
        )
        assert [m.text for m in read_transcript(log)] == ["tool"]

    def test_tool_result_block_list_content(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            _line(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [
                                    {"type": "text", "text": "one"},
                                    {"type": "image"},
                                    {"type": "text", "text": "two"},
                                ],
                            },
                            {"type": "tool_result", "content": 42},
                        ]
                    },
                }
            )
        )
        assert [m.text for m in read_transcript(log)] == ["one\ntwo"]

    def test_non_list_non_string_content_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(_line({"type": "assistant", "message": {"content": 7}}))
        assert read_transcript(log) == []

    def test_long_tool_result_is_clipped_by_lines(self, tmp_path: Path) -> None:
        body = "\n".join(f"line {i}" for i in range(50))
        log = tmp_path / "s.jsonl"
        log.write_text(
            _line(
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": body}]},
                }
            )
        )
        text = read_transcript(log)[0].text
        assert text.endswith("…")
        assert text.count("\n") == 20
        assert "line 19" in text
        assert "line 20" not in text

    def test_long_tool_result_is_clipped_by_chars(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            _line(
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": "x" * 5000}]
                    },
                }
            )
        )
        text = read_transcript(log)[0].text
        assert text.endswith("…")
        assert len(text) < 2100


class TestTranscriptToolIds:
    def test_tool_ids_are_carried_for_pairing(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            "\n".join(
                [
                    _line(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "toolu_1",
                                        "name": "Bash",
                                        "input": {"command": "ls"},
                                    }
                                ]
                            },
                        }
                    ),
                    _line(
                        {
                            "type": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_1",
                                        "content": "a.txt",
                                    }
                                ]
                            },
                        }
                    ),
                ]
            )
        )
        assert [m.tool_id for m in read_transcript(log)] == ["toolu_1", "toolu_1"]

    def test_tool_id_is_none_when_a_log_omits_it(self, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            _line(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
                }
            )
        )
        assert read_transcript(log)[0].tool_id is None


class TestSessionDirsForPath:
    """Resolving a working directory to Claude's transcript directories."""

    @pytest.fixture(autouse=True)
    def _clear_index_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The recorded-cwd index is memoized per projects dir + mtime; clear it
        # so one test's fake tree cannot answer another's lookup.
        monkeypatch.setattr("fujimoto.claude.log_parser._cwd_index_cache", None)

    def _log(self, path: Path, cwd: str = "/test") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_make_entry(cwd=cwd, text="Done.") + "\n")
        return path

    def test_dotted_worktree_path_is_found(self, tmp_path: Path) -> None:
        # The regression: a worktree under the default `<repo>/.fujimoto/
        # worktrees/` root, whose Claude directory has `--fujimoto` in it.
        projects = tmp_path / "projects"
        wt = Path("/repo/.fujimoto/worktrees/20260309-fix")
        self._log(
            projects / "-repo--fujimoto-worktrees-20260309-fix" / "s1.jsonl",
            cwd=str(wt),
        )

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(wt) == [
                projects / "-repo--fujimoto-worktrees-20260309-fix"
            ]
            assert len(get_sessions_for_path(wt)) == 1

    def test_missing_path_resolves_to_nothing(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "projects",
        ):
            assert session_dirs_for_path(Path("/no/such/dir")) == []

    def test_symlinked_path_matches_resolved_directory(self, tmp_path: Path) -> None:
        # Claude records the physical cwd, so a worktree reached through a
        # symlink is filed under the resolved path.
        real = tmp_path / "real" / "wt"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")

        projects = tmp_path / "projects"
        encoded = encode_project_path(real.resolve())
        self._log(projects / encoded / "s1.jsonl", cwd=str(real))

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(link / "wt") == [projects / encoded]

    def test_falls_back_to_recorded_cwd(self, tmp_path: Path) -> None:
        # No encoding rule would produce this directory name — only the `cwd`
        # inside the transcript ties it to the path.
        projects = tmp_path / "projects"
        self._log(projects / "opaque-name" / "s1.jsonl", cwd="/some/project")

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(Path("/some/project")) == [
                projects / "opaque-name"
            ]
            sessions = get_sessions_for_path(Path("/some/project"))

        assert [s.session_id for s in sessions] == ["s1"]

    def test_fallback_ignores_unrelated_directories(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        self._log(projects / "opaque-name" / "s1.jsonl", cwd="/some/project")
        (projects / "not-a-dir.jsonl").write_text("")

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(Path("/other/project")) == []

    def test_fallback_survives_a_malformed_log(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        (projects / "junk").mkdir(parents=True)
        (projects / "junk" / "s0.jsonl").write_text("not json\n")
        self._log(projects / "opaque-name" / "s1.jsonl", cwd="/some/project")

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(Path("/some/project")) == [
                projects / "opaque-name"
            ]

    def test_fallback_with_no_projects_dir(self, tmp_path: Path) -> None:
        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=tmp_path / "nope",
        ):
            assert session_dirs_for_path(Path("/some/project")) == []

    def test_index_picks_up_a_new_session_directory(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert session_dirs_for_path(Path("/some/project")) == []
            self._log(projects / "opaque-name" / "s1.jsonl", cwd="/some/project")
            assert session_dirs_for_path(Path("/some/project")) == [
                projects / "opaque-name"
            ]

    def test_sessions_are_not_double_counted(self, tmp_path: Path) -> None:
        # A trailing slash and a bare path encode identically; the same log
        # must not be parsed twice.
        projects = tmp_path / "projects"
        self._log(projects / "-test-project" / "s1.jsonl")

        with patch(
            "fujimoto.claude.log_parser.get_claude_projects_dir",
            return_value=projects,
        ):
            assert len(get_sessions_for_path(Path("/test/project/"))) == 1

    def test_fallback_survives_an_unreadable_log(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        blocked = self._log(projects / "blocked" / "s0.jsonl", cwd="/some/project")
        blocked.chmod(0o000)
        self._log(projects / "opaque-name" / "s1.jsonl", cwd="/some/project")

        try:
            with patch(
                "fujimoto.claude.log_parser.get_claude_projects_dir",
                return_value=projects,
            ):
                assert session_dirs_for_path(Path("/some/project")) == [
                    projects / "opaque-name"
                ]
        finally:
            blocked.chmod(0o644)
