"""`fujimoto mcp doctor`: does the MCP server a client is configured with work?

`mcp setup` checks a key you propose and writes the entry. The doctor checks
what is *installed*, the way the client will run it:

- **client config**: each client's `fujimoto` entry (`read_client_entry`);
- **launcher**: the entry's `command` exists, is executable, and reports a
  version (compared with this fujimoto's);
- **tools on PATH**: tmux, git and claude on the entry's own `PATH`;
- **MCP startup**: the entry's command and args run with the entry's env
  (on a minimal GUI-like base, cwd `/`), and answer `initialize` and
  `tools/list` over stdio; whether the messaging tools are registered;
- **signing readiness** (only when the entry has `--key`): `mcp_setup`'s key
  checks, against the SSH agent the entry's `SSH_AUTH_SOCK` names.

Read-only: it never signs, sends, writes config or touches a session. The
server it starts is asked only for its tool list, then its stdin is closed.
With no client configured it checks the entry `mcp setup` would write.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from fujimoto import broadcast, debug, mcp_setup
from fujimoto.mcp_setup import FAIL, OK, WARN, Check
from fujimoto.version import get_version

STARTUP_TIMEOUT = 20.0
PROTOCOL_VERSION = "2025-06-18"
MESSAGING_TOOLS = ("send_message", "broadcast")
# What a GUI app passes down from the login session; the entry's env adds
# the rest. Never the shell's PATH or FUJIMOTO_* settings: the point is to
# see what the client will see.
_BASE_ENV = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "SHELL")
_GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Setup's own checks that describe this process, not the configured server.
_LOCAL_ONLY = ("mcp package", "tmux")
_LABELS = {
    mcp_setup.CLAUDE_DESKTOP: "Claude Desktop",
    mcp_setup.CHATGPT: "ChatGPT / Codex",
}
PREVIEW = "setup-preview"


@dataclass
class Target:
    client: str  # claude-desktop | chatgpt | setup-preview
    config_path: str | None
    configured: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)


@dataclass
class DoctorReport:
    targets: list[Target]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.targets)


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str
    server: str | None = None
    version: str | None = None
    tools: tuple[str, ...] = ()
    elapsed: float = 0.0


# -- The server's environment -------------------------------------------------


def server_env(entry: Mapping[str, object]) -> dict[str, str]:
    """The env a GUI client starts the server with: a login base + the entry's."""
    env = {k: os.environ[k] for k in _BASE_ENV if k in os.environ}
    env["PATH"] = _GUI_PATH
    extra = entry.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _argv(entry: Mapping[str, object]) -> tuple[str, list[str]]:
    command = entry.get("command")
    args = entry.get("args")
    return (
        str(command) if isinstance(command, str) else "",
        [str(a) for a in args] if isinstance(args, list) else [],
    )


def entry_key(args: Sequence[str]) -> str | None:
    """The `--key` value in an entry's args (`--key K` or `--key=K`)."""
    for i, arg in enumerate(args):
        if arg == "--key" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--key="):
            return arg.split("=", 1)[1]
    return None


# -- Checks -------------------------------------------------------------------


def _launcher_checks(
    command: str,
    env: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[Check], str | None]:
    """Whether `command` runs, and its version. Returns (checks, resolved path)."""
    if not command:
        return [Check("launcher", FAIL, "the entry has no command")], None
    path = command
    if not os.path.isabs(command):
        found = shutil.which(command, path=env.get("PATH"))
        if found is None:
            return [
                Check(
                    "launcher",
                    FAIL,
                    f"{command!r} is not on the entry's PATH; GUI clients don't "
                    "read your shell profile",
                    ["fujimoto mcp setup <client>   (writes an absolute path)"],
                )
            ], None
        path = found
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return [
            Check(
                "launcher",
                FAIL,
                f"{path} is missing or not executable",
                [
                    "uv tool install --force 'fujimoto[mcp]'",
                    "fujimoto mcp setup <client>",
                ],
            )
        ], None
    try:
        result = run(
            [path, "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=dict(env),
            cwd="/",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [Check("launcher", FAIL, f"{path} --version failed: {e}")], path
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out.startswith("fujimoto "):
        tail = _last_line(result.stderr) or out or f"exit {result.returncode}"
        return [Check("launcher", FAIL, f"{path} --version: {tail}")], path
    theirs = out.removeprefix("fujimoto ").strip()
    checks = [Check("launcher", OK, f"{path} (fujimoto {theirs})")]
    ours = get_version()
    if theirs != ours:
        checks.append(
            Check(
                "launcher version",
                WARN,
                f"the client runs fujimoto {theirs}; this command is {ours}",
                [
                    "uv tool install --force 'fujimoto[mcp]'   (if the client's is older)"
                ],
            )
        )
    return checks, path


def _path_check(env: Mapping[str, str], key: str | None) -> Check:
    wanted = ["tmux", "git", "claude"] + (["ssh-keygen"] if key else [])
    missing = [t for t in wanted if shutil.which(t, path=env.get("PATH")) is None]
    if not missing:
        return Check("tools on PATH", OK, ", ".join(wanted))
    return Check(
        "tools on PATH",
        WARN,
        f"not on the entry's PATH: {', '.join(missing)} (start/resume"
        + (", signing" if "ssh-keygen" in missing else "")
        + " will fail)",
        ["fujimoto mcp setup <client>   (rewrites PATH from where they are now)"],
    )


def _last_line(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def probe_server(
    command: str,
    args: Sequence[str],
    env: Mapping[str, str],
    timeout: float = STARTUP_TIMEOUT,
) -> Probe:
    """Start the server as a client would and ask for its tools.

    `initialize`, `notifications/initialized`, `tools/list` as newline-
    delimited JSON-RPC on stdio; then stdin is closed and the process is
    waited for (terminated, then killed, if it lingers). Nothing else is
    called, so no tool runs.
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=dict(env),
        )
    except OSError as e:
        return Probe(False, f"could not start: {e}")
    reader = _Reader(proc)
    try:
        reader.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "fujimoto-doctor", "version": get_version()},
                },
            }
        )
        deadline = start + timeout
        init = reader.response(1, deadline)
        if "error" in init:
            return Probe(False, f"initialize failed: {init['error']}")
        result = cast("dict[str, object]", init.get("result") or {})
        info = cast("dict[str, object]", result.get("serverInfo") or {})
        reader.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        reader.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = reader.response(2, deadline)
        if "error" in listed:
            return Probe(False, f"tools/list failed: {listed['error']}")
        tools = cast(
            "list[dict[str, object]]",
            cast("dict[str, object]", listed.get("result") or {}).get("tools") or [],
        )
        names = tuple(str(t.get("name")) for t in tools)
        return Probe(
            True,
            "answered initialize and tools/list",
            server=str(info.get("name")) if info.get("name") else None,
            version=str(info.get("version")) if info.get("version") else None,
            tools=names,
            elapsed=round(time.monotonic() - start, 1),
        )
    except _ProbeError as e:
        return Probe(False, str(e), elapsed=round(time.monotonic() - start, 1))
    finally:
        reader.close()


class _ProbeError(Exception):
    pass


class _Reader:
    """Line-delimited JSON-RPC over a child's stdio, with a deadline."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.out = b""
        self.err = b""
        self.sel = selectors.DefaultSelector()
        assert proc.stdout is not None and proc.stderr is not None
        self.sel.register(proc.stdout, selectors.EVENT_READ, "out")
        self.sel.register(proc.stderr, selectors.EVENT_READ, "err")
        self.open = 2

    def send(self, message: dict[str, object]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise _ProbeError(self._exited(f"closed its input ({e})")) from e

    def _exited(self, what: str) -> str:
        code = self.proc.poll()
        tail = _last_line(self.err)
        status = f"exited {code}" if code is not None else what
        return f"the server {status}" + (f": {tail}" if tail else "")

    def response(self, want: int, deadline: float) -> dict[str, object]:
        while True:
            while b"\n" in self.out:
                line, self.out = self.out.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError as e:
                    raise _ProbeError(
                        f"non-JSON on stdout: {line[:120].decode(errors='replace')}"
                    ) from e
                if isinstance(message, dict) and message.get("id") == want:
                    return cast("dict[str, object]", message)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProbeError(self._exited("did not answer in time"))
            if self.open == 0:
                raise _ProbeError(self._exited("closed its output"))
            for key, _ in self.sel.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self.sel.unregister(key.fileobj)
                    self.open -= 1
                elif key.data == "out":
                    self.out += chunk
                else:
                    self.err += chunk

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.sel.close()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _startup_check(probe: Probe, key: str | None) -> list[Check]:
    if not probe.ok:
        return [
            Check(
                "MCP startup",
                FAIL,
                probe.detail,
                [
                    "Run the entry's command by hand to see the full error",
                    "uv tool install --force 'fujimoto[mcp]'   (if `mcp` is missing)",
                ],
            )
        ]
    messaging = all(t in probe.tools for t in MESSAGING_TOOLS)
    who = f"{probe.server or '?'} {probe.version or ''}".strip()
    detail = (
        f"{who} answered in {probe.elapsed:g}s with {len(probe.tools)} tools; "
        f"messaging {'on' if messaging else 'off'}"
    )
    checks = [Check("MCP startup", OK, detail)]
    if key and not messaging:
        checks.append(
            Check(
                "messaging tools",
                FAIL,
                "the entry has --key but send_message/broadcast are not registered",
            )
        )
    return checks


def _agent_keys_at(sock: str | None) -> list[str]:
    """Keys the agent at `sock` holds (`ssh-add -L`); [] with no agent."""
    if not sock:
        return []
    env = {**os.environ, "SSH_AUTH_SOCK": sock}
    try:
        result = subprocess.run(
            ["ssh-add", "-L"],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    keys = []
    for line in result.stdout.splitlines():
        try:
            keys.append(broadcast.normalize_public_key(line))
        except broadcast.BroadcastError:
            continue
    return keys


def _signing_checks(
    client: str, key: str | None, env: Mapping[str, str]
) -> list[Check]:
    if key is None:
        return [
            Check(
                "signing",
                OK,
                "no --key in the entry: a read-only server, messaging off by design",
            )
        ]
    try:
        config = broadcast.load_config(strict=True)
    except broadcast.BroadcastError as e:
        return [Check("broadcast config", FAIL, str(e))]
    name = mcp_setup.default_signer(None if client == PREVIEW else client)
    try:
        public = broadcast.normalize_public_key(key)
        name = next((s.name for s in config.signers if s.public_key == public), name)
    except broadcast.BroadcastError:
        pass  # setup's own key check reports it
    sock = env.get("SSH_AUTH_SOCK")
    report = mcp_setup.check_setup(
        key,
        name=name,
        agent_keys=_agent_keys_at(sock),
        config=config,
        agent_sock=sock or "(SSH_AUTH_SOCK not in the entry)",
    )
    return [c for c in report.checks if c.name not in _LOCAL_ONLY]


# -- Report ---------------------------------------------------------------------


def check_entry(
    client: str,
    entry: Mapping[str, object],
    *,
    timeout: float = STARTUP_TIMEOUT,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    probe: Callable[..., Probe] = probe_server,
) -> list[Check]:
    command, args = _argv(entry)
    env = server_env(entry)
    key = entry_key(args)
    checks, path = _launcher_checks(command, env, run)
    checks.append(_path_check(env, key))
    if path is not None:
        result = probe(path, args, env, timeout)
        debug.log(
            "mcp.doctor_probe",
            client=client,
            ok=result.ok,
            tools=len(result.tools),
            elapsed=result.elapsed,
        )
        checks += _startup_check(result, key)
    else:
        checks.append(Check("MCP startup", FAIL, "skipped: no runnable launcher"))
    checks += _signing_checks(client, key, env)
    return checks


def diagnose(
    clients: Sequence[str] | None = None,
    *,
    timeout: float = STARTUP_TIMEOUT,
    paths: Mapping[str, Path] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    probe: Callable[..., Probe] = probe_server,
) -> DoctorReport:
    """Check each client's installed entry; with none, setup's would-be entry."""
    wanted = list(
        dict.fromkeys(
            mcp_setup.CHATGPT if c == mcp_setup.CODEX else c
            for c in (clients or (mcp_setup.CLAUDE_DESKTOP, mcp_setup.CHATGPT))
        )
    )
    targets: list[Target] = []
    for client in wanted:
        try:
            path, entry = mcp_setup.read_client_entry(client, (paths or {}).get(client))
        except mcp_setup.InstallError as e:
            targets.append(
                Target(client, None, False, [Check("client config", FAIL, str(e))])
            )
            continue
        if entry is None:
            targets.append(
                Target(
                    client,
                    str(path),
                    False,
                    [
                        Check(
                            "client config",
                            WARN,
                            "no fujimoto server configured",
                            [f"fujimoto mcp setup {client}"],
                        )
                    ],
                )
            )
            continue
        checks = [Check("client config", OK, "fujimoto server entry found")]
        checks += check_entry(client, entry, timeout=timeout, run=run, probe=probe)
        targets.append(Target(client, str(path), True, checks))
    if not any(t.configured for t in targets) and not clients:
        entry = mcp_setup.desktop_entry(None)
        targets.append(
            Target(
                PREVIEW,
                None,
                False,
                check_entry(PREVIEW, entry, timeout=timeout, run=run, probe=probe),
            )
        )
    report = DoctorReport(targets)
    debug.log(
        "mcp.doctor",
        targets=",".join(t.client for t in targets),
        checks=",".join(
            f"{t.client}:{c.name.replace(' ', '_')}={c.status}"
            for t in targets
            for c in t.checks
        ),
    )
    return report


def format_doctor(report: DoctorReport) -> str:
    out = ["fujimoto MCP doctor (read-only: nothing is signed, sent or changed)"]
    for t in report.targets:
        out.append("")
        if t.client == PREVIEW:
            out.append(
                "No client is configured. The entry `fujimoto mcp setup` would write:"
            )
        else:
            label = _LABELS.get(t.client, t.client)
            out.append(f"{label}  ({_short_path(t.config_path)})")
        for c in t.checks:
            out.append(f"{mcp_setup._MARK[c.status]} {c.name}: {c.detail}")
            out.extend(f"    {line}" for line in c.fix)
    warnings = sum(c.status == WARN for t in report.targets for c in t.checks)
    out.append("")
    if not report.ok:
        out.append("Fix the ✗ items above, then run this again.")
    elif warnings:
        out.append(f"No failures ({warnings} warning{'s' if warnings != 1 else ''}).")
    else:
        out.append("All checks passed.")
    return "\n".join(out)


def _short_path(path: str | None) -> str:
    if not path:
        return "?"
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


def doctor_json(report: DoctorReport) -> dict[str, object]:
    return {
        "ok": report.ok,
        "targets": [
            {
                "client": t.client,
                "config_path": t.config_path,
                "configured": t.configured,
                "ok": t.ok,
                "checks": [asdict(c) for c in t.checks],
            }
            for t in report.targets
        ],
    }
