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
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from fujimoto import broadcast, debug

DEFAULT_SIGNER = "claude-desktop"


def default_signer(client: str | None) -> str:
    """The signer name `setup` suggests: one per client, so messages name their sender."""
    return "chatgpt" if client in ("chatgpt", "codex") else DEFAULT_SIGNER


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


# fujimoto settings a client must pass on, because GUI apps don't read the
# shell profile that sets them. An explicit allowlist: nothing else from the
# environment (tokens, credentials) is ever copied into a client config.
CARRIED_SETTINGS = ("FUJIMOTO_GIT_ROOT", "FUJIMOTO_WORKTREE_ROOT")


def carried_settings() -> dict[str, str]:
    """The `CARRIED_SETTINGS` that are set, as absolute paths.

    Without them a client-launched server can't resolve a project by name and
    misses worktrees under the root (and their history). Expanded here, so
    the config doesn't depend on the client's `HOME` or a shell.
    """
    out: dict[str, str] = {}
    for name in CARRIED_SETTINGS:
        raw = os.environ.get(name)
        if raw:
            out[name] = str(Path(raw).expanduser().resolve())
    return out


def desktop_entry(public_key: str | None) -> dict[str, object]:
    """The `mcpServers.fujimoto` entry for claude_desktop_config.json."""
    args = ["mcp"]
    if public_key:
        args += ["--key", public_key]
    env = {"PATH": server_path(), **carried_settings()}
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
    agent_sock: str | None = None,
) -> SetupReport:
    """Each step of a secure setup, as `Check`s. Read-only.

    `agent_keys` / `agent_sock` describe the SSH agent to check against
    (default: this process's `SSH_AUTH_SOCK`); `mcp doctor` passes the
    agent a client's config points the server at.
    """
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
        sock = agent_sock or os.environ.get("SSH_AUTH_SOCK")
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
        taken = next(
            (
                s
                for s in config.signers
                if s.name == name and s.public_key != public_key
            ),
            None,
        )
        if not matches and taken is not None:
            checks.append(
                Check(
                    "broadcast signer",
                    FAIL,
                    f"the signer name {name!r} is already used by another key "
                    f"(role {taken.role}); give this key its own name",
                    [
                        f'fujimoto mcp setup [CLIENT] --key "{public_key}" --name <new name>'
                    ],
                )
            )
        elif not matches:
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


def client_config(report: SetupReport) -> dict[str, object]:
    """Just the copy-pasteable client config: `{"mcpServers": {"fujimoto": …}}`.

    The shape Claude Desktop's config file and most MCP clients use, so it
    can be merged in as is.
    """
    return {"mcpServers": {SERVER_NAME: report.desktop_config}}


def format_failures(report: SetupReport) -> str:
    """The failing checks and their fixes, for stderr when config is withheld."""
    out = ["fujimoto: setup has failing checks, so no config was printed:"]
    for c in report.checks:
        if c.status == FAIL:
            out.append(f"{_MARK[c.status]} {c.name}: {c.detail}")
            out.extend(f"    {line}" for line in c.fix)
    out.append("Run `fujimoto mcp setup` for the full report.")
    return "\n".join(out)


def report_json(report: SetupReport) -> dict[str, object]:
    return {
        "ok": report.ok,
        "checks": [asdict(c) for c in report.checks],
        "desktop_config_path": report.desktop_config_path,
        "desktop_config": {SERVER_NAME: report.desktop_config},
    }


# --------------------------------------------------------------------------
# `fujimoto mcp setup claude-desktop`: write the entry into the client config
# --------------------------------------------------------------------------

CLAUDE_DESKTOP, CHATGPT, CODEX = "claude-desktop", "chatgpt", "codex"
CLIENTS = (CLAUDE_DESKTOP, CHATGPT, CODEX)
ADDED, UNCHANGED, REPLACED, CONFLICT = "added", "unchanged", "replaced", "conflict"


class InstallError(Exception):
    """The client config could not be read or written; nothing was changed."""


@dataclass
class InstallResult:
    status: str  # added | unchanged | replaced | conflict
    path: Path
    backup: Path | None = None
    # A unified diff of the old entry against the new one (conflict/replaced).
    diff: str = ""
    client: str = CLAUDE_DESKTOP


def codex_config_path() -> Path:
    """The config the ChatGPT desktop app and the Codex CLI share.

    `$CODEX_HOME/config.toml`, else `~/.codex/config.toml`. The ChatGPT app
    keeps its local (stdio) MCP servers under `[mcp_servers.*]` there.
    """
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def config_path(client: str) -> Path:
    return desktop_config_path() if client == CLAUDE_DESKTOP else codex_config_path()


def _entry_diff(old: object, new: object) -> str:
    import difflib

    a = json.dumps(old, indent=2, sort_keys=True).splitlines()
    b = json.dumps(new, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(
            a, b, "current fujimoto entry", "new fujimoto entry", lineterm=""
        )
    )


def _our_args(entry: object) -> list[object] | None:
    """The args of an entry `setup` wrote, or None if it isn't one.

    Ours runs a `fujimoto` launcher with `mcp` in its args. Those are updated
    in place on a re-run (e.g. adding `--key` later); any other `fujimoto`
    entry is the user's own and needs `--replace`.
    """
    if not isinstance(entry, dict):
        return None
    fields = cast("dict[str, object]", entry)
    command, args = fields.get("command"), fields.get("args")
    if not (isinstance(command, str) and Path(command).name == "fujimoto"):
        return None
    if not isinstance(args, list) or "mcp" not in args:
        return None
    return cast("list[object]", args)


def _is_ours(entry: object) -> bool:
    return _our_args(entry) is not None


def _keep_global_flags(current: object, entry: dict[str, object]) -> dict[str, object]:
    """`entry`, keeping global flags the user put before `mcp` (`--debug-redacted`)."""
    old_args = _our_args(current)
    new_args = entry.get("args")
    if old_args is None or not isinstance(new_args, list):
        return entry
    leading = old_args[: old_args.index("mcp")]
    if not leading:
        return entry
    return {**entry, "args": [*leading, *new_args]}


def _without_ours(data: dict[str, object], table: str) -> dict[str, object]:
    """`data` with `<table>.fujimoto` removed, for "nothing else changed" checks."""
    out = dict(data)
    servers = out.get(table)
    if isinstance(servers, dict):
        rest = {k: v for k, v in servers.items() if k != SERVER_NAME}
        if rest:
            out[table] = rest
        else:  # the table only held ours (or was created for it)
            del out[table]
    return out


# -- JSON (Claude Desktop) -------------------------------------------------


def _indent_of(raw: str) -> int:
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return 2


def _parse_json(raw: str, path: Path) -> dict[str, object]:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise InstallError(
            f"{path} is not valid JSON ({e}); fix it by hand, nothing was changed"
        ) from e
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object; nothing was changed")
    return data


def _render_json(
    raw: str | None, data: dict[str, object], entry: dict[str, object]
) -> str:
    data = dict(data)
    servers = data.get("mcpServers", {})
    data["mcpServers"] = {**cast("dict[str, object]", servers), SERVER_NAME: entry}
    text = json.dumps(data, indent=_indent_of(raw) if raw else 2, ensure_ascii=False)
    return text + "\n" if raw is None or raw.endswith("\n") else text


# -- TOML (ChatGPT desktop / Codex) ----------------------------------------

_OUR_HEADER = re.compile(
    r"""^\s*\[\s*mcp_servers\s*\.\s*(?:fujimoto|"fujimoto"|'fujimoto')\s*(?:\.[^\]]*)?\]\s*(?:\#.*)?$"""
)
_ANY_HEADER = re.compile(r"^\s*\[")


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string for anything setup writes.
    return json.dumps(value, ensure_ascii=False)


def toml_block(entry: dict[str, object]) -> str:
    """`[mcp_servers.fujimoto]` (+ `.env`) for an entry, as TOML text."""
    lines = [f"[mcp_servers.{SERVER_NAME}]"]
    lines.append(f"command = {_toml_string(str(entry['command']))}")
    args = cast("list[object]", entry.get("args", []))
    lines.append("args = [" + ", ".join(_toml_string(str(a)) for a in args) + "]")
    env = cast("dict[str, object]", entry.get("env") or {})
    if env:
        lines += ["", f"[mcp_servers.{SERVER_NAME}.env]"]
        lines += [f"{k} = {_toml_string(str(v))}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _parse_toml(raw: str, path: Path) -> dict[str, object]:
    import tomllib

    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise InstallError(
            f"{path} is not valid TOML ({e}); fix it by hand, nothing was changed"
        ) from e


def _render_toml(
    raw: str | None, data: dict[str, object], entry: dict[str, object]
) -> str:
    """`raw` with fujimoto's own tables replaced by `entry`; nothing else touched.

    Removes every `[mcp_servers.fujimoto…]` table (each runs to the next
    header) and puts the new block where the first one was, or at the end.
    The caller re-parses the result, so a form this can't edit (an inline
    table, dotted keys) is refused rather than written.
    """
    block = toml_block(entry)
    if not raw:
        return block
    lines = raw.splitlines(keepends=True)
    kept: list[str] = []
    insert_at: int | None = None
    skipping = False
    for line in lines:
        if _OUR_HEADER.match(line):
            skipping = True
            if insert_at is None:
                insert_at = len(kept)
            continue
        if skipping and _ANY_HEADER.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    if insert_at is None:
        text = "".join(kept)
        if not text.strip():
            return block
        if text.endswith("\n\n"):
            return text + block
        return text + ("\n" if text.endswith("\n") else "\n\n") + block
    head, tail = "".join(kept[:insert_at]), "".join(kept[insert_at:])
    return head + block + ("\n" + tail if tail else "")


# -- Shared install ----------------------------------------------------------


@dataclass(frozen=True)
class _Format:
    table: str
    parse: Callable[[str, Path], dict[str, object]]
    render: Callable[[str | None, dict[str, object], dict[str, object]], str]
    missing_dir: str


_FORMATS = {
    CLAUDE_DESKTOP: _Format(
        "mcpServers",
        _parse_json,
        _render_json,
        "is Claude Desktop installed and run once?",
    ),
    CHATGPT: _Format(
        "mcp_servers",
        _parse_toml,
        _render_toml,
        "is the ChatGPT desktop app (or Codex) installed and run once?",
    ),
}
_FORMATS[CODEX] = _FORMATS[CHATGPT]


def read_client_entry(
    client: str, path: Path | None = None
) -> tuple[Path, dict[str, object] | None]:
    """The client config's `fujimoto` server entry, read-only.

    None when the file or the entry is missing. A file that doesn't parse,
    or an entry that isn't a table, is an `InstallError`.
    """
    fmt = _FORMATS[client]
    path = path or config_path(client)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return path, None
    except (OSError, UnicodeDecodeError) as e:
        raise InstallError(f"cannot read {path}: {e}") from e
    servers = fmt.parse(raw, path).get(fmt.table)
    if servers is None:
        return path, None
    if not isinstance(servers, dict):
        raise InstallError(f'"{fmt.table}" in {path} is not a table/object')
    entry = cast("dict[str, object]", servers).get(SERVER_NAME)
    if entry is None:
        return path, None
    if not isinstance(entry, dict):
        raise InstallError(f"the fujimoto entry in {path} is not a table/object")
    return path, cast("dict[str, object]", entry)


def install_client_config(
    client: str,
    entry: dict[str, object],
    *,
    path: Path | None = None,
    replace: bool = False,
    stamp: str | None = None,
) -> InstallResult:
    """Add or update the `fujimoto` server in a client's config file.

    An entry fujimoto wrote before (`_is_ours`) is updated in place, keeping
    any global flags before `mcp`; any other `fujimoto` entry is only
    overwritten with `replace`. Every other part of the file is kept: the new
    text is parsed before writing and refused unless only fujimoto's entry
    differs. An existing file is copied to `<name>.bak-<stamp>` first, the
    new one replaces it atomically with the same permissions, and it is read
    back, restoring the backup on any difference. A missing file is created
    0600; a missing config directory means the client isn't installed.
    """
    import tempfile
    from datetime import datetime

    fmt = _FORMATS[client]
    path = path or config_path(client)
    if not path.parent.is_dir():
        raise InstallError(f"{path.parent} does not exist: {fmt.missing_dir}")
    raw: str | None = None
    data: dict[str, object] = {}
    if path.exists():
        try:
            raw = path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            raise InstallError(f"cannot read {path}: {e}") from e
        data = fmt.parse(raw, path)
    servers = data.get(fmt.table, {})
    if not isinstance(servers, dict):
        raise InstallError(
            f'"{fmt.table}" in {path} is not a table/object; nothing was changed'
        )

    current = cast("dict[str, object]", servers).get(SERVER_NAME)
    entry = _keep_global_flags(current, entry)
    log = "mcp.client_install"
    if current == entry:
        debug.log(log, client=client, status=UNCHANGED)
        return InstallResult(UNCHANGED, path, client=client)
    diff = _entry_diff(current, entry) if current is not None else ""
    if current is not None and not replace and not _is_ours(current):
        debug.log(log, client=client, status=CONFLICT)
        return InstallResult(CONFLICT, path, diff=diff, client=client)

    text = fmt.render(raw, data, entry)

    def unchanged_but_ours(written: str) -> bool:
        try:
            after = fmt.parse(written, path)
        except InstallError:  # e.g. our block next to an inline-table one
            return False
        ours = cast("dict[str, object]", after.get(fmt.table) or {}).get(SERVER_NAME)
        return ours == entry and _without_ours(after, fmt.table) == _without_ours(
            data, fmt.table
        )

    if not unchanged_but_ours(text):
        debug.log(log, client=client, status="refused")
        raise InstallError(
            f"{path} defines the fujimoto server in a form setup can't edit safely "
            "(an inline table or dotted keys?); change it by hand, nothing was changed"
        )

    backup: Path | None = None
    mode = 0o600
    if raw is not None:
        stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
        raise InstallError(f"cannot write {path}: {e}") from e

    if not unchanged_but_ours(path.read_text()):  # pragma: no cover - racing writer
        if backup is not None:
            shutil.copy2(backup, path)
        raise InstallError(f"{path} did not read back as written; restored the backup")

    status = REPLACED if current is not None else ADDED
    debug.log(log, client=client, status=status, backup=backup is not None)
    return InstallResult(status, path, backup, diff, client)


def install_desktop_config(
    entry: dict[str, object],
    *,
    path: Path | None = None,
    replace: bool = False,
    stamp: str | None = None,
) -> InstallResult:
    """`install_client_config` for Claude Desktop."""
    return install_client_config(
        CLAUDE_DESKTOP, entry, path=path, replace=replace, stamp=stamp
    )


def client_snippet(client: str, entry: dict[str, object]) -> str:
    """What `--config-only` prints for a client: its own config format."""
    if client in (CHATGPT, CODEX):
        return toml_block(entry).rstrip("\n")
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)


_RESTART = {
    CLAUDE_DESKTOP: "Quit Claude Desktop (⌘Q) and reopen it to load the server.",
    CHATGPT: (
        "Quit the ChatGPT app (⌘Q) and reopen it to load the server. The Codex "
        "CLI reads the same file, so it gets the server too."
    ),
}
_RESTART[CODEX] = (
    "Start a new Codex session to load the server. The ChatGPT desktop app reads "
    "the same file: quit (⌘Q) and reopen it."
)
_APP = {
    CLAUDE_DESKTOP: "Claude Desktop",
    CHATGPT: "ChatGPT/Codex",
    CODEX: "ChatGPT/Codex",
}


def format_install(result: InstallResult) -> str:
    app = _APP[result.client]
    if result.status == UNCHANGED:
        return (
            f"{app} already has this fujimoto entry ({result.path}); nothing changed."
        )
    if result.status == CONFLICT:
        return "\n".join(
            [
                f"{app} already has a different fujimoto entry in {result.path}:",
                "",
                result.diff,
                "",
                "It doesn't look like one fujimoto wrote, so nothing changed. "
                "Re-run with --replace to use the new entry.",
            ]
        )
    verb = "Added" if result.status == ADDED else "Updated"
    lines = [f"{verb} the fujimoto entry in {result.path}."]
    if result.backup is not None:
        lines.append(f"Backup of the previous file: {result.backup}")
    if result.diff:
        lines += ["", result.diff]
    lines += ["", _RESTART[result.client]]
    return "\n".join(lines)


def install_json(result: InstallResult) -> dict[str, object]:
    return {
        "client": result.client,
        "status": result.status,
        "path": str(result.path),
        "backup": str(result.backup) if result.backup else None,
        "diff": result.diff or None,
    }
