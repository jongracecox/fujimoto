from __future__ import annotations

import io
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import debug, session_query, session_state
from fujimoto.claude.live import LiveSession
from fujimoto.claude.log_parser import ClaudeLogError, encode_project_path
from fujimoto.claude.search import ContentMode, Snippet
from fujimoto.session_query import (
    NOT_RUNNING,
    SOURCE_REGISTRY,
    SOURCE_TMUX,
    Activity,
    ActivityIndex,
    Location,
    Locator,
    QueryError,
    Scope,
    find_session_log,
    format_log,
    format_search,
    load_log,
    log_json,
    relative_time,
    resolve_scope,
    run_search,
    search_json,
    use_color,
)

# -- Fixtures and helpers --


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep every test off the real Claude config, session store and tmux."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("FUJIMOTO_WORKTREE_ROOT", raising=False)
    monkeypatch.delenv("FUJIMOTO_GIT_ROOT", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    with (
        patch(
            "fujimoto.session_state._state_path",
            return_value=tmp_path / "state" / "sessions.json",
        ),
        patch("fujimoto.session_query.list_session_paths", return_value={}),
    ):
        yield
    debug.disable()


def _git(cwd: Path, *args: str) -> None:
    # The user's global config can install hooks (a pre-commit shim) or demand
    # commit signing, either of which fails a throwaway repo's commit.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "git" / "proj"
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FUJIMOTO_WORKTREE_ROOT", str(tmp_path / "wt"))
    path = tmp_path / "wt" / "proj" / "20261020-x"
    path.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "-b", "worktree/20261020-x", str(path))
    return path


def _projects(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "projects"


def _transcript(
    tmp_path: Path,
    cwd: Path,
    session_id: str,
    *texts: str,
    age: int = 0,
    branch: str = "main",
) -> Path:
    """Write a transcript for `cwd`; `age` minutes old, for ordering."""
    ts = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc) - timedelta(minutes=age)
    stamp = ts.isoformat().replace("+00:00", "Z")
    log = _projects(tmp_path) / encode_project_path(cwd) / f"{session_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "type": "user",
            "cwd": str(cwd),
            "gitBranch": branch,
            "timestamp": stamp,
            "message": {"content": text},
        }
        for text in texts
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    mtime = ts.timestamp()
    os.utime(log, (mtime, mtime))
    return log


def _index(
    live_sessions: list[LiveSession] | None = None,
    tmux: dict[str, Path] | None = None,
    records: dict[str, session_state.SessionRecord] | None = None,
) -> ActivityIndex:
    return ActivityIndex(live_sessions or [], tmux or {}, records or {})


def _live(session_id: str, cwd: Path | None, **extra: object) -> LiveSession:
    fields: dict = {
        "pid": 42,
        "peer_name": "proj-ab",
        "status": "idle",
        "tmux_session": None,
        **extra,
    }
    return LiveSession(session_id=session_id, cwd=cwd, **fields)


# -- relative_time --


class TestRelativeTime:
    def test_buckets(self) -> None:
        now = datetime.now(tz=timezone.utc)
        assert relative_time(now) == "just now"
        assert relative_time(now - timedelta(minutes=5)) == "5m ago"
        assert relative_time(now - timedelta(hours=3)) == "3h ago"
        assert relative_time(now - timedelta(days=7)) == "7d ago"
        assert relative_time(now - timedelta(days=65)) == "2mo ago"


# -- resolve_scope --


class TestResolveScope:
    def test_all_projects(self) -> None:
        scope = resolve_scope(project=None, all_projects=True)
        assert scope.all_projects
        assert scope.describe() == "all projects"

    def test_from_the_cwd(self, repo: Path) -> None:
        sub = repo / "src"
        sub.mkdir()
        scope = resolve_scope(project=None, all_projects=False, cwd=sub)
        assert scope.project == "proj"
        assert scope.root == repo.resolve()
        assert scope.worktrees == ()
        assert scope.describe() == "project proj"

    def test_defaults_to_the_process_cwd(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo)
        assert resolve_scope(project=None, all_projects=False).project == "proj"

    def test_from_a_worktree_covers_the_main_clone_and_siblings(
        self, repo: Path, worktree: Path
    ) -> None:
        scope = resolve_scope(project=None, all_projects=False, cwd=worktree)
        assert scope.root == repo.resolve()
        assert scope.worktrees == (worktree.resolve(),)

    def test_project_as_path(self, repo: Path) -> None:
        scope = resolve_scope(project=str(repo), all_projects=False)
        assert scope.project == "proj"

    def test_project_by_name_under_git_root(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", str(repo.parent))
        scope = resolve_scope(project="proj", all_projects=False)
        assert scope.root == repo.resolve()

    def test_unknown_name_without_git_root(self) -> None:
        with pytest.raises(QueryError, match="FUJIMOTO_GIT_ROOT is not set"):
            resolve_scope(project="nope", all_projects=False)

    def test_unknown_name_with_git_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", str(tmp_path))
        with pytest.raises(QueryError, match="no such project"):
            resolve_scope(project="nope", all_projects=False)

    def test_cwd_outside_a_repo(self, tmp_path: Path) -> None:
        with pytest.raises(QueryError, match="pass --project or --all-projects"):
            resolve_scope(project=None, all_projects=False, cwd=tmp_path)

    def test_project_path_outside_a_repo(self, tmp_path: Path) -> None:
        with pytest.raises(QueryError, match="is not inside a git repository"):
            resolve_scope(project=str(tmp_path), all_projects=False)

    def test_does_not_create_the_in_project_worktree_dir(self, repo: Path) -> None:
        resolve_scope(project=None, all_projects=False, cwd=repo)
        assert not (repo / ".fujimoto").exists()

    def test_lists_in_project_worktrees(self, repo: Path) -> None:
        wt = repo / ".fujimoto" / "worktrees" / "20261020-y"
        wt.mkdir(parents=True)
        scope = resolve_scope(project=None, all_projects=False, cwd=repo)
        assert scope.worktrees == (wt.resolve(),)


# -- Locator --


class TestLocator:
    def test_project_scope_root_and_worktree(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        wt = tmp_path / "wt" / "20261020-x"
        locator = Locator(Scope(False, "proj", root, (wt,)))
        assert locator.locate(root) == Location("proj", root, None)
        assert locator.locate(wt / "sub") == Location("proj", root, wt)
        # A deleted worktree is still named: the scope says so, not git.
        assert locator.locate(wt).worktree == wt

    def test_all_projects_asks_git(self, repo: Path, worktree: Path) -> None:
        locator = Locator(Scope(all_projects=True))
        assert locator.locate(repo) == Location("proj", repo.resolve(), None)
        found = locator.locate(worktree)
        assert found.project == "proj"
        assert found.worktree == worktree.resolve()

    def test_all_projects_unknown_directories(self, tmp_path: Path) -> None:
        locator = Locator(Scope(all_projects=True))
        assert locator.locate(tmp_path / "gone") == Location(None, None, None)
        assert locator.locate(tmp_path) == Location(None, None, None)

    def test_memoizes(self, repo: Path) -> None:
        locator = Locator(Scope(all_projects=True))
        locator.locate(repo)
        with patch("fujimoto.session_query.get_main_worktree_root") as git:
            locator.locate(repo)
        git.assert_not_called()

    def test_label(self, tmp_path: Path) -> None:
        wt = tmp_path / "20261020-x"
        assert Location("p", tmp_path, wt).label(wt) == "p/20261020-x"
        assert Location("p", tmp_path, None).label(tmp_path) == "p"
        assert Location(None, None, None).label(tmp_path) == str(tmp_path)


# -- ActivityIndex --


class TestActivityIndex:
    def test_registry_match_by_session_id(self, tmp_path: Path) -> None:
        index = _index([_live("s1", tmp_path, tmux_session="proj/x", status="busy")])
        activity = index.for_session("s1", tmp_path)
        assert activity == Activity(
            running=True,
            source=SOURCE_REGISTRY,
            claude_session_id="s1",
            pid=42,
            peer_name="proj-ab",
            status="busy",
            tmux_session="proj/x",
        )

    def test_registry_borrows_tmux_name_from_the_cwd(self, tmp_path: Path) -> None:
        index = _index([_live("s1", tmp_path)], tmux={"proj/x": tmp_path})
        assert index.for_session("s1", tmp_path).tmux_session == "proj/x"

    def test_registry_leaves_ambiguous_tmux_name_unset(self, tmp_path: Path) -> None:
        tmux = {"proj/a": tmp_path, "proj/b": tmp_path}
        index = _index([_live("s1", tmp_path)], tmux=tmux)
        assert index.for_session("s1", tmp_path).tmux_session is None

    def test_another_live_conversation_in_the_cwd_wins_over_tmux(
        self, tmp_path: Path
    ) -> None:
        index = _index([_live("other", tmp_path)], tmux={"proj/x": tmp_path})
        assert index.for_session("s1", tmp_path) is NOT_RUNNING

    def test_tmux_fallback_with_recorded_id(self, tmp_path: Path) -> None:
        record = session_state.SessionRecord(cwd=str(tmp_path), claude_session_id="s1")
        index = _index(tmux={"proj/x": tmp_path}, records={"proj/x": record})
        activity = index.for_session("s1", tmp_path)
        assert activity.running
        assert activity.source == SOURCE_TMUX
        assert activity.tmux_session == "proj/x"
        assert activity.claude_session_id == "s1"
        assert not index.for_session("s2", tmp_path).running

    def test_tmux_fallback_uses_latest_transcript(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _transcript(tmp_path, cwd, "old", "a", age=10)
        _transcript(tmp_path, cwd, "new", "b", age=1)
        index = _index(tmux={"proj/x": cwd})
        assert index.for_session("new", cwd).running
        assert not index.for_session("old", cwd).running

    def test_nothing_running(self, tmp_path: Path) -> None:
        assert _index().for_session("s1", tmp_path) is NOT_RUNNING

    def test_gather_reads_all_three_sources(self, tmp_path: Path) -> None:
        with (
            patch("fujimoto.session_query.live.list_live_sessions", return_value=[]),
            patch(
                "fujimoto.session_query.list_session_paths",
                return_value={"proj/x": tmp_path},
            ),
        ):
            index = ActivityIndex.gather()
        assert index.for_session("s1", tmp_path) is NOT_RUNNING


class TestActivityRendering:
    def test_not_running(self) -> None:
        assert NOT_RUNNING.as_json() == {"running": False}
        assert NOT_RUNNING.describe() == "no"

    def test_running(self) -> None:
        activity = Activity(True, SOURCE_REGISTRY, "s1", 9, "peer", "busy", "p/x")
        assert activity.describe() == "busy · peer peer · tmux p/x · pid 9"
        assert activity.as_json()["claude_session_id"] == "s1"

    def test_minimal(self) -> None:
        assert Activity(True, SOURCE_TMUX).describe() == "running"


# -- run_search --


class TestRunSearch:
    def test_finds_hits_in_root_and_worktrees(
        self, tmp_path: Path, repo: Path, worktree: Path
    ) -> None:
        _transcript(tmp_path, repo, "root-1", "needle in root", age=5)
        _transcript(tmp_path, worktree, "wt-1", "a needle here", age=1)
        _transcript(tmp_path, repo, "miss", "nothing", age=2)
        scope = resolve_scope(project=None, all_projects=False, cwd=repo)
        report = run_search("needle", scope, activity=_index())
        assert [r.hit.session.session_id for r in report.results] == ["wt-1", "root-1"]
        assert report.results[0].location.worktree == worktree.resolve()
        assert report.results[1].location.worktree is None
        assert report.total_logs == 3
        assert report.scanned == 3
        assert not report.truncated

    def test_limit_stops_early_and_reports_truncation(self, tmp_path: Path) -> None:
        cwd = tmp_path / "w"
        for i in range(25):
            _transcript(tmp_path, cwd, f"s{i:02}", "needle", age=i)
        report = run_search("needle", Scope(all_projects=True), limit=3)
        assert [r.hit.session.session_id for r in report.results] == [
            "s00",
            "s01",
            "s02",
        ]
        assert report.truncated
        assert report.scanned == 10  # one batch

    def test_limit_zero_scans_everything(self, tmp_path: Path) -> None:
        cwd = tmp_path / "w"
        for i in range(12):
            _transcript(tmp_path, cwd, f"s{i:02}", "needle", age=i)
        report = run_search("needle", Scope(all_projects=True), limit=0)
        assert len(report.results) == 12
        assert not report.truncated

    def test_exactly_limit_hits_is_not_truncated(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "s1", "needle")
        report = run_search("needle", Scope(all_projects=True), limit=1)
        assert not report.truncated

    def test_regex_case_and_raw_mode(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "s1", "Needle-42")
        scope = Scope(all_projects=True)
        assert run_search(r"needle-\d+", scope, regex=True).results
        assert not run_search("needle", scope, case_sensitive=True).results
        # "gitBranch" is JSON scaffolding: only raw mode sees it.
        assert not run_search("gitBranch", scope).results
        assert run_search("gitBranch", scope, mode=ContentMode.RAW).results

    def test_bad_regex(self) -> None:
        with pytest.raises(QueryError, match="invalid regex"):
            run_search("(", Scope(all_projects=True), regex=True)

    def test_no_hits_skips_gathering_activity(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "s1", "hay")
        with patch.object(ActivityIndex, "gather") as gather:
            report = run_search("needle", Scope(all_projects=True))
        gather.assert_not_called()
        assert report.results == ()

    def test_marks_running_sessions(self, tmp_path: Path) -> None:
        cwd = tmp_path / "w"
        _transcript(tmp_path, cwd, "s1", "needle")
        with patch.object(
            ActivityIndex, "gather", return_value=_index([_live("s1", cwd)])
        ):
            report = run_search("needle", Scope(all_projects=True))
        assert report.results[0].activity.running


class TestSearchOutput:
    def _report(self, tmp_path: Path, *, running: bool = False):
        cwd = tmp_path / "w"
        cwd.mkdir()
        _transcript(tmp_path, cwd, "s1", "a needle and a needle", branch="feat")
        index = _index([_live("s1", cwd, tmux_session="p/x")] if running else [])
        return run_search("needle", Scope(all_projects=True), activity=index)

    def test_json(self, tmp_path: Path) -> None:
        data = search_json(self._report(tmp_path, running=True))
        assert data["scope"] == {
            "all_projects": True,
            "project": None,
            "project_root": None,
        }
        assert data["mode"] == "text"
        [hit] = data["hits"]
        assert hit["session_id"] == "s1"
        assert hit["branch"] == "feat"
        assert hit["cwd_exists"] is True
        assert hit["match_count"] == 2
        assert hit["snippets"] == [
            {"text": "a needle and a needle", "spans": [[2, 8], [15, 21]]}
        ]
        assert hit["active"]["running"] is True
        assert hit["active"]["tmux_session"] == "p/x"
        json.dumps(data)  # serializable

    def test_text(self, tmp_path: Path) -> None:
        text = format_search(self._report(tmp_path, running=True))
        lines = text.splitlines()
        assert lines[0].startswith(f"● {tmp_path / 'w'}  (2 matches; ")
        assert "  session:  s1" in lines
        assert "  branch:   feat" in lines
        assert "  title:    a needle and a needle" in lines
        assert any(line.startswith("  active:   idle · peer proj-ab") for line in lines)
        assert "    a needle and a needle" in lines
        assert "1 matching session · scanned 1/1 transcripts" in text
        assert "fujimoto log <session>" in text

    def test_text_inactive_and_colored(self, tmp_path: Path) -> None:
        text = format_search(self._report(tmp_path), color=True)
        assert text.startswith("○ ")
        assert "  active:   no" in text
        assert "\033[1;33mneedle\033[0m" in text

    def test_text_deleted_cwd_and_truncation(self, tmp_path: Path) -> None:
        for i in range(12):
            _transcript(tmp_path, tmp_path / "gone", f"s{i:02}", "needle")
        report = run_search("needle", Scope(all_projects=True), limit=1)
        text = format_search(report)
        assert "(deleted)" in text
        assert "stopped at --limit 1; more may match" in text

    def test_text_no_results(self) -> None:
        report = run_search("needle", Scope(all_projects=True))
        assert format_search(report) == (
            "No matches for 'needle' in 0 transcripts (all projects, message text)."
        )

    def test_long_title_is_clipped(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "s1", "needle " + "x" * 300)
        text = format_search(run_search("needle", Scope(all_projects=True)))
        title = next(line for line in text.splitlines() if "title:" in line)
        assert title.endswith("…")
        assert len(title) < 120


class TestUseColor:
    def test_tty(self) -> None:
        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]
        assert use_color(stream)

    def test_not_a_tty(self) -> None:
        assert not use_color(io.StringIO())

    def test_no_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]
        assert not use_color(stream)

    def test_highlight_multiple_spans(self) -> None:
        text = session_query._highlight(Snippet("ab ab", ((0, 2), (3, 5))), True)
        assert text == "\033[1;33mab\033[0m \033[1;33mab\033[0m"


# -- find_session_log --


class TestFindSessionLog:
    def test_exact_id(self, tmp_path: Path) -> None:
        log = _transcript(tmp_path, tmp_path / "w", "abc123", "x")
        assert find_session_log("abc123") == log

    def test_unique_prefix(self, tmp_path: Path) -> None:
        log = _transcript(tmp_path, tmp_path / "w", "abc123", "x")
        _transcript(tmp_path, tmp_path / "w", "def456", "x")
        assert find_session_log("abc") == log

    def test_ambiguous_prefix(self, tmp_path: Path) -> None:
        for i in range(7):
            _transcript(tmp_path, tmp_path / "w", f"ab{i}", "x")
        with pytest.raises(QueryError, match=r"'ab' matches 7 sessions: .*, …"):
            find_session_log("ab")

    def test_unknown(self, tmp_path: Path) -> None:
        with pytest.raises(QueryError, match="no session log found"):
            find_session_log("zzz")

    def test_empty_ref(self) -> None:
        with pytest.raises(QueryError, match="no session log found"):
            find_session_log("")

    def test_glob_characters_are_literal(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "abc", "x")
        with pytest.raises(QueryError):
            find_session_log("*")

    def test_path(self, tmp_path: Path) -> None:
        log = tmp_path / "elsewhere.jsonl"
        log.write_text("{}\n")
        assert find_session_log(str(log)) == log


# -- load_log --


def _conversation(tmp_path: Path, cwd: Path) -> Path:
    log = _projects(tmp_path) / encode_project_path(cwd) / "conv.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "type": "user",
            "cwd": str(cwd),
            "timestamp": "2026-10-20T10:00:00Z",
            "message": {"content": "fix the bug"},
        },
        {
            "type": "assistant",
            "cwd": str(cwd),
            "timestamp": "2026-10-20T10:01:00Z",
            "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "cwd": str(cwd),
            "timestamp": "2026-10-20T10:02:00Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "a"}
                ]
            },
        },
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return log


class TestLoadLog:
    def test_parsed(self, tmp_path: Path, repo: Path) -> None:
        _conversation(tmp_path, repo)
        report = load_log("conv", activity=_index())
        assert report.format == "parsed"
        assert [m.role for _, m in report.messages or ()] == [
            "user",
            "assistant",
            "tool_use",
            "tool_result",
        ]
        assert report.total == 4
        assert report.location == Location("proj", repo.resolve(), None)
        assert report.session_id == "conv"

    def test_roles_and_tail(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        report = load_log(
            "conv", roles=["user", "assistant"], tail=1, activity=_index()
        )
        assert [m.text for _, m in report.messages or ()] == ["Looking."]
        assert report.total == 2

    def test_tail_zero_keeps_everything(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        assert len(load_log("conv", tail=0, activity=_index()).messages or ()) == 4

    def test_running(self, tmp_path: Path) -> None:
        cwd = tmp_path / "w"
        _conversation(tmp_path, cwd)
        report = load_log("conv", activity=_index([_live("conv", cwd)]))
        assert report.activity.running

    def test_gathers_activity_by_default(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        with patch.object(ActivityIndex, "gather", return_value=_index()) as gather:
            load_log("conv")
        gather.assert_called_once()

    def test_raw_fallback(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text(
            '{"type": "user", "message": "not a dict"}\n\n{"type": "x"}\n'
            '{"type": "y"}\n'
        )
        report = load_log(str(log), tail=2)
        assert report.format == "raw"
        assert report.session is None
        assert report.location is None
        assert report.lines == ((1, '{"type": "x"}'), (2, '{"type": "y"}'))
        assert report.total == 3
        assert report.parse_error and report.parse_error.startswith("AttributeError")
        assert report.session_id == "broken"

    def test_unparseable_metadata_but_readable_transcript(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        with patch(
            "fujimoto.session_query.parse_session", side_effect=RuntimeError("boom")
        ):
            report = load_log("conv")
        assert report.session is None
        assert report.format == "parsed"

    def test_unreadable(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        with (
            patch(
                "fujimoto.session_query.read_transcript",
                side_effect=ClaudeLogError("Cannot read"),
            ),
            pytest.raises(QueryError, match="Cannot read"),
        ):
            load_log("conv", activity=_index())


class TestLogOutput:
    def test_json_parsed(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        data = log_json(load_log("conv", activity=_index()))
        assert data["session_id"] == "conv"
        assert data["format"] == "parsed"
        assert data["parse_error"] is None
        assert data["messages"][2] == {
            "index": 2,
            "role": "tool_use",
            "text": "Bash\ncommand: ls",
            "timestamp": "2026-10-20T10:01:00+00:00",
            "tool_id": "t1",
        }
        assert data["active"] == {"running": False}

    def test_json_raw(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text('{"type": "user", "message": "x"}\n')
        data = log_json(load_log(str(log)))
        assert data["format"] == "raw"
        assert data["lines"] == [
            {"index": 0, "text": '{"type": "user", "message": "x"}'}
        ]
        assert data["log_path"] == str(log)
        assert "messages" not in data

    def test_text_parsed(self, tmp_path: Path) -> None:
        _conversation(tmp_path, tmp_path / "w")
        text = format_log(load_log("conv", tail=2, activity=_index()))
        lines = text.splitlines()
        assert lines[0] == "# session conv · parsed · 4 messages, showing 2"
        assert "active:   no" in lines
        assert any(line.startswith("### tool_use · #2 · 2026-10-20") for line in lines)
        assert lines[-1] == "a"

    def test_text_raw(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text('{"type": "user", "message": "x"}\n')
        lines = format_log(load_log(str(log))).splitlines()
        assert lines[0] == "# session broken · raw · 1 line"
        assert lines[1] == f"log:      {log}"
        assert lines[2].startswith("warning:  could not parse transcript (")
        assert lines[-1] == '{"type": "user", "message": "x"}'


def test_debug_events_are_written(tmp_path: Path) -> None:
    debug.enable(redact=False, log_dir=tmp_path / "logs")
    _transcript(tmp_path, tmp_path / "w", "s1", "needle")
    run_search("needle", Scope(all_projects=True), activity=_index())
    load_log("s1", activity=_index())
    log = next((tmp_path / "logs").glob("*.log")).read_text()
    for event in ("query.search", "query.activity", "query.log_resolve", "query.log"):
        assert event in log


def test_untitled_session_has_no_title_row(tmp_path: Path) -> None:
    log = _projects(tmp_path) / "-w" / "s1.jsonl"
    log.parent.mkdir(parents=True)
    entry = {
        "type": "assistant",
        "cwd": str(tmp_path),
        "timestamp": "2026-10-20T10:00:00Z",
        "message": {"content": [{"type": "text", "text": "a needle"}]},
    }
    log.write_text(json.dumps(entry) + "\n")
    text = format_search(
        run_search("needle", Scope(all_projects=True), activity=_index())
    )
    assert "title:" not in text


def test_empty_log_has_no_metadata(tmp_path: Path) -> None:
    log = tmp_path / "empty.jsonl"
    log.write_text("\n")
    report = load_log(str(log))
    assert report.session is None
    assert report.format == "parsed"
    assert report.total == 0


class TestDeletedWorktrees:
    def test_project_scope_includes_deleted_worktrees(
        self, tmp_path: Path, repo: Path, worktree: Path
    ) -> None:
        wt_dir = worktree.parent.resolve()
        _transcript(tmp_path, wt_dir / "20261001-gone", "old", "needle long ago")
        _transcript(tmp_path, wt_dir / "20261001-gone" / "sub", "older", "x")
        # Shares the encoded prefix but belongs to another project.
        _transcript(tmp_path, wt_dir.parent / "proj-other" / "y", "nope", "needle")
        scope = resolve_scope(project=None, all_projects=False, cwd=repo)
        assert [w.name for w in scope.worktrees] == ["20261001-gone", "20261020-x"]

        report = run_search("needle", scope, activity=_index())
        [result] = report.results
        assert result.hit.session.session_id == "old"
        assert result.location.worktree is not None
        assert result.location.worktree.name == "20261001-gone"
        assert search_json(report)["hits"][0]["cwd_exists"] is False
        assert "(deleted)" in format_search(report)

    def test_in_project_worktrees_dir(self, tmp_path: Path, repo: Path) -> None:
        base = repo.resolve() / ".fujimoto" / "worktrees"
        _transcript(tmp_path, base / "20261001-gone", "old", "needle")
        _transcript(tmp_path, base, "at-base", "x")  # not a worktree
        scope = resolve_scope(project=None, all_projects=False, cwd=repo)
        assert [w.name for w in scope.worktrees] == ["20261001-gone"]
        assert not (repo / ".fujimoto").exists()


class TestRecordedCwdsUnder:
    def test_confirms_by_recorded_cwd(self, tmp_path: Path) -> None:
        from fujimoto.claude.log_parser import recorded_cwds_under

        base = tmp_path / "wt" / "proj"
        _transcript(tmp_path, base, "a", "x")
        _transcript(tmp_path, base / "b", "b", "x")
        _transcript(tmp_path, tmp_path / "wt" / "proj-other", "c", "x")
        empty = _projects(tmp_path) / encode_project_path(base / "empty")
        empty.mkdir(parents=True)
        assert recorded_cwds_under(base) == [base, base / "b"]

    def test_missing_projects_dir(self, tmp_path: Path) -> None:
        from fujimoto.claude.log_parser import recorded_cwds_under

        assert recorded_cwds_under(tmp_path / "x") == []


# -- Calling session --


class TestExcludeSelf:
    def test_calling_session_is_skipped(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "me", "needle", age=0)
        _transcript(tmp_path, tmp_path / "w", "other", "needle", age=5)
        report = run_search(
            "needle", Scope(all_projects=True), exclude_session="me", limit=1
        )
        assert [r.hit.session.session_id for r in report.results] == ["other"]
        assert report.excluded_session == "me"
        assert search_json(report)["excluded_session"] == "me"
        assert "skipped the calling session" in format_search(report)

    def test_only_reported_when_it_matched(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "other", "needle")
        report = run_search("needle", Scope(all_projects=True), exclude_session="me")
        assert report.excluded_session is None
        assert "skipped" not in format_search(report)

    def test_no_results_mentions_the_skip(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "me", "needle")
        report = run_search("needle", Scope(all_projects=True), exclude_session="me")
        assert format_search(report).endswith(
            "skipped the calling session (--include-self to show it)."
        )

    def test_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
        assert session_query.calling_session_id() == "abc"
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        assert session_query.calling_session_id() is None


# -- Match breakdown --


def _raw_log(tmp_path: Path, session_id: str, entries: list[dict]) -> Path:
    log = _projects(tmp_path) / "-w" / f"{session_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    base = {"cwd": str(tmp_path / "w"), "timestamp": "2026-10-20T10:00:00Z"}
    log.write_text("\n".join(json.dumps({**base, **e}) for e in entries) + "\n")
    return log


class TestBreakdownOutput:
    def test_raw_mode_text_and_json(self, tmp_path: Path) -> None:
        _raw_log(
            tmp_path,
            "s1",
            [
                {"type": "user", "message": {"content": "about AB-1"}},
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": "AB-1 AB-1 listed"}
                        ]
                    },
                },
                {"type": "system", "slug": "AB-1"},
            ],
        )
        report = run_search(
            "AB-1", Scope(all_projects=True), mode=ContentMode.RAW, activity=_index()
        )
        assert search_json(report)["hits"][0]["breakdown"] == {
            "message": 1,
            "tool": 2,
            "other": 1,
        }
        text = format_search(report)
        assert "(4 matches: 1 message, 2 tool, 1 other; " in text
        # Message text is preferred, and tool output is shown unescaped.
        snippets = [
            line.strip() for line in text.splitlines() if line.startswith("    ")
        ]
        assert snippets[:2] == ["about AB-1", "AB-1 AB-1 listed"]

    def test_zero_message_matches_are_shown(self, tmp_path: Path) -> None:
        _raw_log(tmp_path, "s1", [{"type": "system", "slug": "AB-1"}])
        report = run_search(
            "AB-1", Scope(all_projects=True), mode=ContentMode.RAW, activity=_index()
        )
        assert "(1 match: 0 message, 1 other; " in format_search(report)

    def test_text_mode_has_no_breakdown_in_text(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "s1", "needle")
        report = run_search("needle", Scope(all_projects=True), activity=_index())
        assert "(1 match; " in format_search(report)
        assert search_json(report)["hits"][0]["breakdown"] == {
            "message": 1,
            "tool": None,
            "other": None,
        }

    def test_hit_without_breakdown(self, tmp_path: Path) -> None:
        from fujimoto.claude.search import SearchHit

        _transcript(tmp_path, tmp_path / "w", "s1", "needle")
        report = run_search("needle", Scope(all_projects=True), activity=_index())
        bare = SearchHit(report.results[0].hit.session, 2, ())
        assert session_query._describe_matches(bare) == "2 matches"
        assert session_query._breakdown_json(None) is None


# -- Injected messages, grep, head --


def _chat(tmp_path: Path, *turns: tuple[str, str]) -> Path:
    entries = []
    for role, text in turns:
        content = text if role == "user" else [{"type": "text", "text": text}]
        entries.append({"type": role, "message": {"content": content}})
    return _raw_log(tmp_path, "chat", entries)


class TestInjected:
    def test_hidden_by_default(self, tmp_path: Path) -> None:
        _chat(
            tmp_path,
            ("user", "do the thing"),
            ("user", "<task-notification>done</task-notification>"),
            ("assistant", "done"),
        )
        report = load_log("chat", activity=_index())
        assert [(i, m.role) for i, m in report.messages or ()] == [
            (0, "user"),
            (2, "assistant"),
        ]
        assert report.total == 2

    def test_shown_on_request(self, tmp_path: Path) -> None:
        _chat(tmp_path, ("user", "<system-reminder>x</system-reminder>"))
        report = load_log("chat", roles=["injected"], activity=_index())
        assert [m.role for _, m in report.messages or ()] == ["injected"]


class TestGrep:
    @pytest.fixture
    def chat(self, tmp_path: Path) -> Path:
        return _chat(
            tmp_path,
            ("user", "zero"),
            ("assistant", "one NEEDLE"),
            ("user", "two"),
            ("assistant", "three"),
            ("user", "four"),
            ("assistant", "five needle"),
        )

    def test_matches_with_context_and_breaks(self, chat: Path) -> None:
        report = load_log("chat", grep="needle", context=1, activity=_index())
        assert [i for i, _ in report.messages or ()] == [0, 1, 2, 4, 5]
        assert report.matched == 2
        assert report.breaks == frozenset({4})
        text = format_log(report)
        assert text.splitlines()[0] == (
            "# session chat · parsed · 6 messages, 2 matches for --grep, "
            "showing 5 with context"
        )
        assert "⋯" in text

    def test_no_context(self, chat: Path) -> None:
        report = load_log("chat", grep="needle", activity=_index())
        assert [i for i, _ in report.messages or ()] == [1, 5]
        assert format_log(report).splitlines()[0].endswith("2 matches for --grep")

    def test_regex_and_case(self, chat: Path) -> None:
        assert load_log("chat", grep="NEEDLE", case_sensitive=True).matched == 1
        assert load_log("chat", grep=r"f\w+e", regex=True).matched == 1

    def test_bad_regex(self, chat: Path) -> None:
        with pytest.raises(QueryError, match="invalid regex"):
            load_log("chat", grep="(", regex=True)

    def test_head(self, chat: Path) -> None:
        report = load_log("chat", head=2, activity=_index())
        assert [i for i, _ in report.messages or ()] == [0, 1]

    def test_grep_raw_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text('{"type": "user", "message": "a"}\nneedle\nx\ny\nneedle\n')
        report = load_log(str(log), grep="needle")
        assert report.lines == ((1, "needle"), (4, "needle"))
        lines = format_log(report).splitlines()
        assert lines[-3:] == ["needle", "⋯", "needle"]


# -- Summary and links --


class TestLinks:
    def test_kinds_and_dedupe(self) -> None:
        links = session_query.extract_links(
            [
                "PR https://github.com/o/r/pull/12 and https://x.atlassian.net/browse/AB-1",
                "again https://github.com/o/r/pull/12 and "
                "https://w.slack.com/archives/D1/p1792437915766929?thread_ts=1",
            ]
        )
        assert [(link.kind, link.label, link.mentions) for link in links] == [
            ("pr", "o/r#12", 2),
            ("jira", "AB-1", 1),
            ("slack", "D1 1792437915.766929", 1),
        ]
        assert links[0].as_json() == {
            "kind": "pr",
            "url": "https://github.com/o/r/pull/12",
            "label": "o/r#12",
            "mentions": 2,
            "repo": "o/r",
            "number": "12",
        }

    def test_tool_output_only_contributes_prs(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        from fujimoto.claude.log_parser import TranscriptMessage

        links = session_query._message_links(
            [
                TranscriptMessage(
                    "tool_result",
                    "https://x.atlassian.net/browse/AB-9 https://github.com/o/r/pull/3",
                    ts,
                ),
                TranscriptMessage(
                    "tool_use", "gh pr view https://github.com/o/r/pull/3", ts
                ),
            ]
        )
        assert [link.label for link in links] == ["o/r#3"]
        assert links[0].mentions == 1


class TestSummary:
    def test_summary(self, tmp_path: Path) -> None:
        _chat(
            tmp_path,
            ("user", "start https://x.atlassian.net/browse/AB-1"),
            ("assistant", "working"),
            ("assistant", "result table"),
            ("user", "yep, do it"),
            ("user", "<task-notification>x</task-notification>"),
            ("assistant", "handed off https://github.com/o/r/pull/9"),
        )
        report = load_log("chat", summary=True, activity=_index())
        digest = report.summary
        assert digest is not None
        assert digest.first_prompt and digest.first_prompt[0] == 0
        assert digest.replied_to and digest.replied_to[1].text == "result table"
        assert digest.last_user and digest.last_user[1].text == "yep, do it"
        assert digest.last_assistant and digest.last_assistant[0] == 5
        assert digest.role_counts == {"user": 2, "assistant": 3, "injected": 1}
        assert [link.label for link in digest.links] == ["AB-1", "o/r#9"]
        assert report.messages == ()

        text = format_log(report)
        assert text.splitlines()[0] == (
            "# session chat · summary · 6 messages (2 user, 3 assistant, 1 injected)"
        )
        for heading in (
            "## first prompt · #0 · ",
            "## reply the user last answered · #2 · ",
            "## last user message · #3 · ",
            "## last assistant message · #5 · ",
        ):
            assert any(line.startswith(heading) for line in text.splitlines())
        assert "## links" in text

        data = log_json(report)
        assert data["summary"]["replied_to"]["text"] == "result table"
        assert data["summary"]["links"][1]["number"] == "9"
        assert "messages" not in data

    def test_single_prompt_session(self, tmp_path: Path) -> None:
        _chat(tmp_path, ("user", "just this"))
        report = load_log("chat", summary=True, activity=_index())
        text = format_log(report)
        assert "## last user message" not in text
        assert "## links\n(none)" in text
        data = log_json(report)["summary"]
        assert data["replied_to"] is None
        assert data["last_assistant"] is None

    def test_empty_transcript(self, tmp_path: Path) -> None:
        log = tmp_path / "empty.jsonl"
        log.write_text("\n")
        report = load_log(str(log), summary=True)
        assert (
            format_log(report).splitlines()[0]
            == "# session empty · summary · 0 messages"
        )
        assert log_json(report)["summary"]["first_prompt"] is None

    def test_raw_fallback_keeps_links(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text(
            '{"type": "user", "message": "x"}\n'
            '{"see": "https://github.com/o/r/pull/4"}\n'
        )
        report = load_log(str(log), summary=True)
        lines = format_log(report).splitlines()
        assert lines[0] == "# session broken · raw summary · 2 lines"
        assert lines[-2:] == ["## links", "pr     o/r#4  https://github.com/o/r/pull/4"]
        assert log_json(report)["summary"]["links"][0]["label"] == "o/r#4"

    def test_raw_fallback_without_links(self, tmp_path: Path) -> None:
        log = tmp_path / "broken.jsonl"
        log.write_text('{"type": "user", "message": "x"}\n')
        report = load_log(str(log), summary=True)
        assert format_log(report).splitlines()[-1] == "(none)"
        assert log_json(report)["summary"] == {"links": []}


def test_pr_only_in_tool_output_is_kept() -> None:
    from fujimoto.claude.log_parser import TranscriptMessage

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    links = session_query._message_links(
        [TranscriptMessage("tool_result", "https://github.com/o/r/pull/5", ts)]
    )
    assert [link.label for link in links] == ["o/r#5"]


class TestWaitForRegistration:
    def _fake_time(self):
        now = [0.0]
        return now, (lambda: now[0]), (lambda secs: now.__setitem__(0, now[0] + secs))

    def test_found_after_a_few_polls(self, tmp_path: Path) -> None:
        now, clock, sleep = self._fake_time()
        entry = _live("s1", tmp_path, tmux_session="p/x")
        answers = [[], [], [entry]]
        with patch(
            "fujimoto.session_query.live.list_live_sessions",
            side_effect=lambda: answers.pop(0),
        ):
            found = session_query.wait_for_registration(
                "p/x", timeout=5, sleep=sleep, clock=clock
            )
        assert found == entry
        assert now[0] == 1.0

    def test_times_out(self) -> None:
        now, clock, sleep = self._fake_time()
        with patch("fujimoto.session_query.live.list_live_sessions", return_value=[]):
            found = session_query.wait_for_registration(
                "p/x", timeout=2, sleep=sleep, clock=clock
            )
        assert found is None
        assert now[0] == 2.0

    def test_zero_timeout_checks_once(self) -> None:
        with patch(
            "fujimoto.session_query.live.list_live_sessions", return_value=[]
        ) as listed:
            assert session_query.wait_for_registration("p/x", timeout=0) is None
        listed.assert_called_once()


def test_resolve_session_unreadable(tmp_path: Path) -> None:
    log = tmp_path / "empty.jsonl"
    log.write_text("\n")
    with pytest.raises(QueryError, match="cannot read session empty"):
        session_query.resolve_session(str(log))


class TestSortByRelevance:
    def _logs(self, tmp_path: Path) -> None:
        # Newest first: a listing-only hit, then two that discuss the ticket.
        _raw_log(
            tmp_path,
            "listing",
            [
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": "AB-1 " * 10}]
                    },
                }
            ],
        )
        os.utime(_projects(tmp_path) / "-w" / "listing.jsonl", (3_000, 3_000))
        for name, mentions, mtime in (("worked", 3, 1_000), ("touched", 1, 2_000)):
            _raw_log(
                tmp_path,
                name,
                [{"type": "user", "message": {"content": "AB-1"}}] * mentions,
            )
            os.utime(_projects(tmp_path) / "-w" / f"{name}.jsonl", (mtime, mtime))

    def test_message_matches_lead_in_raw_mode(self, tmp_path: Path) -> None:
        self._logs(tmp_path)
        report = run_search(
            "AB-1",
            Scope(all_projects=True),
            mode=ContentMode.RAW,
            sort=session_query.SortOrder.RELEVANCE,
            activity=_index(),
        )
        assert [r.hit.session.session_id for r in report.results] == [
            "worked",
            "touched",
            "listing",
        ]
        assert search_json(report)["sort"] == "relevance"
        assert " · by relevance" in format_search(report)

    def test_scans_everything_before_the_limit(self, tmp_path: Path) -> None:
        self._logs(tmp_path)
        recent = run_search(
            "AB-1", Scope(all_projects=True), mode=ContentMode.RAW, limit=1
        )
        assert recent.results[0].hit.session.session_id == "listing"
        best = run_search(
            "AB-1",
            Scope(all_projects=True),
            mode=ContentMode.RAW,
            limit=1,
            sort=session_query.SortOrder.RELEVANCE,
            activity=_index(),
        )
        assert [r.hit.session.session_id for r in best.results] == ["worked"]
        assert best.scanned == 3
        assert best.truncated
        assert "top 1 shown" in format_search(best)

    def test_text_mode_ranks_by_message_count(self, tmp_path: Path) -> None:
        _transcript(tmp_path, tmp_path / "w", "once", "needle", age=0)
        _transcript(tmp_path, tmp_path / "w", "twice", "needle", "needle", age=5)
        report = run_search(
            "needle",
            Scope(all_projects=True),
            sort=session_query.SortOrder.RELEVANCE,
            activity=_index(),
        )
        assert [r.hit.session.session_id for r in report.results] == ["twice", "once"]

    def test_key_without_breakdown(self, tmp_path: Path) -> None:
        from fujimoto.claude.search import SearchHit

        _transcript(tmp_path, tmp_path / "w", "s1", "needle")
        session = run_search("needle", Scope(all_projects=True)).results[0].hit.session
        key = session_query.relevance_key(SearchHit(session, 4, ()))
        assert key[:2] == (4, 0)
