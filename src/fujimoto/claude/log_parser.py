from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from fujimoto import debug


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


# Claude replaces both separators *and dots* with hyphens when it encodes a
# project path into a directory name: `/repo/.fujimoto/worktrees/x` becomes
# `-repo--fujimoto-worktrees-x`, with a double hyphen where the `/.` was.
# Encoding only the slashes silently missed every transcript for a worktree
# under the default `<repo>/.fujimoto/worktrees/` root.
_ENCODE_CHARS = re.compile(r"[/.]")


def _normalize(path: Path | str) -> str:
    """Path as a plain string with any trailing slash removed."""
    return str(path).rstrip("/") or "/"


def encode_project_path(path: Path) -> str:
    """Encode a project path for use as a Claude projects directory name.

    Replaces forward slashes *and dots* with hyphens, matching Claude's
    convention.

    >>> encode_project_path(Path("/Users/alice/git/myproject"))
    '-Users-alice-git-myproject'
    >>> encode_project_path(Path("/Users/alice/git/worktrees/proj/20260309-fix"))
    '-Users-alice-git-worktrees-proj-20260309-fix'
    >>> encode_project_path(Path("/repo/.fujimoto/worktrees/20260309-fix"))
    '-repo--fujimoto-worktrees-20260309-fix'
    >>> encode_project_path(Path("/tmp/test/"))
    '-tmp-test'
    """
    return _ENCODE_CHARS.sub("-", _normalize(path))


def get_claude_projects_dir() -> Path:
    """Return the path to Claude's projects directory.

    Honours `CLAUDE_CONFIG_DIR`, which relocates Claude's whole config tree —
    transcripts included.

    >>> isinstance(get_claude_projects_dir(), Path)
    True
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "projects"


# How far into a log to look for the `cwd` it was recorded against. The first
# entry almost always carries one; the cap stops a pathological log being read
# in full.
_CWD_SCAN_LINES = 20

# (projects dir, its mtime_ns, cwd -> session dirs). Built only when an encoded
# lookup misses, and rebuilt when the projects directory gains or loses a child.
_cwd_index_cache: tuple[Path, int, dict[str, list[Path]]] | None = None


def _first_cwd(log: Path) -> str | None:
    """The `cwd` recorded near the start of a transcript, if any."""
    try:
        with log.open(errors="replace") as fh:
            for _, line in zip(range(_CWD_SCAN_LINES), fh):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("cwd"):
                    return _normalize(entry["cwd"])
    except OSError:
        return None
    return None


def _cwd_index() -> dict[str, list[Path]]:
    """Map every recorded `cwd` to the session directories holding it.

    The encoding above is a guess at what a *different* program does with a
    path, so it can be wrong again (a new munged character, a symlink Claude
    resolved and we did not). The transcripts themselves record the directory
    they ran in, which is ground truth — so a missed lookup falls back to this
    index rather than reporting no sessions.
    """
    global _cwd_index_cache

    projects_dir = get_claude_projects_dir()
    try:
        mtime = projects_dir.stat().st_mtime_ns
    except OSError:
        return {}

    if _cwd_index_cache is not None:
        cached_dir, cached_mtime, cached_index = _cwd_index_cache
        if cached_dir == projects_dir and cached_mtime == mtime:
            return cached_index

    index: dict[str, list[Path]] = {}
    try:
        entries = sorted(projects_dir.iterdir())
    except OSError:  # pragma: no cover - raced with a delete
        return {}
    for session_dir in entries:
        if not session_dir.is_dir():
            continue
        for log in sorted(session_dir.glob("*.jsonl")):
            cwd = _first_cwd(log)
            if cwd is None:
                continue
            dirs = index.setdefault(cwd, [])
            if session_dir not in dirs:
                dirs.append(session_dir)

    _cwd_index_cache = (projects_dir, mtime, index)
    debug.log(
        "claude.cwd_index",
        projects_dir=debug.rp(projects_dir),
        dirs=len(entries),
        cwds=len(index),
    )
    return index


def session_dirs_for_path(project_path: Path) -> list[Path]:
    """Claude session directories holding transcripts for `project_path`.

    Tries the encoded directory name for the path as given and as resolved
    (Claude records the *physical* cwd, so a symlinked worktree root would
    otherwise never match), then falls back to the recorded-cwd index.
    """
    projects_dir = get_claude_projects_dir()

    variants = _path_variants(project_path)
    candidates: list[Path] = []
    for variant in variants:
        session_dir = projects_dir / encode_project_path(variant)
        if session_dir not in candidates and session_dir.is_dir():
            candidates.append(session_dir)
    if candidates:
        # Which strategy resolved the lookup is the whole diagnostic value
        # here: path encoding is a guess at another program's convention.
        debug.log_capped(
            "claude.session_dirs.resolved",
            "claude.session_dirs",
            dedupe_key=f"claude-dirs-{project_path}",
            path=debug.rp(project_path),
            variants=len(variants),
            via="encoded-name",
            dirs=len(candidates),
        )
        return candidates

    index = _cwd_index()
    for variant in variants:
        found = index.get(_normalize(variant))
        if found:
            # The fallback firing is unusual enough to always be worth a line.
            debug.log_once(
                f"claude-dirs-{project_path}",
                "claude.session_dirs",
                path=debug.rp(project_path),
                variants=len(variants),
                via="cwd-index",
                dirs=len(found),
            )
            return list(found)
    # A path with no transcripts is the normal state of an old worktree, so
    # these are capped — but the encoded name is logged for the ones that are,
    # since a wrong encoding is exactly what this line exists to catch.
    debug.log_capped(
        "claude.session_dirs.missing",
        "claude.session_dirs",
        dedupe_key=f"claude-dirs-{project_path}",
        limit=5,
        path=debug.rp(project_path),
        variants=len(variants),
        via="none",
        dirs=0,
        encoded=debug.rv(encode_project_path(project_path)),
        indexed_cwds=len(index),
    )
    return []


def _path_variants(project_path: Path) -> list[Path]:
    """The path as given, plus its symlink-resolved form when they differ."""
    variants = [project_path]
    try:
        resolved = project_path.resolve()
    except OSError:  # pragma: no cover - resolve rarely raises
        return variants
    if _normalize(resolved) != _normalize(project_path):
        variants.append(resolved)
    return variants


def parse_session(jsonl_path: Path) -> ClaudeSession:
    """Parse a Claude session JSONL log file into a ClaudeSession.

    Raises ClaudeLogError if the file is empty, unreadable, or has no
    parseable entries.
    """
    session_id = jsonl_path.stem

    try:
        text = jsonl_path.read_text()
    except OSError as e:
        debug.log(
            "claude.parse_failed",
            log=debug.rp(jsonl_path),
            reason="unreadable",
            error=e,
        )
        raise ClaudeLogError(f"Cannot read {jsonl_path}: {e}")

    if not text.strip():
        debug.log("claude.parse_failed", log=debug.rp(jsonl_path), reason="empty")
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

        # A valid JSON line need not be an object; anything else has no entry
        # type to dispatch on, so skip it rather than crash the whole parse.
        if not isinstance(entry, dict):
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

    session = ClaudeSession(
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
    debug.log_once(
        f"claude-session-{session_id}",
        "claude.session",
        id=session_id,
        state=state,
        last_entry=entry_type,
        stop_reason=stop_reason,
        ended=session_ended,
        tool_result_after_tool_use=tool_result_after_last_tool_use,
        cwd=debug.rp(session.cwd),
        branch=debug.rv(session.git_branch),
        last_activity=session.last_activity.isoformat(),
        titled=custom_title is not None,
        bytes=len(text),
    )
    return session


def get_sessions_for_path(project_path: Path) -> list[ClaudeSession]:
    """Get all Claude sessions for a given project path.

    Returns sessions sorted by last_activity descending (most recent first).
    Returns an empty list when no session directory resolves to the path, or
    when the ones that do hold no parseable JSONL files.
    """
    encoded = encode_project_path(project_path)
    session_dirs = session_dirs_for_path(project_path)

    if not session_dirs:
        debug.log_capped(
            "claude.discovery.empty",
            "claude.discovery",
            dedupe_key=f"claude-dir-{encoded}",
            limit=5,
            path=debug.rp(project_path),
            encoded=debug.rv(encoded),
            projects_dir=debug.rp(get_claude_projects_dir()),
            exists=False,
        )
        return []

    sessions: list[ClaudeSession] = []
    seen: set[Path] = set()
    failed = 0
    for session_dir in session_dirs:
        for jsonl_file in sorted(session_dir.glob("*.jsonl")):
            if jsonl_file in seen:
                continue
            seen.add(jsonl_file)
            try:
                sessions.append(parse_session(jsonl_file))
            except ClaudeLogError:
                failed += 1
                continue

    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    latest = sessions[0].session_id if sessions else "none"
    if failed:
        # Never capped away: this is the difference between "no sessions" and
        # "sessions fujimoto could not read". The fields are spelled out in
        # both branches rather than unpacked from a dict, so a field can never
        # collide with `log_capped`'s own keywords.
        debug.log_once(
            f"claude-dir-{encoded}",
            "claude.discovery",
            path=debug.rp(project_path),
            encoded=debug.rv(encoded),
            exists=True,
            logs=len(seen),
            parsed=len(sessions),
            failed=failed,
            latest=latest,
        )
    else:
        debug.log_capped(
            "claude.discovery.found",
            "claude.discovery",
            dedupe_key=f"claude-dir-{encoded}",
            path=debug.rp(project_path),
            encoded=debug.rv(encoded),
            exists=True,
            logs=len(seen),
            parsed=len(sessions),
            failed=failed,
            latest=latest,
        )
    return sessions


def _parse_timestamp(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp string, returning epoch on failure."""
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


@dataclass(frozen=True)
class TranscriptMessage:
    """One renderable message from a Claude session transcript."""

    role: str
    text: str
    timestamp: datetime
    # `id` for a tool_use, `tool_use_id` for its result. Carried so a call can
    # be paired with its own result: parallel calls arrive as several tool_use
    # blocks followed by several results, where position alone would mis-pair
    # them.
    tool_id: str | None = None


# Tool inputs and results are frequently enormous (whole files, long diffs).
# The viewer is for reading a conversation back, so they are clipped.
_MAX_BLOCK_CHARS = 2000
_MAX_BLOCK_LINES = 20


def _clip(text: str) -> str:
    """Clip a long tool payload down to something readable."""
    lines = text.splitlines()
    clipped = False
    if len(lines) > _MAX_BLOCK_LINES:
        lines = lines[:_MAX_BLOCK_LINES]
        clipped = True
    text = "\n".join(lines)
    if len(text) > _MAX_BLOCK_CHARS:
        text = text[:_MAX_BLOCK_CHARS]
        clipped = True
    return f"{text.rstrip()}\n…" if clipped else text


def _block_text(block: dict) -> str:
    """Extract displayable text from a content block of unknown shape."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def read_transcript(jsonl_path: Path) -> list[TranscriptMessage]:
    """Read a Claude session log into an ordered list of messages.

    Sidechain (sub-agent) and meta entries are skipped, as are entries whose
    type the parser does not recognize. Raises ClaudeLogError if the file
    cannot be read.
    """
    try:
        text = jsonl_path.read_text()
    except OSError as e:
        raise ClaudeLogError(f"Cannot read {jsonl_path}: {e}")

    messages: list[TranscriptMessage] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = EntryType.from_raw(entry.get("type", ""))
        if entry_type not in (EntryType.ASSISTANT, EntryType.USER):
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue

        ts = _parse_timestamp(entry.get("timestamp", ""))
        content = entry.get("message", {}).get("content")
        role = "user" if entry_type == EntryType.USER else "assistant"

        if isinstance(content, str):
            if content.strip():
                messages.append(TranscriptMessage(role, content.strip(), ts))
            continue
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                body = block.get("text", "").strip()
                if body:
                    messages.append(TranscriptMessage(role, body, ts))
            elif block_type == "thinking":
                body = block.get("thinking", "").strip()
                if body:
                    messages.append(TranscriptMessage("thinking", body, ts))
            elif block_type == "tool_use":
                name = block.get("name", "tool")
                params = block.get("input")
                summary = ""
                if isinstance(params, dict):
                    summary = _clip(
                        "\n".join(f"{k}: {v}" for k, v in params.items())
                    ).strip()
                body = f"{name}\n{summary}" if summary else str(name)
                messages.append(
                    TranscriptMessage("tool_use", body, ts, block.get("id"))
                )
            elif block_type == "tool_result":
                body = _clip(_block_text(block)).strip()
                if body:
                    messages.append(
                        TranscriptMessage(
                            "tool_result", body, ts, block.get("tool_use_id")
                        )
                    )

    return messages
