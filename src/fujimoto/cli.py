from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.worker import get_current_worker
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from fujimoto import debug
from fujimoto.claude import (
    ClaudeLogError,
    ClaudeSession,
    SessionState,
    TranscriptMessage,
    get_sessions_for_path,
    read_raw_transcript,
    read_transcript,
)
from fujimoto.claude import search as claude_search
from fujimoto.claude.search import (
    ContentMode,
    Matcher,
    SearchError,
    SearchHit,
    Snippet,
)
from fujimoto.config import (
    ConfigError,
    build_worktree_path,
    config_once_applied,
    get_next_adhoc_session_name,
    get_next_direct_session_name,
    get_project_worktrees_dir,
    list_projects,
    mark_config_once_applied,
    read_session_meta,
    slugify,
    store_session_meta,
)
from fujimoto.git import (
    GitError,
    cherry_pick_branch,
    create_worktree,
    delete_branch,
    fetch_branch,
    get_current_branch,
    get_default_branch,
    get_main_worktree_root,
    get_project_name,
    get_repo_root,
    get_unpushed_commits,
    has_remote_branch,
    is_branch_merged,
    list_branches,
    push_branch,
    remove_worktree,
)
from fujimoto.project_config import (
    OnError,
    Trigger,
    apply_project_config,
    load_project_config,
    write_config_template,
)
from fujimoto.terminal import open_terminal
from fujimoto.version import get_version
from fujimoto.version_check import check_for_update, dismiss as dismiss_update_version
from fujimoto.vscode import open_vscode
from fujimoto.settings import Settings, load_settings, save_settings
from fujimoto import session_state
from fujimoto.tmux import (
    TmuxError,
    create_session,
    create_session_with_command,
    disable_quick_terminal_binding,
    display_message,
    enable_quick_terminal_binding,
    get_session_path,
    install_tmux,
    is_tmux_installed,
    kill_session,
    launch_claude_in_tmux,
    list_all_sessions,
    session_exists,
    list_project_sessions,
    meta_key,
    quick_terminal_key,
    rename_session,
    PENDING_CLOSE,
    PENDING_FORK,
    PENDING_STOP,
    session_name,
    set_terminal_title,
    take_pending_action,
)

BRANCH_ICON = "\ue0a0"
ICON_EYES = "\U0001f440"
ICON_SHIELD = "\U0001f6e1\ufe0f"
ICON_GEAR = "\u2699"
ICON_ZZZ = "\U0001f4a4"
ICON_GREEN_CIRCLE = "\U0001f7e2"
ICON_ORANGE_CIRCLE = "\U0001f7e0"
ICON_BLACK_CIRCLE = "\u26ab"
ICON_HLINE = "\u2500"
ICON_WIZARD = "\U0001f9d9\U0001f3fd\u200d\u2642\ufe0f"
ICON_FORK = "\U0001f374"


_KEY_PREFIX_LABELS = {"C-": "Ctrl+", "M-": "Alt+", "S-": "Shift+"}


def _friendly_key_label(key: str) -> str:
    """Render a tmux key spec like 'C-`' as a user-friendly 'Ctrl+`'."""
    parts: list[str] = []
    remaining = key
    while len(remaining) >= 2 and remaining[:2] in _KEY_PREFIX_LABELS:
        parts.append(_KEY_PREFIX_LABELS[remaining[:2]])
        remaining = remaining[2:]
    return "".join(parts) + remaining


def _claude_state_label(state: SessionState) -> str:
    if state == SessionState.WAITING_FOR_USER:
        return f" [dim]{ICON_EYES} awaiting input[/]"
    if state == SessionState.WAITING_FOR_TOOL_APPROVAL:
        return f" [dim]{ICON_SHIELD} approve tool[/]"
    if state == SessionState.WORKING:
        return f" [dim]{ICON_GEAR} working[/]"
    if state == SessionState.IDLE:
        return f" [dim]{ICON_ZZZ} idle[/]"
    return ""


def _relative_time(dt: datetime) -> str:
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


def _format_prompt_lines(text: str, max_width: int) -> list[str]:
    """Format a prompt for multi-line display in the resume picker.

    Each raw line is word-wrapped at max_width, so long single-line prompts
    produce multiple display lines.  Returns the first 2 display lines, then
    (when there are more) a '…' marker and the last line.
    """

    def _trunc(line: str) -> str:
        return line[: max_width - 1] + "…" if len(line) > max_width else line

    display_lines: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        display_lines.extend(textwrap.wrap(raw, max_width) or [raw])

    lines = [_trunc(ln) for ln in display_lines]
    if len(lines) <= 2:
        return lines
    return [lines[0], lines[1], "…", lines[-1]]


# Style applied to a matched substring inside a search snippet. `$warning`
# resolves against the active theme, so it reads in light and dark alike.
SNIPPET_MATCH_STYLE = "b $warning"


def _fit_snippet(
    snippet: Snippet, max_width: int
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Trim a snippet to `max_width`, keeping its first match on screen.

    A plain right-truncation would cut the match off on a narrow terminal (the
    snippet is centred on the match with `SNIPPET_RADIUS` chars of lead-in), so
    when the text has to lose characters the window slides to keep the match in
    view. Spans are shifted to the new offsets, and any that fall outside the
    window are dropped.

    Room for an elision marker is reserved at *both* ends whether or not both
    are needed. Costing at most one unused column keeps the arithmetic obvious —
    the alternative is a fixed-point loop, because adding a marker narrows the
    body, which can move the window, which changes whether a marker is needed.
    """
    text = snippet.text
    if len(text) <= max_width:
        return text, snippet.spans

    body_width = max(1, max_width - 2)
    first = snippet.spans[0][0] if snippet.spans else 0
    # Lead-in before the match, clamped so the window never runs off either end.
    low = max(0, min(first - body_width // 4, len(text) - body_width))
    high = low + body_width

    head = "…" if low > 0 else ""
    tail = "…" if high < len(text) else ""
    shift = len(head) - low
    limit = len(head) + (high - low)

    spans: list[tuple[int, int]] = []
    for span_start, span_end in snippet.spans:
        span_start = max(span_start + shift, len(head))
        span_end = min(span_end + shift, limit)
        if span_end > span_start:
            spans.append((span_start, span_end))
    return f"{head}{text[low:high]}{tail}", tuple(spans)


def _render_snippet(snippet: Snippet, max_width: int) -> Content:
    """Styled content for a snippet: dim context, highlighted matches.

    Assembled from `(text, style)` pairs rather than a markup string, because
    snippet text is arbitrary transcript bytes spliced at arbitrary offsets: a
    fragment ending in `[` or `\\` swallows the very tag that closes it, and
    `rich.markup.escape` cannot help — it only escapes a `[` that still looks
    like a tag in the fragment it is given. `Content.assemble` never parses the
    text at all, and styling spans separately also stops `dim` from being
    inherited by the highlight (which would wash it out).
    """
    text, spans = _fit_snippet(snippet, max_width)
    parts: list[tuple[str, str]] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append((text[cursor:start], "dim"))
        parts.append((text[start:end], SNIPPET_MATCH_STYLE))
        cursor = end
    if cursor < len(text):
        parts.append((text[cursor:], "dim"))
    return Content.assemble(*parts)


# Style applied to a matched substring inside the session log viewer. Same
# `$warning` accent as a search snippet, so a match reads the same in both.
LOG_MATCH_STYLE = "b $warning"

# Half-open `(start, end)` offsets of the matches inside one transcript body.
Spans = tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LogBody:
    """One highlightable body in the log viewer, and what hides it.

    Searching updates these in place rather than rebuilding the transcript:
    on a real log a full rebuild costs about half a second, which is a visible
    stall between keystrokes. `folds` is the chain of `Collapsible`s a body sits
    inside (a call, and the run that call belongs to), so a match can open its
    way out of both.
    """

    widget: Static
    text: str
    folds: tuple[Collapsible, ...] = ()


def _match_spans(text: str, matcher: Matcher | None) -> Spans:
    """Where `matcher` hits inside `text`.

    Zero-width matches (`x*` against a run with no `x`) are dropped: they
    highlight nothing and there is nothing to scroll to, so counting them would
    only inflate the tally.
    """
    if matcher is None:
        return ()
    return tuple(
        (m.start(), m.end())
        for m in matcher.pattern.finditer(text)
        if m.end() > m.start()
    )


def _highlight(text: str, spans: Spans) -> Content:
    """Transcript text as `Content`, with `spans` picked out.

    Assembled from `(text, style)` pairs rather than markup for the reason
    snippets are: transcript text is arbitrary bytes and a stray `[` would be
    parsed as a tag.
    """
    if not spans:
        return Content(text)

    parts: list[str | tuple[str, str]] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append(text[cursor:start])
        parts.append((text[start:end], LOG_MATCH_STYLE))
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
    return Content.assemble(*parts)


def _log_body(text: str, folds: tuple[Collapsible, ...] = ()) -> LogBody:
    """An unhighlighted transcript body, ready for a query to be applied."""
    return LogBody(Static(Content(text), classes="log-body"), text, folds)


# How each transcript role is labelled and styled in the log viewer.
_TRANSCRIPT_ROLES: dict[str, tuple[str, str]] = {
    "user": ("You", "log-user"),
    "assistant": ("Claude", "log-assistant"),
    "thinking": ("Thinking", "log-thinking"),
    "tool_use": ("⚒ Tool", "log-tool"),
    "tool_result": ("↳ Result", "log-tool"),
}


def _first_line(text: str, max_width: int) -> str:
    """First non-empty line of `text`, truncated to `max_width`."""
    for raw in text.splitlines():
        if raw.strip():
            line = raw.strip()
            return line[: max_width - 1] + "…" if len(line) > max_width else line
    return ""


def _tool_summary(msg: TranscriptMessage) -> str:
    """One-line title for a collapsed tool call or result.

    `tool_use` bodies are `name\nparams`, so the name becomes the title and the
    first parameter line its detail. A result has no name, so it is summarised
    by size instead — enough to judge whether it is worth opening.
    """
    lines = [ln for ln in msg.text.splitlines() if ln.strip()]
    if msg.role == "tool_use":
        name = lines[0] if lines else "tool"
        detail = _first_line("\n".join(lines[1:]), 60) if len(lines) > 1 else ""
        return f"⚒ {name}  {detail}" if detail else f"⚒ {name}"
    count = len(lines)
    detail = _first_line(msg.text, 60)
    unit = "line" if count == 1 else "lines"
    return (
        f"↳ Result ({count} {unit})  {detail}"
        if detail
        else f"↳ Result ({count} {unit})"
    )


_TOOL_ROLES = ("tool_use", "tool_result")


def _tool_run_title(run: list[TranscriptMessage]) -> str:
    """Title for a folded run of tool calls: how many, and which tools."""
    names = []
    for msg in run:
        if msg.role != "tool_use":
            continue
        name = msg.text.splitlines()[0] if msg.text else "tool"
        if name not in names:
            names.append(name)
    calls = sum(1 for msg in run if msg.role == "tool_use")
    unit = "tool call" if calls == 1 else "tool calls"
    shown = ", ".join(names[:4])
    if len(names) > 4:
        shown += f", +{len(names) - 4} more"
    return f"⚒ {calls} {unit}  {shown}" if shown else f"⚒ {calls} {unit}"


def _result_heading(result: TranscriptMessage) -> str:
    """Divider between a call's arguments and the output it produced."""
    count = len([ln for ln in result.text.splitlines() if ln.strip()])
    return f"↳ {count} {'line' if count == 1 else 'lines'}"


def _tool_collapsible(
    title: str, *contents: Widget, collapsed: bool, classes: str
) -> Collapsible:
    """A `Collapsible` whose title is never parsed as console markup.

    Textual annotates `title` as `str` but hands it to
    `Content.from_text(markup=True)`, so a title cut from transcript bytes
    (`⚒ Bash  command: until [ "$(gh run list …`) opens a tag it cannot close
    and raises `MarkupError`, taking the whole viewer down. `Content` is what
    the widget actually wants — the annotation is just narrower than the
    runtime contract.
    """
    return Collapsible(
        *contents,
        title=Content(title),  # ty: ignore[invalid-argument-type]
        collapsed=collapsed,
        classes=classes,
    )


def _tool_widget(
    msg: TranscriptMessage, result: TranscriptMessage | None = None
) -> tuple[Collapsible, list[LogBody]]:
    """One folded tool call, with its result inside if it has one.

    The result belongs to the call, so it is shown as part of that expansion
    rather than as a row of its own — one thing to open, not two.

    Returns the widget and its bodies, each carrying the fold it sits inside so
    a later search can open its way out to a match.
    """
    if msg.role == "tool_result":
        body = _log_body(msg.text)
        fold = _tool_collapsible(
            _tool_summary(msg), body.widget, collapsed=True, classes="log-tool"
        )
        return fold, [replace(body, folds=(fold,))]

    # The title already names the tool, so the body drops that first line and
    # keeps the arguments, which the title only shows clipped.
    bodies = [_log_body(msg.text.partition("\n")[2] or msg.text)]
    contents: list[Widget] = [bodies[0].widget]
    if result is not None:
        contents.append(Static(_result_heading(result), classes="log-result-heading"))
        bodies.append(_log_body(result.text))
        contents.append(bodies[-1].widget)
    fold = _tool_collapsible(
        _tool_summary(msg), *contents, collapsed=True, classes="log-tool"
    )
    return fold, [replace(body, folds=(fold,)) for body in bodies]


def _pair_results(
    run: list[TranscriptMessage],
) -> list[tuple[TranscriptMessage, TranscriptMessage | None]]:
    """Pair each tool call in a run with its result.

    Pairing is by `tool_id`, falling back to the next unclaimed result for logs
    that carry no ids. A result nothing claims (its call clipped away, or a log
    that only recorded the reply) is returned on its own.
    """
    by_id = {
        m.tool_id: m for m in run if m.role == "tool_result" and m.tool_id is not None
    }
    claimed: set[int] = set()
    pairs: list[tuple[TranscriptMessage, TranscriptMessage | None]] = []

    for index, msg in enumerate(run):
        if msg.role != "tool_use":
            continue
        result = by_id.get(msg.tool_id) if msg.tool_id is not None else None
        if result is None:
            result = next(
                (
                    later
                    for offset, later in enumerate(run[index + 1 :], index + 1)
                    if later.role == "tool_result"
                    and later.tool_id is None
                    and offset not in claimed
                ),
                None,
            )
        if result is not None:
            claimed.add(run.index(result))
        pairs.append((msg, result))

    used = {id(result) for _, result in pairs if result is not None}
    for msg in run:
        if msg.role == "tool_result" and id(msg) not in used:
            pairs.append((msg, None))
    return pairs


def _render_transcript(
    messages: list[TranscriptMessage],
) -> tuple[list[Widget], list[LogBody]]:
    """Build the widgets for a transcript, styled by role.

    Prose (you, Claude, thinking) is rendered as a role header plus a body.
    Tool calls and their results are folded away instead: they are the bulk of a
    transcript by volume and the least of it by interest, and hiding them is what
    keeps the conversation itself readable.

    Each result is folded into its own call's expansion, so a call and its reply
    are one row rather than two. A *run* of consecutive tool messages collapses
    as a unit once it holds more than one call — a session that made twenty calls in a row would otherwise
    still fill the screen with twenty one-line rows. Opening the run reveals the
    individual calls, each still folded. A lone call is left unwrapped, since
    wrapping two rows in a third helps nobody.

    Bodies are `Content` rather than markup for the same reason snippets are:
    transcript text is arbitrary bytes, and a stray `[` or trailing backslash
    would otherwise be parsed as (or corrupt) a console markup tag.

    The second return value is every body in document order, which is what the
    `/` search updates in place — it never rebuilds this.
    """
    widgets: list[Widget] = []
    bodies: list[LogBody] = []
    index = 0
    while index < len(messages):
        msg = messages[index]

        if msg.role not in _TOOL_ROLES:
            label, css = _TRANSCRIPT_ROLES.get(msg.role, (msg.role, "log-assistant"))
            body = _log_body(msg.text)
            widgets.append(Static(label, classes=f"log-role {css}"))
            widgets.append(body.widget)
            bodies.append(body)
            index += 1
            continue

        end = index
        while end < len(messages) and messages[end].role in _TOOL_ROLES:
            end += 1
        run = messages[index:end]
        index = end

        calls = [_tool_widget(call, result) for call, result in _pair_results(run)]
        if sum(1 for m in run if m.role == "tool_use") > 1:
            outer = _tool_collapsible(
                _tool_run_title(run),
                *[widget for widget, _ in calls],
                collapsed=True,
                classes="log-tool log-tool-run",
            )
            widgets.append(outer)
            # A body inside a run is hidden twice over, so it records both folds.
            bodies.extend(
                replace(body, folds=body.folds + (outer,))
                for _, call_bodies in calls
                for body in call_bodies
            )
        else:
            widgets.extend(widget for widget, _ in calls)
            bodies.extend(body for _, call_bodies in calls for body in call_bodies)
    return widgets, bodies


def _is_fork_worktree(worktree: Path) -> bool:
    """Whether this worktree was created by forking another session."""
    return bool(read_session_meta(worktree).get("forked_from_session_id"))


def _get_claude_sessions(
    project_root: Path | None,
    worktrees: list[Path],
) -> tuple[dict[str, ClaudeSession], list[ClaudeSession]]:
    """Fetch Claude sessions for the project root and worktree paths.

    Returns (path_to_latest_session, project_root_sessions).
    """
    path_to_latest: dict[str, ClaudeSession] = {}
    root_sessions: list[ClaudeSession] = []

    if project_root is not None:
        root_sessions = get_sessions_for_path(project_root)
        if root_sessions:
            path_to_latest[str(project_root)] = root_sessions[0]

    for wt in worktrees:
        wt_sessions = get_sessions_for_path(wt)
        if wt_sessions:
            path_to_latest[str(wt)] = wt_sessions[0]

    return path_to_latest, root_sessions


CSS = """\
Screen {
    background: $surface;
}

#main {
    width: 100%;
    height: 100%;
    padding: 1 2;
}

#home-panel {
    height: 1fr;
}

#home-panel .section-label {
    text-style: bold;
    margin-bottom: 0;
}

#home-search {
    margin-bottom: 1;
}

#home-list {
    height: 1fr;
}

#search-panel {
    height: 1fr;
    padding: 1 2;
    border: round $primary;
}

#search-panel .form-label {
    margin-bottom: 1;
    text-style: bold;
}

#search-status {
    color: $text-muted;
    height: 1;
    margin-bottom: 1;
}

#search-results {
    height: 1fr;
}

#search-results > ListItem {
    padding: 0 2;
    margin-bottom: 1;
}

#search-results:focus > ListItem.--highlight {
    background: $accent;
}

#home-list > ListItem {
    padding: 0 2;
}

#home-list:focus > ListItem.--highlight {
    background: $accent;
}

.separator-item {
    color: $text-muted;
    height: 1;
}

#create-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#create-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#create-panel Input {
    margin-bottom: 1;
}

#branch-list {
    height: auto;
    max-height: 6;
    margin-bottom: 1;
}

#branch-list:focus > ListItem.--highlight {
    background: $accent;
}

#fork-branch-list {
    height: auto;
    max-height: 6;
    margin-bottom: 1;
}

#fork-branch-list:focus > ListItem.--highlight {
    background: $accent;
}

#branch-picker-list {
    height: auto;
    max-height: 16;
    margin-bottom: 1;
}

#branch-picker-list:focus > ListItem.--highlight {
    background: $accent;
}

#conflict-panel {
    height: auto;
    padding: 1 2;
    border: round $warning;
}

#conflict-panel .form-label {
    margin-bottom: 1;
    text-style: bold;
}

#conflict-list {
    height: auto;
    max-height: 6;
}

#conflict-list:focus > ListItem.--highlight {
    background: $accent;
}

.hint {
    color: $text-muted;
    margin-top: 1;
}

#log-panel {
    height: 1fr;
    padding: 1 2;
    border: round $primary;
}

/* The messages scroll; the header, search box and hint stay put. */
#log-messages {
    height: 1fr;
}

#log-search {
    margin-bottom: 1;
}

#log-raw-warning {
    color: $warning;
    height: auto;
    margin-bottom: 1;
}

#log-search-status {
    color: $text-muted;
    height: auto;
    margin-bottom: 1;
}

/* The match `n`/`N` last landed on, so it is findable in a dense body. */
.log-match-current {
    background: $accent 30%;
}

#log-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#log-panel .session-info {
    color: $text-muted;
    margin-bottom: 1;
}

.log-role {
    text-style: bold;
    margin-top: 1;
}

.log-user {
    color: $success;
}

.log-assistant {
    color: #a78bfa;
}

.log-thinking {
    color: $text-muted;
}

.log-tool {
    margin-top: 1;
    /* Textual's default Collapsible reserves a bottom pad and a top rule,
       which read as gaps between messages in a transcript. */
    padding: 0;
    border-top: none;
    background: transparent;
}

.log-tool CollapsibleTitle {
    color: $warning;
    padding: 0;
}

.log-tool Contents {
    padding: 0;
    margin: 0 0 0 2;
}

.log-result-heading {
    color: $text-muted;
    margin-top: 1;
}

.log-tool-run > CollapsibleTitle {
    color: $accent;
}

/* Inside an opened run the calls are a list, not separate messages. */
.log-tool-run .log-tool {
    margin-top: 0;
}

.log-body {
    color: $text;
}

#actions-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#actions-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#actions-panel .session-info {
    color: $text-muted;
    margin-bottom: 1;
}

#session-actions {
    height: auto;
    max-height: 10;
}

#session-actions:focus > ListItem.--highlight {
    background: $accent;
}

#finish-panel {
    height: auto;
    padding: 1 2;
    border: round $warning;
}

#finish-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#finish-panel .branch-status {
    color: $text-muted;
    margin-bottom: 1;
}

#finish-list {
    height: auto;
    max-height: 8;
}

#finish-list:focus > ListItem.--highlight {
    background: $accent;
}

#confirm-panel {
    height: auto;
    padding: 1 2;
    border: round $error;
}

#confirm-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#confirm-panel .warning-text {
    color: $warning;
    margin-bottom: 1;
}

#confirm-list {
    height: auto;
    max-height: 4;
}

#confirm-list:focus > ListItem.--highlight {
    background: $accent;
}

#project-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#project-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#project-filter {
    margin-bottom: 1;
}

#project-list {
    height: auto;
    max-height: 20;
}

#project-list > ListItem {
    padding: 0 2;
}

#project-list:focus > ListItem.--highlight {
    background: $accent;
}

#bottom-bar {
    dock: bottom;
    height: 2;
}

#version-label {
    height: 1;
    padding: 0 2;
    text-align: right;
    color: $text-muted;
    background: $surface;
}

#bottom-bar Footer {
    dock: initial;
    height: 1;
}

#update-banner {
    height: auto;
    padding: 0 2;
    margin-bottom: 1;
    background: $accent;
    color: $text;
}

#update-banner .hint {
    color: $text-muted;
    margin-top: 0;
}

QuickTerminalPrompt {
    align: center middle;
}

QuickTerminalPrompt > #qt-dialog {
    width: 60;
    height: auto;
    padding: 1 2;
    border: round $primary;
    background: $surface;
}

QuickTerminalPrompt #qt-buttons {
    height: auto;
    align: center middle;
    margin-top: 1;
}

QuickTerminalPrompt Button {
    margin: 0 1;
}

ConfigErrorDialog {
    align: center middle;
}

ConfigErrorDialog > #ce-dialog {
    width: 80;
    max-width: 90%;
    height: auto;
    max-height: 80%;
    padding: 1 2;
    border: round $error;
    background: $surface;
}

ConfigErrorDialog #ce-message {
    height: auto;
    max-height: 20;
    overflow-y: auto;
    margin-top: 1;
}

ConfigErrorDialog #ce-buttons {
    height: auto;
    align: center middle;
    margin-top: 1;
}
"""


class QuickTerminalPrompt(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "answer_no", show=False),
        Binding("y", "answer_yes", show=False),
        Binding("n", "answer_no", show=False),
    ]

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key
        self._key_label = _friendly_key_label(key)

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Enable quick terminal shortcut?", classes="form-label"),
            Static(""),
            Static(
                f"Press {self._key_label} in any tmux session to toggle a 30% "
                "bottom terminal pane. This installs a global tmux binding "
                "that applies to every tmux session on this machine. You "
                "can change this later from the home screen.",
            ),
            Horizontal(
                Button(f"Yes ({self._key_label})", id="qt-yes", variant="primary"),
                Button("No thanks", id="qt-no"),
                id="qt-buttons",
            ),
            id="qt-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#qt-yes", Button).focus()

    @on(Button.Pressed, "#qt-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#qt-no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_answer_yes(self) -> None:
        self.dismiss(True)

    def action_answer_no(self) -> None:
        self.dismiss(False)


class ConfigErrorDialog(ModalScreen[None]):
    """Modal shown at startup when `.fujimoto.yaml` fails to parse/validate."""

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("enter", "close", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold red]⚠ .fujimoto.yaml is invalid[/]", markup=True),
            # markup=False: validation text can contain brackets that aren't markup.
            Static(self._message, markup=False, id="ce-message"),
            Label(
                "Project config will be skipped until this is fixed.",
                classes="hint",
            ),
            Horizontal(
                Button("OK", id="ce-ok", variant="primary"),
                id="ce-buttons",
            ),
            id="ce-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#ce-ok", Button).focus()

    @on(Button.Pressed, "#ce-ok")
    def _ok(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


@dataclass
class SessionInfo:
    name: str
    session_type: str  # "worktree", "direct", "adhoc", or "claude"
    project: str
    path: Path
    tmux_session: str
    is_active: bool
    branch: str
    # Not running, but the user never terminated it through fujimoto — so it
    # is still theirs to come back to. Renders orange, resumes by default.
    is_stopped: bool = field(default=False)
    claude_session_id: str | None = field(default=None)
    claude_state: SessionState | None = field(default=None)
    is_fork: bool = field(default=False)


class LaunchTarget(NamedTuple):
    """What `main()` should launch once the TUI exits.

    `resume_session_id` means "resume this conversation in place". A fork is
    described instead by `forked_from_session_id` (the *parent's* conversation)
    plus `forked_from_worktree` (where that conversation was running) — for a
    fork, `working_dir` is the newly created worktree, so the two ideas cannot
    share a field. A non-None `forked_from_session_id` is what marks a launch
    as a fork; there is no separate boolean.
    """

    project: str
    working_dir: Path
    tmux_name: str | None
    session_type: str
    resume_session_id: str | None = None
    forked_from_session_id: str | None = None
    forked_from_worktree: Path | None = None


class SessionApp(App):
    TITLE = "Session Manager"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("escape", "go_back", "Back", show=True),
        Binding("d", "dismiss_update", "Dismiss update", show=False),
        # `/` means two different things, so it is two bindings on one key —
        # `check_action` disables whichever doesn't apply, and Textual falls
        # through to the next binding for the key. One action with a switch
        # inside it could not give the footer a per-view label.
        Binding("slash", "search", "Filter", show=True),
        Binding("slash", "log_search", "Search", show=True),
        Binding("s", "session_search", "Search transcripts", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        # Mode toggles for the transcript search and the log viewer's own. They
        # have to fire while the query Input holds focus, so they are Ctrl
        # chords (which Input leaves alone) rather than plain letters — and
        # unlike a plain letter, Textual still shows a chord in the footer with
        # the box focused, which is exactly when you want to see them.
        # `check_action` confines them to the views they act on.
        Binding("ctrl+r", "toggle_search_regex", "Regex", show=True),
        Binding("ctrl+t", "toggle_search_mode", "Raw/text", show=True),
        # Ctrl+I is the same byte as Tab in a legacy terminal; it only arrives
        # distinctly under the kitty keyboard protocol, which Textual requests.
        Binding("ctrl+i", "toggle_search_case", "Match case", show=True),
        # Session-log viewer only: step through the `/` matches. Plain letters
        # are safe here because the query Input swallows them while it has
        # focus, and `check_action` hides both outside the viewer.
        Binding("n", "log_next_match", "Next match", show=True),
        Binding("N", "log_prev_match", "Previous match", show=True),
    ]

    def __init__(
        self,
        pending_fork: Path | None = None,
        pending_close: LaunchTarget | None = None,
    ) -> None:
        super().__init__()
        # Set when an in-session `Ctrl-A f` detached to hand the fork over to
        # the TUI; the home screen then opens straight onto the fork flow.
        self._pending_fork: Path | None = pending_fork
        # Set when an in-session `Ctrl-A x` detached to ask whether the session
        # should be terminated or merely stopped. Carries enough to re-attach
        # if the user cancels.
        self._pending_close: LaunchTarget | None = pending_close
        self._pending_close_target: LaunchTarget | None = None
        self._project_cwd: Path | None = None
        self._project_name: str = ""
        self._current_branch: str = ""
        self._default_branch: str = ""
        self._active_sessions: set[str] = set()
        self._title_value: str = ""
        self._base_branch: str = ""
        self._start_point: str = ""
        self._worktree_path: Path | None = None
        self._launch_target: LaunchTarget | None = None
        self._project_root: Path | None = None
        self._existing_worktrees: list[Path] = []
        self._session_map: dict[str, SessionInfo] = {}
        # Sessions the user still considers open, keyed by tmux name. Loaded
        # once per `_init_git_info` rather than per render, so search keystrokes
        # don't re-read the cache file.
        self._open_sessions: dict[str, session_state.SessionRecord] = {}
        self._available_projects: list[Path] = []
        self._project_dir_paths: dict[str, Path] = {}
        self._selected_session: SessionInfo | None = None
        self._finish_action: str = ""
        self._branch_picker_names: dict[str, str] = {}
        self._poll_timer: object | None = None
        self._claude_state_snapshot: dict[str, tuple[str, SessionState]] = {}
        self._resume_sessions: list[ClaudeSession] = []
        self._log_sessions: list[ClaudeSession] = []
        # Fork state. `_fork_source` being set is what turns the shared create
        # flow into a fork; both are cleared when entering the plain create
        # flow so a cancelled fork can't leak into the next worktree.
        self._forking: bool = False
        self._fork_sessions: list[ClaudeSession] = []
        self._fork_source: ClaudeSession | None = None
        self._fork_parent_path: Path | None = None
        self._update_banner_version: str | None = None
        self._on_home: bool = False
        # Home-screen search. `_searching` is whether the search box is armed
        # (visible/focused); `_search_query` is the live filter, which stays
        # applied after Enter hands focus back to the list.
        self._searching: bool = False
        self._search_query: str = ""
        # Parsed Claude transcript data for the home screen, memoized so a `/`
        # keystroke re-filters rows instead of re-reading every JSONL log.
        self._claude_cache: (
            tuple[dict[str, ClaudeSession], list[ClaudeSession]] | None
        ) = None
        # `.fujimoto/meta.json` is read once per worktree per app run rather
        # than once per rendered row.
        self._fork_marker_cache: dict[Path, bool] = {}
        # Where each inferred "direct" session is really running, and on what
        # branch. Resolving it runs subprocesses, and `_build_home_items` runs
        # on every `/` keystroke, so it is memoized like `_claude_cache`.
        self._direct_cwd_cache: dict[str, tuple[Path, str]] = {}
        # -- Transcript search (`s`) --
        self._on_search: bool = False
        self._transcript_query: str = ""
        self._transcript_regex: bool = False
        # Message text rather than raw: it is the quieter of the two (no JSON
        # keys, uuids or tool noise) and measurably the faster, since the files
        # it has to parse are only the ones that survive the whole-file reject.
        # `ctrl+t` reaches raw, and the choice then sticks for the session.
        self._transcript_mode: ContentMode = ContentMode.TEXT
        self._transcript_case_sensitive: bool = False
        # Bumped for every scan started; batches arriving from a superseded
        # worker carry a stale token and are dropped.
        self._search_token: int = 0
        self._search_debounce: Timer | None = None
        self._search_hits: list[SearchHit] = []
        self._search_result_map: dict[str, SessionInfo] = {}
        # Set when the session-actions menu was opened from a search result, so
        # Cancel/Escape returns to the results instead of the home screen.
        self._actions_from_search: bool = False
        # Which result row the actions menu was opened from, so backing out
        # lands the highlight there instead of at the top of the list.
        self._search_selected_index: int | None = None

        # -- Session log viewer, and its own `/` search --
        self._on_log: bool = False
        self._log_session: ClaudeSession | None = None
        self._log_messages: list[TranscriptMessage] = []
        # Set together when `read_transcript` fails on a shape it doesn't know:
        # the log's lines as written, and what went wrong. `_log_parse_error`
        # being non-None is what puts the viewer in raw mode.
        self._log_raw_lines: list[str] = []
        self._log_parse_error: str | None = None
        # `_log_searching` is whether the query box is armed; `_log_query` is
        # what is highlighted. Regex/case mirror the transcript-search toggles
        # but are tracked separately, since the two views are used differently.
        self._log_searching: bool = False
        self._log_query: str = ""
        self._log_regex: bool = False
        self._log_case_sensitive: bool = False
        self._log_error: str | None = None
        # Every highlightable body in the open transcript, in document order.
        # Searching updates these in place — the viewer is never rebuilt for a
        # query, which is what a half-second render would otherwise cost.
        self._log_bodies: list[LogBody] = []
        # The spans last applied to each body, so a rescan only repaints what
        # actually changed rather than every body in the transcript.
        self._log_spans: list[Spans] = []
        # The subset currently matching, plus which one `n`/`N` is sitting on.
        self._log_matches: list[Static] = []
        self._log_match_index: int = -1
        self._log_debounce: Timer | None = None
        # Same role as `_search_token`: a scan's result can land after the query
        # has moved on, so the handler checks generation rather than trusting
        # worker cancellation.
        self._log_search_token: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="main")
        yield Vertical(
            Static(f"fujimoto v{get_version()}", id="version-label"),
            Footer(),
            id="bottom-bar",
        )

    async def on_mount(self) -> None:
        try:
            if not is_tmux_installed():
                await self._show_tmux_install()
                return
            self._init_git_info()
            self._start_update_check()
            await self._show_home()
            await self._open_pending_fork()
            await self._open_pending_close()
            config_error = self._project_config_error()
            if config_error is not None:
                self.push_screen(ConfigErrorDialog(config_error))
            if load_settings().quick_terminal_enabled is None and quick_terminal_key():
                self._prompt_quick_terminal()
        except (ConfigError, GitError) as e:
            debug.log_exception("tui.mount_failed", e)
            await self._show_error(str(e))

    def _start_update_check(self) -> None:
        def _run() -> None:
            try:
                latest, notify = check_for_update(get_version())
            except Exception:  # noqa: BLE001
                return
            if notify and latest is not None:
                self.call_from_thread(self._on_update_available, latest)

        threading.Thread(target=_run, daemon=True).start()

    def _on_update_available(self, latest: str) -> None:
        self._update_banner_version = latest
        if self._on_home:
            self.run_worker(self._show_home(), exclusive=True)

    async def action_dismiss_update(self) -> None:
        if not self._update_banner_version or not self._on_home:
            return
        dismiss_update_version(self._update_banner_version)
        self._update_banner_version = None
        await self._show_home()

    def _init_git_info(self) -> None:
        cwd = self._project_cwd
        self._project_name = get_project_name(cwd)
        self._project_root = get_repo_root(cwd)
        self._current_branch = get_current_branch(cwd)
        self._default_branch = get_default_branch(cwd)
        self._active_sessions = set(list_project_sessions(self._project_name))
        self._open_sessions = session_state.prune()
        # A different project means different transcripts and worktrees.
        self._claude_cache = None
        self._fork_marker_cache = {}
        self._direct_cwd_cache = {}
        self._available_projects = list_projects()
        self.sub_title = self._project_name
        set_terminal_title(_session_manager_title(self._project_name))
        debug.log(
            "tui.git_info",
            cwd=debug.rp(cwd) if cwd else "none",
            project=debug.rv(self._project_name),
            root=debug.rp(self._project_root),
            current_branch=debug.rref(self._current_branch),
            default_branch=debug.rref(self._default_branch),
            active_sessions=len(self._active_sessions),
            projects=len(self._available_projects),
        )

        self._existing_worktrees = []
        try:
            project_dir = get_project_worktrees_dir(
                self._project_name, self._project_root
            )
            if project_dir.exists():
                self._existing_worktrees = sorted(
                    [d for d in project_dir.iterdir() if d.is_dir()],
                    key=lambda p: p.name,
                    reverse=True,
                )
        except ConfigError:
            pass

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide bindings that don't apply to the view on screen.

        The footer is the only place most of these keys are advertised, so a
        binding that silently does nothing here (`s` in the log viewer, `n`
        anywhere else) is worse than absent. Note the polarity: Textual treats
        `False` as "hide entirely" and `None` as "show, greyed out" — hiding is
        what we want, so this returns `False`, never `None`.
        """
        if action == "search":
            return self._on_home
        if action == "log_search":
            return self._on_log
        if action == "session_search":
            return self._on_home
        if action == "refresh":
            # `action_refresh` already no-ops off the home screen; this is so
            # the footer stops offering it there.
            return self._on_home
        if action in ("log_next_match", "log_prev_match"):
            return self._on_log and bool(self._log_matches)
        if action in ("toggle_search_regex", "toggle_search_case"):
            return self._on_search or self._on_log
        if action == "toggle_search_mode":
            return self._on_search
        return True

    async def _clear_main(self) -> None:
        self._stop_polling()
        self._stop_transcript_search()
        self._stop_log_search()
        self._on_home = False
        main = self.query_one("#main")
        await main.remove_children()
        self.refresh_bindings()

    def _stop_log_search(self) -> None:
        """Leave the log viewer: drop the body list and cancel any pending scan.

        The bodies are live widgets, so holding them past a teardown would pin a
        removed subtree and let `n` scroll to something no longer mounted. The
        token is bumped before the cancel, so a scan already handed back to the
        event loop is stale from the moment the decision is made.
        """
        self._on_log = False
        self._log_bodies = []
        self._log_spans = []
        self._log_matches = []
        self._log_match_index = -1
        if self._log_debounce is not None:
            self._log_debounce.stop()
            self._log_debounce = None
        self._log_search_token += 1
        self.workers.cancel_group(self, "log-search")

    def _stop_transcript_search(self) -> None:
        """Leave the search view: cancel the scan and any pending debounce.

        Bumping the token as well as cancelling means a batch already queued on
        the event loop is dropped rather than mounted into a view that has gone.
        """
        self._on_search = False
        if self._search_debounce is not None:
            self._search_debounce.stop()
            self._search_debounce = None
        self._search_token += 1
        self.workers.cancel_group(self, "transcript-search")

    def _stop_polling(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()  # type: ignore[union-attr]
            self._poll_timer = None

    async def _show_error(self, message: str) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        # Escape the message: error text (e.g. pydantic validation output) can
        # contain brackets that would otherwise be parsed as console markup.
        await main.mount(
            Static(f"[bold red]Error:[/] {escape(message)}", markup=True),
        )

    def _prompt_quick_terminal(self) -> None:
        def _handle(answer: bool | None) -> None:
            if answer is None:
                return
            save_settings(Settings(quick_terminal_enabled=answer))
            if answer:
                enable_quick_terminal_binding()
            self.run_worker(self._refresh_home_settings_row(), exclusive=False)

        self.push_screen(QuickTerminalPrompt(quick_terminal_key()), _handle)

    async def _refresh_home_settings_row(self) -> None:
        """Re-render the home screen so the Settings row reflects the new value."""
        if self._on_home:
            await self._show_home()

    async def _show_tmux_install(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label("tmux is not installed", classes="form-label"),
                Static("tmux is required to manage sessions."),
                Static(""),
                ListView(
                    ListItem(
                        Label(
                            "Install with brew"
                            if sys.platform == "darwin"
                            else "Show install command"
                        ),
                        id="install-tmux",
                    ),
                    ListItem(Label("Quit"), id="quit-app"),
                    id="tmux-install-list",
                ),
                id="conflict-panel",
            )
        )
        self.query_one("#tmux-install-list").focus()

    # -- Home screen --

    def _project_config_error(self) -> str | None:
        """Return the project's `.fujimoto.yaml` validation error, if any."""
        if self._project_root is None:
            return None
        try:
            load_project_config(self._project_root)
        except ConfigError as e:
            return str(e)
        return None

    async def _show_home(self) -> None:
        await self._clear_main()
        self._on_home = True
        self.refresh_bindings()
        self._claude_cache = None
        main = self.query_one("#main")

        if self._update_banner_version is not None:
            current = get_version()
            await main.mount(
                Container(
                    Label(
                        f"📦 fujimoto v{self._update_banner_version} is available "
                        f"(current v{current})",
                    ),
                    Label("press d to dismiss", classes="hint"),
                    id="update-banner",
                )
            )

        search = Input(
            value=self._search_query,
            placeholder="Type to search sessions...",
            id="home-search",
        )
        # The search box lives on the home screen permanently but is only
        # displayed once `/` arms it, so toggling search never remounts.
        search.display = self._searching
        await main.mount(
            Container(
                search,
                ListView(*self._build_home_items(), id="home-list"),
                id="home-panel",
            )
        )
        if self._searching:
            self.query_one("#home-search").focus()
        else:
            self.query_one("#home-list").focus()
        self._poll_timer = self.set_interval(3, self._poll_session_states)

    def _stopped_records(self) -> dict[str, session_state.SessionRecord]:
        """Open records for this project with no live tmux session behind them.

        These are the sessions a restart (or any out-of-band kill) took away.
        Ad hoc sessions are excluded because they are not project-scoped and so
        never appear on a project's home screen.
        """
        return {
            name: rec
            for name, rec in self._open_sessions.items()
            if name not in self._active_sessions and rec.project == self._project_name
        }

    def _claude_session_data(
        self,
    ) -> tuple[dict[str, ClaudeSession], list[ClaudeSession]]:
        """Claude transcript data for the current project, parsed at most once.

        Parsing every log under `~/.claude/projects` costs real time per file,
        and `_build_home_items` runs on every `/` keystroke — so doing the parse
        inline made the filter box visibly lag behind typing. The cache is
        cleared when the home screen is (re)entered or the project changes, and
        refreshed by the 3s state poller; a filter keystroke never invalidates
        it.
        """
        if self._claude_cache is None:
            self._claude_cache = _get_claude_sessions(
                self._project_root, self._existing_worktrees
            )
        return self._claude_cache

    def _matching_worktree(self, path: Path) -> Path | None:
        """The project worktree `path` refers to, if it is one.

        Compared by `resolve()` because the two sides come from different
        places: worktree paths are listed from the configured root, while a
        path reported by tmux or recorded by Claude is the physical one.
        """
        try:
            target = path.resolve()
        except OSError:  # pragma: no cover - resolve() rarely fails
            return None
        for wt in self._existing_worktrees:
            try:
                if wt.resolve() == target:
                    return wt
            except OSError:  # pragma: no cover
                continue
        return None

    def _direct_session_cwd(self, tmux_name: str) -> tuple[Path, str]:
        """The real working directory and branch behind a `direct-N` row.

        A direct row is *inferred* — any active session for the project whose
        name matches no worktree directory — so nothing records where it runs,
        and assuming the repo root was wrong: a resume can leave a `direct-N`
        session sitting in a worktree, and the row then reported the wrong
        directory, the wrong branch and the wrong Claude state. Ask tmux
        instead, falling back to the session record (ignoring a relative `cwd`,
        which older fujimotos wrote for exactly this reason) and only then to
        the project root.
        """
        cached = self._direct_cwd_cache.get(tmux_name)
        if cached is not None:
            return cached

        via = "tmux"
        path = get_session_path(tmux_name)
        if path is None:
            record = self._open_sessions.get(tmux_name)
            if record is not None and record.path.is_absolute():
                path, via = record.path, "record"
        if path is None:
            path = self._project_root or self._project_cwd or Path(".")
            via = "project-root"

        # Prefer the worktree list's spelling of the path, so the result is
        # comparable with the keys of `_claude_session_data`.
        worktree = self._matching_worktree(path)
        if worktree is not None:
            path = worktree

        branch = _session_branch(path)
        debug.log(
            "tui.direct_cwd",
            session=debug.rv(tmux_name),
            via=via,
            path=debug.rp(path),
            branch=debug.rref(branch),
            worktree=worktree is not None,
        )
        resolved = (path, branch)
        self._direct_cwd_cache[tmux_name] = resolved
        return resolved

    def _resume_target(
        self, project: str, cwd: Path, session: SessionInfo | None = None
    ) -> tuple[str, str]:
        """The tmux name and session type for resuming a transcript in `cwd`.

        The name has to follow the *directory*, not the row the resume started
        from. A transcript reached through search carries the worktree it ran
        in, and naming that session `direct-N` cut it loose from that worktree:
        the home screen rendered it against the repo root, the worktree still
        looked idle, and resuming it a second time started another claude on
        the same transcript. So prefer the worktree's own tmux name, and fall
        back to `direct-N` only when that name is genuinely occupied.

        `session` is the row the resume was started from; a worktree row names
        its own worktree even if the transcript recorded a directory that is no
        longer one of the project's.
        """
        worktree = self._matching_worktree(cwd)
        if worktree is None and session is not None:
            if session.session_type == "worktree":
                worktree = session.path
        if worktree is not None:
            name = session_name(project, worktree.name)
            if name not in self._active_sessions:
                debug.log(
                    "tui.resume_target",
                    via="worktree",
                    tmux=debug.rv(name),
                    cwd=debug.rp(cwd),
                )
                return name, "worktree"
        name = get_next_direct_session_name(project, self._active_sessions)
        debug.log(
            "tui.resume_target",
            via="direct",
            tmux=debug.rv(name),
            cwd=debug.rp(cwd),
            in_worktree=worktree is not None,
        )
        return name, "direct"

    def _is_fork(self, worktree: Path) -> bool:
        """Memoized `_is_fork_worktree` — fork provenance never changes."""
        cached = self._fork_marker_cache.get(worktree)
        if cached is None:
            cached = _is_fork_worktree(worktree)
            self._fork_marker_cache[worktree] = cached
        return cached

    def _search_matches(self, *fields: str) -> bool:
        """Case-insensitive substring match of the search query against `fields`.

        An empty query matches everything, so callers can filter
        unconditionally.
        """
        query = self._search_query.strip().lower()
        if not query:
            return True
        return any(query in field.lower() for field in fields if field)

    def _build_home_items(self) -> list[ListItem]:
        """Build the home screen rows, honouring the current search query.

        Also (re)populates `_session_map` and `_claude_state_snapshot`, so it is
        the single place that decides which sessions are on screen — the live
        state poller then only touches rows that survived the filter.
        """
        searching = bool(self._search_query.strip())

        # Fetch Claude session data for state indicators
        path_to_latest, root_claude_sessions = self._claude_session_data()
        self._claude_state_snapshot = {
            k: (v.session_id, v.state) for k, v in path_to_latest.items()
        }
        claimed_claude_ids: set[str] = set()

        stopped_records = self._stopped_records()

        items: list[ListItem] = []
        if not searching:
            if stopped_records:
                count = len(stopped_records)
                plural = "s" if count != 1 else ""
                items.append(
                    ListItem(
                        Label(
                            f"[bold]{ICON_ORANGE_CIRCLE} Restore {count} stopped "
                            f"session{plural}[/]",
                            markup=True,
                        ),
                        id="action-restore",
                    ),
                )
            items += [
                ListItem(
                    Label("[bold]+ New worktree session[/]", markup=True),
                    id="action-create",
                ),
                ListItem(
                    Label(
                        f"[bold]+ New session in {self._project_name}[/]",
                        markup=True,
                    ),
                    id="action-direct",
                ),
                ListItem(
                    Label("[bold]+ Ad hoc session[/]", markup=True),
                    id="action-adhoc",
                ),
            ]

        # Build session map for all items
        self._session_map = {}

        # Collect direct sessions (active tmux sessions without matching worktrees)
        worktree_session_names = set()
        for wt in self._existing_worktrees:
            sname = session_name(self._project_name, wt.name)
            worktree_session_names.add(sname)

        direct_sessions: list[str] = []
        for sname in sorted(self._active_sessions):
            if sname not in worktree_session_names:
                direct_sessions.append(sname)

        # Active sessions section
        active_worktrees = [
            wt
            for wt in self._existing_worktrees
            if session_name(self._project_name, wt.name) in self._active_sessions
        ]

        active_items: list[ListItem] = []

        for sname in direct_sessions:
            item_id = f"ds-{sname.replace('/', '--')}"
            display_name = sname.split("/", 1)[1] if "/" in sname else sname
            path, branch = self._direct_session_cwd(sname)
            if not self._search_matches(
                display_name, sname, self._project_name, branch
            ):
                continue
            cs = path_to_latest.get(str(path))
            cs_id = cs.session_id if cs else None
            cs_state = cs.state if cs else None
            if cs_id:
                claimed_claude_ids.add(cs_id)
            state_suffix = _claude_state_label(cs_state) if cs_state else ""
            info = SessionInfo(
                name=display_name,
                session_type="direct",
                project=self._project_name,
                path=path,
                tmux_session=sname,
                is_active=True,
                branch=branch,
                claude_session_id=cs_id,
                claude_state=cs_state,
            )
            self._session_map[item_id] = info
            active_items.append(
                ListItem(
                    Label(self._build_session_label(info, state_suffix), markup=True),
                    id=item_id,
                )
            )

        for wt in active_worktrees:
            sname = session_name(self._project_name, wt.name)
            item_id = f"wt-{wt.name}"
            branch = f"worktree/{wt.name}"
            if not self._search_matches(wt.name, branch, sname):
                continue
            cs = path_to_latest.get(str(wt))
            cs_id = cs.session_id if cs else None
            cs_state = cs.state if cs else None
            if cs_id:
                claimed_claude_ids.add(cs_id)
            state_suffix = _claude_state_label(cs_state) if cs_state else ""
            info = SessionInfo(
                name=wt.name,
                session_type="worktree",
                project=self._project_name,
                path=wt,
                tmux_session=sname,
                is_active=True,
                branch=branch,
                claude_session_id=cs_id,
                claude_state=cs_state,
                is_fork=self._is_fork(wt),
            )
            self._session_map[item_id] = info
            label_text = self._build_session_label(info, state_suffix)
            active_items.append(ListItem(Label(label_text, markup=True), id=item_id))

        # Stopped sessions sit in the same section as running ones: the circle
        # colour carries the distinction, so there is no need to split them out.
        for sname, rec in sorted(stopped_records.items()):
            display_name = sname.split("/", 1)[1] if "/" in sname else sname
            is_worktree = rec.session_type == "worktree"
            branch = rec.branch or (
                f"worktree/{display_name}" if is_worktree else self._current_branch
            )
            if not self._search_matches(display_name, sname, branch):
                continue
            item_id = (
                f"wt-{display_name}"
                if is_worktree
                else f"ds-{sname.replace('/', '--')}"
            )
            cs = path_to_latest.get(str(rec.path))
            cs_id = cs.session_id if cs else rec.claude_session_id
            if cs_id:
                claimed_claude_ids.add(cs_id)
            info = SessionInfo(
                name=display_name,
                session_type=rec.session_type,
                project=rec.project,
                path=rec.path,
                tmux_session=sname,
                is_active=False,
                is_stopped=True,
                branch=branch,
                claude_session_id=cs_id,
                claude_state=cs.state if cs else None,
                is_fork=is_worktree and self._is_fork(rec.path),
            )
            self._session_map[item_id] = info
            label_text = self._build_session_label(info, "")
            active_items.append(ListItem(Label(label_text, markup=True), id=item_id))

        if active_items:
            items.append(
                ListItem(
                    Static(
                        "───── sessions ─────",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            items.extend(active_items)

        # Inactive worktrees: no live session, and none the user still wants.
        # A worktree with an open record is stopped, and was rendered above.
        inactive_worktrees = [
            wt
            for wt in self._existing_worktrees
            if session_name(self._project_name, wt.name) not in self._active_sessions
            and session_name(self._project_name, wt.name) not in stopped_records
        ]

        inactive_items: list[ListItem] = []
        for wt in inactive_worktrees:
            sname = session_name(self._project_name, wt.name)
            item_id = f"wt-{wt.name}"
            branch = f"worktree/{wt.name}"
            if not self._search_matches(wt.name, branch, sname):
                continue
            cs = path_to_latest.get(str(wt))
            cs_id = cs.session_id if cs else None
            cs_state = cs.state if cs else None
            if cs_id:
                claimed_claude_ids.add(cs_id)
            info = SessionInfo(
                name=wt.name,
                session_type="worktree",
                project=self._project_name,
                path=wt,
                tmux_session=sname,
                is_active=False,
                branch=branch,
                claude_session_id=cs_id,
                claude_state=cs_state,
                is_fork=self._is_fork(wt),
            )
            self._session_map[item_id] = info
            label_text = self._build_session_label(info, "")
            inactive_items.append(ListItem(Label(label_text, markup=True), id=item_id))

        if inactive_items:
            items.append(
                ListItem(
                    Static(
                        "───── inactive worktrees ─────",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            items.extend(inactive_items)

        # Previous Claude sessions (from project root, not claimed by active items)
        previous_claude = [
            cs for cs in root_claude_sessions if cs.session_id not in claimed_claude_ids
        ][:5]

        previous_items: list[ListItem] = []
        for cs in previous_claude:
            short_id = cs.session_id[:8]
            if not self._search_matches(short_id, cs.session_id, cs.git_branch or ""):
                continue
            item_id = f"cs-{cs.session_id}"
            time_label = _relative_time(cs.last_activity)
            branch_label = f"{BRANCH_ICON} {cs.git_branch}" if cs.git_branch else ""
            label_text = f"  {short_id}  [dim]{branch_label}  {time_label}[/]"
            self._session_map[item_id] = SessionInfo(
                name=short_id,
                session_type="claude",
                project=self._project_name,
                path=cs.cwd,
                tmux_session="",
                is_active=False,
                branch=cs.git_branch or "",
                claude_session_id=cs.session_id,
                claude_state=cs.state,
            )
            previous_items.append(ListItem(Label(label_text, markup=True), id=item_id))

        if previous_items:
            items.append(
                ListItem(
                    Static(
                        "───── previous claude sessions ─────",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            items.extend(previous_items)

        self._log_home_inventory(path_to_latest, root_claude_sessions)

        if searching:
            if not items:
                items.append(
                    ListItem(
                        Static("no matching sessions", classes="separator-item"),
                        disabled=True,
                    ),
                )
            return items

        settings_items = self._build_settings_items()
        if self._available_projects or settings_items:
            items.append(
                ListItem(
                    Static(
                        ICON_HLINE * 29,
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            items.extend(settings_items)
            if self._available_projects:
                items.append(
                    ListItem(
                        Label(
                            f"[dim]Switch project (current: {self._project_name})[/]",
                            markup=True,
                        ),
                        id="action-switch-project",
                    ),
                )

        return items

    async def action_search(self) -> None:
        """Arm the home-screen name filter (bound to `/` on the home screen)."""
        if not self._on_home or not self.query("#home-search"):
            return
        self._searching = True
        search = self.query_one("#home-search", Input)
        search.display = True
        search.focus()

    async def _clear_search(self) -> None:
        """Drop the filter, hide the search box and return focus to the list."""
        self._searching = False
        self._search_query = ""
        if self.query("#home-search"):
            search = self.query_one("#home-search", Input)
            search.value = ""
            search.display = False
        await self._refresh_home_list()
        if self.query("#home-list"):
            self.query_one("#home-list").focus()

    async def _refresh_home_list(self) -> None:
        """Re-render the home list rows in place for the current search query."""
        if not self.query("#home-list"):
            return
        home_list = self.query_one("#home-list", ListView)
        await home_list.clear()
        for item in self._build_home_items():
            await home_list.append(item)
        home_list.index = self._first_selectable_index(home_list)

    @staticmethod
    def _first_selectable_index(home_list: ListView) -> int | None:
        """Index of the first non-separator row, so highlight never lands on one."""
        for index, item in enumerate(home_list.children):
            if not item.disabled:
                return index
        return None

    async def action_refresh(self) -> None:
        """Re-read session state and rebuild the home list (bound to `r`).

        The 3s poller only refreshes Claude state on rows that already exist,
        so a worktree created or a tmux session started outside fujimoto never
        appears until the home screen is re-entered. This re-runs the same
        discovery `_init_git_info` does at startup — tmux sessions, the open
        session store, worktrees, projects — drops the memoized transcript data
        and rebuilds the rows in place, keeping the current filter and the
        highlighted row.
        """
        if not self._on_home or not self.query("#home-list"):
            return
        home_list = self.query_one("#home-list", ListView)
        # Captured before the rebuild: appending rows moves the highlight.
        highlighted = home_list.highlighted_child
        target = highlighted.id if highlighted is not None else None
        self._init_git_info()
        debug.log(
            "tui.refresh",
            project=debug.rv(self._project_name),
            worktrees=len(self._existing_worktrees),
            active=len(self._active_sessions),
            open_records=len(self._open_sessions),
            filtered=bool(self._search_query),
        )
        await self._refresh_home_list()
        if target is not None:
            for index, item in enumerate(home_list.children):
                if item.id == target and not item.disabled:
                    home_list.index = index
                    break

    @on(Input.Changed, "#home-search")
    async def on_home_search_changed(self, event: Input.Changed) -> None:
        # Mounting the box with a preserved query fires Changed too; the list is
        # already built for that query, so only react to a real edit.
        if event.value == self._search_query:
            return
        self._search_query = event.value
        await self._refresh_home_list()

    @on(Input.Submitted, "#home-search")
    async def on_home_search_submitted(self, event: Input.Submitted) -> None:
        # The filter stays applied; Enter just hands focus to the filtered list.
        if self.query("#home-list"):
            self.query_one("#home-list").focus()

    # -- Transcript search (`s`) --
    #
    # The `/` filter above matches session *names* from data already in memory.
    # This searches the *contents* of every Claude transcript for the project,
    # which means reading every JSONL log — far too slow to do between
    # keystrokes. So the scan runs in a thread worker, in batches, and results
    # are appended as each batch lands. Typing stays responsive because the
    # only work on the event loop is appending rows.

    SEARCH_DEBOUNCE = 0.3
    SEARCH_MIN_QUERY = 2

    async def action_session_search(self) -> None:
        """Open the transcript search view (bound to `s` on the home screen)."""
        if not self._on_home:
            return
        await self._show_session_search()

    async def _show_session_search(self, *, restore: bool = False) -> None:
        """Mount the transcript search view.

        `restore` re-renders the hits already collected rather than starting a
        fresh scan, which is what returning from a result's actions menu wants.
        """
        await self._clear_main()
        self._on_search = True
        self.refresh_bindings()
        if not restore:
            self._search_hits = []
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label(
                    f"Search session transcripts in {self._project_name}",
                    classes="form-label",
                ),
                Input(
                    value=self._transcript_query,
                    placeholder="Type to search every conversation...",
                    id="search-input",
                ),
                Static(self._search_status_text(), id="search-status", markup=True),
                ListView(*self._build_search_result_items(), id="search-results"),
                Static(
                    "[dim]ctrl+r regex · ctrl+t raw/text · ctrl+i case · "
                    "↑↓ pick · enter open · esc back[/]",
                    markup=True,
                    classes="hint",
                ),
                id="search-panel",
            )
        )
        if restore and self._search_hits:
            count = len(self._search_hits)
            plural = "s" if count != 1 else ""
            self._set_search_status(f"[dim]{count} session{plural} matched[/]")
            # Coming back from a result's actions menu, put the highlight back
            # on that result and focus the list — otherwise walking a set of
            # results means being dropped at the top of it every time.
            results = self.query_one("#search-results", ListView)
            if (
                self._search_selected_index is not None
                and self._search_selected_index < len(results)
            ):
                results.index = self._search_selected_index
            results.focus()
            debug.log(
                "search.restored",
                hits=count,
                index=self._search_selected_index
                if self._search_selected_index is not None
                else "none",
            )
        else:
            self.query_one("#search-input", Input).focus()

    def _search_status_text(self, detail: str = "") -> str:
        """The mode line above the results, plus an optional progress detail."""
        pattern = "regex" if self._transcript_regex else "literal"
        case = "match case" if self._transcript_case_sensitive else "ignore case"
        mode = f"[b]{pattern}[/] · [b]{self._transcript_mode.label}[/] · [b]{case}[/]"
        return f"{mode}  {detail}" if detail else mode

    def _set_search_status(self, detail: str) -> None:
        if self.query("#search-status"):
            self.query_one("#search-status", Static).update(
                self._search_status_text(detail)
            )

    def _build_search_result_items(self) -> list[ListItem]:
        """Rows for the hits collected so far, newest transcript first."""
        self._search_result_map = {}
        if not self._search_hits:
            return []

        # List padding (2 each side) + panel border and padding.
        max_width = max(20, self.size.width - 12)
        items: list[ListItem] = []
        for index, hit in enumerate(self._search_hits):
            items.append(self._build_search_result_item(index, hit, max_width))
        return items

    def _build_search_result_item(
        self, index: int, hit: SearchHit, max_width: int
    ) -> ListItem:
        cs = hit.session
        item_id = f"sr-{index}"
        plural = "es" if hit.match_count != 1 else ""
        branch = f"{BRANCH_ICON} {cs.git_branch}  " if cs.git_branch else ""
        heading = (
            f"{escape(cs.cwd.name or str(cs.cwd))}"
            f"  [dim]{branch}{_relative_time(cs.last_activity)}"
            f"  ·  {hit.match_count} match{plural}[/]"
        )
        labels = [Label(heading, markup=True)]
        for snippet in hit.snippets:
            labels.append(Label(_render_snippet(snippet, max_width)))
        self._search_result_map[item_id] = SessionInfo(
            name=cs.session_id[:8],
            session_type="claude",
            project=self._project_name,
            path=cs.cwd,
            tmux_session="",
            is_active=False,
            branch=cs.git_branch or "",
            claude_session_id=cs.session_id,
            claude_state=cs.state,
        )
        return ListItem(*labels, id=item_id)

    async def action_toggle_search_regex(self) -> None:
        """Flip literal/regex matching and rerun the scan (bound to ctrl+r).

        Shared with the log viewer's `/` search, which keeps its own flag: the
        two views are used differently and a choice in one shouldn't surprise
        the other.
        """
        if self._on_log:
            self._log_regex = not self._log_regex
            self._set_log_status()
            self._start_log_search()
            return
        if not self._on_search:
            return
        self._transcript_regex = not self._transcript_regex
        await self._restart_transcript_search()

    async def action_toggle_search_mode(self) -> None:
        """Flip message-text/raw matching and rerun the scan (bound to ctrl+t)."""
        if not self._on_search:
            return
        self._transcript_mode = self._transcript_mode.toggled()
        await self._restart_transcript_search()

    async def action_toggle_search_case(self) -> None:
        """Flip case sensitivity and rerun (bound to ctrl+i, log viewer too)."""
        if self._on_log:
            self._log_case_sensitive = not self._log_case_sensitive
            self._set_log_status()
            self._start_log_search()
            return
        if not self._on_search:
            return
        self._transcript_case_sensitive = not self._transcript_case_sensitive
        await self._restart_transcript_search()

    async def _restart_transcript_search(self) -> None:
        """Re-render the mode line and rescan under the new settings."""
        self._set_search_status("")
        await self._clear_search_results()
        self._start_transcript_search()

    @on(Input.Changed, "#search-input")
    def on_transcript_search_changed(self, event: Input.Changed) -> None:
        # Mounting the box with a preserved query fires Changed too; when the
        # results for it are already on screen (returning from a result's
        # actions menu) there is nothing to redo.
        if event.value == self._transcript_query and self._search_hits:
            return
        # Debounced: a scan touches every transcript, so restarting it on each
        # keystroke would spend most of its time being cancelled.
        self._transcript_query = event.value
        if self._search_debounce is not None:
            self._search_debounce.stop()
        self._search_debounce = self.set_timer(
            self.SEARCH_DEBOUNCE, self._debounced_transcript_search
        )

    async def _debounced_transcript_search(self) -> None:
        self._search_debounce = None
        await self._clear_search_results()
        self._start_transcript_search()

    @on(Input.Submitted, "#search-input")
    def on_transcript_search_submitted(self, event: Input.Submitted) -> None:
        # Enter hands focus to the results; the scan keeps running.
        if self.query("#search-results"):
            self.query_one("#search-results").focus()

    async def _clear_search_results(self) -> None:
        self._search_hits = []
        self._search_selected_index = None
        if self.query("#search-results"):
            await self.query_one("#search-results", ListView).clear()
        self._search_result_map = {}

    def _start_transcript_search(self) -> None:
        """Kick off a scan, cancelling whatever was running."""
        # Bumping the token first means in-flight batches from the previous
        # scan are recognised as stale even before the worker notices it has
        # been cancelled.
        self._search_token += 1
        self.workers.cancel_group(self, "transcript-search")
        query = self._transcript_query.strip()
        if len(query) < self.SEARCH_MIN_QUERY:
            detail = (
                f"[dim]type at least {self.SEARCH_MIN_QUERY} characters[/]"
                if query
                else ""
            )
            self._set_search_status(detail)
            return
        self._set_search_status("[dim]searching…[/]")
        self._run_transcript_search(
            query,
            self._transcript_regex,
            self._transcript_mode,
            self._transcript_case_sensitive,
            self._search_token,
        )

    @work(thread=True, exclusive=True, group="transcript-search")
    def _run_transcript_search(
        self,
        query: str,
        regex: bool,
        mode: ContentMode,
        case_sensitive: bool,
        token: int,
    ) -> None:
        """Scan every transcript for the project in a background thread.

        Results are handed back a batch at a time via `call_from_thread`, so the
        UI fills in progressively instead of waiting for the whole scan.
        """
        worker = get_current_worker()
        try:
            matcher = claude_search.compile_matcher(
                query, regex=regex, mode=mode, case_sensitive=case_sensitive
            )
        except SearchError as e:
            self.call_from_thread(self._search_failed, token, str(e))
            return

        logs = claude_search.list_session_logs(
            self._project_root, self._existing_worktrees
        )
        total = len(logs)
        try:
            for scanned, hits in claude_search.iter_hits(
                logs, matcher, is_cancelled=lambda: worker.is_cancelled
            ):
                # Re-checked after the batch: the scan may have been
                # superseded while this batch was being collected. Racy by
                # nature, so `_apply_search_batch`'s token check is the
                # guarantee and this is only an early out.
                if worker.is_cancelled:  # pragma: no cover
                    return
                self.call_from_thread(
                    self._apply_search_batch, token, scanned, total, hits
                )
        except RuntimeError:  # pragma: no cover - app shut down mid-scan
            return

    def _apply_search_batch(
        self,
        token: int,
        scanned: int,
        total: int,
        hits: tuple[SearchHit, ...],
    ) -> None:
        """Append a batch of hits and update the progress line."""
        # A batch from a superseded scan would interleave results for a query
        # the user has already moved on from.
        if token != self._search_token or not self._on_search:
            return
        if not self.query("#search-results"):
            return
        results = self.query_one("#search-results", ListView)
        max_width = max(20, self.size.width - 12)
        for hit in hits:
            index = len(self._search_hits)
            self._search_hits.append(hit)
            results.append(self._build_search_result_item(index, hit, max_width))
        if results.index is None and len(results) > 0:
            results.index = 0

        found = len(self._search_hits)
        if scanned >= total:
            if total == 0:
                detail = "[dim]no transcripts for this project[/]"
            else:
                plural = "s" if found != 1 else ""
                detail = f"[dim]{found} session{plural} of {total} matched[/]"
        else:
            detail = f"[dim]searching… {scanned}/{total}  ·  {found} found[/]"
        self._set_search_status(detail)

    def _search_failed(self, token: int, message: str) -> None:
        if token != self._search_token or not self._on_search:
            return
        self._set_search_status(f"[red]{escape(message)}[/]")

    @on(ListView.Selected, "#search-results")
    async def on_search_result_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id and item_id in self._search_result_map:
            # Row ids are `sr-{index}` into `_search_hits`, so the index alone
            # is enough to restore the highlight when the menu is backed out of.
            self._search_selected_index = int(item_id.split("-", 1)[1])
            await self._show_session_actions(
                self._search_result_map[item_id], from_search=True
            )

    def _log_home_inventory(
        self,
        path_to_latest: dict[str, ClaudeSession],
        root_claude_sessions: list[ClaudeSession],
    ) -> None:
        """Record what the home screen resolved, for --debug diagnosis."""
        if not debug.is_enabled():
            return
        debug.log_once(
            "tui-home",
            "tui.home",
            items=len(self._session_map),
            worktrees=len(self._existing_worktrees),
            active_sessions=len(self._active_sessions),
            claude_paths=len(path_to_latest),
            root_claude_sessions=len(root_claude_sessions),
            resume_offered=len(self._resume_sessions),
        )
        # `_existing_worktrees` is newest-first, so a cap keeps the worktrees
        # someone is plausibly working in and counts the long tail.
        skipped_worktrees = 0
        skipped_with_session = 0
        for path in self._existing_worktrees:
            has_session = str(path) in path_to_latest
            logged = debug.log_capped(
                "tui.worktree",
                "tui.worktree",
                dedupe_key=f"tui-worktree-{path}",
                path=debug.rp(path),
                has_claude_session=has_session,
            )
            if not logged:
                skipped_worktrees += 1
                skipped_with_session += has_session
        if skipped_worktrees:
            debug.log_once(
                "tui-worktree-summary",
                "tui.worktree_summary",
                not_logged=skipped_worktrees,
                with_claude_session=skipped_with_session,
                without=skipped_worktrees - skipped_with_session,
            )

        # Running sessions first: they are what a report is usually about.
        items = sorted(
            self._session_map.items(),
            key=lambda kv: (not kv[1].is_active, kv[0]),
        )
        skipped_types: dict[str, int] = {}
        skipped_states: dict[str, int] = {}
        for item_id, info in items:
            logged = debug.log_capped(
                "tui.item",
                "tui.item",
                dedupe_key=f"tui-item-{item_id}",
                id=debug.rid(item_id),
                type=info.session_type,
                name=debug.rv(info.name),
                path=debug.rp(info.path),
                tmux=debug.rv(info.tmux_session),
                active=info.is_active,
                branch=debug.rref(info.branch),
                claude_session=info.claude_session_id or "none",
                claude_state=info.claude_state,
            )
            if not logged:
                skipped_types[info.session_type] = (
                    skipped_types.get(info.session_type, 0) + 1
                )
                state = str(info.claude_state or "unknown")
                skipped_states[state] = skipped_states.get(state, 0) + 1
        if skipped_types:
            debug.log_once(
                "tui-item-summary",
                "tui.item_summary",
                not_logged=sum(skipped_types.values()),
                types=",".join(f"{k}={v}" for k, v in sorted(skipped_types.items())),
                claude_states=",".join(
                    f"{k}={v}" for k, v in sorted(skipped_states.items())
                ),
            )

    def _build_settings_items(self) -> list[ListItem]:
        key = quick_terminal_key()
        if not key:
            label_text = "[dim]Quick terminal shortcut: disabled (env)[/]"
        else:
            enabled = load_settings().quick_terminal_enabled is True
            state = "on" if enabled else "off"
            label_text = (
                f"[dim]Quick terminal shortcut: {state}  "
                f"({_friendly_key_label(key)})[/]"
            )
        return [
            ListItem(
                Label(label_text, markup=True),
                id="action-toggle-quick-terminal",
            )
        ]

    async def _poll_session_states(self) -> None:
        """Poll for Claude session state changes and update labels in-place."""
        if not self.query("#home-list"):
            return

        # Refresh (rather than bypass) the render cache, so the next filter
        # keystroke renders from freshly polled data without re-parsing.
        self._claude_cache = _get_claude_sessions(
            self._project_root, self._existing_worktrees
        )
        new_path_to_latest, _ = self._claude_cache
        new_snapshot = {
            k: (v.session_id, v.state) for k, v in new_path_to_latest.items()
        }
        if new_snapshot == self._claude_state_snapshot:
            return

        self._claude_state_snapshot = new_snapshot

        # Update labels in-place for sessions with changed Claude state
        for item_id, session in self._session_map.items():
            if session.session_type == "claude":
                continue  # Previous sessions don't need live updates
            # The row's own path, which for a direct session is where tmux
            # says it runs — not an assumed project root.
            new_cs = new_path_to_latest.get(str(session.path))
            new_state = new_cs.state if new_cs else None
            if new_state == session.claude_state:
                continue
            # State changed — update the session and its label
            session.claude_state = new_state
            session.claude_session_id = new_cs.session_id if new_cs else None
            state_suffix = (
                _claude_state_label(new_state)
                if new_state and session.is_active
                else ""
            )
            label_text = self._build_session_label(session, state_suffix)
            try:
                item = self.query_one(f"#{item_id}")
                label = item.query_one(Label)
                label.update(label_text)
            except Exception:  # pragma: no cover
                pass

    def _build_session_label(self, session: SessionInfo, state_suffix: str) -> str:
        """Build the display label for a session item.

        Single source of truth for session rows: `_show_home` renders with it
        and `_poll_session_states` updates in place with it, so the two can't
        drift apart.
        """
        fork = f" {ICON_FORK}" if session.is_fork else ""
        if session.is_active:
            if session.session_type == "direct":
                # A `direct-N` session is not always in the repo root — a
                # resume can leave one in a worktree — so name the directory
                # when it differs instead of implying the root.
                location = session.project
                if self._project_root and session.path != self._project_root:
                    location = f"{session.project}/{session.path.name}"
                return (
                    f"{ICON_GREEN_CIRCLE} {session.name}{fork}"
                    f"  [dim]({location} {BRANCH_ICON}"
                    f" {session.branch})[/]{state_suffix}"
                )
            return (
                f"{ICON_GREEN_CIRCLE} {session.name}{fork}"
                f"  [dim]({BRANCH_ICON} {session.branch})[/]{state_suffix}"
            )
        icon = ICON_ORANGE_CIRCLE if session.is_stopped else ICON_BLACK_CIRCLE
        return f"{icon} {session.name}{fork}  [dim]({BRANCH_ICON} {session.branch})[/]"

    # -- Stopping and terminating --

    async def _end_session(self, session: SessionInfo, *, terminate: bool) -> None:
        """Kill a session's tmux session, and set its intent.

        The single handler behind both menu items and both outcomes of the
        `Ctrl-A x` prompt. Stopping leaves the record open so the session comes
        back orange; terminating forgets it, which is the only way a session
        stops being open.
        """
        try:
            if session.is_active:
                try:
                    kill_session(session.tmux_session)
                except TmuxError:
                    # A session that died between listing it and acting on it
                    # is already in the state we wanted. Anything else means it
                    # is still running, and silently marking it closed would
                    # hide a live session.
                    if session_exists(session.tmux_session):
                        raise
                self._active_sessions.discard(session.tmux_session)
            if terminate:
                session_state.mark_closed(session.tmux_session)
            else:
                session_state.touch(session.tmux_session, session.claude_session_id)
            self._init_git_info()
            await self._show_home()
        except (TmuxError, ConfigError, GitError) as e:
            await self._show_error(str(e))

    async def _open_pending_close(self) -> None:
        """Ask what `Ctrl-A x` should do, for a session that just detached."""
        target = self._pending_close
        self._pending_close = None
        if target is None or target.tmux_name is None:
            return
        await self._show_terminate_prompt(target)

    async def _show_terminate_prompt(self, target: LaunchTarget) -> None:
        """Terminate / stop / cancel, defaulting to terminate.

        Three options, not two: the `confirm-before` this replaces could be
        answered `n`, and losing that would be a regression. Cancel re-attaches
        immediately rather than dropping the user on the home screen, so it is
        a true no-op.
        """
        self._pending_close_target = target
        await self._clear_main()
        main = self.query_one("#main")
        name = target.tmux_name or ""
        display_name = name.split("/", 1)[1] if "/" in name else name
        await main.mount(
            Container(
                Label(f"Terminate {display_name}?", classes="form-label"),
                Static(str(target.working_dir), classes="session-info"),
                ListView(
                    ListItem(
                        Label(
                            "Terminate  [dim]— close it and mark it done[/]",
                            markup=True,
                        ),
                        id="tp-terminate",
                    ),
                    ListItem(
                        Label(
                            f"Stop  [dim]— keep it for later "
                            f"{ICON_ORANGE_CIRCLE}, history preserved[/]",
                            markup=True,
                        ),
                        id="tp-stop",
                    ),
                    ListItem(
                        Label("[dim]Cancel — go back to the session[/]", markup=True),
                        id="tp-cancel",
                    ),
                    id="terminate-prompt",
                ),
                Static(
                    f"[dim]Tip: {_friendly_key_label(meta_key())} s stops "
                    f"without asking.[/]"
                    if meta_key()
                    else "",
                    markup=True,
                    classes="hint",
                ),
                id="actions-panel",
            )
        )
        self.query_one("#terminate-prompt").focus()

    @on(ListView.Selected, "#terminate-prompt")
    async def on_terminate_prompt_selected(self, event: ListView.Selected) -> None:
        target = self._pending_close_target
        if target is None or target.tmux_name is None:
            return  # pragma: no cover
        if event.item.id == "tp-cancel":
            # Re-attach rather than returning home, so cancelling costs nothing.
            self._launch_target = target
            self.exit()
            return
        session = SessionInfo(
            name=target.tmux_name,
            session_type=target.session_type,
            project=target.project,
            path=target.working_dir,
            tmux_session=target.tmux_name,
            is_active=True,
            branch="",
        )
        self._pending_close_target = None
        await self._end_session(session, terminate=event.item.id == "tp-terminate")

    async def _restore_stopped_sessions(self) -> None:
        """Relaunch every stopped session in this project, attaching to none.

        Each comes back resuming its most recent conversation, so a forced
        restart costs the user one keypress. Project config is deliberately not
        applied here — it runs when the user actually attaches to one.
        """
        failures: list[str] = []
        for name, rec in sorted(self._stopped_records().items()):
            sessions = get_sessions_for_path(rec.path)
            resume_id = sessions[0].session_id if sessions else rec.claude_session_id
            try:
                create_session(
                    name,
                    rec.path,
                    system_prompt=(
                        None
                        if resume_id
                        else _build_system_prompt(
                            rec.session_type, rec.project, rec.path
                        )
                    ),
                    resume_session_id=resume_id,
                )
            except Exception as e:  # noqa: BLE001
                failures.append(f"{name}: {e}")
        if failures:
            await self._show_error("Could not restore:\n" + "\n".join(failures))
            return
        try:
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

    # -- Session actions submenu --

    async def _show_session_actions(
        self, session: SessionInfo, *, from_search: bool = False
    ) -> None:
        self._selected_session = session
        # Remembered so Cancel/Escape returns to the search results rather than
        # discarding a scan the user may still want to pick through.
        self._actions_from_search = from_search
        await self._clear_main()
        main = self.query_one("#main")

        items: list[ListItem] = []

        if session.session_type == "claude":
            items.append(ListItem(Label("Resume"), id="sa-resume"))
            items.append(ListItem(Label("View session log"), id="sa-viewlog"))
        else:
            has_previous = bool(get_sessions_for_path(session.path))
            if session.is_active:
                items.append(ListItem(Label("Connect"), id="sa-connect"))
                if has_previous:
                    items.append(
                        ListItem(
                            Label("Resume previous session"), id="sa-resume-picker"
                        )
                    )
            else:
                if has_previous:
                    items.append(
                        ListItem(
                            Label("Resume previous session"), id="sa-resume-picker"
                        )
                    )
                items.append(ListItem(Label("Launch"), id="sa-launch"))

            # Forking needs a conversation to fork and a git branch to base the
            # new worktree on, so it is offered for worktree/direct sessions
            # that have at least one previous Claude session.
            if has_previous and session.session_type in ("worktree", "direct"):
                items.insert(1, ListItem(Label("Fork session"), id="sa-fork"))

            # Reading a transcript back needs no live session, so it is offered
            # whenever the path has one.
            if has_previous:
                items.append(ListItem(Label("View session log"), id="sa-viewlog"))

        items.append(ListItem(Label("Open terminal"), id="sa-terminal"))
        items.append(ListItem(Label("Open in VS Code"), id="sa-vscode"))

        if session.session_type != "claude":
            items.append(ListItem(Label("Rename"), id="sa-rename"))

        # Stop keeps the session's record open (orange, resumable); terminate
        # forgets it. Two items rather than one item plus a prompt: a menu is
        # already a choice. Both land in one handler, as the tmux prompt does.
        if session.session_type != "claude":
            if session.is_active:
                items.append(ListItem(Label("Stop session"), id="sa-stop"))
            if session.is_active or session.is_stopped:
                items.append(ListItem(Label("Terminate session"), id="sa-terminate"))

        if session.session_type == "worktree":
            items.append(ListItem(Label("Finish (cleanup/merge)"), id="sa-finish"))

        items.append(ListItem(Label("[dim]Cancel[/]", markup=True), id="sa-cancel"))

        if session.session_type == "claude":
            type_label = "claude session"
            status_label = (
                _claude_state_label(session.claude_state).strip()
                if session.claude_state
                else "unknown"
            )
            # Strip markup tags for plain text info line
            status_label = status_label.replace("[dim]", "").replace("[/]", "")
        else:
            type_label = (
                session.project if session.session_type == "direct" else "worktree"
            )
            if session.is_active:
                status_label = "active"
            elif session.is_stopped:
                status_label = "stopped"
            else:
                status_label = "inactive"
        info_text = f"{type_label} | {status_label} | {BRANCH_ICON} {session.branch}"

        await main.mount(
            Container(
                Label(session.name, classes="form-label"),
                Static(info_text, classes="session-info"),
                ListView(*items, id="session-actions"),
                id="actions-panel",
            )
        )
        self.query_one("#session-actions").focus()

    # -- Resume session picker --

    def _launch_resume(self, session: SessionInfo, cs: ClaudeSession) -> None:
        tmux_name, session_type = self._resume_target(session.project, cs.cwd, session)
        self._launch_target = LaunchTarget(
            session.project,
            cs.cwd,  # authoritative original directory from the session log
            tmux_name,
            session_type,
            cs.session_id,
        )
        self.exit()

    async def _show_resume_session_picker(self, session: SessionInfo) -> None:
        self._selected_session = session
        sessions = get_sessions_for_path(session.path)
        self._resume_sessions = sessions

        if len(sessions) == 1:
            self._launch_resume(session, sessions[0])
            return

        await self._clear_main()
        main = self.query_one("#main")

        items = self._build_claude_session_items(sessions, "rp")

        await main.mount(
            Container(
                Label(session.name, classes="form-label"),
                Static("Resume previous session", classes="session-info"),
                ListView(*items, id="resume-picker"),
                id="actions-panel",
            )
        )
        self.query_one("#resume-picker").focus()

    def _build_claude_session_items(
        self, sessions: list[ClaudeSession], prefix: str
    ) -> list[ListItem]:
        """Build picker rows for a list of Claude sessions.

        Shared by the resume and fork pickers; `prefix` namespaces the widget
        ids (`rp-*` vs `fp-*`) so each picker's handler owns its own rows.
        """
        # Reserve space for list padding (2 chars each side) + container margin
        max_width = max(20, self.size.width - 8)

        if not sessions:
            return [
                ListItem(
                    Label("[dim]No previous sessions found[/]", markup=True),
                    id=f"{prefix}-empty",
                ),
                ListItem(Label("[dim]Cancel[/]", markup=True), id=f"{prefix}-cancel"),
            ]

        items: list[ListItem] = []
        for i, cs in enumerate(sessions):
            time_text = _relative_time(cs.last_activity)
            # Assembled, not markup: a session title is model-written text and
            # a stray `[` in it would otherwise be parsed as a console tag.
            meta = (
                Content.assemble(cs.title, "  ", (time_text, "dim"))
                if cs.title
                else Content.assemble((time_text, "dim"))
            )
            if cs.first_prompt:
                prompt_lines = _format_prompt_lines(cs.first_prompt, max_width)
                item = ListItem(
                    *[Label(Content(ln)) for ln in prompt_lines],
                    Label(meta),
                    id=f"{prefix}-{i}",
                )
            else:
                item = ListItem(Label(meta), id=f"{prefix}-{i}")
            items.append(item)
        items.append(
            ListItem(Label("[dim]Cancel[/]", markup=True), id=f"{prefix}-cancel")
        )
        return items

    # -- Session log viewer --

    async def _show_log_picker(self, session: SessionInfo) -> None:
        """Choose which transcript to read, or open it straight away.

        A `claude` row already names one transcript, and a path with a single
        transcript needs no choice — both skip the picker.
        """
        self._selected_session = session
        sessions = get_sessions_for_path(session.path)

        if session.session_type == "claude":
            match = next(
                (c for c in sessions if c.session_id == session.claude_session_id),
                None,
            )
            if match is None:
                await self._show_error("Session log not found.")
                return
            await self._show_session_log(match)
            return

        self._log_sessions = sessions

        if len(sessions) == 1:
            await self._show_session_log(sessions[0])
            return

        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label(session.name, classes="form-label"),
                Static("View which session log?", classes="session-info"),
                ListView(
                    *self._build_claude_session_items(sessions, "lp"), id="log-picker"
                ),
                id="actions-panel",
            )
        )
        self.query_one("#log-picker").focus()

    async def _show_session_log(self, cs: ClaudeSession) -> None:
        """Open a Claude transcript read-only, without launching Claude.

        A log the parser cannot structure falls back to its raw lines with a
        banner saying so, rather than an error screen: Claude's entry shapes
        evolve, and a transcript you can still read and search beats one you
        cannot open at all. A file that cannot be *read* is a real error —
        there is nothing to fall back to.
        """
        messages: list[TranscriptMessage] = []
        raw_lines: list[str] = []
        parse_error: str | None = None
        try:
            messages = read_transcript(cs.jsonl_path)
        except ClaudeLogError as e:
            await self._show_error(str(e))
            return
        except Exception as e:
            # The traceback is the report: a shape the parser doesn't know is a
            # bug to fix, not just a view to degrade.
            debug.log_exception("claude.transcript_unparsed", e)
            debug.log(
                "tui.log_raw_fallback",
                log=debug.rp(cs.jsonl_path),
                session=debug.rid(cs.session_id),
                error=type(e).__name__,
            )
            try:
                raw_lines = read_raw_transcript(cs.jsonl_path)
            except ClaudeLogError as read_error:
                await self._show_error(str(read_error))
                return
            parse_error = f"{type(e).__name__}: {e}"

        # A fresh transcript starts unsearched; the regex/case choices persist,
        # like the transcript-search toggles do.
        self._log_session = cs
        self._log_messages = messages
        self._log_raw_lines = raw_lines
        self._log_parse_error = parse_error
        self._log_searching = False
        self._log_query = ""
        self._log_error = None
        await self._render_log_view()

    async def _render_log_view(self) -> None:
        """Mount the log viewer for `_log_session`, unhighlighted.

        This runs once per transcript. On a real log building these widgets
        costs about half a second, which is why searching never comes back
        here: `_apply_log_search` updates the bodies that are already mounted.

        Deliberately no `.hint` row, unlike the other views: everything one
        would say is already in the app footer, and a grey strip under a full
        screen of transcript reads as a second footer.
        """
        cs = self._log_session
        if cs is None:  # pragma: no cover - only reachable via _show_session_log
            return
        messages = self._log_messages

        await self._clear_main()
        self._on_log = True
        main = self.query_one("#main")

        # Widget, not Static: `_render_transcript` mixes in Collapsibles.
        body: list[Widget]
        if self._log_parse_error is not None:
            # Raw fallback: one row per line, still built through `_log_body`
            # so `/` highlighting and `n`/`N` work exactly as they do above.
            self._log_bodies = [_log_body(line) for line in self._log_raw_lines]
            body = [b.widget for b in self._log_bodies]
        elif messages:
            body, self._log_bodies = _render_transcript(messages)
        else:
            self._log_bodies = []
            body = [Static("[dim]This log has no messages.[/]", markup=True)]

        search = Input(
            value=self._log_query,
            placeholder="Search this transcript...",
            id="log-search",
            # Textual selects an Input's whole value on focus by default, which
            # would eat the query the first time focus came back to the box.
            select_on_focus=False,
        )
        status = Static(self._log_status_text(), id="log-search-status", markup=True)
        # Mounted always, shown only once `/` arms them, so the header and the
        # message pane don't shuffle when the box appears.
        search.display = self._log_searching
        status.display = self._log_searching

        header = cs.title or cs.first_prompt or cs.session_id
        banner: list[Widget] = []
        if self._log_parse_error is not None:
            banner.append(
                Static(
                    Content.assemble(
                        ("⚠ Could not read this transcript's structure — ", "b"),
                        "showing the raw log. ",
                        (self._log_parse_error, "dim"),
                    ),
                    id="log-raw-warning",
                )
            )

        await main.mount(
            Container(
                # Content, not markup: a title or first prompt is arbitrary
                # transcript text and a stray `[` would be parsed as a tag.
                Label(Content(_first_line(header, 80)), classes="form-label"),
                Static(
                    f"{cs.session_id}  ·  {_relative_time(cs.last_activity)}",
                    classes="session-info",
                ),
                *banner,
                search,
                status,
                # No hint row: the app footer already carries `/` and Escape,
                # and a second strip of grey text under a wall of transcript
                # reads as a second footer. Expanding a tool row is a Tab and
                # an Enter, which the fold's own ▶ marker gives away.
                VerticalScroll(*body, id="log-messages"),
                id="log-panel",
            )
        )

        self._log_spans = []
        self._log_matches = []
        self._log_match_index = -1
        self.refresh_bindings()
        self.query_one("#log-messages", VerticalScroll).focus()
        debug.log(
            "tui.log_view",
            session=debug.rid(cs.session_id),
            log=debug.rp(cs.jsonl_path),
            messages=len(messages),
            widgets=len(body),
            bodies=len(self._log_bodies),
            raw="yes" if self._log_parse_error is not None else "no",
        )

    def _log_matcher(self) -> Matcher | None:
        """Compile the viewer's query, recording a bad regex rather than raising.

        `ContentMode` doesn't apply here: the transcript has already been parsed
        into messages, so there is no raw JSON left to scan.
        """
        self._log_error = None
        query = self._log_query.strip()
        if not query:
            return None
        try:
            return claude_search.compile_matcher(
                query,
                regex=self._log_regex,
                mode=ContentMode.RAW,
                case_sensitive=self._log_case_sensitive,
            )
        except SearchError as e:
            self._log_error = str(e)
            return None

    def _start_log_search(self) -> None:
        """Scan the open transcript for the current query, off the event loop.

        Only the *matching* is threaded — applying the result touches mounted
        widgets and so has to happen on the event loop. That split is what keeps
        the search box from stuttering: nothing is unmounted, and the scan of a
        large transcript never blocks a keystroke.

        An empty or malformed query still runs a scan, with a `None` matcher: it
        is what clears the previous query's highlights.
        """
        self._log_search_token += 1
        self.workers.cancel_group(self, "log-search")
        matcher = self._log_matcher()
        debug.log(
            "log_search.start",
            query=debug.rv(self._log_query),
            chars=len(self._log_query),
            regex=self._log_regex,
            case_sensitive=self._log_case_sensitive,
            bodies=len(self._log_bodies),
            token=self._log_search_token,
            error=self._log_error or "none",
        )
        self._run_log_search(
            [body.text for body in self._log_bodies], matcher, self._log_search_token
        )

    @work(thread=True, exclusive=True, group="log-search")
    def _run_log_search(
        self, texts: list[str], matcher: Matcher | None, token: int
    ) -> None:
        spans = [_match_spans(text, matcher) for text in texts]
        self.call_from_thread(self._apply_log_search, token, spans)

    def _apply_log_search(self, token: int, spans: list[Spans]) -> None:
        """Repaint the bodies for a completed scan, if it is still the current one.

        As with the transcript search, the token — not worker cancellation — is
        the correctness mechanism: a result can already be queued on the event
        loop when the query changes again.
        """
        if token != self._log_search_token or not self._on_log:
            debug.log(
                "log_search.dropped",
                token=token,
                current=self._log_search_token,
                on_log=self._on_log,
            )
            return
        if len(spans) != len(self._log_bodies):  # pragma: no cover - guarded by token
            debug.log(
                "log_search.dropped",
                token=token,
                reason="body-count-changed",
                spans=len(spans),
                bodies=len(self._log_bodies),
            )
            return

        matches: list[Static] = []
        # Every mounted body is *checked*, but only the ones whose matches
        # actually moved are repainted: `Static.update` forces a layout refresh,
        # and a transcript has hundreds of bodies that a given query never
        # touches. Bodies start unhighlighted, so an absent previous entry is ().
        previous = self._log_spans
        # A fold opens if *any* body inside it matched, so the decision has to
        # be accumulated across bodies before it is applied — a run holding one
        # hit and nine misses would otherwise be closed again by the misses.
        # Keyed by id() because a widget's equality is not identity.
        folds: dict[int, tuple[Collapsible, bool]] = {}
        for index, (body, body_spans) in enumerate(zip(self._log_bodies, spans)):
            hit = bool(body_spans)
            was = previous[index] if index < len(previous) else ()
            if body_spans != was:
                body.widget.update(_highlight(body.text, body_spans))
                body.widget.set_class(hit, "log-match")
            if hit:
                matches.append(body.widget)
            for fold in body.folds:
                known = folds.get(id(fold))
                folds[id(fold)] = (fold, hit or (known is not None and known[1]))

        for fold, hit in folds.values():
            # A match opens its way out; everything else folds back up, so a new
            # query starts from the same view every time. Assigned only on a
            # change, since each one costs a refresh.
            if fold.collapsed is hit:
                fold.collapsed = not hit

        # Guarded: these tallies walk every body, and the whole point of the
        # in-place update is not to do per-body work on a keystroke.
        if debug.is_enabled():
            debug.log(
                "log_search.done",
                query=debug.rv(self._log_query),
                token=token,
                bodies=len(self._log_bodies),
                matching_bodies=len(matches),
                matches=sum(len(body_spans) for body_spans in spans),
                repainted=sum(
                    1
                    for index, body_spans in enumerate(spans)
                    if body_spans != (previous[index] if index < len(previous) else ())
                ),
                folds_open=sum(1 for _, hit in folds.values() if hit),
            )
        self._log_spans = spans
        self._log_matches = matches
        self._log_match_index = -1
        self.refresh_bindings()
        if matches:
            self._step_log_match(1)
        else:
            self._set_log_status()

    async def action_log_search(self) -> None:
        """Search the transcript on screen (bound to `/` in the log viewer)."""
        if not self._on_log:
            return
        await self._arm_log_search()

    async def _arm_log_search(self) -> None:
        """Reveal and focus the viewer's query box."""
        if not self.query("#log-search"):
            return  # pragma: no cover - the box is mounted with the viewer
        self._log_searching = True
        search = self.query_one("#log-search", Input)
        search.display = True
        self.query_one("#log-search-status", Static).display = True
        search.focus()

    async def _clear_log_search(self) -> None:
        """Drop the query, hide the box and clear the highlights."""
        self._log_searching = False
        self._log_query = ""
        if self.query("#log-search"):
            box = self.query_one("#log-search", Input)
            box.value = ""
            box.display = False
            self.query_one("#log-search-status", Static).display = False
        if self._log_debounce is not None:
            self._log_debounce.stop()
            self._log_debounce = None
        self._start_log_search()
        if self.query("#log-messages"):
            self.query_one("#log-messages", VerticalScroll).focus()

    @on(Input.Changed, "#log-search")
    def on_log_search_changed(self, event: Input.Changed) -> None:
        if event.value == self._log_query:
            return
        self._log_query = event.value
        if self._log_debounce is not None:
            self._log_debounce.stop()
        self._log_debounce = self.set_timer(
            self.SEARCH_DEBOUNCE, self._debounced_log_search
        )

    def _debounced_log_search(self) -> None:
        self._log_debounce = None
        self._start_log_search()

    @on(Input.Submitted, "#log-search")
    def on_log_search_submitted(self, event: Input.Submitted) -> None:
        """Hand focus to the messages, where `n`/`N` and the arrows work."""
        if self.query("#log-messages"):
            self.query_one("#log-messages", VerticalScroll).focus()

    def _log_status_text(self) -> str:
        """Mode line above the transcript, plus where `n`/`N` currently sits."""
        pattern = "regex" if self._log_regex else "literal"
        case = "match case" if self._log_case_sensitive else "ignore case"
        mode = f"[b]{pattern}[/] · [b]{case}[/]"
        if self._log_error is not None:
            return f"{mode}  [red]{escape(self._log_error)}[/]"
        if not self._log_query.strip():
            return mode
        total = len(self._log_matches)
        if not total:
            return f"{mode}  [dim]no matches[/]"
        position = self._log_match_index + 1 if self._log_match_index >= 0 else 1
        plural = "s" if total != 1 else ""
        return f"{mode}  [dim]{position} of {total} matching message{plural}[/]"

    def _set_log_status(self) -> None:
        if self.query("#log-search-status"):
            self.query_one("#log-search-status", Static).update(self._log_status_text())

    async def action_log_next_match(self) -> None:
        """Scroll to the next `/` match in the log viewer (bound to `n`)."""
        self._step_log_match(1)

    async def action_log_prev_match(self) -> None:
        """Scroll to the previous `/` match (bound to `N`)."""
        self._step_log_match(-1)

    def _step_log_match(self, step: int) -> None:
        """Move the current-match marker by `step`, wrapping at either end.

        Starting from -1 means the first `n` lands on the first match and the
        first `N` on the last, which is what a fresh search wants either way.
        """
        if not self._on_log or not self._log_matches:
            return
        if not self.query("#log-messages"):
            return  # pragma: no cover - the pane outlives the match list
        if 0 <= self._log_match_index < len(self._log_matches):
            self._log_matches[self._log_match_index].remove_class("log-match-current")
        self._log_match_index = (self._log_match_index + step) % len(self._log_matches)
        current = self._log_matches[self._log_match_index]
        current.add_class("log-match-current")
        self.query_one("#log-messages", VerticalScroll).scroll_to_widget(
            current, top=True
        )
        self._set_log_status()

    @on(ListView.Selected, "#log-picker")
    async def on_log_picker_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id is None:  # pragma: no cover - ListItems always carry ids here
            return
        if item_id in ("lp-cancel", "lp-empty"):
            session = self._selected_session
            if session is None:  # pragma: no cover
                return
            # Preserve where the actions menu came from, so backing out of it
            # still returns to the search results rather than the home screen.
            await self._show_session_actions(
                session, from_search=self._actions_from_search
            )
            return
        await self._show_session_log(self._log_sessions[int(item_id.split("-", 1)[1])])

    # -- Fork flow --

    async def _open_pending_fork(self) -> None:
        """Enter the fork flow for a session that requested it via `Ctrl-A f`.

        The requesting session has just detached but is still alive, so it is
        listed on the home screen and present in `_session_map`; match it by
        path. If it can't be found we simply stay on the home screen rather
        than guessing.
        """
        target = self._pending_fork
        self._pending_fork = None
        if target is None:
            return
        resolved = target.resolve()
        for session in self._session_map.values():
            if session.path.resolve() != resolved:
                continue
            if not get_sessions_for_path(session.path):
                await self._show_error(
                    f"No Claude session found in {session.path} to fork."
                )
                return
            await self._show_fork_title_form(session)
            return

    async def _show_fork_title_form(self, session: SessionInfo) -> None:
        """Ask for the forked worktree's name."""
        self._selected_session = session
        self._forking = True
        self._fork_parent_path = session.path
        self._fork_source = None
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label("Fork Session", classes="form-label"),
                Static(
                    f"forking {session.name} ({BRANCH_ICON} {session.branch})",
                    classes="session-info",
                ),
                Label("Title:"),
                Input(placeholder="e.g. try-alternative", id="fork-title-input"),
                Static("[dim]Press Enter to continue[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        self.query_one("#fork-title-input").focus()

    async def _show_fork_branch_select(self) -> None:
        """Pick the base branch for the fork, defaulting to the parent's."""
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        await self._clear_main()
        main = self.query_one("#main")

        meta = read_session_meta(session.path)
        parent_base = meta.get("base_branch") or self._default_branch

        items = [
            ListItem(
                Label(f"Parent branch ({session.branch})"),
                id="fork-branch-parent",
            ),
            ListItem(
                Label(f"Parent's base ({parent_base})"),
                id="fork-branch-base",
            ),
            ListItem(Label("Another branch…"), id="fork-branch-other"),
        ]

        await main.mount(
            Container(
                Label("Select Base Branch", classes="form-label"),
                Static(
                    "the fork inherits this branch's commits",
                    classes="session-info",
                ),
                ListView(*items, id="fork-branch-list"),
                id="create-panel",
            )
        )
        self.query_one("#fork-branch-list").focus()

    async def _choose_fork_source(self) -> None:
        """Pick which conversation to fork, then create the worktree.

        A directory accumulates one Claude session per `claude` invocation, so
        there can be several candidates; with exactly one there is nothing to
        ask, matching the resume flow.
        """
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        sessions = get_sessions_for_path(session.path)
        self._fork_sessions = sessions

        if len(sessions) == 1:
            self._fork_source = sessions[0]
            await self._finalize_create()
            return

        await self._clear_main()
        main = self.query_one("#main")
        items = self._build_claude_session_items(sessions, "fp")
        await main.mount(
            Container(
                Label(session.name, classes="form-label"),
                Static("Fork which session?", classes="session-info"),
                ListView(*items, id="fork-picker"),
                id="actions-panel",
            )
        )
        self.query_one("#fork-picker").focus()

    # -- Rename flow --

    async def _show_rename(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        current_suffix = (
            session.tmux_session.split("/", 1)[1]
            if "/" in session.tmux_session
            else session.tmux_session
        )

        await main.mount(
            Container(
                Label(f"Rename: {session.name}", classes="form-label"),
                Static(""),
                Label("New name:"),
                Input(value=current_suffix, id="rename-input"),
                Static("[dim]Press Enter to rename[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        rename_input = self.query_one("#rename-input", Input)
        rename_input.focus()
        rename_input.cursor_position = len(rename_input.value)

    # -- Finish flow --

    async def _show_finish(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        branch = session.branch
        meta = read_session_meta(session.path)
        base = meta.get("base_branch", self._default_branch)

        # Check branch status
        try:
            unpushed = get_unpushed_commits(branch, cwd=self._project_cwd)
            merged = is_branch_merged(branch, base, cwd=self._project_cwd)
            has_remote = has_remote_branch(branch, cwd=self._project_cwd)
        except GitError:
            unpushed = []
            merged = False
            has_remote = False

        items: list[ListItem] = []

        if merged:
            status_text = f"Branch {branch} has been merged into {base}."
            items.append(ListItem(Label("Delete worktree"), id="finish-delete"))
            if has_remote:
                items.append(
                    ListItem(
                        Label("Delete worktree + remote branch"),
                        id="finish-delete-remote",
                    )
                )
        else:
            commit_count = len(unpushed)
            if commit_count > 0 and not has_remote:
                status_text = (
                    f"Branch {branch} has {commit_count} commit(s) "
                    f"not pushed to any remote."
                )
            elif commit_count > 0:
                status_text = f"Branch {branch} has {commit_count} unpushed commit(s)."
            else:
                status_text = f"Branch {branch} is up to date with origin."

            items.append(ListItem(Label("Push & Create PR"), id="finish-pr"))
            items.append(
                ListItem(
                    Label(f"Cherry-pick to {base}"),
                    id="finish-cherry-pick",
                )
            )
            items.append(ListItem(Label("Discard & Delete"), id="finish-discard"))

        items.append(ListItem(Label("[dim]Cancel[/]", markup=True), id="finish-cancel"))

        await main.mount(
            Container(
                Label(f"Finish: {session.name}", classes="form-label"),
                Static(status_text, classes="branch-status"),
                ListView(*items, id="finish-list"),
                id="finish-panel",
            )
        )
        self.query_one("#finish-list").focus()

    # -- Open terminal sub-menu --

    async def _show_terminal_mode(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        items = [
            ListItem(Label("This window (drop into shell)"), id="term-this"),
            ListItem(Label("New window"), id="term-window"),
            ListItem(Label("[dim]Cancel[/]", markup=True), id="term-cancel"),
        ]

        await main.mount(
            Container(
                Label(f"Open terminal: {session.name}", classes="form-label"),
                Static(str(session.path), classes="session-info"),
                ListView(*items, id="terminal-mode-list"),
                id="terminal-mode-panel",
            )
        )
        self.query_one("#terminal-mode-list").focus()

    @on(ListView.Selected, "#terminal-mode-list")
    async def on_terminal_mode_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        action = event.item.id

        if action == "term-window":
            try:
                open_terminal(session.path)
            except OSError as e:
                await self._show_error(str(e))
                return
            await self._show_session_actions(session)
        elif action == "term-this":
            shell = os.environ.get("SHELL", "/bin/sh")
            try:
                with self.suspend():
                    subprocess.run([shell], cwd=session.path, check=False)
            except OSError as e:
                await self._show_error(f"Failed to launch shell {shell!r}: {e}")
                return
            await self._show_session_actions(session)
        elif action == "term-cancel":
            await self._show_session_actions(session)

    # -- Confirmation dialog --

    async def _show_confirm_discard(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        branch = session.branch
        try:
            unpushed = get_unpushed_commits(branch, cwd=self._project_cwd)
        except GitError:
            unpushed = []

        if unpushed:
            warning = (
                f"{len(unpushed)} commit(s) will be lost.\n"
                "The branch will be deleted and cannot be recovered."
            )
        else:
            warning = "The worktree directory and branch will be removed."

        await main.mount(
            Container(
                Label(
                    f"Delete worktree {session.name}?",
                    classes="form-label",
                ),
                Static(warning, classes="warning-text"),
                ListView(
                    ListItem(Label("Delete"), id="confirm-yes"),
                    ListItem(Label("Cancel"), id="confirm-no"),
                    id="confirm-list",
                ),
                id="confirm-panel",
            )
        )
        self.query_one("#confirm-list").focus()

    # -- Create worktree flow --

    async def _show_create_form(self) -> None:
        # Plain create: make sure no leftover fork state turns this into a fork.
        self._forking = False
        self._fork_source = None
        self._fork_parent_path = None
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label("Create New Worktree", classes="form-label"),
                Static(""),
                Label("Title:"),
                Input(placeholder="e.g. fix-unit-tests", id="title-input"),
                Static("[dim]Press Enter to continue[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        self.query_one("#title-input").focus()

    async def _show_branch_select(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        items: list[ListItem] = [
            ListItem(
                Label(f"Default branch ({self._default_branch}) — fetch & rebase"),
                id="branch-default",
            ),
            ListItem(
                Label(f"Current branch ({self._current_branch})"),
                id="branch-current",
            ),
        ]

        items.append(
            ListItem(
                Label("Another branch…"),
                id="branch-other",
            ),
        )

        await main.mount(
            Container(
                Label("Select Base Branch", classes="form-label"),
                ListView(*items, id="branch-list"),
                id="create-panel",
            )
        )
        self.query_one("#branch-list").focus()

    async def _show_branch_picker(self) -> None:
        """Show a filterable list of all local branches."""
        await self._clear_main()
        main = self.query_one("#main")

        try:
            branches = list_branches(cwd=self._project_cwd)
            if not self._forking:
                # Worktree branches are hidden as base-branch candidates for a
                # plain create, but they are exactly what a fork wants.
                branches = [b for b in branches if not b.startswith("worktree/")]
        except GitError:
            branches = []

        self._branch_picker_names: dict[str, str] = {}
        items: list[ListItem] = []
        for i, branch in enumerate(branches):
            item_id = f"bp-{i}"
            self._branch_picker_names[item_id] = branch
            items.append(ListItem(Label(branch), id=item_id))

        await main.mount(
            Container(
                Label("Select Branch", classes="form-label"),
                Input(placeholder="Type to filter...", id="branch-filter"),
                ListView(*items, id="branch-picker-list"),
                id="create-panel",
            )
        )
        self.query_one("#branch-filter").focus()

    async def _finalize_create(self) -> None:
        try:
            self._worktree_path = build_worktree_path(
                self._project_name, self._title_value, self._project_root
            )
        except ConfigError as e:
            await self._show_error(str(e))
            return

        if self._worktree_path.exists():
            await self._show_conflict()
            return

        await self._do_create_and_launch()

    async def _show_conflict(self) -> None:
        assert self._worktree_path is not None
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label(
                    f"Worktree already exists: {self._worktree_path.name}",
                    classes="form-label",
                ),
                Static(""),
                ListView(
                    ListItem(
                        Label("Connect to existing worktree"), id="conflict-connect"
                    ),
                    ListItem(
                        Label("Create new with numeric suffix"), id="conflict-suffix"
                    ),
                    id="conflict-list",
                ),
                id="conflict-panel",
            )
        )
        self.query_one("#conflict-list").focus()

    async def _do_create_and_launch(self) -> None:
        """Create the worktree and hand off to main() to launch it.

        Shared by the plain create flow and the fork flow — a non-None
        `_fork_source` is the only difference.
        """
        assert self._worktree_path is not None
        assert self._project_root is not None
        fork_id = self._fork_source.session_id if self._fork_source else None
        new_branch = f"worktree/{self._worktree_path.name}"
        try:
            create_worktree(
                self._worktree_path,
                self._start_point or self._base_branch,
                new_branch,
                cwd=self._project_cwd,
            )
            store_session_meta(
                self._worktree_path,
                self._base_branch,
                source_root=self._project_root,
                forked_from_session_id=fork_id,
                forked_from_worktree=self._fork_parent_path if fork_id else None,
            )
        except GitError as e:
            await self._show_error(str(e))
            return
        # Project config (copy/link/init) is applied in main() before launch,
        # uniformly across all connection modes.
        self._launch_target = LaunchTarget(
            self._project_name,
            self._worktree_path,
            None,
            "worktree",
            forked_from_session_id=fork_id,
            forked_from_worktree=self._fork_parent_path if fork_id else None,
        )
        self.exit()

    # -- Project switcher --

    def _build_project_items(self, filter_text: str = "") -> list[ListItem]:
        self._project_dir_paths = {}
        items: list[ListItem] = []
        query = filter_text.lower()
        for proj in self._available_projects:
            if query and query not in proj.name.lower():
                continue
            item_id = f"proj-{proj.name}"
            self._project_dir_paths[item_id] = proj
            if proj.name == self._project_name:
                label_text = f"{ICON_GREEN_CIRCLE} {proj.name}"
            else:
                label_text = f"   {proj.name}"
            items.append(ListItem(Label(label_text), id=item_id))
        return items

    async def _show_project_select(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        items = self._build_project_items()

        await main.mount(
            Container(
                Label("Switch Project", classes="form-label"),
                Input(placeholder="Type to filter...", id="project-filter"),
                ListView(*items, id="project-list"),
                id="project-panel",
            )
        )
        self.query_one("#project-filter").focus()

    def _update_project_suggestion(self) -> None:
        """Set ghost text on the filter input based on the highlighted list item."""
        filter_input = self.query_one("#project-filter", Input)
        project_list = self.query_one("#project-list", ListView)
        typed = filter_input.value
        if len(project_list) > 0 and project_list.index is not None:
            item = project_list.children[project_list.index]
            if item.id and item.id in self._project_dir_paths:
                name = self._project_dir_paths[item.id].name
                if name.lower().startswith(typed.lower()):
                    filter_input._suggestion = typed + name[len(typed) :]
                    return
        filter_input._suggestion = ""

    # -- Event handlers --

    @on(Input.Changed, "#project-filter")
    async def on_project_filter_changed(self, event: Input.Changed) -> None:
        project_list = self.query_one("#project-list", ListView)
        await project_list.clear()
        for item in self._build_project_items(event.value):
            await project_list.append(item)
        if len(project_list) > 0:
            project_list.index = 0
        self._update_project_suggestion()

    @on(Input.Submitted, "#project-filter")
    async def on_project_filter_submitted(self, event: Input.Submitted) -> None:
        await self._select_highlighted_project()

    async def _select_highlighted_project(self) -> None:
        """Select whichever project is currently highlighted in the list."""
        project_list = self.query_one("#project-list", ListView)
        if len(project_list) == 0 or project_list.index is None:
            return
        item = project_list.children[project_list.index]
        item_id = item.id
        if item_id and item_id in self._project_dir_paths:
            self._project_cwd = self._project_dir_paths[item_id]
            self._searching = False
            self._search_query = ""
            try:
                self._init_git_info()
                await self._show_home()
            except (ConfigError, GitError) as e:
                await self._show_error(str(e))

    @on(ListView.Selected, "#project-list")
    async def on_project_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id and item_id in self._project_dir_paths:
            self._project_cwd = self._project_dir_paths[item_id]
            try:
                self._init_git_info()
                await self._show_home()
            except (ConfigError, GitError) as e:
                await self._show_error(str(e))

    async def _on_key(self, event: events.Key) -> None:
        """Handle arrow keys and tab for filter autocomplete."""
        if not self.focused:
            return

        if self.focused.id == "log-search":
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                await self._clear_log_search()
            return

        if self.focused.id == "home-search":
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                await self._clear_search()
                return
            if event.key in ("down", "up") and self.query("#home-list"):
                event.prevent_default()
                event.stop()
                home_list = self.query_one("#home-list", ListView)
                if len(home_list) == 0:
                    return
                step = 1 if event.key == "down" else -1
                idx = home_list.index or 0
                # Skip separator rows so the highlight always lands on a session.
                candidate = idx + step
                while 0 <= candidate < len(home_list):
                    if not home_list.children[candidate].disabled:
                        home_list.index = candidate
                        return
                    candidate += step
            return

        if self.focused.id == "branch-filter":
            branch_list_nodes = self.query("#branch-picker-list")
            if not branch_list_nodes:
                return
            branch_list = self.query_one("#branch-picker-list", ListView)

            if event.key in ("down", "up"):
                event.prevent_default()
                event.stop()
                if len(branch_list) == 0:
                    return
                idx = branch_list.index or 0
                if event.key == "down":
                    idx = min(idx + 1, len(branch_list) - 1)
                else:
                    idx = max(idx - 1, 0)
                branch_list.index = idx
            return

        if self.focused.id != "project-filter":
            return

        project_list_nodes = self.query("#project-list")
        if not project_list_nodes:
            return
        project_list = self.query_one("#project-list", ListView)

        if event.key in ("down", "up"):
            event.prevent_default()
            event.stop()
            if len(project_list) == 0:
                return
            idx = project_list.index or 0
            if event.key == "down":
                idx = min(idx + 1, len(project_list) - 1)
            else:
                idx = max(idx - 1, 0)
            project_list.index = idx
            self._update_project_suggestion()

        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            filter_input = self.query_one("#project-filter", Input)
            if filter_input._suggestion:
                filter_input.value = filter_input._suggestion
                filter_input.cursor_position = len(filter_input.value)
                filter_input._suggestion = ""

    @on(ListView.Selected, "#home-list")
    async def on_home_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        debug.log("tui.selected", list="home", id=debug.rid(item_id))
        if item_id == "action-restore":
            await self._restore_stopped_sessions()
        elif item_id == "action-create":
            await self._show_create_form()
        elif item_id == "action-direct":
            await self._launch_direct_session()
        elif item_id == "action-adhoc":
            self._launch_adhoc_session()
        elif item_id == "action-switch-project":
            await self._show_project_select()
        elif item_id == "action-toggle-quick-terminal":
            await self._toggle_quick_terminal()
        elif item_id and item_id in self._session_map:
            await self._show_session_actions(self._session_map[item_id])

    async def _toggle_quick_terminal(self) -> None:
        if not quick_terminal_key():
            return
        current = load_settings().quick_terminal_enabled is True
        new_value = not current
        save_settings(Settings(quick_terminal_enabled=new_value))
        if new_value:
            enable_quick_terminal_binding()
        else:
            disable_quick_terminal_binding()
        await self._show_home()

    def _launch_adhoc_session(self) -> None:
        all_sessions = set(list_all_sessions())
        tmux_name = get_next_adhoc_session_name(all_sessions)
        adhoc_dir = Path(tempfile.mkdtemp(prefix="fujimoto-adhoc-"))
        self._launch_target = LaunchTarget(
            "adhoc",
            adhoc_dir,
            tmux_name,
            "adhoc",
            None,
        )
        self.exit()

    async def _launch_direct_session(self) -> None:
        await self._show_direct_title_form()

    async def _show_direct_title_form(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        default_name = get_next_direct_session_name(
            self._project_name, self._active_sessions
        ).split("/", 1)[1]
        await main.mount(
            Container(
                Label(
                    f"New Session in {self._project_name}",
                    classes="form-label",
                ),
                Static(""),
                Label("Session name:"),
                Input(value=default_name, id="direct-title-input"),
                Static("[dim]Press Enter to launch[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        title_input = self.query_one("#direct-title-input", Input)
        title_input.focus()
        title_input.cursor_position = len(title_input.value)

    @on(ListView.Selected, "#session-actions")
    async def on_session_action_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        action = event.item.id

        if action == "sa-connect":
            self._launch_target = LaunchTarget(
                session.project,
                session.path,
                session.tmux_session,
                session.session_type,
                None,
            )
            self.exit()
        elif action == "sa-launch":
            self._launch_target = LaunchTarget(
                session.project,
                session.path,
                session.tmux_session,
                session.session_type,
                None,
            )
            self.exit()
        elif action == "sa-resume":
            tmux_name, session_type = self._resume_target(
                session.project, session.path, session
            )
            self._launch_target = LaunchTarget(
                session.project,
                session.path,
                tmux_name,
                session_type,
                session.claude_session_id,
            )
            self.exit()
        elif action == "sa-resume-picker":
            await self._show_resume_session_picker(session)
        elif action == "sa-fork":
            await self._show_fork_title_form(session)
        elif action == "sa-viewlog":
            await self._show_log_picker(session)
        elif action in ("sa-stop", "sa-terminate"):
            await self._end_session(session, terminate=action == "sa-terminate")
        elif action == "sa-terminal":
            await self._show_terminal_mode(session)
        elif action == "sa-vscode":
            try:
                open_vscode(session.path)
            except OSError as e:
                await self._show_error(str(e))
        elif action == "sa-rename":
            await self._show_rename(session)
        elif action == "sa-finish":
            await self._show_finish(session)
        elif action == "sa-cancel":
            if self._actions_from_search:
                await self._show_session_search(restore=True)
                return
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    @on(ListView.Selected, "#finish-list")
    async def on_finish_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        action = event.item.id

        if action == "finish-pr":
            await self._do_push_and_pr(session)
        elif action == "finish-cherry-pick":
            await self._do_cherry_pick(session)
        elif action == "finish-discard":
            self._finish_action = "discard"
            await self._show_confirm_discard(session)
        elif action == "finish-delete":
            self._finish_action = "delete"
            await self._do_delete_worktree(session, remove_remote=False)
        elif action == "finish-delete-remote":
            self._finish_action = "delete-remote"
            await self._do_delete_worktree(session, remove_remote=True)
        elif action == "finish-cancel":
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    @on(ListView.Selected, "#resume-picker")
    async def on_resume_picker_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        item_id = event.item.id
        if item_id in ("rp-cancel", "rp-empty"):
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))
            return
        idx = int(item_id.split("-", 1)[1])
        cs = self._resume_sessions[idx]
        self._launch_resume(session, cs)

    @on(ListView.Selected, "#confirm-list")
    async def on_confirm_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover

        if event.item.id == "confirm-yes":
            await self._do_delete_worktree(session, remove_remote=False)
        else:
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    # -- Finish operations --

    async def _do_push_and_pr(self, session: SessionInfo) -> None:
        branch = session.branch
        try:
            push_branch(branch, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Push failed: {e}")
            return

        # Spin up a background Claude session to create the PR
        pr_session_name = f"{session.project}/pr-{session.name}"
        allowed = "Bash(git:*) Bash(gh:*)"
        command = (
            f'claude -p --allowedTools "{allowed}" '
            f'"Push this branch and create a PR. '
            f'Follow project conventions from CLAUDE.md."'
        )
        try:
            create_session_with_command(pr_session_name, session.path, command)
        except Exception as e:  # pragma: no cover
            await self._show_error(f"Failed to start PR session: {e}")
            return

        try:
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

    async def _do_cherry_pick(self, session: SessionInfo) -> None:
        branch = session.branch
        meta = read_session_meta(session.path)
        base = meta.get("base_branch", self._default_branch)

        try:
            cherry_pick_branch(branch, base, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Cherry-pick failed: {e}")
            return

        await self._do_delete_worktree(session, remove_remote=False)

    async def _do_delete_worktree(
        self, session: SessionInfo, remove_remote: bool
    ) -> None:
        # Removing the worktree ends the session for good, so forget it too —
        # otherwise it would come back as a stopped row pointing at nothing.
        if session.is_active:
            try:
                kill_session(session.tmux_session)
            except TmuxError:
                pass
        session_state.mark_closed(session.tmux_session)

        # Remove git worktree
        try:
            remove_worktree(session.path, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Worktree removal failed: {e}")
            return

        # Delete the branch
        try:
            delete_branch(session.branch, remote=remove_remote, cwd=self._project_cwd)
        except GitError:
            pass  # Branch may already be gone

        try:
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

    @on(ListView.Selected, "#tmux-install-list")
    async def on_tmux_install_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "install-tmux":
            try:
                install_tmux()
                self._init_git_info()
                await self._show_home()
            except (TmuxError, ConfigError, GitError) as e:
                await self._show_error(str(e))
        else:
            self.exit()

    @on(Input.Submitted, "#direct-title-input")
    async def on_direct_title_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        tmux_name = f"{self._project_name}/{slugify(value)}"
        project_path = self._project_cwd or Path(".")
        self._launch_target = LaunchTarget(
            self._project_name,
            project_path,
            tmux_name,
            "direct",
            None,
        )
        self.exit()

    @on(Input.Submitted, "#rename-input")
    async def on_rename_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        session = self._selected_session
        if not value or session is None:
            return
        new_tmux_name = f"{session.project}/{slugify(value)}"
        if new_tmux_name == session.tmux_session:
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))
            return
        try:
            rename_session(session.tmux_session, new_tmux_name)
            session_state.rename(session.tmux_session, new_tmux_name)
            self._init_git_info()
            await self._show_home()
        except TmuxError as e:
            await self._show_error(str(e))
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

    @on(Input.Submitted, "#title-input")
    async def on_title_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        self._title_value = value
        await self._show_branch_select()

    @on(Input.Submitted, "#fork-title-input")
    async def on_fork_title_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        self._title_value = value
        await self._show_fork_branch_select()

    @on(ListView.Selected, "#fork-branch-list")
    async def on_fork_branch_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        self._start_point = ""
        if event.item.id == "fork-branch-parent":
            self._base_branch = session.branch
            await self._choose_fork_source()
        elif event.item.id == "fork-branch-base":
            meta = read_session_meta(session.path)
            self._base_branch = meta.get("base_branch") or self._default_branch
            await self._choose_fork_source()
        elif event.item.id == "fork-branch-other":
            await self._show_branch_picker()

    @on(ListView.Selected, "#fork-picker")
    async def on_fork_picker_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id in ("fp-cancel", "fp-empty"):
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))
            return
        idx = int(item_id.split("-", 1)[1])
        self._fork_source = self._fork_sessions[idx]
        await self._finalize_create()

    @on(ListView.Selected, "#branch-list")
    async def on_branch_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "branch-current":
            self._base_branch = self._current_branch
            self._start_point = ""
            await self._finalize_create()
        elif event.item.id == "branch-default":
            self._base_branch = self._default_branch
            self._start_point = ""
            try:
                fetch_branch(self._default_branch, cwd=self._project_cwd)
                self._start_point = f"origin/{self._default_branch}"
            except GitError:
                pass  # Offline or no remote — proceed with local state
            await self._finalize_create()
        elif event.item.id == "branch-other":
            await self._show_branch_picker()

    @on(Input.Changed, "#branch-filter")
    async def on_branch_filter_changed(self, event: Input.Changed) -> None:
        branch_list = self.query_one("#branch-picker-list", ListView)
        await branch_list.clear()
        query = event.value.lower()
        for item_id, name in self._branch_picker_names.items():
            if query and query not in name.lower():
                continue
            await branch_list.append(ListItem(Label(name), id=item_id))
        if len(branch_list) > 0:
            branch_list.index = 0

    @on(Input.Submitted, "#branch-filter")
    async def on_branch_filter_submitted(self, event: Input.Submitted) -> None:
        await self._select_highlighted_branch()

    async def _after_base_branch_chosen(self) -> None:
        """Continue once `_base_branch` is set.

        A fork still has to pick which conversation it inherits; a plain create
        goes straight to making the worktree.
        """
        if self._forking:
            await self._choose_fork_source()
        else:
            await self._finalize_create()

    async def _select_highlighted_branch(self) -> None:
        branch_list = self.query_one("#branch-picker-list", ListView)
        if len(branch_list) == 0 or branch_list.index is None:
            return
        item = branch_list.children[branch_list.index]
        if item.id and item.id in self._branch_picker_names:
            self._base_branch = self._branch_picker_names[item.id]
            await self._after_base_branch_chosen()

    @on(ListView.Selected, "#branch-picker-list")
    async def on_branch_picker_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id and item_id in self._branch_picker_names:
            self._base_branch = self._branch_picker_names[item_id]
            await self._after_base_branch_chosen()

    @on(ListView.Selected, "#conflict-list")
    async def on_conflict_selected(self, event: ListView.Selected) -> None:
        assert self._worktree_path is not None
        if event.item.id == "conflict-connect":
            self._launch_target = LaunchTarget(
                self._project_name,
                self._worktree_path,
                None,
                "worktree",
                None,
            )
            self.exit()
        elif event.item.id == "conflict-suffix":
            suffix = 2
            while (
                self._worktree_path.parent / f"{self._worktree_path.name}-{suffix}"
            ).exists():
                suffix += 1
            self._worktree_path = (
                self._worktree_path.parent / f"{self._worktree_path.name}-{suffix}"
            )
            await self._do_create_and_launch()

    async def action_go_back(self) -> None:
        if len(self.query("#home-list")) > 0:
            # On the home screen escape quits — unless a filter is active, in
            # which case it drops the filter first.
            if self._searching or self._search_query:
                await self._clear_search()
                return
            self.exit()
        elif self._on_log and (self._log_searching or self._log_query):
            # In the log viewer escape drops the search before leaving the view.
            await self._clear_log_search()
        elif self._actions_from_search and not self._on_search:
            # Back out of a result's actions menu into the results it came from.
            await self._show_session_search(restore=True)
        else:
            # Leaving the search view keeps `_transcript_query`, so `s` reopens
            # on the same query (and rescans, since transcripts move on).
            try:
                await self._show_home()
            except (ConfigError, GitError):
                self.exit()


def _check_prerequisites() -> list[str]:
    """Validate environment before launching the TUI. Returns a list of issues."""
    issues: list[str] = []

    try:
        get_repo_root()
    except GitError:
        issues.append(
            "Not inside a git repository.\n"
            "Run fujimoto from within a git project directory."
        )

    return issues


def _session_branch(working_dir: Path) -> str:
    """Branch a session's directory is on, for display on a stopped row."""
    try:
        return get_current_branch(working_dir)
    except GitError:
        return ""


def _build_system_prompt(session_type: str, project: str, working_dir: Path) -> str:
    if session_type == "adhoc":
        return (
            "This is an ad hoc Claude session that is not in a git project. "
            "It is running in a temporary directory for quick questions, "
            "investigations, and one-off tasks. There is no git repository here."
        )
    if session_type == "worktree":
        meta = read_session_meta(working_dir)
        base_branch = meta.get("base_branch", "unknown") if meta else "unknown"
        return (
            f"You are working in a fujimoto worktree session for project '{project}'. "
            f"This is an isolated git worktree branched from '{base_branch}'. "
            "Focus your work on this worktree's branch."
        )
    return (
        f"You are working in a fujimoto direct session for project '{project}'. "
        "This is the project's main repository directory, not an isolated worktree. "
        "Be cautious with branch operations — other sessions may share this directory."
    )


def _build_fork_system_prompt(
    project: str,
    working_dir: Path,
    parent_worktree: Path | None,
    base_branch: str,
) -> str:
    """Context appended to a forked session so it knows it has moved.

    A forked conversation carries file paths and assumptions from the worktree
    it was originally running in. Without this it would keep editing the parent
    worktree's paths.
    """
    origin = (
        f"The conversation history above happened in a different git worktree: "
        f"{parent_worktree}. "
        if parent_worktree is not None
        else "The conversation history above happened in a different git worktree. "
    )
    return (
        "This session is a fork of an earlier Claude session, running in a "
        f"fujimoto worktree session for project '{project}'. "
        + origin
        + f"You are now in a NEW worktree at {working_dir}, on branch "
        f"'worktree/{working_dir.name}', branched from '{base_branch}'. "
        "File paths referenced earlier in the conversation point at the "
        "original worktree — work in this one instead. The fork was made from "
        "the parent's committed tip, so any uncommitted changes in the "
        "original worktree are not present here."
    )


DEFAULT_WINDOW_TITLE_TEMPLATE = "{git_project} - {worktree_name}"


class _DefaultFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _session_terminal_title(
    project: str, tmux_name: str | None, working_dir: Path, session_type: str
) -> str:
    """Build a terminal title string for a Claude session.

    The "<icon> fujimoto" prefix is always hard-coded. The suffix is rendered
    from the `FUJIMOTO_WINDOW_TITLE` env var (default: "{git_project} -
    {worktree_name}"). Unknown placeholders render as empty strings.
    """
    prefix = f"{ICON_WIZARD} fujimoto"
    template = os.environ.get("FUJIMOTO_WINDOW_TITLE", DEFAULT_WINDOW_TITLE_TEMPLATE)
    if not template.strip():
        return prefix

    try:
        branch = get_current_branch(working_dir)
    except GitError:
        branch = ""

    if session_type == "worktree":
        git_project_dir = str(working_dir.parent)
    elif session_type == "direct":
        git_project_dir = str(working_dir)
    else:
        git_project_dir = ""

    derived_tmux = tmux_name or (
        session_name(project, working_dir.name) if project else working_dir.name
    )

    vars = _DefaultFormatDict(
        git_project=project,
        worktree_name=working_dir.name,
        worktree_path=str(working_dir),
        git_project_dir=git_project_dir,
        branch=branch,
        session_type=session_type,
        tmux_name=derived_tmux,
    )

    try:
        suffix = template.format_map(vars).strip()
    except (ValueError, IndexError):
        suffix = ""

    if not suffix:
        return prefix
    return f"{prefix} - {suffix}"


def _session_manager_title(project: str) -> str:
    """Build the terminal title for the session-manager TUI.

    Uses the same "<icon> fujimoto - <project>" format as a Claude session's
    title, minus the worktree portion.
    """
    prefix = f"{ICON_WIZARD} fujimoto"
    if not project:
        return prefix
    return f"{prefix} - {project}"


def _pause_for_key(prompt: str) -> None:
    """Block until the user presses a key, so a message survives `tmux attach`.

    Reads a single keypress in raw mode on a tty; falls back to line input
    elsewhere and is a no-op when stdin is not a terminal (e.g. tests).
    """
    if not sys.stdin.isatty():  # pragma: no cover - exercised via patching
        return
    print(prompt, end="", flush=True)
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # pragma: no cover - non-POSIX / no termios
        sys.stdin.readline()
    print()


def _resolve_worktree_source(working_dir: Path) -> Path | None:
    """Return the main repo root if `working_dir` is a linked worktree, else None.

    Prefers the `source_root` recorded in session metadata, falling back to
    deriving it from git. Returns None for the main repo, direct/adhoc sessions,
    or anything that isn't a linked worktree — those get no project config.
    """
    recorded = read_session_meta(working_dir).get("source_root")
    try:
        main_root = get_main_worktree_root(working_dir)
    except GitError:
        return None
    if main_root == working_dir.resolve():
        return None  # main repo (direct session), not a linked worktree
    return Path(recorded) if recorded else main_root


def _apply_worktree_config(working_dir: Path) -> bool:
    """Apply project config before launching a worktree session.

    Runs `once` actions only on first creation (tracked by a marker), and
    `always` actions on every connection mode. Errors are shown and the user is
    prompted to acknowledge before the screen is taken over by `tmux attach`.

    Returns True to proceed with the launch, False to abort it (the caller then
    reopens the TUI). Non-worktree sessions always proceed.
    """
    source_root = _resolve_worktree_source(working_dir)
    debug.log(
        "launch.worktree_config",
        working_dir=debug.rp(working_dir),
        source_root=debug.rp(source_root) if source_root else "none",
    )
    if source_root is None:
        return True

    first_time = not config_once_applied(working_dir)
    trigger = Trigger.CREATE if first_time else Trigger.LAUNCH

    # The config is a local file in the main clone (not committed, so it isn't
    # present in worktree checkouts) — read it from the source root. A malformed
    # config is already surfaced on the home screen, so here we just skip it.
    try:
        config = load_project_config(source_root)
    except ConfigError:
        return True

    result = apply_project_config(
        config,
        source_root=source_root,
        worktree_root=working_dir,
        trigger=trigger,
    )
    for warning in result.warnings:
        print(f"fujimoto: {warning}", file=sys.stderr)

    if result.init_error:
        print(f"fujimoto: {result.init_error}", file=sys.stderr)
        abort = config.on_error is OnError.ABORT
        verb = "aborting launch" if abort else "continuing"
        _pause_for_key(f"An error was encountered ({verb}). Press any key...")
        if abort:
            return False
        if first_time:
            mark_config_once_applied(working_dir)
        return True

    if first_time:
        mark_config_once_applied(working_dir)
    return True


def _create_config() -> None:
    """Scaffold a `.fujimoto.yaml` at the repo root (used by --create-config)."""
    try:
        repo_root = get_repo_root()
        dest = write_config_template(repo_root)
    except (ConfigError, GitError) as e:
        print(f"fujimoto: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Created {dest}")


def _run_pane_command(action: str, session: str) -> None:
    """Dispatch in-session pane actions invoked via tmux key bindings."""
    debug.log("pane.command", action=action, session=debug.rv(session))
    path = get_session_path(session)
    if path is None:
        display_message(session, f"fujimoto: could not resolve session '{session}'")
        sys.exit(1)
    try:
        if action == "vscode":
            open_vscode(path)
        elif action == "terminal":
            open_terminal(path)
    except OSError as exc:
        debug.log_exception("pane.failed", exc)
        _finish_debug_log()
        display_message(session, f"fujimoto: {exc}")
        sys.exit(1)
    _finish_debug_log()


def _start_debug_log(*, redact: bool) -> None:
    """Open the diagnostic log and record the environment snapshot."""
    try:
        logger = debug.enable(redact=redact)
    except OSError as exc:
        print(f"fujimoto: could not open debug log: {exc}", file=sys.stderr)
        return
    print(f"fujimoto: debug log → {logger.path}")
    if redact:
        print("fujimoto: sensitive values are redacted in this log")
    debug.log_environment()
    debug.log_section("run")


def _finish_debug_log() -> None:
    """Close the diagnostic log and remind the user where it is."""
    path = debug.log_path()
    if path is None:
        return
    debug.log("shutdown")
    debug.disable()
    print(f"fujimoto: debug log written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="fujimoto", add_help=True)
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"fujimoto {get_version()}",
    )
    parser.add_argument(
        "--create-config",
        action="store_true",
        help="Write a commented .fujimoto.yaml template to the repo root and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Write a detailed diagnostic log to ~/.fujimoto/logs "
            "(override the directory with $FUJIMOTO_LOG_DIR)"
        ),
    )
    parser.add_argument(
        "--debug-redacted",
        action="store_true",
        help=(
            "Like --debug, but replace usernames, project/session/branch names "
            "and path components with shape-preserving redaction tokens"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    pane = sub.add_parser("pane", help="Per-session pane actions")
    pane.add_argument("action", choices=["vscode", "terminal"])
    pane.add_argument("--session", required=True)

    args = parser.parse_args()
    if args.debug or args.debug_redacted:
        _start_debug_log(redact=args.debug_redacted)
    if args.command == "pane":
        _run_pane_command(args.action, args.session)
        return
    if args.create_config:
        _create_config()
        return

    try:
        issues = _check_prerequisites()
        debug.log("prerequisites", issues=len(issues))
        if issues:
            for issue in issues:
                debug.log("prerequisite_issue", detail=debug.rv(issue))
            _finish_debug_log()
            print("fujimoto: configuration error\n", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}\n", file=sys.stderr)
            sys.exit(1)

        pending_fork: Path | None = None
        pending_close: LaunchTarget | None = None
        while True:
            set_terminal_title(f"{ICON_WIZARD} fujimoto")
            app = SessionApp(pending_fork=pending_fork, pending_close=pending_close)
            pending_fork = None
            pending_close = None
            app.run()

            if app._launch_target:
                target = app._launch_target
                project_name = target.project
                working_dir = target.working_dir
                tmux_name = target.tmux_name
                session_type = target.session_type
                fork_id = target.forked_from_session_id
                resume_id = fork_id or target.resume_session_id
                debug.log(
                    "launch.target",
                    project=debug.rv(project_name),
                    working_dir=debug.rp(working_dir),
                    tmux=debug.rv(tmux_name),
                    session_type=session_type,
                    resume=resume_id or "none",
                )
                if fork_id:
                    # A fork resumes the parent's conversation but in a new
                    # worktree, so it needs the prompt explaining the move.
                    meta = read_session_meta(working_dir)
                    system_prompt = _build_fork_system_prompt(
                        project_name,
                        working_dir,
                        target.forked_from_worktree,
                        meta.get("base_branch", "unknown"),
                    )
                elif resume_id:
                    system_prompt = None
                else:
                    system_prompt = _build_system_prompt(
                        session_type, project_name, working_dir
                    )
                set_terminal_title(
                    _session_terminal_title(
                        project_name, tmux_name, working_dir, session_type
                    )
                )
                if not _apply_worktree_config(working_dir):
                    debug.log("launch.aborted", reason="project-config")
                    continue  # setup failed and on_error=abort -> reopen the TUI
                resolved_name = tmux_name or session_name(
                    project_name, working_dir.name
                )
                # Record the session as open *before* attaching: if the host
                # goes down while it is attached, the record is what brings the
                # session back.
                session_state.mark_open(
                    resolved_name,
                    cwd=working_dir,
                    project=project_name,
                    session_type=session_type,
                    branch=_session_branch(working_dir),
                    claude_session_id=resume_id,
                )
                launch_claude_in_tmux(
                    project_name,
                    working_dir,
                    tmux_name,
                    system_prompt=system_prompt,
                    resume_session_id=resume_id,
                    fork_session=bool(fork_id),
                )
                # `Ctrl-A f` / `s` / `x` inside the session flag it and detach,
                # handing the work to the TUI (which can show pickers/prompts).
                action = take_pending_action(resolved_name)
                if action == PENDING_FORK:
                    pending_fork = working_dir
                elif action == PENDING_STOP:
                    # Stop needs no prompt: the record stays open, so the
                    # session reappears as a stopped row ready to resume.
                    try:
                        kill_session(resolved_name)
                    except TmuxError:  # pragma: no cover
                        pass
                    session_state.touch(resolved_name)
                elif action == PENDING_CLOSE:
                    pending_close = LaunchTarget(
                        project_name, working_dir, resolved_name, session_type
                    )
            else:
                debug.log("tui.exit", reason="quit")
                break
        set_terminal_title("")
        _finish_debug_log()
    except (ConfigError, GitError) as e:
        set_terminal_title("")
        debug.log_exception("fatal", e)
        _finish_debug_log()
        print(f"\nfujimoto: {e}", file=sys.stderr)
        sys.exit(1)
    except TmuxError as e:
        set_terminal_title("")
        debug.log_exception("fatal", e)
        _finish_debug_log()
        print(f"\nfujimoto: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        set_terminal_title("")
        debug.log("interrupted")
        _finish_debug_log()
        print("\nAborted.")
        sys.exit(130)
