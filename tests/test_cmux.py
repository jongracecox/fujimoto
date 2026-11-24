from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fujimoto import cmux, debug


@pytest.fixture
def in_cmux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A cmux pane with a bundled CLI and fujimoto found on PATH."""
    cli = tmp_path / "cmux"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SURFACE-1")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(cli))
    monkeypatch.setattr(
        "fujimoto.cmux.fujimoto_executable", lambda: "/opt/bin/fujimoto"
    )
    monkeypatch.chdir(tmp_path)
    return cli


@pytest.fixture
def debug_log(tmp_path: Path) -> Iterator[Path]:
    log_dir = tmp_path / "logs"
    debug.enable(redact=False, log_dir=log_dir)
    yield log_dir
    debug.disable()


def _log_text(log_dir: Path) -> str:
    return next(log_dir.glob("*.log")).read_text()


class TestWithoutSurfaceScope:
    def test_strips_only_the_pane_scoped_keys(self) -> None:
        env = {
            "CMUX_SURFACE_ID": "s",
            "CMUX_PANEL_ID": "p",
            "CMUX_HISTORY_FILE": "h",
            "CMUX_WORKSPACE_ID": "w",
            "PATH": "/bin",
        }
        assert cmux.without_surface_scope(env) == {
            "CMUX_WORKSPACE_ID": "w",
            "PATH": "/bin",
        }

    def test_defaults_to_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CMUX_SURFACE_ID", "s")
        monkeypatch.setenv("FJ_TEST_KEEP", "1")
        env = cmux.without_surface_scope()
        assert "CMUX_SURFACE_ID" not in env
        assert env["FJ_TEST_KEEP"] == "1"


class TestSurfaceId:
    def test_outside_cmux(self) -> None:
        assert cmux.surface_id() is None

    def test_empty_counts_as_outside(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMUX_SURFACE_ID", "")
        assert cmux.surface_id() is None

    def test_inside_cmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMUX_SURFACE_ID", "SURFACE-1")
        assert cmux.surface_id() == "SURFACE-1"


class TestCli:
    def test_prefers_the_bundled_cli(self, in_cmux: Path) -> None:
        assert cmux._cli() == str(in_cmux)

    def test_falls_back_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(tmp_path / "missing"))
        with patch("fujimoto.cmux.shutil.which", return_value="/usr/bin/cmux"):
            assert cmux._cli() == "/usr/bin/cmux"

    def test_none_when_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CMUX_BUNDLED_CLI_PATH", raising=False)
        with patch("fujimoto.cmux.shutil.which", return_value=None):
            assert cmux._cli() is None


class TestResumeCommand:
    def test_reattaches_through_fujimoto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fujimoto.cmux.fujimoto_executable", lambda: "/opt/bin/fujimoto"
        )
        assert cmux.resume_command("proj/wt") == "/opt/bin/fujimoto --attach proj/wt"

    def test_quotes_for_the_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fujimoto.cmux.fujimoto_executable", lambda: "/my apps/fujimoto"
        )
        assert cmux.resume_command("proj/wt") == "'/my apps/fujimoto' --attach proj/wt"

    def test_none_without_fujimoto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fujimoto.cmux.fujimoto_executable", lambda: None)
        assert cmux.resume_command("proj/wt") is None


class TestSetResumeBinding:
    def test_outside_cmux_does_nothing(self, debug_log: Path) -> None:
        with patch("fujimoto.cmux.subprocess.run") as run:
            assert cmux.set_resume_binding("proj/wt") is False
        run.assert_not_called()
        assert "result=not-cmux" in _log_text(debug_log)

    def test_binds_the_pane(self, in_cmux: Path, tmp_path: Path) -> None:
        with patch(
            "fujimoto.cmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as run:
            assert cmux.set_resume_binding("proj/wt") is True
        run.assert_called_once()
        assert run.call_args.args[0] == [
            str(in_cmux),
            "surface",
            "resume",
            "set",
            "--surface",
            "SURFACE-1",
            "--kind",
            "fujimoto",
            "--source",
            "fujimoto",
            "--name",
            "proj/wt",
            "--checkpoint",
            "proj/wt",
            "--cwd",
            str(Path.cwd()),
            "--shell",
            "/opt/bin/fujimoto --attach proj/wt",
        ]
        assert run.call_args.kwargs["timeout"] == cmux.CLI_TIMEOUT_SECONDS

    def test_unavailable_cli(
        self, in_cmux: Path, monkeypatch: pytest.MonkeyPatch, debug_log: Path
    ) -> None:
        monkeypatch.setattr("fujimoto.cmux._cli", lambda: None)
        with patch("fujimoto.cmux.subprocess.run") as run:
            assert cmux.set_resume_binding("proj/wt") is False
        run.assert_not_called()
        log = _log_text(debug_log)
        assert "result=unavailable" in log
        assert "cli=False" in log

    def test_unavailable_fujimoto(
        self, in_cmux: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("fujimoto.cmux.fujimoto_executable", lambda: None)
        with patch("fujimoto.cmux.subprocess.run") as run:
            assert cmux.set_resume_binding("proj/wt") is False
        run.assert_not_called()

    def test_rejected_by_cmux(self, in_cmux: Path, debug_log: Path) -> None:
        with patch(
            "fujimoto.cmux.subprocess.run",
            return_value=MagicMock(returncode=1, stderr="surface not found\n"),
        ):
            assert cmux.set_resume_binding("proj/wt") is False
        log = _log_text(debug_log)
        assert "result=failed" in log
        assert "rc=1" in log
        assert "surface not found" in log

    @pytest.mark.parametrize(
        "error", [OSError("gone"), subprocess.TimeoutExpired("cmux", 5)]
    )
    def test_cli_failure_never_raises(
        self, in_cmux: Path, error: Exception, debug_log: Path
    ) -> None:
        with patch("fujimoto.cmux.subprocess.run", side_effect=error):
            assert cmux.set_resume_binding("proj/wt") is False
        assert "cmux.cli_failed" in _log_text(debug_log)

    def test_deleted_launch_directory_falls_back_to_home(self, in_cmux: Path) -> None:
        with (
            patch("fujimoto.cmux.Path.cwd", side_effect=FileNotFoundError()),
            patch(
                "fujimoto.cmux.subprocess.run", return_value=MagicMock(returncode=0)
            ) as run,
        ):
            assert cmux.set_resume_binding("proj/wt") is True
        args = run.call_args.args[0]
        assert args[args.index("--cwd") + 1] == str(Path.home())


class TestClearResumeBinding:
    def test_clears_this_pane(self, in_cmux: Path, debug_log: Path) -> None:
        with patch(
            "fujimoto.cmux.subprocess.run", return_value=MagicMock(returncode=0)
        ) as run:
            cmux.clear_resume_binding("proj/wt")
        assert run.call_args.args[0] == [
            str(in_cmux),
            "surface",
            "resume",
            "clear",
            "--surface",
            "SURFACE-1",
        ]
        assert "cmux.resume_clear" in _log_text(debug_log)

    def test_outside_cmux_does_nothing(self) -> None:
        with patch("fujimoto.cmux.subprocess.run") as run:
            cmux.clear_resume_binding("proj/wt")
        run.assert_not_called()

    def test_failure_never_raises(self, in_cmux: Path) -> None:
        with patch("fujimoto.cmux.subprocess.run", side_effect=OSError("gone")):
            cmux.clear_resume_binding("proj/wt")  # no raise
