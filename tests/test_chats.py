from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import chats


@pytest.fixture(autouse=True)
def _home(tmp_path: Path):
    with patch("fujimoto.chats.Path.home", return_value=tmp_path):
        yield tmp_path


def _open(name: str = "p/ask", cwd: Path | None = None, **kw: object) -> None:
    chats.record_open(
        name, project="p", cwd=cwd or Path("/r"), claude_session_id=None, **kw
    )


class TestLifecycle:
    def test_open_writes_one_file_per_chat(self, _home: Path) -> None:
        chats.record_open(
            "p/ask",
            project="p",
            cwd=Path("/r"),
            claude_session_id="c1",
            parent_session="boss",
        )
        path = _home / ".cache" / "fujimoto" / "chats" / "p" / "ask.json"
        data = json.loads(path.read_text())
        assert data["name"] == "p/ask"
        assert data["claude_session_id"] == "c1"
        assert data["parent_session"] == "boss"
        assert data["created"] and not data["closed"]
        # Written via a temp file and a rename: nothing left behind.
        assert [p.name for p in path.parent.iterdir()] == ["ask.json"]

    def test_reopen_keeps_id_parent_and_created(self) -> None:
        chats.record_open(
            "p/ask",
            project="p",
            cwd=Path("/r"),
            claude_session_id="c1",
            parent_session="boss",
        )
        first = chats.get("p/ask", "p")
        assert first is not None
        chats.mark_closed("p/ask", "p", "c2")
        _open()
        meta = chats.get("p/ask", "p")
        assert meta is not None
        assert meta.claude_session_id == "c2"
        assert meta.parent_session == "boss"
        assert meta.created == first.created
        assert meta.closed == ""

    def test_mark_closed(self) -> None:
        _open()
        assert chats.mark_closed("p/ask", "p", None) is True
        meta = chats.get("p/ask", "p")
        assert meta is not None and meta.closed
        assert chats.mark_closed("p/nope", "p", None) is False

    def test_remove(self) -> None:
        _open()
        chats.remove("p/ask", "p")
        assert chats.get("p/ask", "p") is None
        chats.remove("p/ask", "p")  # already gone: fine

    def test_rename(self) -> None:
        _open()
        chats.rename("p/ask", "p/better", "p")
        assert chats.get("p/ask", "p") is None
        meta = chats.get("p/better", "p")
        assert meta is not None and meta.slug == "better"
        chats.rename("p/not-a-chat", "p/x", "p")  # no-op
        assert chats.get("p/x", "p") is None


class TestListing:
    def test_list_and_prune(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        _open("p/a", cwd=live)
        _open("p/b", cwd=tmp_path / "gone")
        assert {m.name for m in chats.list_chats("p")} == {"p/a", "p/b"}
        assert [m.name for m in chats.prune("p")] == ["p/a"]
        assert chats.get("p/b", "p") is None

    def test_unknown_project_is_empty(self) -> None:
        assert chats.list_chats("nothing") == []

    @pytest.mark.parametrize(
        "content", ["{nope", "[]", json.dumps({"name": "p/x", "project": "p"})]
    )
    def test_malformed_files_are_skipped(self, _home: Path, content: str) -> None:
        directory = _home / ".cache" / "fujimoto" / "chats" / "p"
        directory.mkdir(parents=True)
        (directory / "bad.json").write_text(content)
        assert chats.list_chats("p") == []

    def test_find_by_conversation_across_projects(self) -> None:
        chats.record_open("q/ask", project="q", cwd=Path("/r"), claude_session_id="c9")
        found = chats.find_by_conversation("c9")
        assert found is not None and found.name == "q/ask"
        assert chats.find_by_conversation("nope") is None

    def test_find_with_no_chats_at_all(self) -> None:
        assert chats.find_by_conversation("c9") is None


def test_write_failure_is_swallowed(_home: Path) -> None:
    with patch("fujimoto.chats.tempfile.mkstemp", side_effect=OSError("ro")):
        _open()
    assert chats.get("p/ask", "p") is None


def test_remove_failure_is_swallowed() -> None:
    _open()
    with patch("pathlib.Path.unlink", side_effect=PermissionError("no")):
        chats.remove("p/ask", "p")
    assert chats.get("p/ask", "p") is not None
