"""`fujimoto mcp doctor`: read-only checks of the installed MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fujimoto import broadcast, debug, mcp_doctor, mcp_setup
from fujimoto.mcp_doctor import Probe
from fujimoto.mcp_setup import FAIL, OK, WARN
from fujimoto.version import get_version

# A stand-in MCP server: answers initialize and tools/list on stdio, the way
# the real one does, so the probe is exercised end to end without the SDK.
FAKE_SERVER = r"""
import json, os, sys
mode = sys.argv[1]
if mode == "crash":
    print("fujimoto mcp needs the `mcp` extra:", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    sys.stdin.read()
    sys.exit(0)
tools = ["list_sessions", "follow_message"]
if mode == "messaging":
    tools += ["send_message", "broadcast"]
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}))
        out = {"protocolVersion": msg["params"]["protocolVersion"],
               "serverInfo": {"name": "fujimoto", "version": "9.9"},
               "capabilities": {}, "cwd": os.getcwd(), "path": os.environ.get("PATH")}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": out}), flush=True)
    elif msg.get("method") == "tools/list":
        if mode == "error":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "boom"}}), flush=True)
        else:
            res = {"tools": [{"name": t} for t in tools]}
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
"""


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("CODEX_HOME", str(h / ".codex"))
    yield h
    debug.disable()


@pytest.fixture
def server(tmp_path: Path) -> Path:
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE_SERVER)
    return path


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    """An executable `fujimoto` that prints a version, like the real one."""
    path = tmp_path / "bin" / "fujimoto"
    path.parent.mkdir()
    path.write_text(f"#!/bin/sh\necho 'fujimoto {get_version()}'\n")
    path.chmod(0o755)
    return path


def _desktop_config(home: Path, entry: dict | None) -> Path:
    path = home / "claude_desktop_config.json"
    servers = {"other": {"command": "x"}}
    if entry is not None:
        servers["fujimoto"] = entry
    path.write_text(json.dumps({"mcpServers": servers}, indent=2))
    return path


def _ok_probe(*tools: str) -> object:
    def probe(command, args, env, timeout) -> Probe:
        return Probe(True, "ok", "fujimoto", "9.9", tools or ("list_sessions",), 0.3)

    return probe


def _statuses(target: mcp_doctor.Target) -> dict[str, str]:
    return {c.name: c.status for c in target.checks}


class TestProbe:
    def test_answers(self, server: Path) -> None:
        probe = mcp_doctor.probe_server(
            sys.executable, [str(server), "plain"], {"PATH": "/usr/bin:/bin"}, 10
        )
        assert probe.ok, probe.detail
        assert (probe.server, probe.version) == ("fujimoto", "9.9")
        assert probe.tools == ("list_sessions", "follow_message")

    def test_crash_reports_the_last_stderr_line(self, server: Path) -> None:
        probe = mcp_doctor.probe_server(sys.executable, [str(server), "crash"], {}, 10)
        assert not probe.ok
        assert "exited 1" in probe.detail and "`mcp` extra" in probe.detail

    def test_timeout(self, server: Path) -> None:
        probe = mcp_doctor.probe_server(sys.executable, [str(server), "hang"], {}, 0.5)
        assert not probe.ok and "did not answer in time" in probe.detail

    def test_error_response(self, server: Path) -> None:
        probe = mcp_doctor.probe_server(sys.executable, [str(server), "error"], {}, 10)
        assert not probe.ok and "tools/list failed" in probe.detail

    def test_cannot_start(self, tmp_path: Path) -> None:
        probe = mcp_doctor.probe_server(str(tmp_path / "missing"), [], {}, 1)
        assert not probe.ok and "could not start" in probe.detail


class TestEnvAndArgs:
    def test_server_env_is_gui_like(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/my/shell/bin:/usr/bin")
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", "/shell/only")
        monkeypatch.setenv("SECRET_TOKEN", "x")
        env = mcp_doctor.server_env({"env": {"FUJIMOTO_WORKTREE_ROOT": "/wt"}})
        assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        assert env["FUJIMOTO_WORKTREE_ROOT"] == "/wt"
        assert "FUJIMOTO_GIT_ROOT" not in env and "SECRET_TOKEN" not in env
        assert mcp_doctor.server_env({"env": {"PATH": "/a"}})["PATH"] == "/a"

    @pytest.mark.parametrize(
        ("args", "key"),
        [
            (["mcp", "--key", "ssh-ed25519 AAA"], "ssh-ed25519 AAA"),
            (["--debug", "mcp", "--key=k.pub"], "k.pub"),
            (["mcp"], None),
            (["mcp", "--key"], None),
        ],
    )
    def test_entry_key(self, args: list[str], key: str | None) -> None:
        assert mcp_doctor.entry_key(args) == key


class TestReadClientEntry:
    def test_json(self, home: Path) -> None:
        path = _desktop_config(home, {"command": "/bin/fujimoto", "args": ["mcp"]})
        assert mcp_setup.read_client_entry("claude-desktop", path)[1] == {
            "command": "/bin/fujimoto",
            "args": ["mcp"],
        }
        assert (
            mcp_setup.read_client_entry("claude-desktop", home / "nope.json")[1] is None
        )
        assert (
            mcp_setup.read_client_entry("claude-desktop", _desktop_config(home, None))[
                1
            ]
            is None
        )

    def test_toml(self, home: Path) -> None:
        path = home / "config.toml"
        path.write_text(
            'model = "x"\n\n[mcp_servers.fujimoto]\ncommand = "/f"\nargs = ["mcp"]\n'
        )
        assert mcp_setup.read_client_entry("chatgpt", path)[1] == {
            "command": "/f",
            "args": ["mcp"],
        }

    @pytest.mark.parametrize(
        "text", ["{not json", '{"mcpServers": []}', '{"mcpServers": {"fujimoto": 3}}']
    )
    def test_bad(self, home: Path, text: str) -> None:
        path = home / "c.json"
        path.write_text(text)
        with pytest.raises(mcp_setup.InstallError):
            mcp_setup.read_client_entry("claude-desktop", path)


class TestDiagnose:
    def test_configured_entry_passes(self, home: Path, launcher: Path) -> None:
        path = _desktop_config(
            home,
            {
                "command": str(launcher),
                "args": ["mcp"],
                "env": {"PATH": "/usr/bin:/bin"},
            },
        )
        before = path.read_bytes()
        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=_ok_probe()
        )
        assert path.read_bytes() == before  # read-only
        (target,) = report.targets
        status = _statuses(target)
        assert target.configured and report.ok
        assert status["client config"] == OK
        assert status["launcher"] == OK
        assert "launcher version" not in status
        assert status["MCP startup"] == OK
        assert status["signing"] == OK  # no key: messaging off by design
        assert status["tools on PATH"] in (OK, WARN)
        text = mcp_doctor.format_doctor(report)
        assert "Claude Desktop" in text and "messaging off" in text
        data = mcp_doctor.doctor_json(report)
        assert data["ok"] is True and data["targets"][0]["client"] == "claude-desktop"

    def test_real_probe_through_the_entry(self, home: Path, server: Path) -> None:
        # A launcher that is the fake server itself (version check via run=).
        path = _desktop_config(
            home, {"command": sys.executable, "args": [str(server), "messaging"]}
        )

        def run(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 0, f"fujimoto {get_version()}\n", ""
            )

        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, run=run
        )
        startup = next(c for c in report.targets[0].checks if c.name == "MCP startup")
        assert startup.status == OK and "messaging on" in startup.detail

    def test_missing_launcher_fails(self, home: Path, tmp_path: Path) -> None:
        path = _desktop_config(
            home, {"command": str(tmp_path / "gone"), "args": ["mcp"]}
        )
        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=_ok_probe()
        )
        status = _statuses(report.targets[0])
        assert not report.ok
        assert status["launcher"] == FAIL and status["MCP startup"] == FAIL

    def test_relative_command_not_on_entry_path(self, home: Path) -> None:
        path = _desktop_config(home, {"command": "fujimoto-nowhere", "args": ["mcp"]})
        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=_ok_probe()
        )
        launcher = report.targets[0].checks[1]
        assert launcher.status == FAIL and "not on the entry's PATH" in launcher.detail

    def test_version_mismatch_warns(self, home: Path, tmp_path: Path) -> None:
        old = tmp_path / "old"
        old.write_text("#!/bin/sh\necho 'fujimoto 0.0.1'\n")
        old.chmod(0o755)
        path = _desktop_config(home, {"command": str(old), "args": ["mcp"]})
        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=_ok_probe()
        )
        assert _statuses(report.targets[0])["launcher version"] == WARN
        assert report.ok

    def test_launcher_that_is_not_fujimoto(self, home: Path, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.write_text("#!/bin/sh\necho 'something else'\n")
        other.chmod(0o755)
        path = _desktop_config(home, {"command": str(other), "args": ["mcp"]})
        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=_ok_probe()
        )
        assert _statuses(report.targets[0])["launcher"] == FAIL

    def test_startup_failure(self, home: Path, launcher: Path) -> None:
        path = _desktop_config(home, {"command": str(launcher), "args": ["mcp"]})

        def probe(*a: object) -> Probe:
            return Probe(False, "the server exited 1: boom")

        report = mcp_doctor.diagnose(
            ["claude-desktop"], paths={"claude-desktop": path}, probe=probe
        )
        startup = next(c for c in report.targets[0].checks if c.name == "MCP startup")
        assert startup.status == FAIL and "boom" in startup.detail
        assert "✗ MCP startup" in mcp_doctor.format_doctor(report)

    def test_unconfigured_clients_and_setup_preview(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, launcher: Path
    ) -> None:
        monkeypatch.setattr(mcp_setup, "fujimoto_command", lambda: str(launcher))
        report = mcp_doctor.diagnose(
            paths={"claude-desktop": home / "none.json", "chatgpt": home / "none.toml"},
            probe=_ok_probe(),
        )
        clients = [t.client for t in report.targets]
        assert clients == ["claude-desktop", "chatgpt", mcp_doctor.PREVIEW]
        assert _statuses(report.targets[0]) == {"client config": WARN}
        assert report.targets[0].checks[0].fix == ["fujimoto mcp setup claude-desktop"]
        assert _statuses(report.targets[2])["MCP startup"] == OK
        assert "No client is configured" in mcp_doctor.format_doctor(report)

    def test_named_client_skips_the_preview_and_codex_is_chatgpt(
        self, home: Path
    ) -> None:
        report = mcp_doctor.diagnose(
            ["codex", "chatgpt"],
            paths={"chatgpt": home / "none.toml"},
            probe=_ok_probe(),
        )
        assert [t.client for t in report.targets] == ["chatgpt"]

    def test_unparseable_config_fails(self, home: Path) -> None:
        path = home / "bad.json"
        path.write_text("{")
        report = mcp_doctor.diagnose(["claude-desktop"], paths={"claude-desktop": path})
        assert _statuses(report.targets[0]) == {"client config": FAIL}


class TestSigning:
    @pytest.fixture
    def key(self, tmp_path: Path) -> str:
        priv = tmp_path / "agent"
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "pw",
                "-C",
                "t",
                "-f",
                str(priv),
            ],
            check=True,
        )
        return broadcast.normalize_public_key(priv.with_suffix(".pub").read_text())

    def _entry(
        self, launcher: Path, key: str, sock: str | None = "/tmp/agent.sock"
    ) -> dict:
        env = {"PATH": "/usr/bin:/bin"}
        if sock:
            env["SSH_AUTH_SOCK"] = sock
        return {"command": str(launcher), "args": ["mcp", "--key", key], "env": env}

    def test_ready(
        self, key: str, launcher: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = broadcast.BroadcastSettings(
            True, [broadcast.Signer("my-agent", key, "agent")]
        )
        monkeypatch.setattr(broadcast, "load_config", lambda **kw: config)
        socks: list[str | None] = []
        monkeypatch.setattr(
            mcp_doctor, "_agent_keys_at", lambda sock: socks.append(sock) or [key]
        )
        checks = mcp_doctor.check_entry(
            "claude-desktop",
            self._entry(launcher, key),
            probe=_ok_probe("list_sessions", "send_message", "broadcast"),
        )
        status = {c.name: c.status for c in checks}
        assert socks == ["/tmp/agent.sock"]  # the entry's agent, not ours
        assert status["broadcast signer"] == OK
        assert status["SSH agent"] == OK
        assert "/tmp/agent.sock" in next(
            c.detail for c in checks if c.name == "SSH agent"
        )
        assert status["broadcast enabled"] == OK
        assert "mcp package" not in status and "tmux" not in status
        startup = next(c for c in checks if c.name == "MCP startup")
        assert "messaging on" in startup.detail
        # Nothing private is printed: only a public key prefix.
        assert all("PRIVATE" not in c.detail for c in checks)

    def test_agent_missing_key_and_messaging_off(
        self, key: str, launcher: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = broadcast.BroadcastSettings(False, [])
        monkeypatch.setattr(broadcast, "load_config", lambda **kw: config)
        checks = mcp_doctor.check_entry(
            "chatgpt", self._entry(launcher, key, sock=None), probe=_ok_probe()
        )
        status = {c.name: c.status for c in checks}
        assert status["SSH agent"] == FAIL
        assert "(SSH_AUTH_SOCK not in the entry)" in next(
            c.detail for c in checks if c.name == "SSH agent"
        )
        assert status["broadcast signer"] == FAIL
        assert status["broadcast enabled"] == FAIL
        assert status["messaging tools"] == FAIL

    def test_bad_config(
        self, key: str, launcher: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def bad(**kw: object) -> broadcast.BroadcastSettings:
            raise broadcast.BroadcastError("invalid config")

        monkeypatch.setattr(broadcast, "load_config", bad)
        checks = mcp_doctor.check_entry(
            "claude-desktop", self._entry(launcher, key), probe=_ok_probe()
        )
        assert checks[-1].name == "broadcast config" and checks[-1].status == FAIL

    def test_agent_keys_at(self, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
        assert mcp_doctor._agent_keys_at(None) == []
        seen: list[str] = []

        def run(argv, **kw):
            seen.append(kw["env"]["SSH_AUTH_SOCK"])
            return subprocess.CompletedProcess(argv, 0, f"{key}\nnot a key\n", "")

        monkeypatch.setattr(mcp_doctor.subprocess, "run", run)
        assert mcp_doctor._agent_keys_at("/s") == [key] and seen == ["/s"]


class TestCli:
    def test_json_exit_code(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import cli

        monkeypatch.setattr(
            mcp_setup, "desktop_config_path", lambda: home / "missing.json"
        )
        monkeypatch.setattr(
            sys, "argv", ["fujimoto", "mcp", "doctor", "claude-desktop", "--json"]
        )
        cli.main()  # an unconfigured client is a warning, not a failure
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True
        assert data["targets"][0]["checks"][0]["status"] == "warn"

    def test_text_fails_with_exit_1(
        self,
        home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import cli

        path = _desktop_config(
            home, {"command": str(tmp_path / "gone"), "args": ["mcp"]}
        )
        monkeypatch.setattr(mcp_setup, "desktop_config_path", lambda: path)
        monkeypatch.setattr(
            sys, "argv", ["fujimoto", "mcp", "doctor", "claude-desktop"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "✗ launcher" in capsys.readouterr().out


def test_probe_runs_in_root_with_the_given_env(server: Path, tmp_path: Path) -> None:
    """The server starts where a GUI client starts it: cwd `/`, env as given."""
    out = tmp_path / "seen.json"
    script = tmp_path / "record.py"
    script.write_text(
        "import json, os, sys\n"
        f"json.dump({{'cwd': os.getcwd(), 'env': dict(os.environ)}}, open({str(out)!r}, 'w'))\n"
        f"exec(open({str(server)!r}).read())\n"
    )
    probe = mcp_doctor.probe_server(
        sys.executable, [str(script), "plain"], {"PATH": "/usr/bin", "X": "1"}, 10
    )
    assert probe.ok, probe.detail
    seen = json.loads(out.read_text())
    assert seen["cwd"] == "/"
    assert "CLAUDE_CODE_SESSION_ID" not in seen["env"]
    assert (seen["env"]["X"], seen["env"]["PATH"]) == ("1", "/usr/bin")


class TestSelfChecks:
    """MCP `server_health`: what a running server can check about itself."""

    def _bin(self, tmp_path: Path, version: str | None) -> str:
        d = tmp_path / "selfbin"
        d.mkdir()
        if version is not None:
            f = d / "fujimoto"
            f.write_text(f"#!/bin/sh\necho 'fujimoto {version}'\n")
            f.chmod(0o755)
        return f"{d}:/usr/bin:/bin"

    def _drift(self, tmp_path: Path, version: str | None) -> mcp_setup.Check:
        env = {"PATH": self._bin(tmp_path, version)}
        return mcp_doctor.self_checks(None, env=env)[0]

    def test_same_version(self, tmp_path: Path) -> None:
        check = self._drift(tmp_path, get_version())
        assert check.name == "version" and check.status == OK

    def test_drift_says_restart(self, tmp_path: Path) -> None:
        check = self._drift(tmp_path, "99.0")
        assert check.status == WARN and "but" in check.detail and "99.0" in check.detail
        assert "Restart the MCP client" in check.fix[0]

    def test_no_fujimoto_on_path(self, tmp_path: Path) -> None:
        check = self._drift(tmp_path, None)
        assert check.status == WARN and "no `fujimoto` on its PATH" in check.detail

    def test_launcher_without_version(self, tmp_path: Path) -> None:
        env = {"PATH": self._bin(tmp_path, get_version())}

        def run(argv, **kw):
            raise OSError("boom")

        check = mcp_doctor.self_checks(None, env=env, run=run)[0]
        assert check.status == WARN and "gave no version" in check.detail

    def test_no_key_and_signing_with_own_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = [c.name for c in mcp_doctor.self_checks(None, env={"PATH": "/usr/bin"})]
        assert names == ["version", "tools on PATH", "signing"]
        no_key = mcp_doctor.self_checks(None, env={"PATH": "/usr/bin"})[-1]
        assert no_key.detail.startswith("this server was started without --key")

        priv = tmp_path / "k"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(priv)],
            check=True,
        )
        key = broadcast.normalize_public_key(priv.with_suffix(".pub").read_text())
        config = broadcast.BroadcastSettings(
            True, [broadcast.Signer("a", key, "agent")]
        )
        monkeypatch.setattr(broadcast, "load_config", lambda **kw: config)
        monkeypatch.setattr(mcp_doctor, "_agent_keys_at", lambda sock: [])
        checks = mcp_doctor.self_checks(key, env={"PATH": "/usr/bin"})
        agent = next(c for c in checks if c.name == "SSH agent")
        assert agent.status == FAIL
        assert "(SSH_AUTH_SOCK not in this server's environment)" in agent.detail
