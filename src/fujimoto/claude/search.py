"""Full-text search across Claude Code transcript logs.

The home screen's `/` filter matches session *names*; this module matches
session *contents* — the JSONL transcripts Claude Code writes under
`~/.claude/projects/`. That means touching every byte of every log, which is
far too slow to do synchronously between keystrokes, so the work is exposed as
a batched generator (`iter_hits`) that a caller can drive from a background
thread and render incrementally.

Three orthogonal axes control matching:

* **Pattern** — a literal substring (the query is `re.escape`d) or a regex.
  Both compile to a `re.Pattern`, so there is one scanning code path.
* **Case** — insensitive (the default) or sensitive, applied as the pattern's
  `re.IGNORECASE` flag so it costs nothing at scan time.
* **Content** (`ContentMode`) — `RAW` scans the transcript bytes as written,
  so tool inputs, tool output, file contents, paths and shell commands all
  match; `TEXT` parses each entry and scans only what the user and Claude
  actually said (string content and `text` blocks), which is quieter but
  cannot see anything a tool produced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fujimoto import debug
from fujimoto.claude.log_parser import (
    ClaudeLogError,
    ClaudeSession,
    parse_session,
    session_dirs_for_path,
)

# How many logs are scanned before results are handed back to the caller.
# Small enough that the first hits appear almost immediately, large enough that
# the cross-thread hand-off isn't the dominant cost.
DEFAULT_BATCH_SIZE = 10

# Per-session snippet budget. A session that mentions the query fifty times is
# not fifty times more interesting, and the row only has so many lines.
MAX_SNIPPETS = 3

# Characters of context kept either side of a match in a snippet.
SNIPPET_RADIUS = 60

_WHITESPACE_RUN = re.compile(r"\s+")


class SearchError(Exception):
    """A query that cannot be compiled — in practice, a malformed regex."""


class ContentMode(StrEnum):
    """Which part of a transcript the query is matched against."""

    RAW = "raw"
    TEXT = "text"

    @property
    def label(self) -> str:
        """Short human-readable name for the status line.

        >>> ContentMode.RAW.label
        'raw'
        >>> ContentMode.TEXT.label
        'message text'
        """
        return "raw" if self is ContentMode.RAW else "message text"

    def toggled(self) -> ContentMode:
        """The other mode, for a one-key toggle.

        >>> ContentMode.RAW.toggled()
        <ContentMode.TEXT: 'text'>
        >>> ContentMode.TEXT.toggled()
        <ContentMode.RAW: 'raw'>
        """
        return ContentMode.TEXT if self is ContentMode.RAW else ContentMode.RAW


@dataclass(frozen=True)
class Matcher:
    """A compiled query plus the content mode it should be applied to."""

    pattern: re.Pattern[str]
    mode: ContentMode

    def present_in(self, text: str) -> bool:
        """Whether the pattern occurs anywhere in `text`."""
        return self.pattern.search(text) is not None


@dataclass(frozen=True)
class Snippet:
    """A one-line window of context, plus where the matches sit inside it.

    `spans` are `(start, end)` half-open offsets into `text`, sorted and
    non-overlapping, so a renderer can highlight the matched substrings without
    re-running the pattern — which it could not do reliably anyway, since `text`
    has had its whitespace collapsed and may no longer match a query that spans
    a line break.
    """

    text: str
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SearchHit:
    """One transcript that matched, with enough context to render a row."""

    session: ClaudeSession
    match_count: int
    snippets: tuple[Snippet, ...]


def compile_matcher(
    query: str,
    *,
    regex: bool = False,
    mode: ContentMode = ContentMode.RAW,
    case_sensitive: bool = False,
) -> Matcher:
    """Compile `query` into a `Matcher`.

    A literal query is escaped so regex metacharacters in ordinary search
    terms (a path, `foo(bar)`) are taken at face value.

    >>> compile_matcher("a.c").pattern.search("abc") is None
    True
    >>> compile_matcher("a.c", regex=True).pattern.search("abc") is None
    False
    >>> compile_matcher("HELLO").pattern.search("hello") is None
    False

    Case sensitivity is off by default and applies to literal and regex
    queries alike:

    >>> compile_matcher("HELLO", case_sensitive=True).pattern.search("hello")
    >>> compile_matcher("h.llo", regex=True, case_sensitive=True).present_in("HELLO")
    False

    Raises SearchError for a regex that will not compile:

    >>> try:
    ...     compile_matcher("(unclosed", regex=True)
    ... except SearchError as e:
    ...     print(str(e).startswith("invalid regex:"))
    True
    """
    source = query if regex else re.escape(query)
    flags = re.NOFLAG if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(source, flags)
    except re.error as e:
        debug.log(
            "search.compile",
            query=debug.rv(query),
            regex=regex,
            mode=mode,
            case_sensitive=case_sensitive,
            error=str(e),
        )
        raise SearchError(f"invalid regex: {e}") from e
    debug.log(
        "search.compile",
        query=debug.rv(query),
        chars=len(query),
        regex=regex,
        mode=mode,
        case_sensitive=case_sensitive,
    )
    return Matcher(pattern=pattern, mode=mode)


def list_session_logs(
    project_root: Path | None,
    worktrees: Iterable[Path],
) -> list[Path]:
    """Transcript logs for a project root and its worktrees, newest first.

    Ordering is by file mtime descending, so a caller that renders results as
    they arrive shows the most recently touched sessions first — which is the
    order the home screen already implies.
    """
    targets: list[Path] = []
    if project_root is not None:
        targets.append(project_root)
    targets.extend(worktrees)

    mtimes: dict[Path, float] = {}
    for target in targets:
        for session_dir in session_dirs_for_path(target):
            for log in session_dir.glob("*.jsonl"):
                if log in mtimes:
                    continue
                try:
                    mtimes[log] = log.stat().st_mtime
                except OSError:  # pragma: no cover - vanished glob->stat
                    continue

    logs = sorted(mtimes, key=lambda p: mtimes[p], reverse=True)
    # "Search found nothing" is ambiguous without knowing whether there was
    # anything to search in the first place.
    debug.log_once(
        "search-logs",
        "search.logs",
        targets=len(targets),
        project_root=debug.rp(project_root) if project_root else "none",
        logs=len(logs),
    )
    return logs


def search_log(log_path: Path, matcher: Matcher) -> SearchHit | None:
    """Search a single transcript, returning None when it does not match.

    The whole file is read and rejected with one `re.search` before any JSON
    parsing happens; that fast path is what makes scanning hundreds of logs
    viable, since the overwhelming majority do not contain the query.
    """
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        debug.log(
            "search.log_unreadable",
            log=debug.rp(log_path),
            error=type(exc).__name__,
        )
        return None

    if not matcher.present_in(text):
        return None

    if matcher.mode is ContentMode.RAW:
        count, snippets = _scan_raw(text, matcher)
    else:
        count, snippets = _scan_message_text(text, matcher)

    # In TEXT mode the whole-file hit may have been in the JSON scaffolding
    # (a key name, a uuid, a cwd) rather than in anything anyone said.
    if not count:
        return None

    try:
        session = parse_session(log_path)
    except ClaudeLogError as exc:
        # The log matched but could not be parsed, so the hit is dropped —
        # invisible to the user, and worth seeing in a debug log.
        debug.log(
            "search.hit_discarded",
            log=debug.rp(log_path),
            matches=count,
            reason=str(exc),
        )
        return None

    return SearchHit(session=session, match_count=count, snippets=tuple(snippets))


def iter_hits(
    logs: list[Path],
    matcher: Matcher,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    is_cancelled: Callable[[], bool] | None = None,
) -> Iterator[tuple[int, tuple[SearchHit, ...]]]:
    """Scan `logs` in batches, yielding `(scanned_so_far, hits_in_batch)`.

    A batch is yielded every `batch_size` logs plus once at the end, so a
    caller gets partial results (and a progress count) long before the scan
    finishes. `is_cancelled` is polled before each log so a superseded search
    stops promptly; a cancelled scan yields nothing further.

    >>> list(iter_hits([], compile_matcher("x")))
    [(0, ())]
    """
    debug.log("search.scan", phase="start", logs=len(logs), mode=matcher.mode)
    if not logs:
        yield 0, ()
        return

    batch: list[SearchHit] = []
    hits = 0
    scanned = 0
    for log in logs:
        if is_cancelled is not None and is_cancelled():
            return
        hit = search_log(log, matcher)
        scanned += 1
        if hit is not None:
            batch.append(hit)
            hits += 1
        if scanned % batch_size == 0:
            yield scanned, tuple(batch)
            batch = []

    if scanned % batch_size:
        yield scanned, tuple(batch)
    debug.log(
        "search.scan",
        phase="done",
        logs=len(logs),
        scanned=scanned,
        hits=hits,
        mode=matcher.mode,
    )


def _scan_raw(text: str, matcher: Matcher) -> tuple[int, list[Snippet]]:
    """Count matches and collect snippets over the raw transcript lines."""
    count = 0
    snippets: list[Snippet] = []
    for line in text.splitlines():
        count, snippets = _collect(line, matcher, count, snippets)
    return count, snippets


def _scan_message_text(text: str, matcher: Matcher) -> tuple[int, list[Snippet]]:
    """Count matches and collect snippets over user/assistant message text."""
    count = 0
    snippets: list[Snippet] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        content = _message_text(entry)
        if not content:
            continue
        count, snippets = _collect(content, matcher, count, snippets)
    return count, snippets


def _collect(
    source: str,
    matcher: Matcher,
    count: int,
    snippets: list[Snippet],
) -> tuple[int, list[Snippet]]:
    """Count every match in `source`, snippetting the first few distinct ones.

    Matches falling inside a window already emitted are skipped for snippet
    purposes (they are already visible in it, and highlighted by its `spans`),
    so `MAX_SNIPPETS` buys three *different* places rather than three
    near-identical views of one dense paragraph. They are still counted.
    """
    covered = -1
    for match in matcher.pattern.finditer(source):
        count += 1
        if len(snippets) < MAX_SNIPPETS and match.start() > covered:
            snippets.append(_snippet(source, match.start(), match.end(), matcher))
            covered = min(len(source), match.end() + SNIPPET_RADIUS)
    return count, snippets


def _message_text(entry: dict) -> str:
    """The prose of a transcript entry — what the user and Claude said.

    String content and `text` blocks only. `tool_use` inputs and `tool_result`
    output are deliberately excluded: they are what RAW mode is for, and
    including them here would erase the distinction between the two modes.

    >>> _message_text({"message": {"content": "hello"}})
    'hello'
    >>> _message_text({"message": {"content": [{"type": "text", "text": "hi"}]}})
    'hi'
    >>> _message_text({"message": {"content": [{"type": "tool_result", "content": "x"}]}})
    ''
    >>> _message_text({"type": "progress"})
    ''
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def _snippet(source: str, start: int, end: int, matcher: Matcher) -> Snippet:
    """A single-line window of context around `source[start:end]`.

    The window is collapsed to one line (runs of whitespace and unprintable
    characters become a single space) because a result row has one line to
    spend. That transformation moves every character, so the offsets of the
    matches inside it are mapped through it rather than recomputed — see
    `_collapse`. Every match in the window is reported, not just the anchor one,
    so a dense passage highlights all of its hits.

    >>> m = compile_matcher("needle")
    >>> _snippet("a needle here", 2, 8, m)
    Snippet(text='a needle here', spans=((2, 8),))
    >>> _snippet("needle and needle", 0, 6, m).spans
    ((0, 6), (11, 17))
    >>> _snippet("x" * 200 + "needle", 200, 206, m).text[:1]
    '…'
    """
    low = max(0, start - SNIPPET_RADIUS)
    high = min(len(source), end + SNIPPET_RADIUS)
    window = source[low:high]
    text, offsets = _collapse(window)

    head = "…" if low > 0 else ""
    tail = "…" if high < len(source) else ""
    shift = len(head)

    spans: list[tuple[int, int]] = []
    for match in matcher.pattern.finditer(window):
        span_start = offsets[match.start()] + shift
        span_end = offsets[match.end()] + shift
        if span_end > span_start:
            spans.append((span_start, span_end))

    return Snippet(text=f"{head}{text}{tail}", spans=tuple(spans))


def _collapse(window: str) -> tuple[str, list[int]]:
    r"""Collapse `window` to one line, with a map from old offsets to new ones.

    Returns `(text, offsets)` where `offsets` has `len(window) + 1` entries and
    `offsets[i]` is where `window[i]` ended up in `text` — so a half-open
    `window` span `(a, b)` maps to `(offsets[a], offsets[b])`. Leading and
    trailing whitespace are dropped, and any run of whitespace or unprintable
    characters in between becomes a single space.

    >>> _collapse("a\t \nb")
    ('a b', [0, 1, 2, 2, 2, 3])
    >>> _collapse("  padded  ")
    ('padded', [0, 0, 0, 1, 2, 3, 4, 5, 6, 6, 6])
    """
    out: list[str] = []
    offsets: list[int] = []
    prev_space = True  # suppresses leading whitespace
    for ch in window:
        offsets.append(len(out))
        if ch.isspace() or not ch.isprintable():
            if prev_space:
                continue
            out.append(" ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False
    offsets.append(len(out))

    text = "".join(out)
    if text.endswith(" "):
        text = text[:-1]
        offsets = [min(offset, len(text)) for offset in offsets]
    return text, offsets
