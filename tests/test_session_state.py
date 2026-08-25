from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import session_state


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path):
    """Keep every test off the real ~/.cache/fujimoto/sessions.json.

    Redirects HOME rather than `_state_path` itself, so the path construction
    stays under test instead of being stubbed out.
    """
    with patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path / ".cache" / "fujimoto" / "sessions.json"


class TestLoadState:
    def test_missing_file_is_empty(self) -> None:
        assert session_state.load_state() == {}

    def test_corrupt_json_is_empty(self, _isolate_state: Path) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text("{not json")
        assert session_state.load_state() == {}

    def test_non_dict_payload_is_empty(self, _isolate_state: Path) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text("[1, 2, 3]")
        assert session_state.load_state() == {}

    def test_skips_malformed_entries(self, _isolate_state: Path) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(
            json.dumps(
                {
                    "good": {"cwd": "/tmp/a", "project": "p", "session_type": "direct"},
                    "no-cwd": {"project": "p"},
                    "not-a-dict": "nope",
                }
            )
        )
        state = session_state.load_state()
        assert set(state) == {"good"}

    def test_ignores_unknown_keys(self, _isolate_state: Path) -> None:
        # Forward compatibility: a newer fujimoto's extra fields must not crash
        # an older one.
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(
            json.dumps({"s": {"cwd": "/tmp/a", "project": "p", "future_key": 1}})
        )
        assert session_state.load_state()["s"].cwd == "/tmp/a"

    def test_unreadable_cache_is_empty(self, _isolate_state: Path) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text("{}")
        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            assert session_state.load_state() == {}


class TestSaveState:
    def test_swallows_os_error(self) -> None:
        with patch.object(Path, "mkdir", side_effect=OSError("readonly")):
            session_state.save_state({})  # must not raise


class TestMarkOpen:
    def test_round_trip(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt",
            cwd=tmp_path,
            project="proj",
            session_type="worktree",
            branch="worktree/wt",
            claude_session_id="abc",
        )
        record = session_state.load_state()["proj/wt"]
        assert record.cwd == str(tmp_path)
        assert record.project == "proj"
        assert record.session_type == "worktree"
        assert record.branch == "worktree/wt"
        assert record.claude_session_id == "abc"
        assert record.last_seen
        assert record.path == tmp_path

    def test_reconnect_keeps_known_claude_id(self, tmp_path: Path) -> None:
        # A plain reconnect passes no resume id; it must not blank out the id
        # recorded when the session was first launched.
        session_state.mark_open(
            "proj/wt",
            cwd=tmp_path,
            project="proj",
            session_type="worktree",
            claude_session_id="abc",
        )
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        assert session_state.load_state()["proj/wt"].claude_session_id == "abc"

    def test_new_id_overwrites(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt",
            cwd=tmp_path,
            project="proj",
            session_type="worktree",
            claude_session_id="abc",
        )
        session_state.mark_open(
            "proj/wt",
            cwd=tmp_path,
            project="proj",
            session_type="worktree",
            claude_session_id="def",
        )
        assert session_state.load_state()["proj/wt"].claude_session_id == "def"


class TestMarkClosed:
    def test_removes_record(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_closed("proj/wt")
        assert session_state.load_state() == {}

    def test_unknown_name_is_noop(self) -> None:
        session_state.mark_closed("proj/nope")
        assert session_state.load_state() == {}

    def test_leaves_other_records(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/a", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_open(
            "proj/b", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_closed("proj/a")
        assert set(session_state.load_state()) == {"proj/b"}


class TestTouch:
    def test_keeps_record_open(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.touch("proj/wt")
        assert "proj/wt" in session_state.load_state()

    def test_records_claude_id(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.touch("proj/wt", "xyz")
        assert session_state.load_state()["proj/wt"].claude_session_id == "xyz"

    def test_unknown_name_is_noop(self) -> None:
        session_state.touch("proj/nope")
        assert session_state.load_state() == {}


class TestRename:
    def test_moves_record(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/old", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.rename("proj/old", "proj/new")
        state = session_state.load_state()
        assert set(state) == {"proj/new"}
        assert state["proj/new"].cwd == str(tmp_path)

    def test_unknown_name_is_noop(self) -> None:
        session_state.rename("proj/nope", "proj/new")
        assert session_state.load_state() == {}


class TestPrune:
    def test_drops_records_whose_directory_is_gone(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        session_state.mark_open(
            "proj/live", cwd=live, project="proj", session_type="worktree"
        )
        session_state.mark_open(
            "proj/dead",
            cwd=tmp_path / "dead",
            project="proj",
            session_type="worktree",
        )
        assert set(session_state.prune()) == {"proj/live"}
        # And the drop is persisted, not just filtered on the way out.
        assert set(session_state.load_state()) == {"proj/live"}

    def test_no_write_when_nothing_to_drop(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/live", cwd=tmp_path, project="proj", session_type="worktree"
        )
        with patch("fujimoto.session_state.save_state") as save:
            assert set(session_state.prune()) == {"proj/live"}
        save.assert_not_called()


class TestStatePath:
    def test_lives_in_the_shared_fujimoto_cache_dir(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        assert (
            session_state._state_path()
            == tmp_path / ".cache" / "fujimoto" / "sessions.json"
        )
