from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fujimoto import broadcast, debug, mcp_server, session_query
from fujimoto.claude.log_parser import encode_project_path
from fujimoto.claude.search import ContentMode
from fujimoto.global_config import update_global_key
from fujimoto.mcp_server import Commands, FujimotoToolError

pytest.importorskip("mcp")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDqZqdeM8UD5c8bP1N3xsHbsnyHKuxZ3G8ZIhXmGhgk1"
OTHER = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield
    debug.disable()


@pytest.fixture(autouse=True)
def _refs_pass_through(request: pytest.FixtureRequest):
    """Session refs reach the mocked loaders as given (TestSessionIdFor aside)."""
    if request.node.cls is not None and request.node.cls.__name__ == "TestSessionIdFor":
        yield
        return
    with patch.object(mcp_server, "session_id_for", side_effect=lambda ref: ref):
        yield


def _signers(*signers: tuple[str, str, str]) -> None:
    update_global_key(
        "broadcast",
        {
            "enabled": True,
            "signers": [{"name": n, "public_key": k, "role": r} for n, k, r in signers],
        },
    )


class FakeRun:
    """Records argv/cwd and answers like the CLI would."""

    def __init__(self, stdout: object = None, rc: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.stdout = json.dumps(stdout if stdout is not None else {"ok": True})
        self.rc = rc
        self.stderr = stderr

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd))
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]


# -- Read tools --


class TestReadTools:
    def test_list_maps_arguments(self) -> None:
        scope = session_query.Scope(all_projects=True)
        with (
            patch.object(session_query, "resolve_scope", return_value=scope) as rs,
            patch.object(session_query, "run_list") as run,
            patch.object(session_query, "list_json", return_value={"sessions": []}),
        ):
            assert mcp_server.list_sessions("proj", False, True, 5, format="json") == {
                "sessions": []
            }
        rs.assert_called_once_with(
            project="proj", all_projects=False, outside_repo_all=True
        )
        run.assert_called_once_with(
            scope, limit=5, running_only=True, waiting_only=False, states=(), refs=()
        )

    def test_project_and_all_projects_conflict(self) -> None:
        with pytest.raises(FujimotoToolError, match="not both"):
            mcp_server.list_sessions("proj", True)

    def test_query_error_becomes_tool_error(self) -> None:
        with (
            patch.object(
                session_query,
                "resolve_scope",
                side_effect=session_query.QueryError("no project named x"),
            ),
            pytest.raises(FujimotoToolError, match="no project named x"),
        ):
            mcp_server.search_sessions("q", project="x")

    def test_search_maps_arguments(self) -> None:
        scope = session_query.Scope(all_projects=True)
        with (
            patch.object(session_query, "resolve_scope", return_value=scope),
            patch.object(session_query, "run_search") as run,
            patch.object(session_query, "search_json", return_value={"hits": []}),
        ):
            mcp_server.search_sessions("ABC-123", raw=True, limit=3, sort="relevance")
        run.assert_called_once_with(
            "ABC-123",
            scope,
            regex=False,
            case_sensitive=False,
            mode=ContentMode.RAW,
            limit=3,
            sort=session_query.SortOrder.RELEVANCE,
        )

    def test_read_defaults_to_a_tail(self) -> None:
        with (
            patch.object(session_query, "load_log") as load,
            patch.object(session_query, "log_json", return_value={}),
        ):
            mcp_server.read_transcript("8f0e2c1a", format="json")
        assert load.call_args.kwargs["tail"] == mcp_server.DEFAULT_TAIL

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"summary": True},
            {"grep": "x"},
            {"head": 3},
            {"after": 4},
            {"reply_to": "m1"},
        ],
    )
    def test_read_window_given_keeps_everything_else(self, kwargs: dict) -> None:
        with (
            patch.object(session_query, "load_log") as load,
            patch.object(session_query, "log_json", return_value={}),
        ):
            mcp_server.read_transcript("8f0e2c1a", format="json", **kwargs)
        assert load.call_args.kwargs["tail"] is None

    def test_read_rejects_unknown_roles(self) -> None:
        with pytest.raises(FujimotoToolError, match="unknown role"):
            mcp_server.read_transcript("8f0e2c1a", roles=["robot"])

    def test_read_rejects_summary_with_cursor(self) -> None:
        with pytest.raises(FujimotoToolError, match="summary"):
            mcp_server.read_transcript("8f0e2c1a", summary=True, after=3)

    def test_wait_is_capped(self) -> None:
        with (
            patch.object(session_query, "load_log_waiting") as waiting,
            patch.object(session_query, "log_json", return_value={}),
        ):
            mcp_server.read_transcript("8f0e2c1a", after=1, wait=9999, format="json")
        assert waiting.call_args.kwargs["wait"] == mcp_server.MAX_WAIT

    def test_reads_a_real_transcript(self, tmp_path: Path) -> None:
        cwd = tmp_path / "w"
        log = tmp_path / "claude" / "projects" / encode_project_path(cwd) / "conv.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": str(cwd),
                    "timestamp": "2026-10-20T10:00:00Z",
                    "message": {"content": "fix the bug"},
                }
            )
            + "\n"
        )
        with patch.object(
            session_query.ActivityIndex,
            "gather",
            return_value=session_query.ActivityIndex([], {}, {}),
        ):
            data = mcp_server.read_transcript("conv", format="json")
            text = mcp_server.read_transcript("conv")
            cut = mcp_server.read_transcript("conv", fields=["cursor"])
        assert isinstance(data, dict)
        assert [m["text"] for m in data["messages"]] == ["fix the bug"]
        assert isinstance(text, str) and "fix the bug" in text
        assert text.startswith("# session conv")
        assert cut == {"session_id": "conv", "cursor": 0}

    def test_list_text_default_drops_cli_hints(self) -> None:
        with (
            patch.object(session_query, "run_list"),
            patch.object(
                session_query,
                "format_list",
                return_value="● 8f0e2c1a  proj  now  idle\n\n1 session\nNext: fujimoto list",
            ),
        ):
            out = mcp_server.list_sessions(all_projects=True)
        assert out == "● 8f0e2c1a  proj  now  idle\n\n1 session"

    def test_list_filters_and_refs(self) -> None:
        from fujimoto.claude.log_parser import SessionState

        with (
            patch.object(session_query, "resolve_scope") as rs,
            patch.object(session_query, "run_list") as run,
            patch.object(
                session_query, "list_json", return_value={"sessions": []}
            ) as lj,
        ):
            mcp_server.list_sessions(
                project="ignored",
                sessions=["8f0e", "peer-1"],
                waiting=True,
                states=["waiting_for_user"],
                fields=["active.waiting", "title"],
            )
        rs.assert_not_called()  # named sessions are found anywhere
        assert run.call_args.args[0].all_projects
        kw = run.call_args.kwargs
        assert kw["refs"] == ("8f0e", "peer-1") and kw["waiting_only"] is True
        assert kw["states"] == (SessionState.WAITING_FOR_USER,)
        assert lj.call_args.args[1] == ("active.waiting", "title")  # fields → json

    def test_bad_state_and_field(self) -> None:
        with pytest.raises(FujimotoToolError, match="unknown state 'busy'.*working"):
            mcp_server.list_sessions(states=["busy"])
        with pytest.raises(FujimotoToolError, match="unknown field 'nope'"):
            mcp_server.list_sessions(fields=["nope"])
        with pytest.raises(FujimotoToolError, match="unknown field"):
            mcp_server.read_transcript("s", fields=["active.nope"])

    def test_list_ref_error(self) -> None:
        with (
            patch.object(
                session_query,
                "run_list",
                side_effect=session_query.QueryError("no session matches 'zz'"),
            ),
            pytest.raises(FujimotoToolError, match="no session matches"),
        ):
            mcp_server.list_sessions(sessions=["zz"])

    def test_search_text_and_json(self) -> None:
        scope = session_query.Scope(all_projects=True)
        with (
            patch.object(session_query, "resolve_scope", return_value=scope),
            patch.object(session_query, "run_search"),
            patch.object(session_query, "format_search", return_value="hit\nNext: x"),
            patch.object(session_query, "search_json", return_value={"hits": []}),
        ):
            assert mcp_server.search_sessions("q") == "hit"
            assert mcp_server.search_sessions("q", format="json") == {"hits": []}

    def test_listening_and_status(self) -> None:
        _signers(("me", KEY, "user"))
        with patch.object(broadcast, "list_listeners", return_value=[]):
            data = mcp_server.listening_sessions()
        assert data["enabled"] is True and data["listening"] == []
        with patch.object(broadcast, "status", return_value=[]):
            assert mcp_server.message_status("abc") == {"messages": []}

    def test_status_error(self) -> None:
        with (
            patch.object(broadcast, "status", side_effect=OSError("denied")),
            pytest.raises(FujimotoToolError, match="denied"),
        ):
            mcp_server.message_status()


# -- Mutating tools --


class TestCommands:
    def test_start_session_runs_in_project_root(self, tmp_path: Path) -> None:
        run = FakeRun({"tmux_session": "proj/x"})
        scope = session_query.Scope(all_projects=False, project="proj", root=tmp_path)
        with patch.object(session_query, "resolve_scope", return_value=scope):
            out = Commands(run=run).start_session(
                "proj", "fix it", base="develop", prompt="-start here"
            )
        assert out == {"tmux_session": "proj/x"}
        assert run.calls == [
            (
                ["new", "fix it", "--detach", "--base=develop", "--prompt=-start here"],
                tmp_path,
            )
        ]

    def test_start_session_base_conflict(self) -> None:
        with pytest.raises(FujimotoToolError, match="not both"):
            Commands(run=FakeRun()).start_session(
                "p", "t", base="b", current_branch=True
            )

    def test_start_chat(self) -> None:
        run = FakeRun()
        Commands(run=run).start_chat("proj", title="q", scratch=True, prompt="hi")
        assert run.argv == [
            "chat",
            "q",
            "--project=proj",
            "--detach",
            "--in-scratch",
            "--prompt=hi",
        ]
        Commands(run=run).start_chat("proj", worktree="20261020-x")
        assert run.argv == ["chat", "--project=proj", "--detach", "--in=20261020-x"]
        with pytest.raises(FujimotoToolError, match="not both"):
            Commands(run=run).start_chat("proj", worktree="w", scratch=True)

    def test_resume_runs_in_the_sessions_cwd(self, tmp_path: Path) -> None:
        run = FakeRun()
        found = MagicMock(session_id="8f0e2c1a-full", cwd=tmp_path)
        with patch.object(session_query, "resolve_session", return_value=found):
            Commands(run=run).resume_session("8f0e", fork=True, wait=10, prompt="go")
        assert run.calls == [
            (
                [
                    "resume",
                    "8f0e2c1a-full",
                    "--detach",
                    "--fork",
                    "--wait=10",
                    "--prompt=go",
                ],
                tmp_path,
            )
        ]

    def test_resume_missing_cwd_falls_back_to_home(self, tmp_path: Path) -> None:
        run = FakeRun()
        found = MagicMock(session_id="s", cwd=tmp_path / "gone")
        with patch.object(session_query, "resolve_session", return_value=found):
            Commands(run=run).resume_session("s")
        assert run.calls[0][1] == Path.home()

    def test_resume_unknown(self) -> None:
        with (
            patch.object(
                session_query,
                "resolve_session",
                side_effect=session_query.QueryError("no session log"),
            ),
            pytest.raises(FujimotoToolError, match="no session log"),
        ):
            Commands(run=FakeRun()).resume_session("zz")

    def test_stop(self) -> None:
        run = FakeRun({"stopped": True})
        assert Commands(run=run).stop_session("proj/x") == {"stopped": True}
        assert run.argv == ["stop", "proj/x", "--json"]

    def test_send_signs_with_the_key(self) -> None:
        run = FakeRun({"id": "m1"})
        Commands(key=KEY, run=run).send_message("peer-1", "status?", ttl="10m", wait=2)
        assert run.argv == [
            "send",
            "peer-1",
            "status?",
            f"--key={KEY}",
            "--ttl=10m",
            "--wait=2",
            "--json",
        ]

    def test_broadcast_needs_one_scope(self) -> None:
        run = FakeRun()
        c = Commands(key=KEY, run=run)
        with pytest.raises(FujimotoToolError, match="exactly one"):
            c.broadcast("hi")
        with pytest.raises(FujimotoToolError, match="exactly one"):
            c.broadcast("hi", project="p", all_projects=True)
        c.broadcast("hi", project="p")
        assert run.argv[:4] == ["broadcast", "send", "hi", "--project=p"]
        c.broadcast("hi", all_projects=True)
        assert run.argv[3] == "--all-projects"

    @pytest.mark.parametrize("value", ["-rf", "--json", "  "])
    def test_flag_like_positionals_are_refused(self, value: str) -> None:
        with pytest.raises(FujimotoToolError):
            Commands(key=KEY, run=FakeRun()).send_message("s", value)

    def test_cli_error_is_its_last_stderr_line(self) -> None:
        run = FakeRun(rc=1, stderr="noise\nfujimoto: session 8f is not running\n")
        with pytest.raises(FujimotoToolError, match=r"^session 8f is not running$"):
            Commands(run=run).stop_session("8f")

    def test_cli_error_without_stderr(self) -> None:
        with pytest.raises(FujimotoToolError, match="exit 2"):
            Commands(run=FakeRun(rc=2)).stop_session("8f")

    def test_stderr_notes_are_kept(self) -> None:
        run = FakeRun({"a": 1}, stderr="trust this directory first")
        assert (
            Commands(run=run).stop_session("x")["notes"] == "trust this directory first"
        )

    def test_non_json_output(self) -> None:
        run = FakeRun()
        run.stdout = "hello"
        with pytest.raises(FujimotoToolError, match="no JSON"):
            Commands(run=run).stop_session("x")

    def test_list_output_is_wrapped(self) -> None:
        assert Commands(run=FakeRun([1, 2])).stop_session("x") == {"result": [1, 2]}

    @pytest.mark.parametrize(
        ("exc", "match"),
        [
            (subprocess.TimeoutExpired(["x"], 1), "timed out"),
            (OSError("no python"), "could not run"),
        ],
    )
    def test_runner_failures(self, exc: Exception, match: str) -> None:
        def boom(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            raise exc

        with pytest.raises(FujimotoToolError, match=match):
            Commands(run=boom).stop_session("x")

    def test_real_cli_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`python -m fujimoto` really runs, and its error reaches the tool."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "should-not-leak")
        env = mcp_server._child_env()
        assert "CLAUDE_CODE_SESSION_ID" not in env
        with pytest.raises(FujimotoToolError, match="unknown|not|no "):
            Commands().stop_session("definitely-not-a-session-zz")

    def test_main_module(self, capsys: pytest.CaptureFixture[str]) -> None:
        import runpy

        with (
            patch("sys.argv", ["fujimoto", "--version"]),
            pytest.raises(SystemExit) as exc,
        ):
            runpy.run_module("fujimoto", run_name="__main__")
        assert exc.value.code == 0
        assert capsys.readouterr().out.startswith("fujimoto ")


# -- Key check and server --


class TestCheckKey:
    def test_agent_key_is_normalized(self) -> None:
        _signers(("claude-desktop", KEY, "agent"))
        assert mcp_server.check_key(KEY + " comment") == KEY

    def test_user_key_is_refused(self) -> None:
        _signers(("laptop", KEY, "user"))
        with pytest.raises(FujimotoToolError, match="user signer 'laptop'"):
            mcp_server.check_key(KEY)

    def test_garbage(self) -> None:
        with pytest.raises(FujimotoToolError, match="--key"):
            mcp_server.check_key("not a key")


def _tools(server) -> list[str]:
    import anyio

    return [t.name for t in anyio.run(server.list_tools)]


class TestServer:
    def test_without_key_no_messaging(self) -> None:
        names = _tools(mcp_server.build_server())
        assert "send_message" not in names and "broadcast" not in names
        assert {"list_sessions", "read_transcript", "stop_session"} <= set(names)

    def test_with_key_messaging(self) -> None:
        names = _tools(mcp_server.build_server(KEY))
        assert {"send_message", "broadcast"} <= set(names)

    def test_annotations(self) -> None:
        import anyio

        tools = {t.name: t for t in anyio.run(mcp_server.build_server(KEY).list_tools)}
        assert tools["list_sessions"].annotations.read_only_hint is True
        assert tools["stop_session"].annotations.destructive_hint is True
        assert tools["send_message"].annotations.read_only_hint is False

    def _call(self, server, name: str, args: dict):
        import anyio

        async def go():
            return await server.call_tool(name, args)

        return anyio.run(go)

    def test_every_tool_reaches_its_command(self) -> None:
        run = FakeRun({"ok": 1})
        server = mcp_server.build_server(KEY, run=run)
        scope = session_query.Scope(all_projects=False, project="p", root=Path.home())
        found = MagicMock(session_id="s", cwd=Path.home())
        with (
            patch.object(session_query, "resolve_scope", return_value=scope),
            patch.object(session_query, "resolve_session", return_value=found),
        ):
            for name, args, first in [
                ("start_session", {"project": "p", "title": "t"}, "new"),
                ("start_chat", {"project": "p"}, "chat"),
                ("resume_session", {"session": "s"}, "resume"),
                ("stop_session", {"session": "s"}, "stop"),
                ("send_message", {"session": "s", "text": "hi"}, "send"),
                ("broadcast", {"text": "hi", "all_projects": True}, "broadcast"),
            ]:
                result = self._call(server, name, args)
                assert not result.is_error, (name, result)
                assert run.argv[0] == first

    def test_read_tools_through_the_server(self) -> None:
        server = mcp_server.build_server()
        with (
            patch.object(mcp_server, "list_sessions", return_value={"sessions": []}),
            patch.object(mcp_server, "search_sessions", return_value={"hits": []}),
            patch.object(mcp_server, "read_transcript", return_value={"messages": []}),
            patch.object(
                mcp_server, "listening_sessions", return_value={"listening": []}
            ),
            patch.object(mcp_server, "message_status", return_value={"messages": []}),
        ):
            for name, args in [
                ("list_sessions", {}),
                ("search_sessions", {"query": "x"}),
                ("read_transcript", {"session": "s"}),
                ("listening_sessions", {}),
                ("message_status", {}),
            ]:
                result = self._call(server, name, args)
                assert not result.is_error, (name, result)

    def test_tool_error_is_reported(self) -> None:
        # call_tool raises; the protocol layer turns it into an is_error result.
        from mcp.server.mcpserver.exceptions import ToolError

        server = mcp_server.build_server()
        with pytest.raises(ToolError, match="unknown role"):
            self._call(server, "read_transcript", {"session": "s", "roles": ["x"]})


class TestServe:
    def test_missing_extra(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.dict(sys.modules, {"mcp": None}):
            assert mcp_server.serve(None) == 1
        assert "fujimoto[mcp]" in capsys.readouterr().err

    def test_bad_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        _signers(("laptop", KEY, "user"))
        assert mcp_server.serve(KEY) == 1
        assert "user signer" in capsys.readouterr().err

    def test_runs_stdio(self) -> None:
        _signers(("claude-desktop", OTHER, "agent"))
        server = MagicMock()
        with patch.object(mcp_server, "build_server", return_value=server) as build:
            assert mcp_server.serve(OTHER) == 0
        build.assert_called_once_with(OTHER)
        server.run.assert_called_once_with("stdio")


def test_messaging_docs_explain_launch_time_trust() -> None:
    """A rejection from a pre-key session must not read as a signing failure."""
    import anyio

    tools = {t.name: t for t in anyio.run(mcp_server.build_server(KEY).list_tools)}
    for name in ("send_message", "broadcast"):
        assert "launched" in (tools[name].description or "")
    assert "not signed by a configured signer" in mcp_server.INSTRUCTIONS


class TestSessionIdFor:
    def test_id_or_prefix_goes_straight_through(self, tmp_path: Path) -> None:
        with patch.object(
            session_query, "find_session_log", return_value=tmp_path / "abcd-1234.jsonl"
        ):
            assert mcp_server.session_id_for("abcd") == "abcd-1234"

    def test_peer_name_resolves_through_list(self) -> None:
        listed = MagicMock()
        listed.session.session_id = "full-id"
        report = MagicMock(sessions=(listed,))
        with (
            patch.object(
                session_query,
                "find_session_log",
                side_effect=session_query.QueryError(
                    "no session log found for 'peer-1'"
                ),
            ),
            patch.object(session_query, "run_list", return_value=report) as run,
        ):
            assert mcp_server.session_id_for("peer-1") == "full-id"
        assert run.call_args.kwargs["refs"] == ("peer-1",)

    def test_unknown_keeps_the_original_error(self) -> None:
        with (
            patch.object(
                session_query,
                "find_session_log",
                side_effect=session_query.QueryError("no session log found for 'zz'"),
            ),
            patch.object(
                session_query,
                "run_list",
                side_effect=session_query.QueryError("unknown session 'zz'"),
            ),
            pytest.raises(FujimotoToolError, match="no session log found for 'zz'"),
        ):
            mcp_server.session_id_for("zz")
