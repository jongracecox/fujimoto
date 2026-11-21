"""Signed broadcast messages to every running fujimoto session.

`fujimoto broadcast send "…"` reaches every listening Claude session (or one
project's), which deals with it at its next safe stopping point. Delivery uses
only documented Claude Code hooks; nothing types into a terminal and nothing
talks to Claude Code's private messaging socket.

How a message travels:

1. The sender builds a JSON payload (id, scope, created/expires, text), signs
   it with `ssh-keygen -Y sign` in the `fujimoto-broadcast` namespace, and
   writes payload + signature into the mailbox (`~/.cache/fujimoto/broadcast/
   messages/`).
2. Every fujimoto-launched session runs a *waiter* (`python -m
   fujimoto.broadcast wait`) as a `SessionStart` and a `Stop` hook with
   `async` + `asyncRewake`. The waiter blocks until a new mailbox entry
   appears, checks it, and exits 2 with the text on stderr — which Claude
   Code documents as waking the session "immediately even when the session is
   idle". The `Stop` hook re-arms a fresh waiter after every turn; a lock keeps
   it to one waiter per Claude process.
3. The waiter writes a receipt per delivery or rejection, which `broadcast
   status` reads.

Security: anything running as the user can write to the mailbox, so a file
being there proves nothing. A waiter delivers only what verifies against the
signers fixed into its hook command when the session launched — so an agent
that can write files still cannot broadcast without a signing key. Expiry
bounds replay, and the scope is part of the signed payload.

This module is imported by the per-session waiter, so it must stay light:
stdlib and `debug` at module scope, anything heavier (the global config,
git, the Claude registry) lazily.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from fujimoto import debug

NAMESPACE = "fujimoto-broadcast"
PAYLOAD_VERSION = 1
# A session-targeted payload is version 2. A waiter that predates `session`
# would ignore the field and — since a targeted payload has no project —
# deliver it to every session; version 2 is what it rejects instead.
TARGETED_PAYLOAD_VERSION = 2
DEFAULT_TTL = timedelta(hours=1)
# Bounds how long a signed message can be replayed into a session.
MAX_TTL = timedelta(hours=24)
# A payload created this far in the future is rejected (clock skew allowance).
CLOCK_SKEW = timedelta(minutes=5)
MAX_MESSAGE_CHARS = 4000
# Mailbox files bigger than this are not even read.
MAX_FILE_BYTES = 64 * 1024
POLL_INTERVAL = 1.0
# Claude Code enforces `timeout` on an `asyncRewake` hook and kills it
# silently. A message sent after that is not lost — the session's next `Stop`
# re-arms a waiter, which delivers it — but it is late, so keep this long.
HOOK_TIMEOUT = 7 * 24 * 3600
EXIT_DELIVER = 2
# Mailbox/receipt entries are pruned this long after they expire.
PRUNE_AFTER = timedelta(days=1)

PREAMBLE = (
    "Broadcast from the user (via fujimoto, signed by {signer}). "
    "Deal with this at the next safe stopping point in your current work."
)

_SIGNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_KEY_TYPES = (
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "ssh-rsa",
)
# A Claude session id: a UUID in practice; any id-shaped token is accepted.
_SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_TTL = re.compile(r"^(\d+)\s*([smhd])$")
_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class BroadcastError(Exception):
    """A broadcast could not be built, signed, verified or configured."""


@dataclass(frozen=True)
class Signer:
    """A key allowed to sign broadcasts: a label and an OpenSSH public key.

    `public_key` is the `<type> <base64>` pair, without a comment, so two
    spellings of the same key compare equal.
    """

    name: str
    public_key: str


@dataclass
class BroadcastSettings:
    """The `broadcast` section of the global config, validated.

    Off by default: with `enabled` false no session gets the broadcast hooks.
    `signers` are the keys a session accepts a broadcast from, fixed into each
    session when it launches. `signing_key` is what `send` signs with — a key
    path or a literal public key, like git's `user.signingkey`; `None` means
    "the agent key matching a signer".
    """

    enabled: bool = False
    signers: list[Signer] = field(default_factory=list)
    signing_key: str | None = None


def load_config(*, strict: bool = False) -> BroadcastSettings:
    """Read `broadcast` from `~/.config/fujimoto/config.yaml`.

    Fails closed: an unreadable config means "disabled" (or, with `strict`,
    a `BroadcastError` for the CLI to report), and a signer whose name or key
    doesn't validate is dropped rather than trusted.
    """
    from fujimoto.config import ConfigError
    from fujimoto.global_config import load_global_config

    try:
        raw = load_global_config().broadcast
    except ConfigError as e:
        debug.log("broadcast.config", valid=False)
        if strict:
            raise BroadcastError(str(e)) from e
        return BroadcastSettings()
    signers = []
    for s in raw.signers:
        try:
            signers.append(
                Signer(validate_signer_name(s.name), normalize_public_key(s.public_key))
            )
        except BroadcastError:
            debug.log("broadcast.config_signer_skipped", signer=debug.rv(s.name))
    debug.log_once(
        "broadcast-config",
        "broadcast.config",
        valid=True,
        enabled=raw.enabled,
        signers=len(signers),
        signing_key=raw.signing_key is not None,
    )
    return BroadcastSettings(raw.enabled, signers, raw.signing_key)


def save_config(config: BroadcastSettings) -> None:
    """Write the `broadcast` section, keeping the rest of the file as it was."""
    from fujimoto.config import ConfigError
    from fujimoto.global_config import update_global_key

    value = {
        "enabled": config.enabled,
        "signers": [
            {"name": s.name, "public_key": s.public_key} for s in config.signers
        ],
        "signing_key": config.signing_key,
    }
    try:
        update_global_key("broadcast", value)
    except (ConfigError, OSError) as e:
        raise BroadcastError(f"could not save the global config: {e}") from e
    debug.log(
        "broadcast.config_saved", enabled=config.enabled, signers=len(config.signers)
    )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def broadcast_dir() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "broadcast"


def messages_dir() -> Path:
    return broadcast_dir() / "messages"


def receipts_dir() -> Path:
    return broadcast_dir() / "receipts"


def state_dir() -> Path:
    return broadcast_dir() / "state"


def ensure_dirs() -> None:
    """Create the mailbox tree, private to the user (0700)."""
    for d in (broadcast_dir(), messages_dir(), receipts_dir(), state_dir()):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def _private_dir_ok(path: Path) -> bool:
    """True if `path` is a real directory owned by us and not group/world writable."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.getuid()
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _read_private_file(path: Path) -> bytes:
    """Read a mailbox file, refusing symlinks, foreign owners and oversize files."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BroadcastError("not a regular file")
        if st.st_uid != os.getuid():
            raise BroadcastError("owned by another user")
        if st.st_size > MAX_FILE_BYTES:
            raise BroadcastError("too large")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read(MAX_FILE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    """Create `path` (0600) with `data`; fail if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    _write_new(tmp, data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Payload:
    """What is signed. `project` is `None` for every project.

    `session` targets one Claude session by its full id (resolved by the
    sender from the sessions listening at the time), so a message can never
    land in a different session that later takes the same tmux or peer name.
    It is optional and omitted from the bytes when unset, so payloads written
    before it existed still verify and parse.
    """

    id: str
    project: str | None
    created: datetime
    expires: datetime
    message: str
    session: str | None = None

    def to_bytes(self) -> bytes:
        body: dict[str, object] = {
            "v": PAYLOAD_VERSION if self.session is None else TARGETED_PAYLOAD_VERSION,
            "id": self.id,
            "project": self.project,
            "created": _iso(self.created),
            "expires": _iso(self.expires),
            "message": self.message,
        }
        if self.session is not None:
            body["session"] = self.session
        return json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Payload:
        try:
            raw = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BroadcastError(f"payload is not JSON: {e}") from e
        if not isinstance(raw, dict) or raw.get("v") not in (
            PAYLOAD_VERSION,
            TARGETED_PAYLOAD_VERSION,
        ):
            raise BroadcastError("unsupported payload version")
        msg_id, project, message = raw.get("id"), raw.get("project"), raw.get("message")
        if not isinstance(msg_id, str) or not re.fullmatch(
            r"[A-Za-z0-9-]{1,64}", msg_id
        ):
            raise BroadcastError("bad id")
        if project is not None and not isinstance(project, str):
            raise BroadcastError("bad project")
        session = raw.get("session")
        if (session is None) != (raw["v"] == PAYLOAD_VERSION):
            raise BroadcastError("session target requires payload version 2")
        if session is not None and (
            not isinstance(session, str) or not _SESSION_ID.fullmatch(session)
        ):
            raise BroadcastError("bad session")
        if not isinstance(message, str) or not message.strip():
            raise BroadcastError("empty message")
        if len(message) > MAX_MESSAGE_CHARS:
            raise BroadcastError("message too long")
        try:
            created = datetime.fromisoformat(raw["created"])
            expires = datetime.fromisoformat(raw["expires"])
        except (KeyError, TypeError, ValueError) as e:
            raise BroadcastError("bad timestamps") from e
        if created.tzinfo is None or expires.tzinfo is None:
            raise BroadcastError("timestamps must carry a timezone")
        return cls(msg_id, project, created, expires, message, session)

    def check_time(self, now: datetime) -> None:
        """Raise unless the payload is current and its lifetime is within bounds."""
        if self.expires <= self.created or self.expires - self.created > MAX_TTL:
            raise BroadcastError("lifetime out of range")
        if self.created > now + CLOCK_SKEW:
            raise BroadcastError("created in the future")
        if now >= self.expires:
            raise BroadcastError("expired")

    def covers(self, project: str | None, session_id: str | None = None) -> bool:
        """Whether a session in `project` with id `session_id` is a recipient."""
        if self.session is not None and self.session != session_id:
            return False
        return self.project is None or self.project == project

    @property
    def scope_label(self) -> str:
        if self.session is not None:
            return f"session {self.session[:8]}"
        return "all projects" if self.project is None else f"project {self.project}"


def parse_ttl(text: str) -> timedelta:
    """`30m`, `1h`, `90s`, `1d` → timedelta, within `MAX_TTL`.

    >>> parse_ttl("90m")
    datetime.timedelta(seconds=5400)
    """
    m = _TTL.match(text.strip().lower())
    if not m:
        raise BroadcastError(f"bad --ttl {text!r}: use e.g. 30m, 1h, 12h")
    ttl = timedelta(seconds=int(m[1]) * _TTL_UNITS[m[2]])
    if not timedelta(0) < ttl <= MAX_TTL:
        raise BroadcastError(f"--ttl must be between 1s and {MAX_TTL}")
    return ttl


def new_payload(
    message: str,
    project: str | None,
    ttl: timedelta = DEFAULT_TTL,
    now: datetime | None = None,
    session: str | None = None,
) -> Payload:
    message = message.strip()
    if not message:
        raise BroadcastError("message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise BroadcastError(f"message is longer than {MAX_MESSAGE_CHARS} characters")
    created = (now or _now()).replace(microsecond=0)
    msg_id = f"{created.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    return Payload(msg_id, project, created, created + ttl, message, session)


# ---------------------------------------------------------------------------
# Keys and signers
# ---------------------------------------------------------------------------


def normalize_public_key(text: str) -> str:
    """A literal OpenSSH public key, or a path to a `.pub` file → `type base64`.

    The comment is dropped so two spellings of one key compare equal.
    """
    text = text.strip()
    candidate = Path(text).expanduser()
    if not text.startswith(_KEY_TYPES) and candidate.is_file():
        text = candidate.read_text().strip()
    parts = text.split()
    if len(parts) < 2 or parts[0] not in _KEY_TYPES:
        raise BroadcastError(
            "not an OpenSSH public key (expected e.g. 'ssh-ed25519 AAAA…' "
            "or a path to a .pub file)"
        )
    try:
        base64.b64decode(parts[1], validate=True)
    except binascii.Error as e:
        raise BroadcastError("public key is not valid base64") from e
    return f"{parts[0]} {parts[1]}"


def validate_signer_name(name: str) -> str:
    if not _SIGNER_NAME.match(name):
        raise BroadcastError(
            "signer names are letters, digits and . _ @ - (up to 64 characters)"
        )
    return name


def allowed_signers_text(signers: Iterable[Signer]) -> str:
    """OpenSSH `allowed_signers` lines, each restricted to our namespace."""
    return "".join(
        f'{s.name} namespaces="{NAMESPACE}" {s.public_key}\n' for s in signers
    )


def encode_signers(signers: Sequence[Signer]) -> str:
    """Signers as one shell-safe token, for the hook command line."""
    raw = json.dumps([[s.name, s.public_key] for s in signers], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_signers(token: str) -> list[Signer]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(token.encode()))
        if not isinstance(raw, list) or not raw:
            raise ValueError("expected a non-empty list")
        return [
            Signer(validate_signer_name(n), normalize_public_key(k)) for n, k in raw
        ]
    except (ValueError, TypeError, binascii.Error) as e:
        raise BroadcastError(f"bad --signers: {e}") from e


def _ssh_keygen(
    args: list[str], *, input_bytes: bytes | None = None, interactive: bool = False
) -> subprocess.CompletedProcess[bytes]:
    cmd = ["ssh-keygen", *args]
    try:
        if interactive:
            # Leave stdin on the terminal: a passphrase-protected key file
            # prompts there.
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            result = subprocess.run(cmd, input=input_bytes, capture_output=True)
    except FileNotFoundError as e:
        raise BroadcastError("ssh-keygen not found (OpenSSH 8.1+ is required)") from e
    debug.log(
        "broadcast.ssh_keygen",
        op=args[1] if len(args) > 1 else "?",
        rc=result.returncode,
    )
    return result


def _agent_keys() -> list[str]:
    """Public keys the SSH agent holds, normalized; [] without an agent."""
    try:
        result = subprocess.run(["ssh-add", "-L"], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    keys = []
    for line in result.stdout.splitlines():
        try:
            keys.append(normalize_public_key(line))
        except BroadcastError:
            continue
    return keys


def resolve_signing_key(
    key: str | None, signers: Sequence[Signer], workdir: Path
) -> Path:
    """The file to hand `ssh-keygen -Y sign -f`.

    `key` is a path (public → the agent signs; private → the file signs) or a
    literal public key (the agent signs). With no key, the first agent key
    that is a configured signer is used.
    """
    if key:
        path = Path(key).expanduser()
        if path.is_file():
            debug.log("broadcast.signing_key", via="path")
            return path
        literal = normalize_public_key(key)
        debug.log("broadcast.signing_key", via="literal")
    else:
        approved = {s.public_key for s in signers}
        literal = next((k for k in _agent_keys() if k in approved), "")
        if not literal:
            raise BroadcastError(
                "no signing key: the SSH agent holds none of the configured "
                "signers' keys. Set one with `fujimoto broadcast set-key` or pass --key."
            )
        debug.log("broadcast.signing_key", via="agent")
    pub = workdir / "signing-key.pub"
    pub.write_text(literal + "\n")
    return pub


def sign(data: bytes, key: str | None, signers: Sequence[Signer]) -> bytes:
    """Sign `data` in the broadcast namespace; returns the armoured signature."""
    with tempfile.TemporaryDirectory(prefix="fujimoto-sign-") as tmp:
        work = Path(tmp)
        keyfile = resolve_signing_key(key, signers, work)
        payload = work / "payload.json"
        payload.write_bytes(data)
        result = _ssh_keygen(
            ["-Y", "sign", "-f", str(keyfile), "-n", NAMESPACE, str(payload)],
            interactive=True,
        )
        sig = payload.with_name("payload.json.sig")
        if result.returncode != 0 or not sig.exists():
            detail = result.stderr.decode(errors="replace").strip()
            raise BroadcastError(f"signing failed: {detail or 'ssh-keygen error'}")
        return sig.read_bytes()


def verify(data: bytes, signature: bytes, signers: Sequence[Signer]) -> str:
    """The name of the signer whose key made `signature` over `data`.

    Raises `BroadcastError` unless it verifies against one of `signers` in the
    broadcast namespace. The name comes from verification, never from the
    payload, so a message cannot claim to be from someone else.
    """
    if not signers:
        raise BroadcastError("no signers configured")
    with tempfile.TemporaryDirectory(prefix="fujimoto-verify-") as tmp:
        allowed = Path(tmp) / "allowed_signers"
        allowed.write_text(allowed_signers_text(signers))
        sig = Path(tmp) / "payload.sig"
        sig.write_bytes(signature)
        found = _ssh_keygen(
            [
                "-Y",
                "find-principals",
                "-s",
                str(sig),
                "-f",
                str(allowed),
                "-n",
                NAMESPACE,
            ]
        )
        principal = found.stdout.decode(errors="replace").strip().splitlines()
        if found.returncode != 0 or not principal:
            raise BroadcastError("not signed by a configured signer")
        name = principal[0]
        checked = _ssh_keygen(
            [
                "-Y",
                "verify",
                "-f",
                str(allowed),
                "-I",
                name,
                "-n",
                NAMESPACE,
                "-s",
                str(sig),
            ],
            input_bytes=data,
        )
        if checked.returncode != 0:
            raise BroadcastError("signature does not match the message")
        return name


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One mailbox message: `<arrival-ns>-<payload id>.json` plus `.sig`."""

    path: Path

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def sig_path(self) -> Path:
        return self.path.with_name(self.path.name + ".sig")


def list_entries() -> list[Entry]:
    """Mailbox entries, oldest arrival first. Hidden temp files are ignored."""
    try:
        paths = [
            p
            for p in messages_dir().iterdir()
            if p.suffix == ".json" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    return [Entry(p) for p in sorted(paths, key=lambda p: p.name)]


def post(data: bytes, signature: bytes) -> Entry:
    """Write a signed payload into the mailbox (signature first, then payload)."""
    payload = Payload.from_bytes(data)
    ensure_dirs()
    if any(e.name.endswith(f"-{payload.id}") for e in list_entries()):
        raise BroadcastError(f"broadcast {payload.id} is already in the mailbox")
    entry = Entry(messages_dir() / f"{time.time_ns():020d}-{payload.id}.json")
    _atomic_write(entry.sig_path, signature)
    # The waiter keys on the .json, so it only ever sees a complete pair.
    _atomic_write(entry.path, data)
    debug.log("broadcast.posted", id=payload.id, scope=debug.rv(payload.scope_label))
    return entry


def prune(now: datetime | None = None) -> int:
    """Delete mailbox entries and receipts well past expiry. Returns the count."""
    now = now or _now()
    removed = 0
    for entry in list_entries():
        try:
            payload = Payload.from_bytes(_read_private_file(entry.path))
            stale = now > payload.expires + PRUNE_AFTER
        except (OSError, BroadcastError):
            stale = (
                now.timestamp() - entry.path.stat().st_mtime
                > PRUNE_AFTER.total_seconds()
            )
        if stale:
            entry.path.unlink(missing_ok=True)
            entry.sig_path.unlink(missing_ok=True)
            for r in (receipts_dir() / entry.name).glob("*.json"):
                r.unlink(missing_ok=True)
            try:
                (receipts_dir() / entry.name).rmdir()
            except OSError:
                pass
            removed += 1
    if removed:
        debug.log("broadcast.pruned", entries=removed)
    return removed


# ---------------------------------------------------------------------------
# Receipts and listeners
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    entry: str
    session_id: str
    project: str | None
    cwd: str | None
    outcome: str  # "delivered" | "rejected"
    reason: str | None
    at: str


def _write_receipt(receipt: Receipt) -> None:
    d = receipts_dir() / receipt.entry
    try:
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            d / f"{receipt.session_id}.json",
            json.dumps(receipt.__dict__, indent=2).encode(),
        )
    except OSError as e:
        debug.log_exception("broadcast.receipt_failed", e)


def read_receipts(entry_name: str) -> list[Receipt]:
    out = []
    for p in sorted((receipts_dir() / entry_name).glob("*.json")):
        try:
            out.append(Receipt(**json.loads(p.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    return out


@dataclass(frozen=True)
class Listener:
    """A live waiter: one Claude session that will receive broadcasts.

    `tmux_session` and `peer_name` come from Claude Code's process registry
    (matched by session id), for naming a `--session` target; either can be
    missing.
    """

    pid: int
    session_id: str
    project: str | None
    cwd: str | None
    tmux_session: str | None = None
    peer_name: str | None = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _registry_names() -> dict[str, tuple[str | None, str | None]]:
    """session id → (tmux session, peer name) for running Claude processes."""
    from fujimoto.claude.live import list_live_sessions

    try:
        return {
            s.session_id: (s.tmux_session, s.peer_name) for s in list_live_sessions()
        }
    except OSError:
        return {}


def list_listeners() -> list[Listener]:
    """Waiters that are running now, from their lock files, with the names
    Claude's registry knows them by."""
    names = _registry_names()
    out = []
    for p in sorted(state_dir().glob("*.lock")):
        try:
            raw = json.loads(p.read_text())
            listener = Listener(
                int(raw["pid"]),
                str(raw["session_id"]),
                raw.get("project"),
                raw.get("cwd"),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _pid_alive(listener.pid):
            tmux_session, peer_name = names.get(listener.session_id, (None, None))
            out.append(
                replace(listener, tmux_session=tmux_session, peer_name=peer_name)
            )
    return out


def resolve_target(ref: str) -> Listener:
    """The one listening session `ref` names: its tmux session, its peer
    name, or a prefix (4+ characters) of its Claude session id.

    Resolved against the sessions listening *now*, so the signed payload can
    carry the exact session id. No match or several matches is an error that
    lists what is listening.
    """
    ref = ref.strip()
    listeners = list_listeners()
    exact = [
        li for li in listeners if ref in (li.tmux_session, li.peer_name, li.session_id)
    ]
    matches = exact or (
        [li for li in listeners if len(ref) >= 4 and li.session_id.startswith(ref)]
    )
    debug.log(
        "broadcast.resolve_target", listeners=len(listeners), matches=len(matches)
    )
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(_listener_name(li) for li in listeners) or "none"
    if not matches:
        raise BroadcastError(
            f"no listening session matches {ref!r} (listening: {available}). "
            "A session listens only if launched with broadcast enabled, and not "
            "while it is handling a broadcast."
        )
    raise BroadcastError(f"{ref!r} matches {len(matches)} sessions: {available}")


def _listener_name(li: Listener) -> str:
    return li.tmux_session or li.peer_name or li.session_id[:8]


# ---------------------------------------------------------------------------
# Sending and status (used by `fujimoto broadcast`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    payload: Payload
    entry: Entry
    signer: str
    listeners: list[Listener]
    receipts: list[Receipt]


def _await_receipts(
    entry: Entry,
    expected: int,
    wait: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[Receipt]:
    deadline = clock() + wait
    receipts = read_receipts(entry.name)
    while len(receipts) < expected and clock() < deadline:
        sleep(0.25)
        receipts = read_receipts(entry.name)
    return receipts


def send(
    message: str,
    project: str | None,
    *,
    ttl: timedelta = DEFAULT_TTL,
    key: str | None = None,
    wait: float = 5.0,
    config: BroadcastSettings | None = None,
    session: str | None = None,
) -> SendResult:
    """Build, sign, self-verify and post a broadcast, then collect receipts.

    `session` (a tmux name, peer name or id prefix) targets one listening
    session; it is resolved first and the payload names its exact id, with
    no project scope.
    """
    config = config or load_config()
    if not config.signers:
        raise BroadcastError(
            "no signers configured: add your key with `fujimoto broadcast add-signer`"
        )
    if session is not None:
        target = resolve_target(session)
        payload = new_payload(message, None, ttl, session=target.session_id)
    else:
        payload = new_payload(message, project, ttl)
    data = payload.to_bytes()
    signature = sign(data, key or config.signing_key, config.signers)
    # Fail here, not silently in every session: a key that isn't a configured
    # signer would be rejected by all of them.
    signer = verify(data, signature, config.signers)
    return _post_and_collect(payload, data, signature, signer, wait)


def submit(
    data: bytes,
    signature: bytes,
    *,
    wait: float = 5.0,
    config: BroadcastSettings | None = None,
) -> SendResult:
    """Post a payload signed elsewhere (`send --signed`), after checking it."""
    config = config or load_config()
    payload = Payload.from_bytes(data)
    payload.check_time(_now())
    signer = verify(data, signature, config.signers)
    return _post_and_collect(payload, data, signature, signer, wait)


def _post_and_collect(
    payload: Payload, data: bytes, signature: bytes, signer: str, wait: float
) -> SendResult:
    prune()
    entry = post(data, signature)
    listeners = [
        li for li in list_listeners() if payload.covers(li.project, li.session_id)
    ]
    receipts = (
        _await_receipts(entry, len(listeners), wait)
        if listeners and wait > 0
        else read_receipts(entry.name)
    )
    debug.log(
        "broadcast.sent",
        id=payload.id,
        signer=debug.rv(signer),
        listeners=len(listeners),
        receipts=len(receipts),
    )
    return SendResult(payload, entry, signer, listeners, receipts)


@dataclass(frozen=True)
class StatusRow:
    entry: str
    payload: Payload | None
    receipts: list[Receipt]


def status(ref: str | None = None) -> list[StatusRow]:
    """Recent broadcasts (newest first), or the one whose id contains `ref`."""
    rows = []
    for entry in reversed(list_entries()):
        if ref and ref not in entry.name:
            continue
        try:
            payload: Payload | None = Payload.from_bytes(_read_private_file(entry.path))
        except (OSError, BroadcastError):
            payload = None
        rows.append(StatusRow(entry.name, payload, read_receipts(entry.name)))
    return rows


# ---------------------------------------------------------------------------
# Output for `fujimoto broadcast`
# ---------------------------------------------------------------------------


def _short_path(path: str | None) -> str:
    if not path:
        return "?"
    home = str(Path.home())
    return (
        "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    )


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


def _receipt_line(r: Receipt) -> str:
    mark = "✓" if r.outcome == "delivered" else "✗"
    reason = f" ({r.reason})" if r.reason else ""
    return (
        f"  {mark} {r.project or '-'}  {r.session_id[:8]}  {_short_path(r.cwd)}{reason}"
    )


def format_send(result: SendResult) -> str:
    p = result.payload
    lines = [
        f"Broadcast {p.id} to {p.scope_label}, signed by {result.signer}, "
        f"expires {_local(p.expires)}."
    ]
    if not result.listeners:
        lines.append(
            "No sessions are listening in scope. Sessions get broadcast when "
            "they are launched with it enabled (see `fujimoto broadcast show`)."
        )
        return "\n".join(lines)
    delivered = [r for r in result.receipts if r.outcome == "delivered"]
    lines.append(
        f"Delivered to {len(delivered)} of {len(result.listeners)} listening sessions:"
    )
    lines.extend(_receipt_line(r) for r in result.receipts)
    pending = len(result.listeners) - len(result.receipts)
    if pending > 0:
        lines.append(
            f"{pending} not confirmed yet — check with `fujimoto broadcast status {p.id}`."
        )
    return "\n".join(lines)


def send_json(result: SendResult) -> dict[str, object]:
    p = result.payload
    return {
        "id": p.id,
        "entry": result.entry.name,
        "project": p.project,
        "session": p.session,
        "signer": result.signer,
        "expires": _iso(p.expires),
        "listening": len(result.listeners),
        "receipts": [r.__dict__ for r in result.receipts],
    }


def format_status(rows: Sequence[StatusRow]) -> str:
    if not rows:
        return "No broadcasts in the mailbox."
    out = []
    for row in rows:
        p = row.payload
        if p is None:
            out.append(f"{row.entry}  (unreadable)")
        else:
            state = "expired" if _now() >= p.expires else f"expires {_local(p.expires)}"
            first = p.message.splitlines()[0]
            first = first if len(first) <= 70 else first[:69] + "…"
            delivered = sum(r.outcome == "delivered" for r in row.receipts)
            out.append(
                f"{p.id}  {p.scope_label}  {state}  delivered {delivered}\n  “{first}”"
            )
        out.extend(_receipt_line(r) for r in row.receipts)
    return "\n".join(out)


def status_json(rows: Sequence[StatusRow]) -> list[dict[str, object]]:
    return [
        {
            "entry": row.entry,
            "id": row.payload.id if row.payload else None,
            "project": row.payload.project if row.payload else None,
            "session": row.payload.session if row.payload else None,
            "expires": _iso(row.payload.expires) if row.payload else None,
            "message": row.payload.message if row.payload else None,
            "receipts": [r.__dict__ for r in row.receipts],
        }
        for row in rows
    ]


def show_json(config: BroadcastSettings) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "signers": [
            {"name": s.name, "public_key": s.public_key} for s in config.signers
        ],
        "signing_key": config.signing_key,
        "listening": [li.__dict__ for li in list_listeners()],
    }


def format_show(config: BroadcastSettings) -> str:
    lines = [f"Broadcast: {'enabled' if config.enabled else 'disabled'}"]
    if config.signers:
        lines.append("Signers:")
        lines.extend(f"  {s.name}  {s.public_key[:40]}…" for s in config.signers)
    else:
        lines.append("Signers: none (add one with `fujimoto broadcast add-signer`)")
    lines.append(
        f"Signing key: {config.signing_key or 'the agent key matching a signer'}"
    )
    listeners = list_listeners()
    lines.append(f"Listening sessions: {len(listeners)}")
    lines.extend(
        f"  {li.project or '-'}  {li.session_id[:8]}  "
        f"{li.tmux_session or li.peer_name or '-'}  {_short_path(li.cwd)}"
        for li in listeners
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Launch: the hooks a session gets
# ---------------------------------------------------------------------------


def hook_command(
    project: str | None, signers: Sequence[Signer], debug_mode: str | None = None
) -> str:
    """The waiter invocation. Signers travel in the command itself, so a
    running session's trust can't be changed by editing a file."""
    args = [
        sys.executable,
        "-m",
        "fujimoto.broadcast",
        "wait",
        "--signers",
        encode_signers(signers),
    ]
    if project:
        args += ["--project", project]
    if debug_mode:
        args.append(f"--{debug_mode}")
    return shlex.join(args)


def hook_settings(
    project: str | None, signers: Sequence[Signer], debug_mode: str | None = None
) -> dict[str, object]:
    hook = {
        "type": "command",
        "command": hook_command(project, signers, debug_mode),
        "async": True,
        "asyncRewake": True,
        "timeout": HOOK_TIMEOUT,
    }
    return {"hooks": {"SessionStart": [{"hooks": [hook]}], "Stop": [{"hooks": [hook]}]}}


def _load_settings_arg(value: str, working_dir: Path) -> dict[str, object]:
    text = value.strip()
    if not text.startswith("{"):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = working_dir / path
        text = path.read_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    return data


def merge_settings_args(
    args: Sequence[str], ours: dict[str, object], working_dir: Path
) -> tuple[str, ...]:
    """`args` with our hooks added under `--settings`.

    claude keeps only the *last* `--settings` it is given (verified: two
    flags each adding a SessionStart hook run only the second), so when the
    launch options already carry one, its value is read and our hooks are
    merged into it rather than added alongside.
    """
    out = list(args)
    idx = None
    for i, a in enumerate(out):
        if a == "--settings" or a.startswith("--settings="):
            idx = i
    if idx is None:
        return (*out, "--settings", json.dumps(ours, separators=(",", ":")))
    if out[idx] == "--settings":
        if idx + 1 >= len(out):
            raise BroadcastError("--settings in the launch options has no value")
        value, span = out[idx + 1], 2
    else:
        value, span = out[idx].split("=", 1)[1], 1
    try:
        existing = _load_settings_arg(value, working_dir)
    except (OSError, ValueError) as e:
        raise BroadcastError(f"cannot read the launch options' --settings: {e}") from e
    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise BroadcastError("the launch options' --settings has a non-object 'hooks'")
    hooks = cast("dict[str, Any]", hooks)
    for event, groups in cast("dict[str, Any]", ours["hooks"]).items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise BroadcastError(
                f"the launch options' --settings has a malformed {event} hook"
            )
        current.extend(groups)
    out[idx : idx + span] = ["--settings", json.dumps(existing, separators=(",", ":"))]
    return tuple(out)


def _project_opted_out(working_dir: Path) -> bool:
    from fujimoto.config import ConfigError
    from fujimoto.git import GitError, get_main_worktree_root
    from fujimoto.project_config import load_project_config

    try:
        root = get_main_worktree_root(working_dir)
        return not load_project_config(root).broadcast
    except (GitError, ConfigError, OSError):
        return False  # no repo, or no readable config: nothing to opt out


def launch_args(
    project: str | None, working_dir: Path, args: Sequence[str]
) -> tuple[str, ...]:
    """The claude arguments for a launch, with broadcast hooks when they apply.

    Never raises: a launch must not fail because broadcast can't be set up.
    The reason it was left out is in the debug log.
    """
    config = load_config()
    reason = None
    if not config.enabled:
        reason = "disabled"
    elif not config.signers:
        reason = "no-signers"
    elif _project_opted_out(working_dir):
        reason = "project-opt-out"
    if reason:
        debug.log("broadcast.launch", included=False, reason=reason)
        return tuple(args)
    logger = debug.logger()
    mode = None if logger is None else ("debug-redacted" if logger.redact else "debug")
    try:
        merged = merge_settings_args(
            args, hook_settings(project, config.signers, mode), working_dir
        )
    except BroadcastError as e:
        debug.log(
            "broadcast.launch",
            included=False,
            reason="settings-merge",
            detail=debug.rv(str(e)),
        )
        print(f"fujimoto: broadcast not enabled for this session: {e}", file=sys.stderr)
        return tuple(args)
    debug.log(
        "broadcast.launch",
        included=True,
        project=debug.rv(project),
        signers=len(config.signers),
        merged="--settings" in args or any(a.startswith("--settings=") for a in args),
    )
    return merged


# ---------------------------------------------------------------------------
# The waiter (runs inside each session as an asyncRewake hook)
# ---------------------------------------------------------------------------


def _parent_pid(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True
        )
        return int(out.stdout.strip())
    except (OSError, ValueError):
        return None


def find_claude_pid(start: int | None = None, max_depth: int = 6) -> int | None:
    """The Claude Code process this hook runs under: the nearest ancestor with
    a registry file. Keying the lock on it (not the session id) keeps one
    waiter per process across `/clear`, which changes the session id."""
    from fujimoto.claude.live import get_claude_sessions_dir

    registry = get_claude_sessions_dir()
    pid: int | None = os.getppid() if start is None else start
    for _ in range(max_depth):
        if pid is None or pid <= 1:
            return None
        if (registry / f"{pid}.json").exists():
            return pid
        pid = _parent_pid(pid)
    return None


@dataclass
class _WaiterState:
    mark: str  # newest mailbox entry already considered
    delivered: list[str]  # payload ids delivered to this process


def _load_state(path: Path) -> _WaiterState | None:
    try:
        raw = json.loads(path.read_text())
        return _WaiterState(
            str(raw["mark"]), [str(i) for i in raw.get("delivered", [])]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_state(path: Path, state: _WaiterState) -> None:
    _atomic_write(
        path,
        json.dumps({"mark": state.mark, "delivered": state.delivered[-200:]}).encode(),
    )


@dataclass(frozen=True)
class _Checked:
    entry: Entry
    payload: Payload | None
    signer: str | None
    reason: str | None  # None = deliver


def check_entry(
    entry: Entry,
    signers: Sequence[Signer],
    project: str | None,
    now: datetime,
    session_id: str | None = None,
) -> _Checked:
    """Decide what a waiter does with one mailbox entry."""
    try:
        data = _read_private_file(entry.path)
        signature = _read_private_file(entry.sig_path)
        payload = Payload.from_bytes(data)
    except (OSError, BroadcastError) as e:
        return _Checked(entry, None, None, f"unreadable: {e}")
    if not payload.covers(project, session_id):
        return _Checked(entry, payload, None, "out-of-scope")
    try:
        payload.check_time(now)
        signer = verify(data, signature, signers)
    except BroadcastError as e:
        return _Checked(entry, payload, None, str(e))
    return _Checked(entry, payload, signer, None)


def render(delivered: Sequence[_Checked]) -> str:
    parts = []
    for c in delivered:
        assert c.payload is not None
        parts.append(f"{PREAMBLE.format(signer=c.signer)}\n\n{c.payload.message}")
    return "\n\n---\n\n".join(parts)


def wait(
    hook_input: dict[str, object],
    signers: Sequence[Signer],
    project: str | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
    claude_pid: int | None = None,
) -> tuple[int, str]:
    """Block until a broadcast for this session arrives. Returns (exit code, stderr text).

    Exit 0 means "nothing to say" (another waiter already serves this
    process, or `max_polls` ran out); exit 2 wakes the session with the text.
    """
    session_id = str(hook_input.get("session_id") or "unknown")
    event = str(hook_input.get("hook_event_name") or "?")
    cwd = hook_input.get("cwd")
    key = str(claude_pid) if claude_pid else f"s-{session_id}"
    if not _private_dir_ok(broadcast_dir()):
        ensure_dirs()
    lock = state_dir() / f"{key}.lock"
    state_path = state_dir() / f"{key}.json"

    lock_body = json.dumps(
        {"pid": os.getpid(), "session_id": session_id, "project": project, "cwd": cwd}
    ).encode()
    if not _take_lock(lock, lock_body):
        debug.log("broadcast.wait", outcome="already-armed", event=event)
        return 0, ""
    state = _load_state(state_path)
    entries = list_entries()
    if state is None or (
        event == "SessionStart" and hook_input.get("source") == "startup"
    ):
        # A new process starts from "now": it does not replay older broadcasts.
        state = _WaiterState(entries[-1].name if entries else "", [])
        _save_state(state_path, state)
    debug.log(
        "broadcast.wait",
        outcome="armed",
        event=event,
        signers=len(signers),
        project=debug.rv(project),
    )

    polls = 0
    try:
        while True:
            fresh = [e for e in list_entries() if e.name > state.mark]
            delivered: list[_Checked] = []
            for entry in fresh:
                checked = check_entry(
                    entry, signers, project, datetime.now(timezone.utc), session_id
                )
                state.mark = entry.name
                if checked.reason == "out-of-scope":
                    continue
                pid_ = checked.payload.id if checked.payload else entry.name
                if checked.reason is None and pid_ in state.delivered:
                    continue
                if checked.reason is None:
                    state.delivered.append(pid_)
                    delivered.append(checked)
                debug.log(
                    "broadcast.wait_entry",
                    entry=entry.name,
                    outcome="delivered" if checked.reason is None else "rejected",
                    reason=checked.reason,
                )
                _write_receipt(
                    Receipt(
                        entry.name,
                        session_id,
                        project,
                        cwd if isinstance(cwd, str) else None,
                        "delivered" if checked.reason is None else "rejected",
                        checked.reason,
                        _iso(_now()),
                    )
                )
            if fresh:
                _save_state(state_path, state)
            if delivered:
                return EXIT_DELIVER, render(delivered)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return 0, ""
            sleep(POLL_INTERVAL)
    finally:
        if _read_lock_pid(lock) == os.getpid():
            lock.unlink(missing_ok=True)


def _take_lock(lock: Path, body: bytes) -> bool:
    """Create `lock` exclusively; a lock left by a dead waiter is taken over.

    Exclusive create, not check-then-write: a `SessionStart` and a `Stop`
    hook starting together must not both become the waiter.
    """
    for _ in range(2):
        try:
            _write_new(lock, body)
            return True
        except FileExistsError:
            holder = _read_lock_pid(lock)
            if holder is not None and _pid_alive(holder):
                return False
            # Dead (or unreadable) holder — e.g. killed by the hook timeout,
            # which gives it no chance to clean up.
            lock.unlink(missing_ok=True)
    return False


def _read_lock_pid(lock: Path) -> int | None:
    try:
        return int(json.loads(lock.read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m fujimoto.broadcast wait …` — the hook entry point."""
    parser = argparse.ArgumentParser(prog="python -m fujimoto.broadcast")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser(
        "wait", help="(hook) wait for a broadcast, then wake the session"
    )
    w.add_argument("--signers", required=True)
    w.add_argument("--project")
    w.add_argument("--debug", action="store_true")
    w.add_argument("--debug-redacted", action="store_true")
    args = parser.parse_args(argv)
    if args.debug or args.debug_redacted:
        debug.enable(redact=args.debug_redacted)
    try:
        signers = decode_signers(args.signers)
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            hook_input = {}
        if not isinstance(hook_input, dict):
            hook_input = {}
        code, text = wait(
            hook_input, signers, args.project, claude_pid=find_claude_pid()
        )
    except BroadcastError as e:
        debug.log("broadcast.wait", outcome="error", detail=debug.rv(str(e)))
        return 0
    finally:
        debug.disable()
    if text:
        print(text, file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised via the hook
    sys.exit(main())
