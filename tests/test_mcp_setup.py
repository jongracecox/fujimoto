from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import broadcast, debug, mcp_setup
from fujimoto.mcp_setup import FAIL, OK, WARN, check_setup


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    debug.disable()


@pytest.fixture(autouse=True)
def _mcp_installed():
    """Report the `mcp` extra as installed, whatever this environment has."""
    real = importlib.util.find_spec
    with patch(
        "importlib.util.find_spec",
        side_effect=lambda name, *a: object() if name == "mcp" else real(name, *a),
    ):
        yield


def _keypair(tmp_path: Path, name: str, passphrase: str = "") -> tuple[Path, str]:
    """A real ed25519 key pair; returns (public path, normalized public key)."""
    priv = tmp_path / name
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            passphrase,
            "-C",
            "t",
            "-f",
            str(priv),
        ],
        check=True,
        capture_output=True,
    )
    pub = priv.with_suffix(".pub")
    return pub, broadcast.normalize_public_key(pub.read_text())


def _config(*signers: tuple[str, str, str], enabled: bool = True):
    return broadcast.BroadcastSettings(
        enabled=enabled,
        signers=[broadcast.Signer(n, k, r) for n, k, r in signers],
        signing_key=None,
    )


def _status(report: mcp_setup.SetupReport) -> dict[str, str]:
    return {c.name: c.status for c in report.checks}


class TestCheckSetup:
    def test_secure_setup_passes(self, tmp_path: Path) -> None:
        pub, key = _keypair(tmp_path, "k", passphrase="correct horse")
        report = check_setup(
            str(pub), agent_keys=[key], config=_config(("claude-desktop", key, "agent"))
        )
        status = _status(report)
        assert report.ok
        assert status["private key at rest"] == OK
        assert status["SSH agent"] == OK
        assert status["broadcast signer"] == OK
        assert status["per-use confirmation"] == WARN
        assert report.desktop_config["args"] == ["mcp", "--key", key]

    def test_unencrypted_private_key_fails(self, tmp_path: Path) -> None:
        pub, key = _keypair(tmp_path, "k")
        report = check_setup(
            str(pub), agent_keys=[key], config=_config(("claude-desktop", key, "agent"))
        )
        assert _status(report)["private key at rest"] == FAIL
        assert not report.ok

    def test_private_key_path_is_refused(self, tmp_path: Path) -> None:
        pub, _ = _keypair(tmp_path, "k")
        report = check_setup(str(pub.with_suffix("")), agent_keys=[], config=_config())
        check = next(c for c in report.checks if c.name == "signing key")
        assert check.status == FAIL
        assert "private key file" in check.detail
        assert report.desktop_config["args"] == ["mcp"]

    def test_user_key_is_refused(self, tmp_path: Path) -> None:
        pub, key = _keypair(tmp_path, "k", passphrase="pw")
        report = check_setup(
            str(pub), agent_keys=[key], config=_config(("laptop", key, "user"))
        )
        check = next(c for c in report.checks if c.name == "broadcast signer")
        assert check.status == FAIL and "'laptop'" in check.detail

    def test_unregistered_key_says_how(self, tmp_path: Path) -> None:
        _, key = _keypair(tmp_path, "k")
        report = check_setup(key, name="desk", agent_keys=[key], config=_config())
        check = next(c for c in report.checks if c.name == "broadcast signer")
        assert check.status == FAIL
        assert (
            check.fix[0] == f'fujimoto broadcast add-signer desk "{key}" --role agent'
        )
        # A literal key has no file beside it.
        assert _status(report)["private key at rest"] == OK

    def test_key_not_in_agent(self, tmp_path: Path) -> None:
        _, key = _keypair(tmp_path, "k")
        report = check_setup(
            key, agent_keys=[], config=_config(("claude-desktop", key, "agent"))
        )
        assert _status(report)["SSH agent"] == FAIL

    def test_no_key_is_read_only_setup(self) -> None:
        report = check_setup(None, config=_config(enabled=False))
        status = _status(report)
        assert status["signing key"] == WARN
        assert status["broadcast enabled"] == WARN
        assert report.ok
        assert "SSH_AUTH_SOCK" not in report.desktop_config["env"]

    def test_disabled_broadcast_fails_with_a_key(self, tmp_path: Path) -> None:
        _, key = _keypair(tmp_path, "k")
        report = check_setup(
            key,
            agent_keys=[key],
            config=_config(("claude-desktop", key, "agent"), enabled=False),
        )
        assert _status(report)["broadcast enabled"] == FAIL

    def test_bad_key_text(self) -> None:
        report = check_setup("nonsense", agent_keys=[], config=_config())
        assert _status(report)["signing key"] == FAIL

    def test_invalid_broadcast_config(self) -> None:
        with patch.object(
            broadcast, "load_config", side_effect=broadcast.BroadcastError("bad yaml")
        ):
            report = check_setup(None)
        assert _status(report)["broadcast config"] == FAIL

    def test_missing_extra_and_tmux(self) -> None:
        with (
            patch("importlib.util.find_spec", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            report = check_setup(None, config=_config())
        status = _status(report)
        assert status["mcp package"] == FAIL and status["tmux"] == WARN

    def test_passphrase_unknown(self, tmp_path: Path) -> None:
        pub, key = _keypair(tmp_path, "k")
        with patch.object(mcp_setup, "private_key_has_passphrase", return_value=None):
            report = check_setup(
                str(pub), agent_keys=[key], config=_config(("c", key, "agent"))
            )
        assert _status(report)["private key at rest"] == WARN


class TestHelpers:
    def test_passphrase_detection(self, tmp_path: Path) -> None:
        plain, _ = _keypair(tmp_path, "plain")
        locked, _ = _keypair(tmp_path, "locked", passphrase="pw")
        assert mcp_setup.private_key_has_passphrase(plain.with_suffix("")) is False
        assert mcp_setup.private_key_has_passphrase(locked.with_suffix("")) is True
        assert mcp_setup.private_key_has_passphrase(tmp_path / "missing") is None

    def test_passphrase_detection_without_ssh_keygen(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=OSError):
            assert mcp_setup.private_key_has_passphrase(tmp_path / "k") is None

    def test_stable_dir_skips_wrapper_dirs(self, tmp_path: Path) -> None:
        shim = tmp_path / "term" / "cli-shims" / "abc"
        real = tmp_path / "bin"
        for d in (shim, real):
            d.mkdir(parents=True)
            tool = d / "claude"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
        path = f"{shim}:{tmp_path / 'empty'}::{real}"
        assert mcp_setup._stable_dir("claude", path) == str(real)
        assert mcp_setup._stable_dir("nothing-here", path) is None

    def test_fujimoto_command_falls_back_to_argv(self) -> None:
        with patch.object(mcp_setup, "_stable_dir", return_value=None):
            assert Path(mcp_setup.fujimoto_command()).is_absolute()

    def test_desktop_config_path(self) -> None:
        assert mcp_setup.desktop_config_path().name == "claude_desktop_config.json"

    def test_unreadable_key_path(self, tmp_path: Path) -> None:
        assert mcp_setup._is_private_key_file(str(tmp_path)) is False


class TestOutput:
    def test_text_and_json(self, tmp_path: Path) -> None:
        _, key = _keypair(tmp_path, "k")
        report = check_setup(key, agent_keys=[], config=_config())
        text = mcp_setup.format_report(report)
        assert "✗ SSH agent" in text
        assert "Fix the ✗ items" in text
        assert '"mcpServers"' in text
        data = mcp_setup.report_json(report)
        assert data["ok"] is False
        assert data["desktop_config"]["fujimoto"]["args"] == ["mcp", "--key", key]
        json.dumps(data)


class TestCli:
    def _main(self, *argv: str) -> None:
        from fujimoto import cli

        with patch("sys.argv", ["fujimoto", *argv]):
            cli.main()

    def test_setup_text_exit_code(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(broadcast, "_agent_keys", return_value=[]),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp", "setup", "--key", "nonsense")
        assert exc.value.code == 1
        assert "✗ signing key" in capsys.readouterr().out

    def test_setup_json_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._main("mcp", "setup", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True

    def test_serve_dispatch(self) -> None:
        with patch("fujimoto.mcp_server.serve", return_value=0) as serve:
            self._main("mcp", "--key", "K")
        serve.assert_called_once_with("K")

    def test_serve_failure_exits(self) -> None:
        with (
            patch("fujimoto.mcp_server.serve", return_value=1),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp")
        assert exc.value.code == 1


class TestConfigOnly:
    def _main(self, *argv: str) -> None:
        from fujimoto import cli

        with patch("sys.argv", ["fujimoto", *argv]):
            cli.main()

    def test_prints_only_pasteable_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._main("mcp", "setup", "--config-only")
        out = capsys.readouterr().out
        data = json.loads(out)
        assert list(data) == ["mcpServers"]
        entry = data["mcpServers"]["fujimoto"]
        assert entry["args"] == ["mcp"]
        assert Path(entry["command"]).is_absolute()
        assert "PATH" in entry["env"]

    def test_failing_check_prints_no_config(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(broadcast, "_agent_keys", return_value=[]),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp", "setup", "--key", "nonsense", "--config-only")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no config was printed" in captured.err
        assert "✗ signing key" in captured.err

    def test_json_and_config_only_conflict(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._main("mcp", "setup", "--json", "--config-only")
        assert exc.value.code == 2

    def test_client_config_with_key(self, tmp_path: Path) -> None:
        _, key = _keypair(tmp_path, "k")
        report = check_setup(
            key, agent_keys=[key], config=_config(("claude-desktop", key, "agent"))
        )
        entry = mcp_setup.client_config(report)["mcpServers"]["fujimoto"]
        assert entry["args"] == ["mcp", "--key", key]
