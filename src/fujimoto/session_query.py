"""Read-only session queries for the command line: `fujimoto search` and `log`.

The TUI's transcript search (`s`) and log viewer are interactive; these are
the same capabilities shaped for scripts and agents. Both print a compact text
form by default and a JSON document with `--json`, and neither needs tmux or
touches anything on disk.

Three questions every result answers:

* **Where is it?** — `Location`: the project, its root, and the worktree the
  transcript's `cwd` belongs to (None for the project root or a directory
  outside any repo).
* **Is it running?** — `Activity`, decided by `ActivityIndex`. Claude Code's
  own registry of running processes (`claude.live`) is the primary source,
  since it names the exact conversation each process is writing. Fujimoto's
  tmux sessions and `session_state` records are the fallback for a Claude
  Code too old to keep a registry — there, a live tmux session in the
  transcript's directory counts only if its record names this conversation,
  or (with no recorded id) this is the newest transcript in that directory.
* **What did it say?** — snippets for a search hit; the parsed transcript for
  `log`, falling back to the raw JSONL lines when the parser can't structure
  it, exactly as the TUI's viewer does.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TextIO, TypeVar

from fujimoto import debug, session_state
from fujimoto.claude import live
from fujimoto.claude import search as claude_search
from fujimoto.claude.log_parser import (
    ClaudeLogError,
    ClaudeSession,
    TranscriptMessage,
    get_claude_projects_dir,
    parse_session,
    read_raw_transcript,
    read_transcript,
    recorded_cwds_under,
    session_dirs_for_path,
)
from fujimoto.claude.search import (
    ContentMode,
    MatchBreakdown,
    SearchError,
    SearchHit,
    Snippet,
)
from fujimoto.config import (
    ConfigError,
    get_git_projects_root,
    get_project_worktrees_dir,
)
from fujimoto.git import GitError, get_main_worktree_root, get_repo_root
from fujimoto.tmux import list_session_paths

DEFAULT_LIMIT = 20


class SortOrder(StrEnum):
    """How `search` orders (and therefore cuts) its hits."""

    RECENT = "recent"
    RELEVANCE = "relevance"


_T = TypeVar("_T")

# Not a transcript role at all: harness-injected "user" entries, re-roled by
# `_classify` so they can be filtered like one. Hidden unless asked for.
INJECTED = "injected"
TRANSCRIPT_ROLES = (
    "user",
    "assistant",
    "thinking",
    "tool_use",
    "tool_result",
    INJECTED,
)
DEFAULT_ROLES = tuple(r for r in TRANSCRIPT_ROLES if r != INJECTED)
_INJECTED_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)

# Set by Claude Code in every shell it runs: the session issuing the command.
CALLING_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

# The registry is the primary source; tmux is what is left without it.
SOURCE_REGISTRY = "claude-registry"
SOURCE_TMUX = "tmux"

_TITLE_CHARS = 100

_MATCH_ON = "\033[1;33m"
_MATCH_OFF = "\033[0m"


class QueryError(Exception):
    """A query that cannot run: bad scope, bad regex, unknown session."""


def relative_time(dt: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    delta = now - dt
    if delta.days > 30:
        months = delta.days // 30
        return f"{months}mo ago"
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    minutes = delta.seconds // 60
    if minutes > 0:
        return f"{minutes}m ago"
    return "just now"


def _same_path(a: Path, b: Path) -> bool:
    return _resolved(a) == _resolved(b)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - resolve rarely raises
        return path


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What a search covers: one project (root + worktrees) or everything."""

    all_projects: bool
    project: str | None = None
    root: Path | None = None
    worktrees: tuple[Path, ...] = ()

    def describe(self) -> str:
        return "all projects" if self.all_projects else f"project {self.project}"


def _project_scope(root: Path) -> Scope:
    """The root plus every worktree it has had — including deleted ones.

    The live worktrees are the ones the home screen lists. A deleted worktree
    leaves its transcripts behind, and those are often exactly what a history
    search is for, so the worktrees directory is also matched against the
    `cwd`s Claude recorded: any immediate child of it with transcripts but no
    directory joins the scope. (A worktree that lived under a *different*
    worktrees root — the env var changed since — is not found.)
    """
    try:
        directory = get_project_worktrees_dir(root.name, root, create=False)
    except ConfigError:  # pragma: no cover - only without a root, never here
        return Scope(all_projects=False, project=root.name, root=root)
    existing = (
        {d for d in directory.iterdir() if d.is_dir()} if directory.is_dir() else set()
    )
    deleted: set[Path] = set()
    base = _resolved(directory)
    for cwd in recorded_cwds_under(base):
        if cwd == base:
            continue
        worktree = base / cwd.relative_to(base).parts[0]
        if not worktree.is_dir():
            deleted.add(directory / worktree.name)
    debug.log(
        "query.worktrees",
        existing=len(existing),
        deleted=len(deleted),
    )
    return Scope(
        all_projects=False,
        project=root.name,
        root=root,
        worktrees=tuple(sorted(existing | deleted)),
    )


def resolve_scope(
    *, project: str | None, all_projects: bool, cwd: Path | None = None
) -> Scope:
    """The search scope from `--project`, `--all-projects` or the cwd.

    `--project` takes a path (any directory inside the repo, or one of its
    worktrees) or a project name under `$FUJIMOTO_GIT_ROOT`. Either way the
    project is the *main* clone, so searching from a worktree covers its
    siblings too.
    """
    if all_projects:
        debug.log("query.scope", via="all")
        return Scope(all_projects=True)

    if project is not None:
        path = Path(project).expanduser()
        via = "path"
        if not path.is_dir():
            git_root = get_git_projects_root()
            if git_root is None or not (git_root / project).is_dir():
                hint = (
                    f"and no such project under $FUJIMOTO_GIT_ROOT ({git_root})"
                    if git_root
                    else "and $FUJIMOTO_GIT_ROOT is not set"
                )
                debug.log("query.scope", via="unknown", project=debug.rv(project))
                raise QueryError(
                    f"unknown project {project!r}: not a directory, {hint}"
                )
            path = git_root / project
            via = "git-root"
    else:
        path = cwd or Path.cwd()
        via = "cwd"

    try:
        root = get_main_worktree_root(path)
    except GitError as e:
        debug.log("query.scope", via=via, path=debug.rp(path), error="not-a-repo")
        if via == "cwd":
            raise QueryError(
                "not inside a git repository; pass --project or --all-projects"
            ) from e
        raise QueryError(f"{path} is not inside a git repository") from e

    scope = _project_scope(root)
    debug.log(
        "query.scope",
        via=via,
        project=debug.rv(root.name),
        root=debug.rp(root),
        worktrees=len(scope.worktrees),
    )
    return scope


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """Which project and worktree a transcript's working directory belongs to."""

    project: str | None
    project_root: Path | None
    worktree: Path | None

    def label(self, cwd: Path) -> str:
        """`project/worktree`, `project`, or the bare cwd when unknown."""
        if self.project is None:
            return str(cwd)
        if self.worktree is None:
            return self.project
        return f"{self.project}/{self.worktree.name}"


class Locator:
    """Maps a transcript `cwd` to a `Location`, memoized per directory.

    Within a project scope the answer comes from the scope itself, which also
    covers a worktree that has since been deleted. Across all projects it is
    asked of git, which only works while the directory exists — so a deleted
    worktree there has no project.
    """

    def __init__(self, scope: Scope) -> None:
        self._scope = scope
        self._cache: dict[Path, Location] = {}

    def locate(self, cwd: Path) -> Location:
        if cwd not in self._cache:
            self._cache[cwd] = self._locate(cwd)
        return self._cache[cwd]

    def _locate(self, cwd: Path) -> Location:
        scope = self._scope
        if not scope.all_projects:
            target = _resolved(cwd)
            for worktree in scope.worktrees:
                wt = _resolved(worktree)
                if target == wt or wt in target.parents:
                    return Location(scope.project, scope.root, worktree)
            return Location(scope.project, scope.root, None)

        if not cwd.is_dir():
            return Location(None, None, None)
        try:
            root = get_main_worktree_root(cwd)
            top = get_repo_root(cwd)
        except GitError:
            debug.log_capped(
                "query.locate.failed",
                "query.locate",
                dedupe_key=str(cwd),
                limit=5,
                cwd=debug.rp(cwd),
                outcome="not-a-repo",
            )
            return Location(None, None, None)
        worktree = None if _same_path(top, root) else top
        return Location(root.name, root, worktree)


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """Whether a transcript's conversation is running, and where."""

    running: bool
    source: str | None = None
    claude_session_id: str | None = None
    pid: int | None = None
    peer_name: str | None = None
    status: str | None = None
    tmux_session: str | None = None

    def as_json(self) -> dict[str, object]:
        if not self.running:
            return {"running": False}
        return {
            "running": True,
            "source": self.source,
            "claude_session_id": self.claude_session_id,
            "pid": self.pid,
            "peer_name": self.peer_name,
            "status": self.status,
            "tmux_session": self.tmux_session,
        }

    def describe(self) -> str:
        if not self.running:
            return "no"
        parts = [self.status or "running"]
        if self.peer_name:
            parts.append(f"peer {self.peer_name}")
        if self.tmux_session:
            parts.append(f"tmux {self.tmux_session}")
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        return " · ".join(parts)


NOT_RUNNING = Activity(running=False)


def _latest_session_id(cwd: Path) -> str | None:
    """The newest transcript's id for a directory, by mtime — no parsing."""
    newest: tuple[float, str] | None = None
    for session_dir in session_dirs_for_path(cwd):
        for log in session_dir.glob("*.jsonl"):
            try:
                mtime = log.stat().st_mtime
            except OSError:  # pragma: no cover - vanished glob->stat
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, log.stem)
    return newest[1] if newest else None


class ActivityIndex:
    """Running sessions, gathered once per command and matched per transcript."""

    def __init__(
        self,
        live_sessions: Sequence[live.LiveSession],
        tmux_paths: dict[str, Path],
        records: dict[str, session_state.SessionRecord],
    ) -> None:
        self._by_id = {entry.session_id: entry for entry in live_sessions}
        self._live_cwds = {_resolved(e.cwd) for e in live_sessions if e.cwd}
        self._tmux_by_cwd: dict[Path, list[str]] = {}
        for name, path in tmux_paths.items():
            self._tmux_by_cwd.setdefault(_resolved(path), []).append(name)
        self._records = records
        self._latest: dict[Path, str | None] = {}

    @classmethod
    def gather(cls) -> ActivityIndex:
        return cls(
            live.list_live_sessions(),
            list_session_paths(),
            session_state.load_state(),
        )

    def for_session(self, session_id: str, cwd: Path) -> Activity:
        activity, reason = self._match(session_id, cwd)
        debug.log_capped(
            "query.activity",
            "query.activity",
            dedupe_key=session_id,
            limit=20,
            session=debug.rv(session_id),
            source=activity.source or "none",
            reason=reason,
        )
        return activity

    def _match(self, session_id: str, cwd: Path) -> tuple[Activity, str]:
        entry = self._by_id.get(session_id)
        target = _resolved(cwd)
        if entry is not None:
            tmux_name = entry.tmux_session
            if tmux_name is None:
                names = self._tmux_by_cwd.get(target, [])
                tmux_name = names[0] if len(names) == 1 else None
            activity = Activity(
                running=True,
                source=SOURCE_REGISTRY,
                claude_session_id=entry.session_id,
                pid=entry.pid,
                peer_name=entry.peer_name,
                status=entry.status,
                tmux_session=tmux_name,
            )
            return activity, "registry-id"

        # The registry knows a process in this directory, and it is writing a
        # different conversation — so this one is not live, whatever tmux says.
        if target in self._live_cwds:
            return NOT_RUNNING, "registry-other-session"

        for name in self._tmux_by_cwd.get(target, []):
            record = self._records.get(name)
            recorded = record.claude_session_id if record else None
            if recorded:
                matched = recorded == session_id
            else:
                if target not in self._latest:
                    self._latest[target] = _latest_session_id(cwd)
                matched = self._latest[target] == session_id
            if matched:
                activity = Activity(
                    running=True,
                    source=SOURCE_TMUX,
                    claude_session_id=session_id,
                    tmux_session=name,
                )
                return activity, "tmux-record" if recorded else "tmux-latest"
        return NOT_RUNNING, "none"


# --------------------------------------------------------------------------
# Shared rendering
# --------------------------------------------------------------------------


def _title(session: ClaudeSession) -> str | None:
    text = session.title or session.first_prompt
    if not text:
        return None
    line = " ".join(text.split())
    if len(line) > _TITLE_CHARS:
        line = line[: _TITLE_CHARS - 1].rstrip() + "…"
    return line


def _session_json(
    session: ClaudeSession, location: Location, activity: Activity
) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "project": location.project,
        "project_root": str(location.project_root) if location.project_root else None,
        "worktree": str(location.worktree) if location.worktree else None,
        "cwd": str(session.cwd),
        "cwd_exists": session.cwd.is_dir(),
        "branch": session.git_branch,
        "title": session.title,
        "first_prompt": session.first_prompt,
        "last_activity": session.last_activity.isoformat(),
        "state": session.state.value,
        "log_path": str(session.jsonl_path),
        "active": activity.as_json(),
    }


def _session_detail_lines(
    session: ClaudeSession, activity: Activity, indent: str
) -> list[str]:
    cwd = str(session.cwd) + ("" if session.cwd.is_dir() else "  (deleted)")
    rows = [("session", session.session_id), ("cwd", cwd)]
    if session.git_branch:
        rows.append(("branch", session.git_branch))
    title = _title(session)
    if title:
        rows.append(("title", title))
    rows.append(("active", activity.describe()))
    return [f"{indent}{key + ':':<9} {value}" for key, value in rows]


def use_color(stream: TextIO) -> bool:
    """ANSI highlighting only for a terminal, and never under `NO_COLOR`."""
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _highlight(snippet: Snippet, color: bool) -> str:
    """A snippet with its matches wrapped in ANSI bold, when `color`.

    >>> _highlight(Snippet("a needle", ((2, 8),)), True)
    'a \\x1b[1;33mneedle\\x1b[0m'
    >>> _highlight(Snippet("a needle", ((2, 8),)), False)
    'a needle'
    """
    if not color:
        return snippet.text
    out: list[str] = []
    pos = 0
    for start, end in snippet.spans:
        out.append(snippet.text[pos:start])
        out.append(f"{_MATCH_ON}{snippet.text[start:end]}{_MATCH_OFF}")
        pos = end
    out.append(snippet.text[pos:])
    return "".join(out)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    hit: SearchHit
    location: Location
    activity: Activity


@dataclass(frozen=True)
class SearchReport:
    query: str
    regex: bool
    case_sensitive: bool
    mode: ContentMode
    scope: Scope
    total_logs: int
    scanned: int
    truncated: bool
    results: tuple[SearchResult, ...]
    # The calling session, when its own transcript matched and was left out.
    excluded_session: str | None = None
    sort: SortOrder = SortOrder.RECENT


def calling_session_id() -> str | None:
    """The Claude session running this command, if any.

    An agent searching for what it was just asked about always matches its own
    transcript first, so `search` leaves it out unless asked not to.
    """
    return os.environ.get(CALLING_SESSION_ENV) or None


def run_search(
    query: str,
    scope: Scope,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    mode: ContentMode = ContentMode.TEXT,
    limit: int = DEFAULT_LIMIT,
    exclude_session: str | None = None,
    sort: SortOrder = SortOrder.RECENT,
    activity: ActivityIndex | None = None,
) -> SearchReport:
    """Search the scope's transcripts, newest first, stopping at `limit` hits.

    Logs are scanned in mtime order, so the first `limit` hits are the most
    recently used sessions and the rest of the logs need not be read at all.
    `limit=0` scans everything. `truncated` says results may have been left
    out — either hits past the limit, or logs never scanned. A hit for
    `exclude_session` is dropped before the limit is applied.

    `SortOrder.RELEVANCE` ranks by `relevance_key` instead, which needs every
    log scanned before the top `limit` can be known — no early stop.
    """
    try:
        matcher = claude_search.compile_matcher(
            query, regex=regex, mode=mode, case_sensitive=case_sensitive
        )
    except SearchError as e:
        raise QueryError(str(e)) from e

    if scope.all_projects:
        logs = claude_search.list_all_session_logs()
    else:
        logs = claude_search.list_session_logs(scope.root, scope.worktrees)

    hits: list[SearchHit] = []
    excluded = False
    scanned = 0
    for scanned, batch in claude_search.iter_hits(logs, matcher):
        for hit in batch:
            if exclude_session and hit.session.session_id == exclude_session:
                excluded = True
            else:
                hits.append(hit)
        if sort is SortOrder.RECENT and limit and len(hits) >= limit:
            break
    truncated = bool(limit) and (len(hits) > limit or scanned < len(logs))
    if sort is SortOrder.RELEVANCE:
        hits.sort(key=relevance_key, reverse=True)
        hits = hits[:limit] if limit else hits
    else:
        hits = hits[:limit] if limit else hits
        hits.sort(key=lambda h: h.session.last_activity, reverse=True)

    results: list[SearchResult] = []
    if hits:
        index = activity or ActivityIndex.gather()
        locator = Locator(scope)
        for hit in hits:
            cwd = hit.session.cwd
            results.append(
                SearchResult(
                    hit,
                    locator.locate(cwd),
                    index.for_session(hit.session.session_id, cwd),
                )
            )
    debug.log(
        "query.search",
        scope="all" if scope.all_projects else "project",
        logs=len(logs),
        scanned=scanned,
        hits=len(results),
        truncated=truncated,
        limit=limit,
        mode=mode,
        excluded_self=excluded,
        sort=sort,
        running=sum(1 for r in results if r.activity.running),
    )
    return SearchReport(
        query=query,
        regex=regex,
        case_sensitive=case_sensitive,
        mode=mode,
        scope=scope,
        total_logs=len(logs),
        scanned=scanned,
        truncated=truncated,
        results=tuple(results),
        excluded_session=exclude_session if excluded else None,
        sort=sort,
    )


def relevance_key(hit: SearchHit) -> tuple[int, int, datetime]:
    """Rank a hit: message matches, then tool matches, then recency.

    A session *working on* something names it again and again in what the user
    and Claude say; one that merely saw it — a Jira search, a log listing —
    has it only in tool output. So message matches lead, and raw match count
    (which a single long listing inflates) is not used at all. Without a
    breakdown, every match counts as a message match.
    """
    b = hit.breakdown
    message = b.message if b is not None else hit.match_count
    tool = b.tool or 0 if b is not None else 0
    return message, tool, hit.session.last_activity


def _breakdown_json(breakdown: MatchBreakdown | None) -> dict[str, int | None] | None:
    if breakdown is None:
        return None
    return {
        "message": breakdown.message,
        "tool": breakdown.tool,
        "other": breakdown.other,
    }


def search_json(report: SearchReport) -> dict[str, object]:
    scope = report.scope
    return {
        "query": report.query,
        "regex": report.regex,
        "case_sensitive": report.case_sensitive,
        "mode": report.mode.value,
        "scope": {
            "all_projects": scope.all_projects,
            "project": scope.project,
            "project_root": str(scope.root) if scope.root else None,
        },
        "total_logs": report.total_logs,
        "scanned": report.scanned,
        "truncated": report.truncated,
        "excluded_session": report.excluded_session,
        "sort": report.sort.value,
        "hits": [
            {
                **_session_json(r.hit.session, r.location, r.activity),
                "match_count": r.hit.match_count,
                "breakdown": _breakdown_json(r.hit.breakdown),
                "snippets": [
                    {"text": s.text, "spans": [list(span) for span in s.spans]}
                    for s in r.hit.snippets
                ],
            }
            for r in report.results
        ],
    }


def _plural(n: int, word: str) -> str:
    """`1 match`, `2 matches`, `3 messages`.

    >>> _plural(1, "match"), _plural(2, "match"), _plural(0, "line")
    ('1 match', '2 matches', '0 lines')
    """
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}{'es' if word.endswith(('s', 'ch')) else 's'}"


def _describe_matches(hit: SearchHit) -> str:
    """`4 matches`, or with a raw-mode breakdown `74 matches: 6 message, 68 tool`.

    Zero categories are left out, except `message`, whose zero is the point:
    it says the session never *discussed* the query, it only saw it.
    """
    text = _plural(hit.match_count, "match")
    b = hit.breakdown
    if b is None or b.tool is None:
        return text
    parts = [f"{b.message} message"]
    if b.tool:
        parts.append(f"{b.tool} tool")
    if b.other:
        parts.append(f"{b.other} other")
    return f"{text}: {', '.join(parts)}"


def format_search(report: SearchReport, *, color: bool = False) -> str:
    """The human- and agent-readable result listing."""
    skipped = (
        "; skipped the calling session (--include-self to show it)"
        if report.excluded_session
        else ""
    )
    if not report.results:
        return (
            f"No matches for {report.query!r} in {report.total_logs} transcripts "
            f"({report.scope.describe()}, {report.mode.label}){skipped}."
        )

    lines: list[str] = []
    for r in report.results:
        session = r.hit.session
        marker = "●" if r.activity.running else "○"
        when = relative_time(session.last_activity)
        label = r.location.label(session.cwd)
        lines.append(f"{marker} {label}  ({_describe_matches(r.hit)}; {when})")
        lines.extend(_session_detail_lines(session, r.activity, "  "))
        lines.extend(f"    {_highlight(s, color)}" for s in r.hit.snippets)
        lines.append("")

    count = len(report.results)
    footer = (
        f"{count} matching session{'s' if count != 1 else ''} · scanned "
        f"{report.scanned}/{report.total_logs} transcripts · "
        f"{report.scope.describe()} · {report.mode.label}"
    )
    if report.sort is SortOrder.RELEVANCE:
        footer += " · by relevance"
        if report.truncated:
            footer += f" · top {count} shown"
    elif report.truncated:
        footer += f" · stopped at --limit {count}; more may match"
    if report.excluded_session:
        footer += " · skipped the calling session (--include-self to show it)"
    lines.append(footer)
    lines.append(
        "Next: fujimoto log <session> --summary, or --grep PATTERN to find the passage"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Log
# --------------------------------------------------------------------------


def find_session_log(ref: str, projects_dir: Path | None = None) -> Path:
    """The transcript for a session id, a unique id prefix, or a `.jsonl` path."""
    path = Path(ref).expanduser()
    if ref.endswith(".jsonl") and path.is_file():
        debug.log("query.log_resolve", via="path")
        return path

    root = projects_dir or get_claude_projects_dir()
    escaped = glob.escape(ref)
    exact = list(root.glob(f"*/{escaped}.jsonl"))
    if exact:
        debug.log("query.log_resolve", via="exact", candidates=len(exact))
        return max(exact, key=_mtime)

    candidates = list(root.glob(f"*/{escaped}*.jsonl")) if ref else []
    ids = sorted({c.stem for c in candidates})
    debug.log("query.log_resolve", via="prefix", ids=len(ids))
    if len(ids) == 1:
        return max(candidates, key=_mtime)
    if not ids:
        raise QueryError(f"no session log found for {ref!r}")
    shown = ", ".join(ids[:5]) + (", …" if len(ids) > 5 else "")
    raise QueryError(f"{ref!r} matches {len(ids)} sessions: {shown}")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - vanished between glob and stat
        return 0.0


def _classify(message: TranscriptMessage) -> TranscriptMessage:
    """Re-role a harness-injected "user" message as `injected`.

    Background-task notifications, system reminders and slash-command output
    are recorded as user entries, but nobody typed them. They made up a third
    of the "user" messages in a real transcript, which buries the turns that
    matter.

    >>> from datetime import datetime, timezone
    >>> ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    >>> _classify(TranscriptMessage("user", "<task-notification>x", ts)).role
    'injected'
    >>> _classify(TranscriptMessage("user", "fix it", ts)).role
    'user'
    >>> _classify(TranscriptMessage("assistant", "<system-reminder>", ts)).role
    'assistant'
    """
    if message.role == "user" and message.text.lstrip().startswith(_INJECTED_PREFIXES):
        return replace(message, role=INJECTED)
    return message


@dataclass(frozen=True)
class Link:
    """A GitHub PR, Jira issue or Slack message a transcript refers to."""

    kind: str
    url: str
    label: str
    mentions: int
    # Kind-specific identifiers an agent can pass straight to another tool:
    # `repo`/`number`, `key`, or `channel`/`ts`.
    ids: tuple[tuple[str, str], ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "url": self.url,
            "label": self.label,
            "mentions": self.mentions,
            **dict(self.ids),
        }


_LINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pr", re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")),
    (
        "jira",
        re.compile(r"https://([\w-]+\.atlassian\.net)/browse/([A-Z][A-Z0-9]+-\d+)"),
    ),
    (
        "slack",
        re.compile(
            r"https://([\w-]+)\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})"
        ),
    ),
)


def _link_for(kind: str, match: re.Match[str]) -> tuple[str, str, tuple]:
    """(canonical url, label, ids) for one regex match."""
    if kind == "pr":
        repo, number = match.groups()
        url = f"https://github.com/{repo}/pull/{number}"
        return url, f"{repo}#{number}", (("repo", repo), ("number", number))
    if kind == "jira":
        host, key = match.groups()
        return f"https://{host}/browse/{key}", key, (("key", key),)
    workspace, channel, secs, micros = match.groups()
    ts = f"{secs}.{micros}"
    url = f"https://{workspace}.slack.com/archives/{channel}/p{secs}{micros}"
    return url, f"{channel} {ts}", (("channel", channel), ("ts", ts))


def extract_links(
    texts: Sequence[str], *, kinds: Sequence[str] | None = None
) -> list[Link]:
    """Links found in `texts`, deduplicated, in order of first mention.

    A Jira link is keyed by its issue (a `?focusedCommentId=` is dropped) and a
    Slack link by its message, so repeated mentions count up rather than
    listing the same thing twice.

    >>> [l.label for l in extract_links([
    ...     "see https://x.atlassian.net/browse/AB-1?focusedCommentId=2 and",
    ...     "https://x.atlassian.net/browse/AB-1 plus https://github.com/o/r/pull/7",
    ... ])]
    ['AB-1', 'o/r#7']
    >>> extract_links(["https://w.slack.com/archives/C1/p1792437915766929"])[0].ids
    (('channel', 'C1'), ('ts', '1792437915.766929'))
    """
    found: dict[str, list] = {}
    for text in texts:
        for kind, pattern in _LINK_PATTERNS:
            if kinds is not None and kind not in kinds:
                continue
            for match in pattern.finditer(text):
                url, label, ids = _link_for(kind, match)
                if url in found:
                    found[url][3] += 1
                else:
                    found[url] = [kind, url, label, 1, ids]
    return [Link(k, u, label, n, ids) for k, u, label, n, ids in found.values()]


def _message_links(messages: Sequence[TranscriptMessage]) -> list[Link]:
    """Links in what the session said and did, not in everything it read.

    Messages and tool-call inputs count; tool *output* counts only for a PR,
    since that is where the session's own `gh pr create` reports its PR. Any
    other link in tool output is mostly a listing — a Jira search returns
    dozens of issues the session never worked on.
    """
    said = [m.text for m in messages if m.role in ("user", "assistant", "tool_use")]
    results = [m.text for m in messages if m.role == "tool_result"]
    links = extract_links(said)
    seen = {link.url for link in links}
    for link in extract_links(results, kinds=("pr",)):
        if link.url not in seen:
            links.append(link)
    return links


@dataclass(frozen=True)
class Summary:
    """The shape of a session at a glance: how it opened, how it ended, links."""

    first_prompt: tuple[int, TranscriptMessage] | None
    # The final exchange: the reply the user last answered, their answer, and
    # Claude's last word. A last user message is often just "yes, do it" — it
    # means nothing without the reply it agreed to.
    replied_to: tuple[int, TranscriptMessage] | None
    last_user: tuple[int, TranscriptMessage] | None
    last_assistant: tuple[int, TranscriptMessage] | None
    role_counts: dict[str, int]
    links: tuple[Link, ...]


def _last(items: list[tuple[int, TranscriptMessage]], role: str):
    return next((item for item in reversed(items) if item[1].role == role), None)


def summarize(messages: list[tuple[int, TranscriptMessage]]) -> Summary:
    counts: dict[str, int] = {}
    for _, m in messages:
        counts[m.role] = counts.get(m.role, 0) + 1
    last_user = _last(messages, "user")
    before = [item for item in messages if last_user and item[0] < last_user[0]]
    return Summary(
        first_prompt=next((item for item in messages if item[1].role == "user"), None),
        replied_to=_last(before, "assistant"),
        last_user=last_user,
        last_assistant=_last(messages, "assistant"),
        role_counts=counts,
        links=tuple(_message_links([m for _, m in messages])),
    )


@dataclass(frozen=True)
class LogReport:
    path: Path
    session: ClaudeSession | None
    location: Location | None
    activity: Activity
    # `messages` when the transcript parsed, `lines` when it fell back to raw.
    # Each is `(index, item)`, `index` being the position in the whole
    # transcript so a message can be referred to across differently filtered
    # views.
    messages: tuple[tuple[int, TranscriptMessage], ...] | None
    lines: tuple[tuple[int, str], ...] | None
    parse_error: str | None
    # Messages (or lines) the role filter kept, before --grep/--head/--tail.
    total: int
    # How many matched --grep, or None without one.
    matched: int | None = None
    # Indices printed after a gap in a --grep view, so the text form can mark
    # where context was cut.
    breaks: frozenset[int] = frozenset()
    summary: Summary | None = None
    # `--summary` on a raw fallback: only the links can be recovered.
    summarized: bool = False
    raw_links: tuple[Link, ...] = ()

    @property
    def session_id(self) -> str:
        return self.session.session_id if self.session else self.path.stem

    @property
    def format(self) -> str:
        return "parsed" if self.lines is None else "raw"

    @property
    def shown(self) -> int:
        return len(self.messages if self.messages is not None else self.lines or ())


def _select(
    items: list[tuple[int, _T]],
    text_of: Callable[[_T], str],
    *,
    matcher: claude_search.Matcher | None,
    context: int,
    head: int | None,
    tail: int | None,
) -> tuple[list[tuple[int, _T]], int | None, frozenset[int]]:
    """Apply --grep (with context), then --head / --tail, to filtered items."""
    matched: int | None = None
    breaks: set[int] = set()
    if matcher is not None:
        hits = [
            i for i, (_, item) in enumerate(items) if matcher.present_in(text_of(item))
        ]
        matched = len(hits)
        keep: set[int] = set()
        for i in hits:
            keep.update(range(max(0, i - context), min(len(items), i + context + 1)))
        positions = sorted(keep)
        breaks = {
            items[pos][0]
            for prev, pos in zip(positions, positions[1:])
            if pos != prev + 1
        }
        items = [items[pos] for pos in positions]
    if head:
        items = items[:head]
    if tail:
        items = items[-tail:]
    return items, matched, frozenset(breaks)


def load_log(
    ref: str,
    *,
    roles: Sequence[str] | None = None,
    grep: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    context: int = 0,
    head: int | None = None,
    tail: int | None = None,
    summary: bool = False,
    activity: ActivityIndex | None = None,
) -> LogReport:
    """Read a session's transcript: parsed, or raw lines if the parser fails.

    `roles` filters parsed messages (default: everything but `injected`) and
    is meaningless for raw lines. `grep` keeps the messages (or lines) it
    matches plus `context` either side; `head`/`tail` then keep the first or
    last N (0 or None keeps all). `summary` replaces the messages with the
    session's first prompt, final exchange and the links it mentions.
    """
    matcher = None
    if grep is not None:
        try:
            matcher = claude_search.compile_matcher(
                grep, regex=regex, mode=ContentMode.TEXT, case_sensitive=case_sensitive
            )
        except SearchError as e:
            raise QueryError(str(e)) from e

    path = find_session_log(ref)
    try:
        session: ClaudeSession | None = parse_session(path)
    except ClaudeLogError:
        session = None
    except Exception as exc:  # same contract as the TUI's discovery path
        debug.log_exception("query.session_unparsed", exc)
        session = None

    parsed: list[TranscriptMessage] | None = None
    raw: list[str] | None = None
    parse_error: str | None = None
    try:
        parsed = read_transcript(path)
    except ClaudeLogError as e:
        raise QueryError(str(e)) from e
    except Exception as exc:
        # An entry shape the parser doesn't know is a parser bug, but not a
        # reason to withhold the transcript: show its lines as written.
        debug.log_exception("claude.transcript_unparsed", exc)
        parse_error = f"{type(exc).__name__}: {exc}"
        try:
            raw = read_raw_transcript(path)
        except ClaudeLogError as e:  # pragma: no cover - read just succeeded
            raise QueryError(str(e)) from e

    messages: list[tuple[int, TranscriptMessage]] | None = None
    lines: list[tuple[int, str]] | None = None
    matched: int | None = None
    breaks: frozenset[int] = frozenset()
    digest: Summary | None = None
    raw_links: tuple[Link, ...] = ()
    if parsed is not None:
        everything = [(i, _classify(m)) for i, m in enumerate(parsed)]
        if summary:
            digest = summarize(everything)
            messages = []
            total = len(everything)
        else:
            wanted = set(roles) if roles else set(DEFAULT_ROLES)
            messages = [item for item in everything if item[1].role in wanted]
            total = len(messages)
            messages, matched, breaks = _select(
                messages,
                lambda m: m.text,
                matcher=matcher,
                context=context,
                head=head,
                tail=tail,
            )
    else:
        indexed = list(enumerate(raw or []))
        total = len(indexed)
        if summary:
            raw_links = tuple(extract_links([line for _, line in indexed]))
            lines = []
        else:
            lines, matched, breaks = _select(
                indexed,
                lambda line: line,
                matcher=matcher,
                context=context,
                head=head,
                tail=tail,
            )

    location: Location | None = None
    found = NOT_RUNNING
    if session is not None:
        location = Locator(Scope(all_projects=True)).locate(session.cwd)
        index = activity or ActivityIndex.gather()
        found = index.for_session(session.session_id, session.cwd)

    report = LogReport(
        path=path,
        session=session,
        location=location,
        activity=found,
        messages=tuple(messages) if messages is not None else None,
        lines=tuple(lines) if lines is not None else None,
        parse_error=parse_error,
        total=total,
        matched=matched,
        breaks=breaks,
        summary=digest,
        summarized=summary,
        raw_links=raw_links,
    )
    debug.log(
        "query.log",
        log=debug.rp(path),
        format=report.format,
        total=total,
        shown=report.shown,
        grep=matcher is not None,
        matched=matched if matched is not None else "none",
        summary=summary,
        links=len(digest.links if digest else raw_links),
        metadata=session is not None,
        running=found.running,
    )
    return report


def _message_json(index: int, m: TranscriptMessage) -> dict[str, object]:
    return {
        "index": index,
        "role": m.role,
        "text": m.text,
        "timestamp": m.timestamp.isoformat(),
        "tool_id": m.tool_id,
    }


def log_json(report: LogReport) -> dict[str, object]:
    if report.session is not None and report.location is not None:
        header = _session_json(report.session, report.location, report.activity)
    else:
        header = {
            "session_id": report.session_id,
            "log_path": str(report.path),
            "active": report.activity.as_json(),
        }
    body: dict[str, object] = {
        "format": report.format,
        "parse_error": report.parse_error,
        "total": report.total,
        "matched": report.matched,
    }
    digest = report.summary
    if digest is not None:
        body["summary"] = {
            "first_prompt": _message_json(*digest.first_prompt)
            if digest.first_prompt
            else None,
            "replied_to": _message_json(*digest.replied_to)
            if digest.replied_to
            else None,
            "last_user": _message_json(*digest.last_user) if digest.last_user else None,
            "last_assistant": _message_json(*digest.last_assistant)
            if digest.last_assistant
            else None,
            "role_counts": digest.role_counts,
            "links": [link.as_json() for link in digest.links],
        }
    elif report.summarized:
        body["summary"] = {"links": [link.as_json() for link in report.raw_links]}
    elif report.messages is not None:
        body["messages"] = [_message_json(i, m) for i, m in report.messages]
    else:
        body["lines"] = [{"index": i, "text": line} for i, line in report.lines or ()]
    return {**header, **body}


def _local_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _format_links(links: Sequence[Link]) -> list[str]:
    if not links:
        return ["(none)"]
    width = max(len(link.label) for link in links)
    return [
        f"{link.kind:<6} {link.label:<{width}}  {link.url}"
        + (f"  ({link.mentions} mentions)" if link.mentions > 1 else "")
        for link in links
    ]


def _format_summary(digest: Summary) -> list[str]:
    out: list[str] = []
    sections = [("first prompt", digest.first_prompt)]
    if digest.last_user is not None and digest.last_user != digest.first_prompt:
        sections.append(("reply the user last answered", digest.replied_to))
        sections.append(("last user message", digest.last_user))
    sections.append(("last assistant message", digest.last_assistant))
    for title, item in sections:
        if item is None:
            continue
        index, m = item
        out.append(f"## {title} · #{index} · {_local_time(m.timestamp)}")
        out.append(m.text)
        out.append("")
    out.append("## links")
    out.extend(_format_links(digest.links))
    return out


def format_log(report: LogReport) -> str:
    """The transcript as role-headed text blocks, or raw lines under a warning."""
    unit = "message" if report.lines is None else "line"
    count = _plural(report.total, unit)
    if report.summarized:
        if report.summary is not None:
            roles = ", ".join(f"{n} {r}" for r, n in report.summary.role_counts.items())
            count += f" ({roles})" if roles else ""
    elif report.matched is not None:
        count += f", {_plural(report.matched, 'match')} for --grep"
        if report.shown != report.matched:
            count += f", showing {report.shown} with context"
    elif report.shown < report.total:
        count += f", showing {report.shown}"
    view = report.format
    if report.summarized:
        view = "summary" if report.lines is None else "raw summary"
    out = [f"# session {report.session_id} · {view} · {count}"]
    if report.session is not None:
        out.extend(_session_detail_lines(report.session, report.activity, ""))
    else:
        out.append(f"{'log:':<9} {report.path}")
    if report.parse_error:
        out.append(f"{'warning:':<9} could not parse transcript ({report.parse_error})")
    out.append("")

    if report.summary is not None:
        out.extend(_format_summary(report.summary))
        return "\n".join(out)
    if report.lines is not None:
        if report.summarized:
            out.append("## links")
            out.extend(_format_links(report.raw_links))
            return "\n".join(out)
        for index, line in report.lines:
            if index in report.breaks:
                out.append("⋯")
            out.append(line)
        return "\n".join(out)

    for index, m in report.messages or ():
        if index in report.breaks:
            out.append("⋯")
            out.append("")
        out.append(f"### {m.role} · #{index} · {_local_time(m.timestamp)}")
        out.append(m.text)
        out.append("")
    return "\n".join(out).rstrip("\n")


def dump_json(data: dict[str, object]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Resume / stop support
# --------------------------------------------------------------------------


def wait_for_registration(
    tmux_session: str,
    *,
    timeout: float,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> live.LiveSession | None:
    """The registry entry for the claude process in `tmux_session`, once it has one.

    A process started in the background writes its registry file a second or
    two after tmux starts it, and that file is the only place to learn a
    fork's new conversation id — and the proof the process came up at all.
    Polls until `timeout` seconds have passed; `timeout=0` checks once.
    """
    deadline = clock() + timeout
    polls = 0
    while True:
        polls += 1
        for entry in live.list_live_sessions():
            if entry.tmux_session == tmux_session:
                debug.log("query.registered", found=True, polls=polls)
                return entry
        if clock() >= deadline:
            debug.log("query.registered", found=False, polls=polls)
            return None
        sleep(interval)


def resolve_session(ref: str) -> ClaudeSession:
    """The parsed session for an id, id prefix or `.jsonl` path."""
    path = find_session_log(ref)
    try:
        return parse_session(path)
    except ClaudeLogError as e:
        raise QueryError(f"cannot read session {path.stem}: {e}") from e


def tmux_for_session(ref: str) -> tuple[str, str | None]:
    """The tmux session running a Claude session: `(tmux name, session id)`.

    `ref` is a tmux session name, or anything `resolve_session` takes. A live
    Claude session with no tmux session behind it (started outside fujimoto
    and outside tmux) cannot be stopped from here.
    """
    paths = list_session_paths()
    if ref in paths:
        record = session_state.load_state().get(ref)
        return ref, record.claude_session_id if record else None
    session = resolve_session(ref)
    activity = ActivityIndex.gather().for_session(session.session_id, session.cwd)
    if not activity.running:
        raise QueryError(f"session {session.session_id} is not running")
    if activity.tmux_session is None:
        raise QueryError(
            f"session {session.session_id} is running outside tmux "
            f"(pid {activity.pid}); stop it from its own terminal"
        )
    return activity.tmux_session, activity.claude_session_id


# --------------------------------------------------------------------------
# Agent instructions
# --------------------------------------------------------------------------

AGENT_INSTRUCTIONS = "agent_instructions.md"


def agent_instructions() -> str:
    """The bundled guide `fujimoto agent-instructions` prints."""
    return (
        resources.files("fujimoto.templates")
        .joinpath(AGENT_INSTRUCTIONS)
        .read_text(encoding="utf-8")
    )
