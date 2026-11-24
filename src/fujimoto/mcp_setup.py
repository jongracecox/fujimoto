"""`fujimoto mcp setup`: check a Claude Desktop setup and say what to do next.

Read-only by design. It never creates keys or edits config: it reports each
step as ok / warn / fail and prints the exact command for the next one, so a
person (or Claude guiding them) runs every change knowingly. The checks are
what make the setup secure:

- the signing key is a **public** key, so the server signs through an SSH
  agent and never reads a private key file;
- the private key, if it is a file at all, has a passphrase;
- the agent holds the key (otherwise every send fails);
- the key is a broadcast signer with role `agent`, so messages never read as
  "from the user"; a key that matches a `user` signer is refused.

Stdlib + fujimoto only, so it works before the `mcp` extra is installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fujimoto import broadcast, debug

DEFAULT_SIGNER = "claude-desktop"
SERVER_NAME = "fujimoto"
OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    # Commands to run (or steps to take) to fix it, in order.
    fix: list[str] = field(default_factory=list)


@dataclass
class SetupReport:
    checks: list[Check]
    desktop_config: dict[str, object]
    desktop_config_path: str

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)


def desktop_config_path() -> Path:
    """Where Claude Desktop reads its MCP servers from."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform.startswith("win"):  # pragma: no cover - not a target
        return (
            Path(os.environ.get("APPDATA", ""))
            / "Claude"
            / "claude_desktop_config.json"
        )
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def fujimoto_command() -> str:
    """An absolute path to run fujimoto: GUI apps don't get the shell's PATH."""
    found = _stable_dir("fujimoto")
    if found:
        return str(Path(found) / "fujimoto")
    return str(Path(sys.argv[0]).absolute())


def server_path() -> str:
    """PATH for the server: where tmux, git and ssh-keygen live right now.

    Claude Desktop starts servers with a minimal PATH, and the lifecycle
    tools need tmux and git (Homebrew's are not on it).
    """
    dirs: list[str] = []
    for tool in ("tmux", "git", "ssh-keygen", "claude"):
        found = _stable_dir(tool)
        if found and found not in dirs:
            dirs.append(found)
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def _stable_dir(tool: str, path: str | None = None) -> str | None:
    """The first PATH directory holding `tool` that isn't a shim directory.

    Terminal wrappers put per-session shim directories ahead on PATH (e.g.
    `~/.cmuxterm/cmux-cli-shims/<id>/claude`), which won't exist when Claude
    Desktop starts the server later.
    """
    for directory in (path if path is not None else os.environ.get("PATH", "")).split(
        os.pathsep
    ):
        # Only the directory's own name and its parent's: the rest of the
        # path (a home or temp directory) says nothing about shims.
        tail = Path(directory).parts[-2:]
        if not directory or any("shim" in part.lower() for part in tail):
            continue
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return directory
    return None


def desktop_entry(public_key: str | None) -> dict[str, object]:
    """The `mcpServers.fujimoto` entry for claude_desktop_config.json."""
    args = ["mcp"]
    if public_key:
        args += ["--key", public_key]
    env = {"PATH": server_path()}
    sock = os.environ.get("SSH_AUTH_SOCK")
    if public_key and sock:
        env["SSH_AUTH_SOCK"] = sock
    return {"command": fujimoto_command(), "args": args, "env": env}


def _private_key_file(key_arg: str) -> Path | None:
    """The private key file next to a `.pub` path, if there is one."""
    path = Path(key_arg).expanduser()
    if path.suffix == ".pub" and path.with_suffix("").is_file():
        return path.with_suffix("")
    return None


def _is_private_key_file(key_arg: str) -> bool:
    path = Path(key_arg).expanduser()
    if not path.is_file():
        return False
    try:
        head = path.read_text(errors="replace")[:100]
    except OSError:
        return False
    return "PRIVATE KEY" in head


def private_key_has_passphrase(path: Path) -> bool | None:
    """True/False, or None when ssh-keygen can't tell (missing, unreadable).

    `ssh-keygen -y -P ''` derives the public key with an empty passphrase:
    it succeeds only for an unencrypted key.
    """
    try:
        result = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return False
    # Match the phrase, not a word: stderr also echoes the path, which may
    # contain anything.
    if b"incorrect passphrase" in result.stderr:
        return True
    return None


def _setup_key_steps(name: str) -> list[str]:
    return [
        "Create a key the SSH agent holds and that is protected at rest. Either:",
        "  • a key manager that keeps the private key and asks on use "
        "(1Password, Secretive, a hardware key), or",
        f"  • ssh-keygen -t ed25519 -C {name} -f ~/.ssh/fujimoto_{name.replace('-', '_')}"
        "   (set a passphrase), then",
        f"    ssh-add -c ~/.ssh/fujimoto_{name.replace('-', '_')}"
        "   (-c asks to confirm every signature; needs an ssh-askpass program)",
        f"Then: fujimoto mcp setup --key <public key or .pub path> --name {name}",
    ]


def check_setup(
    key: str | None,
    *,
    name: str = DEFAULT_SIGNER,
    agent_keys: Sequence[str] | None = None,
    config: broadcast.BroadcastSettings | None = None,
) -> SetupReport:
    checks: list[Check] = []

    has_mcp = importlib.util.find_spec("mcp") is not None
    checks.append(
        Check("mcp package", OK, "installed")
        if has_mcp
        else Check(
            "mcp package",
            FAIL,
            "the `mcp` extra is not installed",
            ["uv tool install --force 'fujimoto[mcp]'"],
        )
    )
    tmux = shutil.which("tmux")
    checks.append(
        Check("tmux", OK, tmux)
        if tmux
        else Check(
            "tmux",
            WARN,
            "not on PATH: start/resume tools will fail",
            ["brew install tmux"],
        )
    )

    try:
        config = config or broadcast.load_config(strict=True)
    except broadcast.BroadcastError as e:
        checks.append(Check("broadcast config", FAIL, str(e)))
        return SetupReport(checks, desktop_entry(None), str(desktop_config_path()))

    public_key: str | None = None
    if key is None:
        checks.append(
            Check(
                "signing key",
                WARN,
                "none given: the server will only read sessions and start/stop "
                "them; send_message and broadcast are off",
                _setup_key_steps(name),
            )
        )
    elif _is_private_key_file(key):
        checks.append(
            Check(
                "signing key",
                FAIL,
                "that is a private key file; pass the public key (.pub) so the "
                "server signs through the SSH agent and never reads the private key",
                [f"fujimoto mcp setup --key {key}.pub --name {name}"],
            )
        )
    else:
        try:
            public_key = broadcast.normalize_public_key(key)
        except broadcast.BroadcastError as e:
            checks.append(Check("signing key", FAIL, str(e), _setup_key_steps(name)))
        else:
            checks.append(Check("signing key", OK, public_key[:40] + "…"))

    if public_key is not None:
        private = _private_key_file(key or "")
        if private is not None:
            encrypted = private_key_has_passphrase(private)
            if encrypted is False:
                checks.append(
                    Check(
                        "private key at rest",
                        FAIL,
                        f"{private} has no passphrase: any process running as "
                        "you can read it and sign as this agent",
                        [f"ssh-keygen -p -f {private}   (add a passphrase)"],
                    )
                )
            elif encrypted is True:
                checks.append(
                    Check(
                        "private key at rest", OK, f"{private} is passphrase-protected"
                    )
                )
            else:
                checks.append(
                    Check(
                        "private key at rest",
                        WARN,
                        f"could not tell whether {private} has a passphrase",
                    )
                )
        else:
            checks.append(
                Check(
                    "private key at rest",
                    OK,
                    "no private key file beside it (held by an agent or key manager)",
                )
            )

        held = list(agent_keys) if agent_keys is not None else broadcast._agent_keys()
        sock = os.environ.get("SSH_AUTH_SOCK")
        if public_key in held:
            checks.append(Check("SSH agent", OK, f"holds the key ({sock})"))
        else:
            fix = ["Add the key to your SSH agent (ssh-add -c <private key>), or"]
            fix.append(
                "point SSH_AUTH_SOCK at the agent that holds it, then re-run setup"
            )
            checks.append(
                Check(
                    "SSH agent",
                    FAIL,
                    f"the agent at {sock or '(SSH_AUTH_SOCK unset)'} does not hold the key",
                    fix,
                )
            )
        checks.append(
            Check(
                "per-use confirmation",
                WARN,
                "fujimoto can't see whether the agent asks before each signature. "
                "Without that, any local process that reaches the agent can sign "
                f"as {name}; prefer `ssh-add -c` or a key manager that prompts",
            )
        )

        matches = [s for s in config.signers if s.public_key == public_key]
        if not matches:
            checks.append(
                Check(
                    "broadcast signer",
                    FAIL,
                    "the key is not a trusted signer yet",
                    [
                        f'fujimoto broadcast add-signer {name} "{public_key}" --role agent',
                        "Sessions trust signers fixed at launch: restart running "
                        "sessions to receive this agent's messages",
                    ],
                )
            )
        elif any(s.role == broadcast.USER for s in matches):
            user = next(s for s in matches if s.role == broadcast.USER)
            checks.append(
                Check(
                    "broadcast signer",
                    FAIL,
                    f"the key is the user signer {user.name!r}: messages would read "
                    "as typed by you. Use a separate key for the MCP server",
                    _setup_key_steps(name),
                )
            )
        else:
            signer = matches[0]
            checks.append(
                Check("broadcast signer", OK, f"{signer.name} (role {signer.role})")
            )

    checks.append(
        Check("broadcast enabled", OK, "sessions get the hooks")
        if config.enabled
        else Check(
            "broadcast enabled",
            WARN if public_key is None else FAIL,
            "off: sessions launched now can't receive messages",
            ["fujimoto broadcast enable", "then restart sessions that should listen"],
        )
    )

    report = SetupReport(checks, desktop_entry(public_key), str(desktop_config_path()))
    debug.log(
        "mcp.setup",
        key=public_key is not None,
        checks=",".join(f"{c.name.replace(' ', '_')}={c.status}" for c in checks),
    )
    return report


def format_report(report: SetupReport) -> str:
    out = ["fujimoto MCP server setup", ""]
    for c in report.checks:
        out.append(f"{_MARK[c.status]} {c.name}: {c.detail}")
        out.extend(f"    {line}" for line in c.fix)
    out.append("")
    if not report.ok:
        out.append("Fix the ✗ items above, then run this again.")
        out.append("")
    out.append(
        f'Add this under "mcpServers" in {report.desktop_config_path}, '
        "then restart Claude Desktop:"
    )
    out.append("")
    out.append(json.dumps({SERVER_NAME: report.desktop_config}, indent=2))
    return "\n".join(out)


def report_json(report: SetupReport) -> dict[str, object]:
    return {
        "ok": report.ok,
        "checks": [asdict(c) for c in report.checks],
        "desktop_config_path": report.desktop_config_path,
        "desktop_config": {SERVER_NAME: report.desktop_config},
    }
