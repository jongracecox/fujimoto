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
    def from_raw(cls, value: str) -> EntryType:
        """Convert a raw string to an EntryType.

        >>> EntryType.from_raw("assistant")
        <EntryType.ASSISTANT: 'assistant'>
        >>> EntryType.from_raw("user")
        <EntryType.USER: 'user'>
        """
        try:
            return cls(value)
        except ValueError:
            raise ClaudeLogError(
                f"Unknown entry type: {value!r}. Claude log format may have changed."
            )


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"

    @classmethod
    def from_raw(cls, value: str) -> StopReason:
        """Convert a raw string to a StopReason.

        >>> StopReason.from_raw("end_turn")
        <StopReason.END_TURN: 'end_turn'>
        >>> StopReason.from_raw("tool_use")
        <StopReason.TOOL_USE: 'tool_use'>
        """
        try:
            return cls(value)
        except ValueError:
            raise ClaudeLogError(
                f"Unknown stop_reason: {value!r}. Claude log format may have changed."
            )


class SessionState(StrEnum):
    WAITING_FOR_USER = "waiting_for_user"
    PROCESSING = "processing"
    UNKNOWN = "unknown"


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

    @property
    def is_active(self) -> bool:
        return self.state in (SessionState.WAITING_FOR_USER, SessionState.PROCESSING)


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

    Raises ClaudeLogError if the file is empty, unreadable, or contains
    unknown entry types or stop reasons.
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

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = EntryType.from_raw(entry["type"])
        last_any = entry

        if entry.get("isSidechain"):
            continue

        if entry_type in (EntryType.ASSISTANT, EntryType.USER):
            last_meaningful = entry

    if last_meaningful is None:
        # No meaningful entries — build from whatever we have
        if last_any is None:
            raise ClaudeLogError(f"No parseable entries in {jsonl_path}")

        return ClaudeSession(
            jsonl_path=jsonl_path,
            session_id=session_id,
            state=SessionState.UNKNOWN,
            last_entry_type=EntryType.from_raw(last_any["type"]),
            stop_reason=None,
            cwd=Path(last_any.get("cwd", "/")),
            git_branch=last_any.get("gitBranch"),
            last_activity=_parse_timestamp(last_any.get("timestamp", "")),
        )

    entry_type = EntryType.from_raw(last_meaningful["type"])
    stop_reason: StopReason | None = None
    state: SessionState

    if entry_type == EntryType.ASSISTANT:
        raw_stop = last_meaningful.get("message", {}).get("stop_reason")
        if raw_stop is not None:
            stop_reason = StopReason.from_raw(raw_stop)
            state = (
                SessionState.WAITING_FOR_USER
                if stop_reason == StopReason.END_TURN
                else SessionState.PROCESSING
            )
        else:
            state = SessionState.UNKNOWN
    else:
        # Last meaningful entry is USER → processing
        state = SessionState.PROCESSING

    return ClaudeSession(
        jsonl_path=jsonl_path,
        session_id=session_id,
        state=state,
        last_entry_type=entry_type,
        stop_reason=stop_reason,
        cwd=Path(last_meaningful.get("cwd", "/")),
        git_branch=last_meaningful.get("gitBranch"),
        last_activity=_parse_timestamp(last_meaningful.get("timestamp", "")),
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
