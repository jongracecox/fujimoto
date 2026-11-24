"""`fujimoto mcp`: a local stdio MCP server over fujimoto's session commands.

Lets an MCP client such as Claude Desktop list, search and read Claude Code
sessions, start, resume and stop them, and message them through `fujimoto
send` / `broadcast`.

Two ways in, chosen per tool:

- **Read tools** call `session_query` / `broadcast` in-process: they return
  the same dicts as the CLI's `--json`, without a process per call.
- **Mutating tools** run `python -m fujimoto … --json` as a subprocess, with
  stdin closed. On a stdio server stdout *is* the protocol stream, and the
  launch path redirects fd 1 and starts tmux and claude; a child process
  can't write into the stream, and the tools depend only on the CLI's JSON
  contract, not on `cli.py` internals.

Messaging is only offered when the server is started with `--key`, the public
half of an agent-role signer held by an SSH agent (`fujimoto mcp setup`
checks all of that). The key is always passed to `send` as a literal public
key, so `ssh-keygen` signs through the agent and never reads a private key
file (which would want a passphrase from a terminal the server doesn't have).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from fujimoto import broadcast, debug, session_query
from fujimoto.claude.log_parser import SessionState
from fujimoto.claude.search import ContentMode

SERVER_NAME = "fujimoto"
SUBPROCESS_TIMEOUT = 180.0
# A read with no window would hand the client a whole transcript.
DEFAULT_TAIL = 40
MAX_WAIT = 120.0
# How long a start with a prompt waits for Claude's first transcript line.
START_WAIT = 10.0

INSTRUCTIONS = """\
fujimoto manages Claude Code sessions running in tmux, one per git worktree
(or a "chat" in a project). Use it to see what sessions exist, what they did,
and to steer them.

- "What needs me?": list_sessions(waiting=true). It reads Claude Code's own
  status, so there's no need to read transcripts to find out.
- Otherwise start with list_sessions (running_only for what is live now) or
  search_sessions for a ticket key, branch or phrase.
- Results are compact text by default. Ask for format=json, or pass
  `fields`, only when you need exact values.
- read_transcript with summary=true is the cheap first look at a session;
  then tail/grep for detail. `cursor` in a result can be passed back as
  `after` to read only what is new.
- send_message reaches one running session; it acts on it at its next safe
  stopping point. Note the returned id, then follow_message(id, wait=60) for
  delivery, reply and whether the session is blocked on the user (or
  read_transcript(reply_to=id, wait=60) for just the reply). Prefer it over
  broadcast. list_sessions shows "listening" for sessions it can reach.
- Messages are signed as an agent: sessions see them as from this agent,
  never as typed by the user. Don't claim the user approved something.
- A session trusts only signers that existed when it launched. A receipt
  rejected as "not signed by a configured signer" means the session predates
  this agent's key, not that signing failed: suggest stopping and resuming it
  through fujimoto, or message a newer session.
- If a tool fails in a way that looks like setup (signing, tmux, a stale
  version), call server_health: it says which and how to fix it.
- start_session creates a worktree and session; stop_session ends one.
  Confirm with the user before stopping a session or broadcasting.
"""


class FujimotoToolError(Exception):
    """A tool call failed in a way the client should see as a tool error."""


# --------------------------------------------------------------------------
# Read tools (in-process)
# --------------------------------------------------------------------------


def _scope(project: str | None, all_projects: bool) -> session_query.Scope:
    if project and all_projects:
        raise FujimotoToolError("give project or all_projects, not both")
    try:
        return session_query.resolve_scope(
            project=project, all_projects=all_projects, outside_repo_all=True
        )
    except session_query.QueryError as e:
        raise FujimotoToolError(str(e)) from e


Format = Literal["text", "json"]
Result = str | dict[str, object]


def _text(rendered: str) -> str:
    """CLI text for a tool result, without its "Next: fujimoto …" CLI hints.

    The hints name shell commands; an MCP client uses the tools instead (the
    server instructions say which).
    """
    return "\n".join(
        line for line in rendered.splitlines() if not line.startswith("Next: ")
    ).rstrip("\n")


def _fields(
    fields: Sequence[str] | None, valid: Sequence[str]
) -> tuple[str, ...] | None:
    if not fields:
        return None
    try:
        return session_query.parse_fields(",".join(fields), valid)
    except session_query.QueryError as e:
        raise FujimotoToolError(str(e)) from e


def _states(states: Sequence[str] | None) -> tuple[SessionState, ...]:
    out = []
    for name in states or ():
        try:
            out.append(SessionState(name))
        except ValueError as e:
            valid = ", ".join(s.value for s in SessionState)
            raise FujimotoToolError(
                f"unknown state {name!r}; choose from: {valid}"
            ) from e
    return tuple(out)


def session_id_for(ref: str) -> str:
    """A full session id for an id, id prefix, `.jsonl` path or running peer name.

    `find_session_log` takes everything but a peer name; `run_list(refs=…)`
    resolves that, exactly as `list_sessions(sessions=…)` does.
    """
    try:
        return session_query.find_session_log(ref).stem
    except session_query.QueryError as first:
        try:
            report = session_query.run_list(
                session_query.Scope(all_projects=True), refs=(ref,)
            )
        except session_query.QueryError:
            raise FujimotoToolError(str(first)) from first
        if not report.sessions:  # pragma: no cover - run_list raises instead
            raise FujimotoToolError(str(first)) from first
        return report.sessions[0].session.session_id


STARTING_NOTE = (
    "No transcript yet: Claude writes it with the session's first message. "
    "Read again in a few seconds; stop_session works by this id or the tmux name."
)


def _starting_row(pending: session_query.PendingSession) -> dict[str, object]:
    """A list/read row for a session that is running but has no transcript."""
    return {
        "session_id": pending.session_id,
        "state": "starting",
        "title": None,
        "active": {"running": True, "tmux_session": pending.tmux_session},
        "note": STARTING_NOTE,
    }


def _starting_line(pending: session_query.PendingSession) -> str:
    return (
        f"◐ {pending.session_id[:8]}  starting · tmux {pending.tmux_session} · "
        "no transcript yet"
    )


def _pending_or_none(ref: str) -> session_query.PendingSession | None:
    """`ref`'s pending session, only if it has no transcript (a cheap check first)."""
    try:
        session_query.find_session_log(ref)
    except session_query.QueryError:
        return session_query.pending_session(ref)
    return None


def list_sessions(
    project: str | None = None,
    all_projects: bool = False,
    running_only: bool = False,
    limit: int = session_query.DEFAULT_LIMIT,
    sessions: Sequence[str] | None = None,
    waiting: bool = False,
    states: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
    format: Format = "text",
) -> Result:
    picked = _fields(fields, session_query.LIST_FIELDS)
    wanted = _states(states)
    # Named sessions are found wherever they are, so the scope is moot.
    scope = (
        session_query.Scope(all_projects=True)
        if sessions
        else _scope(project, all_projects)
    )
    refs = tuple(sessions or ())
    # A session launched moments ago has no transcript for run_list to find;
    # report it as starting rather than "not found". A filter can't match it.
    starting: list[session_query.PendingSession] = []
    if refs and not (waiting or wanted or running_only):
        for ref in refs:
            pending = _pending_or_none(ref)
            if pending is not None:
                starting.append(pending)
        found = {p.session_id for p in starting} | {p.tmux_session for p in starting}
        refs = tuple(r for r in refs if r not in found)
    report = None
    if refs or not starting:
        try:
            report = session_query.run_list(
                scope,
                limit=limit,
                running_only=running_only,
                waiting_only=waiting,
                states=wanted,
                refs=refs,
            )
        except session_query.QueryError as e:
            raise FujimotoToolError(str(e)) from e
    if format == "json" or picked:
        data: dict[str, object] = (
            session_query.list_json(report, picked)
            if report is not None
            else {"refs": list(sessions or ()), "sessions": []}
        )
        rows = [_starting_row(p) for p in starting]
        if picked:
            rows = [session_query.cut_row(r, picked) for r in rows]
        data["sessions"] = [*cast("list[object]", data["sessions"]), *rows]
        return data
    lines = [_starting_line(p) for p in starting]
    if report is not None:
        lines.append(_text(session_query.format_list(report)))
    return "\n".join(lines)


def search_sessions(
    query: str,
    project: str | None = None,
    all_projects: bool = False,
    regex: bool = False,
    case_sensitive: bool = False,
    raw: bool = False,
    limit: int = session_query.DEFAULT_LIMIT,
    sort: Literal["recent", "relevance"] = "recent",
    format: Format = "text",
) -> Result:
    scope = _scope(project, all_projects)
    try:
        report = session_query.run_search(
            query,
            scope,
            regex=regex,
            case_sensitive=case_sensitive,
            mode=ContentMode.RAW if raw else ContentMode.TEXT,
            limit=limit,
            sort=session_query.SortOrder(sort),
        )
    except session_query.QueryError as e:
        raise FujimotoToolError(str(e)) from e
    if format == "json":
        return session_query.search_json(report)
    return _text(session_query.format_search(report))


def read_transcript(
    session: str,
    summary: bool = False,
    grep: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    context: int = 0,
    head: int | None = None,
    tail: int | None = None,
    roles: Sequence[str] | None = None,
    after: int | None = None,
    reply_to: str | None = None,
    wait: float = 0.0,
    fields: Sequence[str] | None = None,
    format: Format = "text",
    *,
    sleep: Callable[[float], None] | None = None,
) -> Result:
    unknown = sorted(set(roles or ()) - set(session_query.TRANSCRIPT_ROLES))
    if unknown:
        valid = ", ".join(session_query.TRANSCRIPT_ROLES)
        raise FujimotoToolError(
            f"unknown role(s) {', '.join(unknown)} (valid: {valid})"
        )
    if summary and (after is not None or reply_to):
        raise FujimotoToolError("summary can't be combined with after or reply_to")
    picked = _fields(fields, session_query.LOG_FIELDS)
    windowed = (
        summary
        or grep is not None
        or head is not None
        or tail is not None
        or after is not None
        or reply_to is not None
    )
    if not windowed:
        tail = DEFAULT_TAIL
    wait = max(0.0, min(wait, MAX_WAIT))
    extra: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    try:
        resolved = session_id_for(session)
    except FujimotoToolError:
        pending = session_query.pending_session(session)
        if pending is None:
            raise
        row = _starting_row(pending)
        if format == "json" or picked:
            return session_query.cut_row(row, picked) if picked else row
        return f"# session {pending.session_id} · starting\n{_starting_line(pending)}\n{STARTING_NOTE}"
    try:
        report = session_query.load_log_waiting(
            lambda: session_query.load_log(
                resolved,
                roles=list(roles) if roles else None,
                grep=grep,
                regex=regex,
                case_sensitive=case_sensitive,
                context=context,
                head=head,
                tail=tail,
                summary=summary,
                after=after,
                reply_to=reply_to,
            ),
            wait=wait,
            **extra,
        )
    except session_query.QueryError as e:
        raise FujimotoToolError(str(e)) from e
    if format == "json" or picked:
        return session_query.log_json(report, picked)
    return _text(session_query.format_log(report))


def listening_sessions() -> dict[str, object]:
    """Broadcast settings, the sessions that can receive a message now, and
    the version of the fujimoto serving this (the server's own, not whatever
    `fujimoto` is on PATH)."""
    from fujimoto.version import get_version

    try:
        config = broadcast.load_config(strict=True)
    except broadcast.BroadcastError as e:
        raise FujimotoToolError(str(e)) from e
    return {"version": get_version(), **broadcast.show_json(config)}


def message_status(message_id: str | None = None) -> dict[str, object]:
    try:
        rows = broadcast.status(message_id)
    except (broadcast.BroadcastError, OSError) as e:
        raise FujimotoToolError(str(e)) from e
    return {"messages": broadcast.status_json(rows)}


def server_health(key: str | None) -> dict[str, object]:
    """`mcp_doctor.self_checks` for this server: version drift, tools on its
    PATH, and signing readiness for its own key."""
    from dataclasses import asdict

    from fujimoto import mcp_doctor
    from fujimoto.version import get_version

    checks = mcp_doctor.self_checks(key)
    return {
        "ok": all(c.status != "fail" for c in checks),
        "version": get_version(),
        "messaging": key is not None,
        "checks": [asdict(c) for c in checks],
    }


def follow_message(
    message_id: str, wait: float = 0.0, format: Format = "text"
) -> Result:
    """`fujimoto broadcast follow`, in-process, with `wait` capped at MAX_WAIT."""
    from fujimoto import follow

    try:
        report = follow.follow(message_id, wait=min(max(wait, 0.0), MAX_WAIT))
    except (broadcast.BroadcastError, session_query.QueryError, OSError) as e:
        raise FujimotoToolError(str(e)) from e
    if format == "json":
        return follow.follow_json(report)
    return _text(follow.format_follow(report))


# --------------------------------------------------------------------------
# Mutating tools (subprocess)
# --------------------------------------------------------------------------

Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def _run_cli(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fujimoto", *argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        env=_child_env(),
    )


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The server isn't a Claude Code session; a stray id would make `search`
    # hide a session or `--parent` name the wrong one.
    env.pop(session_query.CALLING_SESSION_ENV, None)
    return env


def _positional(value: str, what: str) -> str:
    """Reject a value argparse would read as a flag."""
    if not value.strip():
        raise FujimotoToolError(f"{what} is empty")
    if value.startswith("-"):
        raise FujimotoToolError(f"{what} can't start with '-'")
    return value


class Commands:
    """The mutating tools: each builds argv, runs the CLI, parses its JSON."""

    def __init__(
        self,
        key: str | None = None,
        run: Runner = _run_cli,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.key = key
        self._run = run
        self._sleep = sleep
        self._clock = clock

    def _await_transcript(
        self, result: dict[str, object], prompted: bool
    ) -> dict[str, object]:
        """Mark a fresh session `transcript_ready`, waiting briefly if it has a prompt.

        Claude writes the transcript with the first message: within seconds
        when there's a prompt, not until someone types when there isn't. Until
        then, lookups by id report the session as starting.
        """
        session_id = result.get("claude_session_id")
        if not isinstance(session_id, str) or result.get("session_started") is False:
            return result
        deadline = self._clock() + (START_WAIT if prompted else 0.0)
        while True:
            try:
                session_query.find_session_log(session_id)
                ready = True
            except session_query.QueryError:
                ready = False
            if ready or self._clock() >= deadline:
                break
            self._sleep(0.5)
        result["transcript_ready"] = ready
        if not ready:
            result["note"] = STARTING_NOTE
        debug.log("mcp.start_wait", prompted=prompted, ready=ready)
        return result

    def _call(self, argv: list[str], cwd: Path) -> dict[str, object]:
        command = argv[0] if argv[0] != "broadcast" else "broadcast-" + argv[1]
        try:
            result = self._run(argv, cwd)
        except subprocess.TimeoutExpired as e:
            debug.log("mcp.cli", command=command, rc="timeout")
            raise FujimotoToolError(f"fujimoto {command} timed out") from e
        except OSError as e:
            debug.log("mcp.cli", command=command, rc="oserror")
            raise FujimotoToolError(f"could not run fujimoto: {e}") from e
        debug.log("mcp.cli", command=command, rc=result.returncode)
        stderr = result.stderr.strip()
        if result.returncode != 0:
            message = stderr.splitlines()[-1] if stderr else f"exit {result.returncode}"
            raise FujimotoToolError(message.removeprefix("fujimoto: "))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise FujimotoToolError(
                f"fujimoto {command} printed no JSON: {result.stdout[:200]!r}"
            ) from e
        if not isinstance(data, dict):
            data = {"result": data}
        if stderr:
            data["notes"] = stderr
        return data

    def _project_root(self, project: str) -> Path:
        scope = _scope(project, False)
        if scope.root is None:  # pragma: no cover - a named project has a root
            raise FujimotoToolError(f"project {project} has no root")
        return scope.root

    def start_session(
        self,
        project: str,
        title: str,
        base: str | None = None,
        current_branch: bool = False,
        prompt: str | None = None,
    ) -> dict[str, object]:
        if base and current_branch:
            raise FujimotoToolError("give base or current_branch, not both")
        argv = ["new", _positional(title, "title"), "--detach"]
        if base:
            argv.append(f"--base={base}")
        if current_branch:
            argv.append("--current")
        if prompt:
            argv.append(f"--prompt={prompt}")
        result = self._call(argv, self._project_root(project))
        return self._await_transcript(result, bool(prompt))

    def start_chat(
        self,
        project: str,
        title: str | None = None,
        worktree: str | None = None,
        scratch: bool = False,
        prompt: str | None = None,
    ) -> dict[str, object]:
        if worktree and scratch:
            raise FujimotoToolError("give worktree or scratch, not both")
        argv = ["chat"]
        if title:
            argv.append(_positional(title, "title"))
        argv += [f"--project={project}", "--detach"]
        if worktree:
            argv.append(f"--in={worktree}")
        if scratch:
            argv.append("--in-scratch")
        if prompt:
            argv.append(f"--prompt={prompt}")
        result = self._call(argv, Path.home())
        return self._await_transcript(result, bool(prompt))

    def resume_session(
        self,
        session: str,
        fork: bool = False,
        prompt: str | None = None,
        wait: float | None = None,
    ) -> dict[str, object]:
        try:
            found = session_query.resolve_session(
                session_id_for(_positional(session, "session"))
            )
        except session_query.QueryError as e:
            raise FujimotoToolError(str(e)) from e
        # `resume` checks it runs inside a repo; the session's own directory
        # is the one it will run in (a missing one is the CLI's error to give).
        cwd = found.cwd if found.cwd.is_dir() else Path.home()
        argv = ["resume", found.session_id, "--detach"]
        if fork:
            argv.append("--fork")
        if wait is not None:
            argv.append(f"--wait={wait}")
        if prompt:
            argv.append(f"--prompt={prompt}")
        return self._call(argv, cwd)

    def stop_session(self, session: str) -> dict[str, object]:
        return self._call(
            ["stop", _positional(session, "session"), "--json"], Path.home()
        )

    def _require_key(self) -> str:
        if not self.key:  # pragma: no cover - tools aren't registered without it
            raise FujimotoToolError("messaging is off: the server has no --key")
        return self.key

    def send_message(
        self, session: str, text: str, ttl: str = "1h", wait: float = 5.0
    ) -> dict[str, object]:
        argv = [
            "send",
            _positional(session, "session"),
            _positional(text, "text"),
            f"--key={self._require_key()}",
            f"--ttl={ttl}",
            f"--wait={wait}",
            "--json",
        ]
        return self._call(argv, Path.home())

    def broadcast(
        self,
        text: str,
        project: str | None = None,
        all_projects: bool = False,
        ttl: str = "1h",
        wait: float = 5.0,
    ) -> dict[str, object]:
        if bool(project) == all_projects:
            raise FujimotoToolError("give exactly one of project or all_projects")
        argv = ["broadcast", "send", _positional(text, "text")]
        argv.append(f"--project={project}" if project else "--all-projects")
        argv += [
            f"--key={self._require_key()}",
            f"--ttl={ttl}",
            f"--wait={wait}",
            "--json",
        ]
        return self._call(argv, Path.home())


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


def check_key(key: str) -> str:
    """The key as a literal public key, refusing one that would sign as the user."""
    try:
        public = broadcast.normalize_public_key(key)
        config = broadcast.load_config(strict=True)
    except broadcast.BroadcastError as e:
        raise FujimotoToolError(f"--key: {e}") from e
    for signer in config.signers:
        if signer.public_key == public and signer.role == broadcast.USER:
            raise FujimotoToolError(
                f"--key is the user signer {signer.name!r}; the MCP server needs "
                "its own agent key (see `fujimoto mcp setup`)"
            )
    return public


def build_server(key: str | None = None, run: Runner = _run_cli) -> Any:
    """The MCPServer with every tool registered. Imports `mcp` lazily."""
    from anyio import to_thread
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    from fujimoto.version import get_version

    commands = Commands(key=key, run=run)
    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=get_version(),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    starts = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
    destructive = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, open_world_hint=False
    )

    async def call(name: str, fn: Callable[[], Any]) -> Any:
        # Off the event loop: transcript reads, waits and subprocesses block.
        try:
            result = await to_thread.run_sync(fn)
        except FujimotoToolError as e:
            debug.log("mcp.tool", tool=name, ok=False)
            raise ToolError(str(e)) from e
        debug.log("mcp.tool", tool=name, ok=True)
        return result

    @server.tool(name="list_sessions", annotations=read_only)
    async def _list_sessions(
        project: str | None = None,
        all_projects: bool = False,
        running_only: bool = False,
        limit: int = session_query.DEFAULT_LIMIT,
        sessions: list[str] | None = None,
        waiting: bool = False,
        states: list[str] | None = None,
        fields: list[str] | None = None,
        format: Format = "text",
    ) -> Result:
        """Sessions, most recently used first: short id, project/worktree,
        when, status (busy/idle, and what it is waiting on), peer name, title.
        waiting=true: only running sessions waiting on the user (a permission
        prompt, a question, or an idle prompt) - the cheap way to answer
        "what needs me?". `sessions`: look up specific sessions by id, id
        prefix or peer name (project/all_projects are then ignored).
        `states` filters by transcript state (working, waiting_for_user,
        waiting_for_tool_approval, idle, unknown). With neither project nor
        all_projects, every project is listed. format=json returns the full
        record per session; `fields` (e.g. ["active.waiting", "title"])
        trims it, implies json, and always includes the full session_id."""
        return await call(
            "list_sessions",
            lambda: list_sessions(
                project,
                all_projects,
                running_only,
                limit,
                sessions,
                waiting,
                states,
                fields,
                format,
            ),
        )

    @server.tool(name="search_sessions", annotations=read_only)
    async def _search_sessions(
        query: str,
        project: str | None = None,
        all_projects: bool = False,
        regex: bool = False,
        case_sensitive: bool = False,
        raw: bool = False,
        limit: int = session_query.DEFAULT_LIMIT,
        sort: Literal["recent", "relevance"] = "recent",
        format: Format = "text",
    ) -> Result:
        """Full-text search of session transcripts (messages by default; raw
        also searches tool output). Use sort=relevance to find the session
        that discussed something most, e.g. a ticket key. format=json for
        structured hits with match spans."""
        return await call(
            "search_sessions",
            lambda: search_sessions(
                query,
                project,
                all_projects,
                regex,
                case_sensitive,
                raw,
                limit,
                sort,
                format,
            ),
        )

    @server.tool(name="read_transcript", annotations=read_only)
    async def _read_transcript(
        session: str,
        summary: bool = False,
        grep: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        context: int = 0,
        head: int | None = None,
        tail: int | None = None,
        roles: list[str] | None = None,
        after: int | None = None,
        reply_to: str | None = None,
        wait: float = 0.0,
        fields: list[str] | None = None,
        format: Format = "text",
    ) -> Result:
        """One session's transcript. `session` is an id, id prefix or peer
        name. summary=true gives the first prompt, the final exchange and
        links. With no summary/grep/head/tail/after/reply_to, the last 40
        messages. `roles` picks from user, assistant, thinking, tool_use,
        tool_result, injected. `after` takes the `cursor` of an earlier read;
        `reply_to` takes a send_message id; `wait` (seconds, max 120)
        re-reads until something arrives. `fields` (e.g. ["cursor",
        "messages"] or ["summary.last_assistant.text"]) trims a json result
        and implies format=json."""
        return await call(
            "read_transcript",
            lambda: read_transcript(
                session,
                summary,
                grep,
                regex,
                case_sensitive,
                context,
                head,
                tail,
                roles,
                after,
                reply_to,
                wait,
                fields,
                format,
            ),
        )

    @server.tool(name="server_health", annotations=read_only)
    async def _server_health() -> dict[str, object]:
        """This server's own health: whether it runs the installed fujimoto
        version (if not, the client must be restarted), whether tmux, git
        and claude are on its PATH (start/resume need them), and, when it
        can send messages, whether its SSH agent still holds its key and the
        key is still a trusted agent signer. Each check is ok/warn/fail with
        the fix. Read-only: nothing is signed or changed. Use it when a tool
        fails unexpectedly, before guessing at the cause."""
        return await call("server_health", lambda: server_health(key))

    @server.tool(name="listening_sessions", annotations=read_only)
    async def _listening_sessions() -> dict[str, object]:
        """Whether broadcast is on, the trusted signers, the running
        sessions that can receive a message right now, and `version`: the
        fujimoto version this server runs."""
        return await call("listening_sessions", listening_sessions)

    @server.tool(name="message_status", annotations=read_only)
    async def _message_status(message_id: str | None = None) -> dict[str, object]:
        """Recent messages and broadcasts (or one, by id or part of it), with
        a delivery receipt per session. A reply is in the transcript: use
        read_transcript(reply_to=id)."""
        return await call("message_status", lambda: message_status(message_id))

    @server.tool(name="follow_message", annotations=read_only)
    async def _follow_message(
        message_id: str, wait: float = 30.0, format: Format = "text"
    ) -> Result:
        """What became of a message sent to one session, waiting up to
        `wait` seconds (max 120) for an outcome. Reports separately: the
        delivery receipt; for a relayed approval, whether it verifies for
        the target and whether the target's transcript shows it running
        `fujimoto broadcast verify` (only that is evidence the recipient
        checked); the reply text; and the target's activity, including a
        wait on the user (outcome "blocked"). `reply.cursor` can be passed
        to read_transcript(after=...) for what follows."""
        return await call(
            "follow_message", lambda: follow_message(message_id, wait, format)
        )

    @server.tool(name="start_session", annotations=starts)
    async def _start_session(
        project: str,
        title: str,
        base: str | None = None,
        current_branch: bool = False,
        prompt: str | None = None,
    ) -> dict[str, object]:
        """Create a git worktree in `project` (name or path) and start a
        Claude Code session in it, in the background. Starts from the
        freshly fetched default branch unless base or current_branch. An
        existing worktree with this title is reused. `prompt` is its first
        message. Returns its tmux name and Claude session id."""
        return await call(
            "start_session",
            lambda: commands.start_session(
                project, title, base, current_branch, prompt
            ),
        )

    @server.tool(name="start_chat", annotations=starts)
    async def _start_chat(
        project: str,
        title: str | None = None,
        worktree: str | None = None,
        scratch: bool = False,
        prompt: str | None = None,
    ) -> dict[str, object]:
        """Start a chat session about `project` without creating a
        worktree: in the project root by default, in an existing worktree,
        or in the project's scratch directory."""
        return await call(
            "start_chat",
            lambda: commands.start_chat(project, title, worktree, scratch, prompt),
        )

    @server.tool(name="resume_session", annotations=starts)
    async def _resume_session(
        session: str,
        fork: bool = False,
        prompt: str | None = None,
        wait: float | None = None,
    ) -> dict[str, object]:
        """Bring a stopped session back in the background (or report the one
        already running). fork=true continues in a new conversation and
        leaves the original as it was. `prompt` needs a new process."""
        return await call(
            "resume_session",
            lambda: commands.resume_session(session, fork, prompt, wait),
        )

    @server.tool(name="stop_session", annotations=destructive)
    async def _stop_session(session: str) -> dict[str, object]:
        """Stop a running session (tmux name or Claude session id). Its
        transcript and worktree stay; resume_session brings it back."""
        return await call("stop_session", lambda: commands.stop_session(session))

    if key:

        @server.tool(name="send_message", annotations=starts)
        async def _send_message(
            session: str, text: str, ttl: str = "1h", wait: float = 5.0
        ) -> dict[str, object]:
            """Send a signed message to one running session (tmux name, peer
            name or 4+ character id prefix); it acts on it at its next safe
            stopping point. Returns the message id and delivery receipts.
            The session sees it as from this agent, not from the user.
            A session trusts only the signers that existed when it
            launched: a receipt rejected as "not signed by a configured
            signer" means the session predates this agent's key. It must be
            stopped and resumed through fujimoto (not /clear, not a
            fujimoto restart), or pick a newer session."""
            return await call(
                "send_message", lambda: commands.send_message(session, text, ttl, wait)
            )

        @server.tool(name="broadcast", annotations=starts)
        async def _broadcast(
            text: str,
            project: str | None = None,
            all_projects: bool = False,
            ttl: str = "1h",
            wait: float = 5.0,
        ) -> dict[str, object]:
            """Send a signed message to every listening session in one
            project, or with all_projects to every session on the machine.
            Prefer send_message; confirm the audience with the user first.
            Sessions launched before this agent's key was trusted reject
            it (see send_message)."""
            return await call(
                "broadcast",
                lambda: commands.broadcast(text, project, all_projects, ttl, wait),
            )

    return server


MISSING_EXTRA = (
    "fujimoto mcp needs the `mcp` extra:\n"
    "  uv tool install --force 'fujimoto[mcp]'\n"
    "Then run `fujimoto mcp setup` for the Claude Desktop config."
)


def serve(key: str | None) -> int:
    """Run the stdio server until the client disconnects. Returns an exit code."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(MISSING_EXTRA, file=sys.stderr)
        debug.log("mcp.serve", started=False, reason="missing-extra")
        return 1
    public: str | None = None
    if key:
        try:
            public = check_key(key)
        except FujimotoToolError as e:
            print(f"fujimoto: {e}", file=sys.stderr)
            debug.log("mcp.serve", started=False, reason="bad-key")
            return 1
    debug.log("mcp.serve", started=True, messaging=public is not None)
    build_server(public).run("stdio")
    return 0
