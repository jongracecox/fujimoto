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

    def test_tolerates_a_non_string_created(self, _isolate_state: Path) -> None:
        # A record predating `created`, and a hand-edited one with the wrong
        # type, both have to load — just without an ordering stamp.
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(
            json.dumps(
                {
                    "old": {"cwd": "/tmp/a"},
                    "bad": {"cwd": "/tmp/b", "created": 17},
                }
            )
        )
        state = session_state.load_state()
        assert state["old"].created == ""
        assert state["bad"].created == ""

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
        assert record.created
        assert record.path == tmp_path

    def test_created_survives_a_reconnect(self, tmp_path: Path) -> None:
        # Reopening a session is not creating one: the stamp the home screen
        # orders by must not jump to now.
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        first = session_state.load_state()["proj/wt"].created
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        assert session_state.load_state()["proj/wt"].created == first

    def test_backfill_uses_the_worktree_directory_age(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        """A legacy record must not claim it was created at upgrade time.

        Stamping `_now()` here made every months-old session leap to the top of
        the home screen the first time it was relaunched.
        """
        wt = tmp_path / "20260901-old-work"
        wt.mkdir()
        _isolate_state.parent.mkdir(parents=True, exist_ok=True)
        _isolate_state.write_text(
            json.dumps(
                {"proj/wt": {"cwd": str(wt), "last_seen": "2026-09-02T00:00:00+00:00"}}
            )
        )
        session_state.mark_open(
            "proj/wt", cwd=wt, project="proj", session_type="worktree"
        )
        created = session_state.load_state()["proj/wt"].created
        expected = session_state._directory_created(wt)
        assert created == expected
        assert created != ""
        # And emphatically not "now".
        assert created < session_state._now()

    def test_backfill_uses_last_seen_for_a_direct_session(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        """A direct session's cwd is the repo, whose age is the clone's."""
        _isolate_state.parent.mkdir(parents=True, exist_ok=True)
        _isolate_state.write_text(
            json.dumps(
                {
                    "proj/direct-1": {
                        "cwd": str(tmp_path),
                        "last_seen": "2026-09-02T00:00:00+00:00",
                    }
                }
            )
        )
        session_state.mark_open(
            "proj/direct-1", cwd=tmp_path, project="proj", session_type="direct"
        )
        record = session_state.load_state()["proj/direct-1"]
        assert record.created == "2026-09-02T00:00:00+00:00"

    def test_backfill_falls_back_to_now_with_nothing_to_go_on(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        _isolate_state.parent.mkdir(parents=True, exist_ok=True)
        _isolate_state.write_text(json.dumps({"proj/direct-1": {"cwd": str(tmp_path)}}))
        session_state.mark_open(
            "proj/direct-1", cwd=tmp_path, project="proj", session_type="direct"
        )
        assert session_state.load_state()["proj/direct-1"].created

    def test_backfill_of_a_worktree_with_no_directory_uses_last_seen(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        gone = tmp_path / "deleted"
        _isolate_state.parent.mkdir(parents=True, exist_ok=True)
        _isolate_state.write_text(
            json.dumps(
                {
                    "proj/wt": {
                        "cwd": str(gone),
                        "last_seen": "2026-09-02T00:00:00+00:00",
                    }
                }
            )
        )
        session_state.mark_open(
            "proj/wt", cwd=gone, project="proj", session_type="worktree"
        )
        assert (
            session_state.load_state()["proj/wt"].created == "2026-09-02T00:00:00+00:00"
        )

    def test_created_is_backfilled_for_an_older_record(
        self, _isolate_state: Path, tmp_path: Path
    ) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(json.dumps({"proj/wt": {"cwd": str(tmp_path)}}))
        assert session_state.load_state()["proj/wt"].created == ""
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        assert session_state.load_state()["proj/wt"].created

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


class TestMarkStopped:
    def test_keeps_record_open(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_stopped("proj/wt")
        assert "proj/wt" in session_state.load_state()

    def test_records_claude_id(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_stopped("proj/wt", "xyz")
        assert session_state.load_state()["proj/wt"].claude_session_id == "xyz"

    def test_stamps_the_kind_so_it_is_not_taken_for_a_crash(
        self, tmp_path: Path
    ) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        assert (
            session_state.load_state()["proj/wt"].stop_kind
            is session_state.StopKind.RECOVERED
        )
        session_state.mark_stopped("proj/wt")
        assert (
            session_state.load_state()["proj/wt"].stop_kind
            is session_state.StopKind.STOPPED
        )

    def test_the_latest_decision_wins(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_stopped("proj/wt", kind=session_state.StopKind.PARKED)
        session_state.mark_stopped("proj/wt")
        assert (
            session_state.load_state()["proj/wt"].stop_kind
            is session_state.StopKind.STOPPED
        )

    def test_unknown_name_is_noop(self) -> None:
        session_state.mark_stopped("proj/nope")
        assert session_state.load_state() == {}


class TestStopKind:
    def test_absent_field_reads_as_recovered(self, _isolate_state: Path) -> None:
        # Both an older fujimoto's records and a session nothing ever stopped.
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(json.dumps({"s": {"cwd": "/tmp/a"}}))
        assert session_state.load_state()["s"].stop_kind is (
            session_state.StopKind.RECOVERED
        )

    def test_legacy_parked_flag_is_honoured(self, _isolate_state: Path) -> None:
        # An upgrade must not re-label something shelved on purpose as a crash.
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(json.dumps({"s": {"cwd": "/tmp/a", "parked": True}}))
        assert session_state.load_state()["s"].stop_kind is (
            session_state.StopKind.PARKED
        )

    def test_nonsense_value_reads_as_recovered(self, _isolate_state: Path) -> None:
        _isolate_state.parent.mkdir(parents=True)
        _isolate_state.write_text(
            json.dumps(
                {
                    "s": {"cwd": "/tmp/a", "stop_kind": "banana"},
                    "t": {"cwd": "/tmp/b", "stop_kind": 7},
                }
            )
        )
        state = session_state.load_state()
        assert state["s"].stop_kind is session_state.StopKind.RECOVERED
        assert state["t"].stop_kind is session_state.StopKind.RECOVERED

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        session_state.mark_open(
            "proj/wt", cwd=tmp_path, project="proj", session_type="worktree"
        )
        session_state.mark_stopped("proj/wt", kind=session_state.StopKind.PARKED)
        assert (
            session_state.load_state()["proj/wt"].stop_kind
            is session_state.StopKind.PARKED
        )


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
