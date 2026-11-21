from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import debug, filelock


@pytest.fixture
def debug_log(tmp_path: Path):
    """Turn debug logging on into `tmp_path`, and return a reader for it."""
    debug.enable(redact=False, log_dir=tmp_path / "logs")

    def read() -> str:
        return "".join(p.read_text() for p in (tmp_path / "logs").glob("*.log"))

    yield read
    debug.disable()


class TestLocked:
    def test_holder_is_recorded_while_held_and_cleared_after(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "sessions.json"
        with filelock.locked(state) as acquired:
            assert acquired is True
            holder = filelock.read_holder(state)
            assert holder is not None
            assert holder["pid"] == os.getpid()
            assert holder["since"]
            assert holder["command"]
        assert filelock.read_holder(state) is None

    def test_contended_lock_times_out_and_names_the_holder(
        self, tmp_path: Path, debug_log
    ) -> None:
        state = tmp_path / "sessions.json"
        with filelock.locked(state):
            # A second open file description conflicts, as another process would.
            with filelock.locked(state, timeout=0.05) as acquired:
                assert acquired is False
        log = debug_log()
        assert "filelock.timeout" in log
        assert f"held_by_pid={os.getpid()}" in log

    def test_waiting_is_logged(self, tmp_path: Path, debug_log) -> None:
        state = tmp_path / "sessions.json"
        real_flock = filelock.fcntl.flock
        calls = {"n": 0}

        def busy_twice(fd: int, op: int) -> None:
            if op & filelock.fcntl.LOCK_EX and calls["n"] < 2:
                calls["n"] += 1
                raise BlockingIOError
            real_flock(fd, op)

        with patch("fujimoto.filelock.fcntl.flock", side_effect=busy_twice):
            with filelock.locked(state) as acquired:
                assert acquired is True
        assert "filelock.waited" in debug_log()

    def test_unopenable_lock_runs_unlocked(self, tmp_path: Path, debug_log) -> None:
        with patch("fujimoto.filelock.os.open", side_effect=PermissionError("ro")):
            with filelock.locked(tmp_path / "s.json") as acquired:
                assert acquired is False
        assert "filelock.unavailable" in debug_log()

    def test_timeout_with_unreadable_holder(self, tmp_path: Path, debug_log) -> None:
        state = tmp_path / "sessions.json"
        with filelock.locked(state):
            with patch("fujimoto.filelock.read_holder", return_value=None):
                with filelock.locked(state, timeout=0.01) as acquired:
                    assert acquired is False
        assert "held_by_pid=unknown" in debug_log()


class TestReadHolder:
    @pytest.mark.parametrize("content", ["", "not json", "[1, 2]"])
    def test_garbage_is_none(self, tmp_path: Path, content: str) -> None:
        state = tmp_path / "s.json"
        filelock.lock_path(state).write_text(content)
        assert filelock.read_holder(state) is None

    def test_missing_is_none(self, tmp_path: Path) -> None:
        assert filelock.read_holder(tmp_path / "s.json") is None


class TestAtomicWrite:
    def test_writes_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "s.json"
        filelock.atomic_write(target, "{}")
        assert target.read_text() == "{}"
        assert [p.name for p in target.parent.iterdir()] == ["s.json"]

    def test_failed_rename_removes_the_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "s.json"
        target.write_text("old")
        with patch("fujimoto.filelock.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                filelock.atomic_write(target, "new")
        assert target.read_text() == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


_WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from fujimoto import launch_options, session_state

    worker, count = sys.argv[1], int(sys.argv[2])
    for i in range(count):
        session_state.mark_open(
            f"p/{worker}-{i}", cwd=Path("/r"), project="p", session_type="chat"
        )
        launch_options.remember_for_conversation(f"{worker}-{i}", ("--x",))
    """
)


def test_concurrent_writers_lose_nothing(tmp_path: Path) -> None:
    """Several real processes updating both stores at once.

    Without the lock, overlapping read-change-write cycles drop records.
    """
    workers, count = 4, 25
    env = {**os.environ, "HOME": str(tmp_path)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, f"w{n}", str(count)],
            env=env,
            stderr=subprocess.PIPE,
        )
        for n in range(workers)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode()

    with patch("pathlib.Path.home", return_value=tmp_path):
        from fujimoto import launch_options, session_state

        names = set(session_state.load_state())
        cached = launch_options._load_cache()
    expected = {f"w{n}-{i}" for n in range(workers) for i in range(count)}
    assert names == {f"p/{e}" for e in expected}
    assert set(cached) == expected
