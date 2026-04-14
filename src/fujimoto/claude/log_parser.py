from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class ClaudeLogError(Exception):
    pass


class EntryType(StrEnum):
    ASSISTANT = "assistant"
    USER = "user"
    FILE_HISTORY = "file-history-snapshot"
    SYSTEM = "system"
    PROGRESS = "progress"

    @classmethod
    def from_raw(cls, value: str) -> EntryType | None:
        """Convert a raw string to an EntryType, or None if unrecognized.

        Returns None for unknown types (e.g. last-prompt, queue-operation)
        so they can be skipped without crashing the parser.

        >>> EntryType.from_raw("assistant")
        <EntryType.ASSISTANT: 'assistant'>
        >>> EntryType.from_raw("user")
        <EntryType.USER: 'user'>
        >>> EntryType.from_raw("last-prompt") is None
        True
        """
        try:
            return cls(value)
        except ValueError:
            return None


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"

    @classmethod
    def from_raw(cls, value: str) -> StopReason | None:
        """Convert a raw string to a StopReason, or None if unrecognized.

        >>> StopReason.from_raw("end_turn")
        <StopReason.END_TURN: 'end_turn'>
        >>> StopReason.from_raw("tool_use")
        <StopReason.TOOL_USE: 'tool_use'>
        >>> StopReason.from_raw("max_tokens") is None
        True
        """
        try:
            return cls(value)
        except ValueError:
            return None


class SessionState(StrEnum):
    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_TOOL_APPROVAL = "waiting_for_tool_approval"
    IDLE = "idle"
    UNKNOWN = "unknown"


# Raw entry type strings that signal the session has ended.
_SESSION_END_TYPES = frozenset({"last-prompt"})


@dataclass(frozen=True)
class ClaudeSession:
    jsonl_path: Path
    session_id: str
    state: SessionState
    last_entry_type: EntryType
    stop_reason: StopReason | None
    cwd: Path
    git_branch: str | None
    last_activity: datetime
    title: str | None = None
    first_prompt: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state in (
            SessionState.WORKING,
            SessionState.WAITING_FOR_USER,
            SessionState.WAITING_FOR_TOOL_APPROVAL,
        )


def encode_project_path(path: Path) -> str:
    """Encode a project path for use as a Claude projects directory name.

    Replaces forward slashes with hyphens, matching Claude's convention.

    >>> encode_project_path(Path("/Users/alice/git/myproject"))
    '-Users-alice-git-myproject'
    >>> encode_project_path(Path("/Users/alice/git/worktrees/proj/20260309-fix"))
    '-Users-alice-git-worktrees-proj-20260309-fix'
    >>> encode_project_path(Path("/tmp/test/"))
    '-tmp-test'
    """
    return str(path).rstrip("/").replace("/", "-")


def get_claude_projects_dir() -> Path:
    """Return the path to Claude's projects directory.

    >>> isinstance(get_claude_projects_dir(), Path)
    True
    """
    return Path.home() / ".claude" / "projects"


def parse_session(jsonl_path: Path) -> ClaudeSession:
    """Parse a Claude session JSONL log file into a ClaudeSession.

    Raises ClaudeLogError if the file is empty, unreadable, or has no
    parseable entries.
    """
    session_id = jsonl_path.stem

    try:
        text = jsonl_path.read_text()
    except OSError as e:
        raise ClaudeLogError(f"Cannot read {jsonl_path}: {e}")

    if not text.strip():
        raise ClaudeLogError(f"Empty session log: {jsonl_path}")

    last_meaningful: dict | None = None
    last_any: dict | None = None
    session_ended = False
    tool_result_after_last_tool_use = False
    custom_title: str | None = None
    first_prompt: str | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_type = entry.get("type", "")

        if raw_type in _SESSION_END_TYPES:
            session_ended = True
            continue

        if raw_type == "custom-title":
            custom_title = entry.get("customTitle") or custom_title
            continue

        entry_type = EntryType.from_raw(raw_type)
        if entry_type is None:
            # Unknown type — skip without crashing
            continue

        last_any = entry

        if entry.get("isSidechain"):
            continue

        if first_prompt is None and entry_type == EntryType.USER:
            # Capture the first non-meta, non-command user message with string
            # content. isMeta=true covers <local-command-caveat> injections.
            # Content starting with "<" covers slash-command executions such as
            # <command-name>/model</command-name> and any other XML-tagged
            # injections Claude Code may add. Tool-result replies have array
            # content, so the isinstance(str) check excludes them.
            if not entry.get("isMeta"):
                content = entry.get("message", {}).get("content")
                if isinstance(content, str) and not content.lstrip().startswith("<"):
                    first_prompt = content

        if entry_type in (EntryType.ASSISTANT, EntryType.USER):
            last_meaningful = entry

        # Track whether a tool_result follows the most recent tool_use
        if entry_type == EntryType.ASSISTANT:
            raw_stop = entry.get("message", {}).get("stop_reason")
            if raw_stop == StopReason.TOOL_USE:
                tool_result_after_last_tool_use = False
        elif entry_type == EntryType.USER:
            content = entry.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_after_last_tool_use = True
                        break

    if last_meaningful is None:
        # No meaningful entries — build from whatever we have
        if last_any is None:
            raise ClaudeLogError(f"No parseable entries in {jsonl_path}")

        last_any_type = EntryType.from_raw(last_any["type"])
        return ClaudeSession(
            jsonl_path=jsonl_path,
            session_id=session_id,
            state=SessionState.IDLE if session_ended else SessionState.UNKNOWN,
            last_entry_type=last_any_type
            if last_any_type is not None
            else EntryType.SYSTEM,
            stop_reason=None,
            cwd=Path(last_any.get("cwd", "/")),
            git_branch=last_any.get("gitBranch"),
            last_activity=_parse_timestamp(last_any.get("timestamp", "")),
            title=custom_title,
            first_prompt=first_prompt,
        )

    entry_type = EntryType.from_raw(last_meaningful["type"])
    assert entry_type is not None  # last_meaningful is always assistant or user
    stop_reason: StopReason | None = None
    state: SessionState

    if session_ended:
        # Session has a last-prompt marker — it's no longer running
        state = SessionState.IDLE
    elif entry_type == EntryType.ASSISTANT:
        raw_stop = last_meaningful.get("message", {}).get("stop_reason")
        if raw_stop is not None:
            stop_reason = StopReason.from_raw(raw_stop)
            if stop_reason == StopReason.TOOL_USE:
                if tool_result_after_last_tool_use:
                    state = SessionState.WORKING
                else:
                    state = SessionState.WAITING_FOR_TOOL_APPROVAL
            else:
                state = SessionState.WAITING_FOR_USER
        else:
            # No stop_reason (interrupted/canceled via Esc) — user is at the prompt
            state = SessionState.WAITING_FOR_USER
    else:
        # Last meaningful entry is USER → working
        state = SessionState.WORKING

    return ClaudeSession(
        jsonl_path=jsonl_path,
        session_id=session_id,
        state=state,
        last_entry_type=entry_type,
        stop_reason=stop_reason,
        cwd=Path(last_meaningful.get("cwd", "/")),
        git_branch=last_meaningful.get("gitBranch"),
        last_activity=_parse_timestamp(last_meaningful.get("timestamp", "")),
        title=custom_title,
        first_prompt=first_prompt,
    )


def get_sessions_for_path(project_path: Path) -> list[ClaudeSession]:
    """Get all Claude sessions for a given project path.

    Returns sessions sorted by last_activity descending (most recent first).
    Returns an empty list if the encoded directory doesn't exist or has no JSONL files.
    """
    projects_dir = get_claude_projects_dir()
    encoded = encode_project_path(project_path)
    session_dir = projects_dir / encoded

    if not session_dir.is_dir():
        return []

    sessions: list[ClaudeSession] = []
    for jsonl_file in session_dir.glob("*.jsonl"):
        try:
            sessions.append(parse_session(jsonl_file))
        except ClaudeLogError:
            continue

    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions


def _parse_timestamp(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp string, returning epoch on failure."""
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)
