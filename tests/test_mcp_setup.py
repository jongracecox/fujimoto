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


ENTRY = {"command": "/x/fujimoto", "args": ["mcp"], "env": {"PATH": "/usr/bin"}}


def _desktop_file(tmp_path: Path, data: object, indent: int = 2) -> Path:
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent) + "\n")
    path.chmod(0o600)
    return path


class TestInstallDesktopConfig:
    EXISTING = {
        "preferences": {"theme": "dark"},
        "mcpServers": {"other": {"command": "other-server", "args": ["--x"]}},
        "coworkUserFilesPath": "/somewhere",
    }

    def test_adds_entry_keeps_everything_else(self, tmp_path: Path) -> None:
        path = _desktop_file(tmp_path, self.EXISTING, indent=4)
        result = mcp_setup.install_desktop_config(ENTRY, path=path, stamp="20261124")
        assert result.status == mcp_setup.ADDED
        after = json.loads(path.read_text())
        assert after["mcpServers"] == {**self.EXISTING["mcpServers"], "fujimoto": ENTRY}
        assert {k: v for k, v in after.items() if k != "mcpServers"} == {
            "preferences": {"theme": "dark"},
            "coworkUserFilesPath": "/somewhere",
        }
        assert list(after) == list(self.EXISTING)  # key order kept
        assert path.read_text().splitlines()[1].startswith('    "')  # indent kept
        assert path.read_text().endswith("\n")
        assert path.stat().st_mode & 0o777 == 0o600
        assert result.backup == path.with_name(path.name + ".bak-20261124")
        assert json.loads(result.backup.read_text()) == self.EXISTING

    def test_identical_entry_is_a_no_op(self, tmp_path: Path) -> None:
        path = _desktop_file(tmp_path, {"mcpServers": {"fujimoto": ENTRY}})
        before = path.read_text()
        result = mcp_setup.install_desktop_config(ENTRY, path=path)
        assert result.status == mcp_setup.UNCHANGED
        assert result.backup is None
        assert path.read_text() == before
        assert list(path.parent.glob("*.bak-*")) == []

    def test_own_entry_is_updated_without_replace(self, tmp_path: Path) -> None:
        """Re-running with --key later updates what an earlier setup wrote."""
        old = {"command": "/x/fujimoto", "args": ["mcp"], "env": {"PATH": "/usr/bin"}}
        path = _desktop_file(tmp_path, {"mcpServers": {"fujimoto": old}})
        keyed = {**ENTRY, "args": ["mcp", "--key", "ssh-ed25519 AAAA"]}
        result = mcp_setup.install_desktop_config(keyed, path=path, stamp="1")
        assert result.status == mcp_setup.REPLACED
        assert '+    "--key",' in result.diff
        assert json.loads(path.read_text())["mcpServers"]["fujimoto"] == keyed
        assert json.loads(result.backup.read_text())["mcpServers"]["fujimoto"] == old

    def test_global_flags_before_mcp_are_kept(self, tmp_path: Path) -> None:
        old = {"command": "/x/fujimoto", "args": ["--debug-redacted", "mcp"]}
        path = _desktop_file(tmp_path, {"mcpServers": {"fujimoto": old}})
        keyed = {**ENTRY, "args": ["mcp", "--key", "K"]}
        mcp_setup.install_desktop_config(keyed, path=path, stamp="1")
        args = json.loads(path.read_text())["mcpServers"]["fujimoto"]["args"]
        assert args == ["--debug-redacted", "mcp", "--key", "K"]
        # And with the flags kept, a re-run is a no-op.
        again = mcp_setup.install_desktop_config(keyed, path=path)
        assert again.status == mcp_setup.UNCHANGED

    @pytest.mark.parametrize(
        "foreign",
        [
            {"command": "node", "args": ["fujimoto-bridge.js"]},
            {"command": "/x/fujimoto", "args": ["list"]},
            {"command": "/x/fujimoto"},
            "not even an object",
        ],
    )
    def test_foreign_entry_needs_replace(self, tmp_path: Path, foreign: object) -> None:
        path = _desktop_file(tmp_path, {"mcpServers": {"fujimoto": foreign}})
        before = path.read_text()
        result = mcp_setup.install_desktop_config(ENTRY, path=path)
        assert result.status == mcp_setup.CONFLICT
        assert result.diff.startswith("--- current fujimoto entry")
        assert path.read_text() == before
        assert list(path.parent.glob("*.bak-*")) == []
        result = mcp_setup.install_desktop_config(
            ENTRY, path=path, replace=True, stamp="1"
        )
        assert result.status == mcp_setup.REPLACED
        assert json.loads(path.read_text())["mcpServers"]["fujimoto"] == ENTRY
        assert (
            json.loads(result.backup.read_text())["mcpServers"]["fujimoto"] == foreign
        )

    def test_missing_file_is_created_private(self, tmp_path: Path) -> None:
        path = tmp_path / "Claude" / "claude_desktop_config.json"
        path.parent.mkdir()
        result = mcp_setup.install_desktop_config(ENTRY, path=path)
        assert result.status == mcp_setup.ADDED and result.backup is None
        assert json.loads(path.read_text()) == {"mcpServers": {"fujimoto": ENTRY}}
        assert path.stat().st_mode & 0o777 == 0o600

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text("")
        mcp_setup.install_desktop_config(ENTRY, path=path, stamp="1")
        assert json.loads(path.read_text()) == {"mcpServers": {"fujimoto": ENTRY}}

    def test_missing_claude_dir(self, tmp_path: Path) -> None:
        with pytest.raises(mcp_setup.InstallError, match="Claude Desktop installed"):
            mcp_setup.install_desktop_config(ENTRY, path=tmp_path / "no" / "c.json")

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("{not json", "not valid JSON"),
            ("[1, 2]", "not a JSON object"),
            ('{"mcpServers": []}', '"mcpServers"'),
        ],
    )
    def test_bad_file_is_left_alone(
        self, tmp_path: Path, text: str, match: str
    ) -> None:
        path = tmp_path / "c.json"
        path.write_text(text)
        with pytest.raises(mcp_setup.InstallError, match=match):
            mcp_setup.install_desktop_config(ENTRY, path=path)
        assert path.read_text() == text
        assert list(tmp_path.glob("*.bak-*")) == []

    def test_unreadable_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(mcp_setup.InstallError, match="cannot read"):
            mcp_setup.install_desktop_config(ENTRY, path=path)

    def test_write_failure_leaves_original(self, tmp_path: Path) -> None:
        path = _desktop_file(tmp_path, self.EXISTING)
        before = path.read_text()
        with (
            patch("os.replace", side_effect=OSError("disk full")),
            pytest.raises(mcp_setup.InstallError, match="cannot write"),
        ):
            mcp_setup.install_desktop_config(ENTRY, path=path, stamp="1")
        assert path.read_text() == before
        assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == []

    def test_messages(self, tmp_path: Path) -> None:
        p = tmp_path / "c.json"
        added = mcp_setup.InstallResult(mcp_setup.ADDED, p, tmp_path / "c.json.bak-1")
        assert "Backup of the previous file" in mcp_setup.format_install(added)
        assert "⌘Q" in mcp_setup.format_install(added)
        same = mcp_setup.InstallResult(mcp_setup.UNCHANGED, p)
        assert "nothing changed" in mcp_setup.format_install(same)
        conflict = mcp_setup.InstallResult(mcp_setup.CONFLICT, p, diff="-a\n+b")
        assert "--replace" in mcp_setup.format_install(conflict)
        assert "doesn't look like one fujimoto wrote" in mcp_setup.format_install(
            conflict
        )
        replaced = mcp_setup.InstallResult(mcp_setup.REPLACED, p, None, "-a\n+b")
        assert "Updated" in mcp_setup.format_install(replaced)
        assert mcp_setup.install_json(same) == {
            "client": "claude-desktop",
            "status": "unchanged",
            "path": str(p),
            "backup": None,
            "diff": None,
        }


class TestInstallCli:
    def _main(self, *argv: str) -> None:
        from fujimoto import cli

        with patch("sys.argv", ["fujimoto", *argv]):
            cli.main()

    def test_writes_the_checked_entry(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _desktop_file(tmp_path, {"mcpServers": {}})
        with patch.object(mcp_setup, "desktop_config_path", return_value=path):
            self._main("mcp", "setup", "claude-desktop")
        assert "Added the fujimoto entry" in capsys.readouterr().out
        entry = json.loads(path.read_text())["mcpServers"]["fujimoto"]
        assert entry["args"] == ["mcp"]

    def test_failing_check_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _desktop_file(tmp_path, {"mcpServers": {}})
        before = path.read_text()
        with (
            patch.object(mcp_setup, "desktop_config_path", return_value=path),
            patch.object(broadcast, "_agent_keys", return_value=[]),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp", "setup", "claude-desktop", "--key", "nonsense")
        assert exc.value.code == 1
        assert path.read_text() == before
        assert "no config was printed" in capsys.readouterr().err

    def test_conflict_exits_1_and_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _desktop_file(tmp_path, {"mcpServers": {"fujimoto": {"command": "old"}}})
        with (
            patch.object(mcp_setup, "desktop_config_path", return_value=path),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp", "setup", "claude-desktop", "--json")
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "conflict"

    def test_install_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                mcp_setup, "desktop_config_path", return_value=tmp_path / "x" / "c.json"
            ),
            pytest.raises(SystemExit) as exc,
        ):
            self._main("mcp", "setup", "claude-desktop")
        assert exc.value.code == 1
        assert "Claude Desktop installed" in capsys.readouterr().err

    def test_replace_needs_a_client(self) -> None:
        with pytest.raises(SystemExit) as exc:
            self._main("mcp", "setup", "--replace")
        assert exc.value.code == 2

    def test_config_only_with_client_prints_its_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "codex" / "config.toml"
        path.parent.mkdir()
        with patch.object(mcp_setup, "codex_config_path", return_value=path):
            self._main("mcp", "setup", "chatgpt", "--config-only")
        out = capsys.readouterr().out
        assert out.startswith("[mcp_servers.fujimoto]\n")
        assert not path.exists()  # printed, not written
        self._main("mcp", "setup", "claude-desktop", "--config-only")
        assert "mcpServers" in json.loads(capsys.readouterr().out)

    def test_chatgpt_writes_toml(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "codex" / "config.toml"
        path.parent.mkdir()
        path.write_text('model = "x"\n')
        with patch.object(mcp_setup, "codex_config_path", return_value=path):
            self._main("mcp", "setup", "codex", "--json")
        result = json.loads(capsys.readouterr().out)
        assert result["client"] == "codex" and result["status"] == "added"
        import tomllib

        data = tomllib.loads(path.read_text())
        assert data["model"] == "x"
        assert data["mcp_servers"]["fujimoto"]["args"] == ["mcp"]


CODEX_CONFIG = """\
# Codex / ChatGPT desktop config
model = "gpt-x"

[desktop]
theme = "light"   # keep this comment

[mcp_servers.node_repl]
command = "/Applications/ChatGPT.app/node_repl"
args = []
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
CODEX_HOME = "/Users/someone/.codex"

[plugins."browser@openai-bundled"]
enabled = true
"""


def _codex_file(tmp_path: Path, text: str = CODEX_CONFIG) -> Path:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def _toml(path: Path) -> dict:
    import tomllib

    return tomllib.loads(path.read_text())


TOML_ENTRY = {
    "command": "/Users/someone/.local/bin/fujimoto",
    "args": ["mcp", "--key", "ssh-ed25519 AAAA"],
    "env": {"PATH": "/usr/bin:/bin", "SSH_AUTH_SOCK": "/Users/some one/agent.sock"},
}


class TestCodexConfig:
    def test_appends_and_leaves_the_rest_byte_for_byte(self, tmp_path: Path) -> None:
        path = _codex_file(tmp_path)
        result = mcp_setup.install_client_config(
            "chatgpt", TOML_ENTRY, path=path, stamp="1"
        )
        assert result.status == mcp_setup.ADDED
        text = path.read_text()
        assert text.startswith(CODEX_CONFIG)  # untouched, comments included
        assert _toml(path)["mcp_servers"]["fujimoto"] == TOML_ENTRY
        assert _toml(path)["mcp_servers"]["node_repl"]["startup_timeout_sec"] == 120
        assert path.stat().st_mode & 0o777 == 0o600
        assert result.backup is not None and result.backup.read_text() == CODEX_CONFIG
        assert "ChatGPT app" in mcp_setup.format_install(result)

    def test_rerun_is_a_no_op(self, tmp_path: Path) -> None:
        path = _codex_file(tmp_path)
        mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path, stamp="1")
        before = path.read_text()
        again = mcp_setup.install_client_config("codex", TOML_ENTRY, path=path)
        assert again.status == mcp_setup.UNCHANGED
        assert path.read_text() == before

    def test_own_block_is_updated_in_place(self, tmp_path: Path) -> None:
        old = mcp_setup.toml_block(
            {"command": "/x/fujimoto", "args": ["--debug-redacted", "mcp"]}
        )
        text = CODEX_CONFIG.replace("[plugins.", old + "\n[plugins.", 1)
        path = _codex_file(tmp_path, text)
        result = mcp_setup.install_client_config(
            "chatgpt", TOML_ENTRY, path=path, stamp="1"
        )
        assert result.status == mcp_setup.REPLACED
        ours = _toml(path)["mcp_servers"]["fujimoto"]
        assert ours["args"] == ["--debug-redacted", "mcp", "--key", "ssh-ed25519 AAAA"]
        new_text = path.read_text()
        # Same place: still before the plugins table, nothing else moved.
        assert new_text.index("[mcp_servers.fujimoto]") < new_text.index("[plugins.")
        assert new_text.count("[mcp_servers.fujimoto]") == 1
        assert _toml(path)["plugins"] == _toml(_codex_file(tmp_path / "o"))["plugins"]

    def test_foreign_entry_needs_replace(self, tmp_path: Path) -> None:
        text = (
            CODEX_CONFIG
            + '\n[mcp_servers.fujimoto]\ncommand = "node"\nargs = ["x.js"]\n'
        )
        path = _codex_file(tmp_path, text)
        result = mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)
        assert result.status == mcp_setup.CONFLICT
        assert path.read_text() == text
        result = mcp_setup.install_client_config(
            "chatgpt", TOML_ENTRY, path=path, replace=True, stamp="1"
        )
        assert result.status == mcp_setup.REPLACED
        assert _toml(path)["mcp_servers"]["fujimoto"] == TOML_ENTRY

    @pytest.mark.parametrize(
        "form",
        [
            '[mcp_servers]\nfujimoto = { command = "/x/fujimoto", args = ["mcp"] }\n',
            'mcp_servers.fujimoto.command = "/x/fujimoto"\n'
            'mcp_servers.fujimoto.args = ["mcp"]\n',
        ],
    )
    def test_forms_it_cannot_edit_are_refused(self, tmp_path: Path, form: str) -> None:
        path = _codex_file(tmp_path, form)
        with pytest.raises(mcp_setup.InstallError, match="can't edit safely"):
            mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)
        assert path.read_text() == form
        assert list(path.parent.glob("*.bak-*")) == []

    def test_invalid_toml_is_left_alone(self, tmp_path: Path) -> None:
        path = _codex_file(tmp_path, "this is = = not toml\n")
        with pytest.raises(mcp_setup.InstallError, match="not valid TOML"):
            mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)

    def test_mcp_servers_not_a_table(self, tmp_path: Path) -> None:
        path = _codex_file(tmp_path, 'mcp_servers = "nope"\n')
        with pytest.raises(mcp_setup.InstallError, match="not a table"):
            mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)

    @pytest.mark.parametrize("text", ["", "model = 1", "model = 1\n", "model = 1\n\n"])
    def test_append_spacing(self, tmp_path: Path, text: str) -> None:
        path = _codex_file(tmp_path, text)
        mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path, stamp="1")
        new = path.read_text()
        assert "\n\n\n" not in new
        assert _toml(path)["mcp_servers"]["fujimoto"] == TOML_ENTRY

    def test_missing_file_and_dir(self, tmp_path: Path) -> None:
        path = tmp_path / ".codex" / "config.toml"
        with pytest.raises(mcp_setup.InstallError, match="ChatGPT desktop app"):
            mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)
        path.parent.mkdir()
        mcp_setup.install_client_config("chatgpt", TOML_ENTRY, path=path)
        assert path.read_text() == mcp_setup.toml_block(TOML_ENTRY)

    def test_codex_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
        assert mcp_setup.codex_config_path() == tmp_path / "ch" / "config.toml"
        monkeypatch.delenv("CODEX_HOME")
        assert mcp_setup.codex_config_path() == Path.home() / ".codex" / "config.toml"
        assert mcp_setup.config_path("chatgpt") == mcp_setup.codex_config_path()

    def test_block_without_env_and_snippets(self) -> None:
        block = mcp_setup.toml_block({"command": "/x/fujimoto", "args": ["mcp"]})
        assert ".env]" not in block
        assert mcp_setup.client_snippet("codex", TOML_ENTRY).startswith("[mcp_servers.")
        assert "mcpServers" in mcp_setup.client_snippet("claude-desktop", TOML_ENTRY)

    def test_codex_restart_message(self, tmp_path: Path) -> None:
        r = mcp_setup.InstallResult(mcp_setup.ADDED, tmp_path, client="codex")
        assert "new Codex session" in mcp_setup.format_install(r)


class TestSignerName:
    def test_default_follows_client(self) -> None:
        assert mcp_setup.default_signer("chatgpt") == "chatgpt"
        assert mcp_setup.default_signer("codex") == "chatgpt"
        assert mcp_setup.default_signer("claude-desktop") == "claude-desktop"
        assert mcp_setup.default_signer(None) == "claude-desktop"

    def test_name_taken_by_another_key_fails(self, tmp_path: Path) -> None:
        _, desktop = _keypair(tmp_path, "d")
        _, other = _keypair(tmp_path, "o")
        report = check_setup(
            other,
            name="claude-desktop",
            agent_keys=[other],
            config=_config(("claude-desktop", desktop, "agent")),
        )
        check = next(c for c in report.checks if c.name == "broadcast signer")
        assert check.status == FAIL
        assert "already used by another key" in check.detail
        assert "add-signer" not in " ".join(check.fix)  # never suggest a clash
        assert "--name <new name>" in check.fix[0]

    def test_chatgpt_cli_suggests_chatgpt_signer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from fujimoto import cli

        _, key = _keypair(tmp_path, "k")
        with (
            patch.object(broadcast, "_agent_keys", return_value=[key]),
            patch("sys.argv", ["fujimoto", "mcp", "setup", "chatgpt", "--key", key]),
            pytest.raises(SystemExit),
        ):
            cli.main()
        # With a client, failing checks go to stderr and nothing is written.
        err = capsys.readouterr().err
        assert f'add-signer chatgpt "{key}" --role agent' in err


class TestCarriedSettings:
    def test_only_the_allowlist_and_only_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", str(tmp_path / "git"))
        monkeypatch.delenv("FUJIMOTO_WORKTREE_ROOT", raising=False)
        monkeypatch.setenv("FUJIMOTO_SECRET_TOKEN", "nope")
        monkeypatch.setenv("GITHUB_TOKEN", "nope")
        env = mcp_setup.desktop_entry(None)["env"]
        assert env["FUJIMOTO_GIT_ROOT"] == str((tmp_path / "git").resolve())
        assert "FUJIMOTO_WORKTREE_ROOT" not in env
        assert "FUJIMOTO_SECRET_TOKEN" not in env and "GITHUB_TOKEN" not in env

    def test_tilde_is_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FUJIMOTO_WORKTREE_ROOT", "~/wt")
        assert mcp_setup.carried_settings()["FUJIMOTO_WORKTREE_ROOT"] == str(
            (Path.home() / "wt").resolve()
        )

    def test_reaches_config_only_and_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import cli

        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", str(tmp_path))
        with patch("sys.argv", ["fujimoto", "mcp", "setup", "--config-only"]):
            cli.main()
        env = json.loads(capsys.readouterr().out)["mcpServers"]["fujimoto"]["env"]
        assert env["FUJIMOTO_GIT_ROOT"] == str(tmp_path.resolve())
        block = mcp_setup.toml_block(mcp_setup.desktop_entry(None))
        assert f'FUJIMOTO_GIT_ROOT = "{tmp_path.resolve()}"' in block
