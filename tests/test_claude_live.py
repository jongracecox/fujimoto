from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fujimoto.claude import live


def _write(directory: Path, name: str, data: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data))
    return path


def _entry(pid: int, session_id: str = "sess-1", **extra: object) -> dict:
    return {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/repo",
        "name": "repo-ab",
        "status": "busy",
        "tmux": "proj/20261020-x:@3.%7",
        **extra,
    }


class TestSessionsDir:
    def test_sits_next_to_projects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere/claude")
        assert live.get_claude_sessions_dir() == Path("/elsewhere/claude/sessions")


class TestTmuxSessionFromPane:
    def test_parses_session_name(self) -> None:
        assert live.tmux_session_from_pane("a/b-c:@12.%34") == "a/b-c"

    @pytest.mark.parametrize("value", [None, 7, "", "a/b", "a:@x.%1"])
    def test_rejects_anything_else(self, value: object) -> None:
        assert live.tmux_session_from_pane(value) is None


class TestPidAlive:
    def test_own_pid_is_alive(self) -> None:
        assert live.pid_alive(os.getpid())

    def test_non_positive_is_dead(self) -> None:
        assert not live.pid_alive(0)
        assert not live.pid_alive(-1)

    def test_missing_process_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(live.os, "kill", _raise)
        assert not live.pid_alive(12345)

    def test_someone_elses_process_is_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(live.os, "kill", _raise)
        assert live.pid_alive(1)


class TestListLiveSessions:
    def test_reads_a_live_entry(self, tmp_path: Path) -> None:
        _write(tmp_path, "1.json", _entry(os.getpid()))
        [entry] = live.list_live_sessions(tmp_path)
        assert entry == live.LiveSession(
            pid=os.getpid(),
            session_id="sess-1",
            cwd=Path("/repo"),
            peer_name="repo-ab",
            status="busy",
            tmux_session="proj/20261020-x",
        )

    def test_optional_fields_may_be_missing(self, tmp_path: Path) -> None:
        _write(tmp_path, "1.json", {"pid": os.getpid(), "sessionId": "s"})
        [entry] = live.list_live_sessions(tmp_path)
        assert entry.cwd is None
        assert entry.peer_name is None
        assert entry.status is None
        assert entry.tmux_session is None

    def test_skips_stale_and_malformed_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live, "pid_alive", lambda pid: pid == 1)
        _write(tmp_path, "1.json", _entry(1, "alive"))
        _write(tmp_path, "2.json", _entry(2, "stale"))
        _write(tmp_path, "3.json", ["not", "a", "dict"])
        _write(tmp_path, "4.json", {"pid": "x", "sessionId": "s"})
        _write(tmp_path, "5.json", {"pid": 1, "sessionId": ""})
        (tmp_path / "6.json").write_text("{not json")
        _write(tmp_path, "7.key", {"ignored": True})
        assert [e.session_id for e in live.list_live_sessions(tmp_path)] == ["alive"]

    def test_missing_directory_is_empty(self, tmp_path: Path) -> None:
        assert live.list_live_sessions(tmp_path / "absent") == []

    def test_defaults_to_claude_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write(tmp_path / "sessions", "1.json", _entry(os.getpid()))
        assert len(live.list_live_sessions()) == 1
