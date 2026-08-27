"""Diagnostic logging for `fujimoto --debug` / `--debug-redacted`.

The module owns a process-wide optional :class:`DebugLogger`. When debug mode
is off (the default) every logging helper here is a cheap no-op, so call sites
throughout the codebase can log unconditionally.

Two flavours of logging exist:

* ``--debug`` — log everything verbatim.
* ``--debug-redacted`` — log everything, but pass user-identifying strings
  (usernames, project names, branch names, path components) through
  :func:`redact_text` first. The redaction token keeps the *shape* of the
  value, which is usually what matters when diagnosing a bug::

      [REDACTED-3f9a-12-CONTAINS.-]
       |         |    |  |
       |         |    |  `-- the non-alphanumeric characters it contains
       |         |    `----- character length
       |         `---------- stable 4-hex fingerprint (same value -> same token)
       `-------------------- marker

Secrets (anything whose env var name looks like a key/token/password) are
always redacted, in both modes.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterable, Mapping

LOG_DIR_ENV = "FUJIMOTO_LOG_DIR"
DEFAULT_LOG_DIR = Path.home() / ".fujimoto" / "logs"

MAX_OUTPUT_CHARS = 2000
"""Captured subprocess output longer than this is truncated in the log."""

DEFAULT_SERIES_CAP = 10
"""How many items of a repeating series are logged in full before summarising.

A machine with fifty worktrees produced fifty near-identical inventory lines
per series, which buried the handful of events describing what the user did.
The first few carry the shape of the data; the rest are counted instead.
"""

_SECRET_NAME_RE = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE", re.IGNORECASE
)

_SAFE_ARG_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Path components preserved verbatim under redaction. The bar is deliberately
# high: a component is only safe if it CANNOT plausibly be something the user
# named. Operating-system directories qualify, and so do dotted config
# directories (a project is not called `.claude`). Ordinary words do NOT —
# `git`, `logs`, `src` and even `fujimoto` are all perfectly good directory and
# repository names, and preserving them leaked the project name of any repo
# that happened to be called one of them (including fujimoto's own).
_SAFE_PATH_COMPONENTS = frozenset(
    {
        "",
        "~",
        ".cache",
        ".claude",
        ".config",
        ".fujimoto",
        ".git",
        ".local",
        ".venv",
        "Applications",
        "Contents",
        "Library",
        "MacOS",
        "System",
        "Users",
        "Volumes",
        "bin",
        "etc",
        "folders",
        "home",
        "homebrew",
        "local",
        "mnt",
        "nix",
        "opt",
        "private",
        "projects",
        "root",
        "sbin",
        "share",
        "snap",
        "tmp",
        "usr",
        "var",
        "worktrees",
    }
)
"""Path components that cannot plausibly be a name the user chose."""

# Git ref vocabulary. These are kept readable in *command arguments* only
# (`git symbolic-ref refs/remotes/origin/HEAD` is noise once redacted), and
# never in filesystem paths — a directory called `origin` or `main` is a name
# the user chose, whereas a ref called `origin/main` is git's own vocabulary.
_SAFE_REF_COMPONENTS = frozenset(
    {
        "HEAD",
        "heads",
        "main",
        "master",
        "origin",
        "refs",
        "remotes",
        "tags",
        "upstream",
    }
)
"""Git's own ref vocabulary, safe to keep in a command line."""

# Directories under which fujimoto and Claude Code own the names. A component
# is only matched against `_OWNED_NAMES` when its parent is one of these (or is
# itself an owned name), so `~/.cache/fujimoto` is preserved while
# `~/git/fujimoto` — a repo someone chose to call that — is not.
_OWNED_PARENTS = frozenset({".cache", ".claude", ".config", ".fujimoto", ".local"})

# Names fujimoto and Claude Code choose themselves. Preserving them is not just
# readability: hashing a *constant* puts a known plaintext in the log, and a
# reader who knows `~/.cache/<X>` is always "fujimoto" can then match that
# fingerprint anywhere else it appears — which is how the project name leaked
# back out despite the salt.
_OWNED_NAMES = frozenset(
    {
        "config_once_applied",
        "fujimoto",
        "logs",
        "meta.json",
        "projects",
        "sessions.json",
        "settings.json",
        "version_check.json",
        "worktrees",
    }
)
"""Names fujimoto/Claude own, preserved only under an owned parent."""

_OWN_LOG_RE = re.compile(r"^fujimoto-\d{8}-\d{6}-\d+\.log$")

# Widget-id prefixes used by the TUI. The prefix says what kind of row was
# involved, which is the diagnostic part; the tail can carry a project or
# branch name and is redacted.
_ID_PREFIX_RE = re.compile(r"^([a-z]{2,8})-(.*)$")
_PLAIN_WORDS_RE = re.compile(r"^[a-z][a-z-]*$")

# Env vars worth capturing even though they are not fujimoto's own.
_INTERESTING_ENV = (
    "HOME",
    "SHELL",
    "TERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "COLORTERM",
    "TMUX",
    "TMUX_PANE",
    "LANG",
    "LC_ALL",
    "EDITOR",
    "VISUAL",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "PWD",
    "SSH_TTY",
    "COLUMNS",
    "LINES",
)

_ENV_PREFIXES = ("FUJIMOTO_", "CLAUDE_", "ANTHROPIC_")

_TOOLS = ("tmux", "git", "claude", "gh", "code", "uv", "brew")

_VERSION_ARGS = {
    "tmux": ["-V"],
    "git": ["--version"],
    "claude": ["--version"],
    "gh": ["--version"],
    "uv": ["--version"],
    "brew": ["--version"],
}


_salt = b""
"""Per-run redaction salt. Never written to the log — see `_fingerprint`."""


def _fingerprint(text: str) -> str:
    """A short fingerprint, so equal values redact identically within a log.

    Salted per run, and the salt is never logged. Without a salt the digest is
    known-plaintext: a log necessarily contains fixed strings (fujimoto's own
    `~/.cache/fujimoto`, for one), which would hand a reader the digest of a
    known word — and from there any project name could be confirmed by
    guessing it and hashing. Salting keeps correlation *within* one log, which
    is all it is for, while making a fingerprint meaningless outside it.
    """
    return hashlib.sha256(_salt + text.encode("utf-8", "replace")).hexdigest()[:4]


def redact_text(text: str) -> str:
    """Replace `text` with a shape-preserving redaction token.

    >>> redact_text("")
    '[REDACTED-EMPTY]'
    >>> redact_text("myproject")
    '[REDACTED-9a76-9]'
    >>> redact_text("20260827-fix.thing")
    '[REDACTED-e027-18-CONTAINS-.]'
    """
    if not text:
        return "[REDACTED-EMPTY]"
    specials: list[str] = []
    for char in text:
        if not char.isalnum() and char not in specials:
            specials.append(char)
    token = f"[REDACTED-{_fingerprint(text)}-{len(text)}"
    if specials:
        token += "-CONTAINS" + "".join(specials)
    return token + "]"


def redact_path(path: object) -> str:
    """Redact a filesystem path, keeping separators, depth and system dir names.

    The user's home directory collapses to ``~`` so the log still shows when a
    path is home-relative. Only components that cannot be a name the user chose
    survive — ``git`` is redacted, ``.claude`` is not.

    >>> redact_path("/Users/alice/git/thing")
    '/Users/[REDACTED-2bd8-5]/[REDACTED-9a88-3]/[REDACTED-5de9-5]'
    """
    raw = str(path)
    home = str(Path.home())
    if raw == home:
        return "~"
    if home != "/" and raw.startswith(home + "/"):
        raw = "~/" + raw[len(home) + 1 :]

    out: list[str] = []
    owned_context = False
    for part in raw.split("/"):
        if part in _SAFE_PATH_COMPONENTS:
            out.append(part)
            owned_context = part in _OWNED_PARENTS
            continue
        if owned_context and (part in _OWNED_NAMES or _OWN_LOG_RE.match(part)):
            out.append(part)
            continue
        out.append(redact_text(part))
        owned_context = False
    return "/".join(out)


def redact_arg(arg: str) -> str:
    """Redact one element of a command line, keeping flags and subcommands.

    Lowercase words (``rev-parse``, ``worktree``, ``main``) and anything
    starting with ``-`` survive; paths are redacted component-wise; everything
    else (branch names, session names) becomes a redaction token.

    >>> redact_arg("rev-parse")
    'rev-parse'
    >>> redact_arg("--show-toplevel")
    '--show-toplevel'
    >>> redact_arg("worktree/20260827-x")
    '[REDACTED-d635-8]/[REDACTED-dc1a-10-CONTAINS-]'
    """
    if not arg:
        return arg
    if _is_git_ref(arg):
        return arg
    if "/" in arg or arg.startswith("~"):
        if arg.startswith("-") and "=" in arg:
            flag, _, value = arg.partition("=")
            return f"{flag}={redact_path(value)}"
        return redact_path(arg)
    if arg.startswith("-") or _SAFE_ARG_RE.match(arg):
        return arg
    return redact_text(arg)


def _is_git_ref(arg: str) -> bool:
    """True if every component of `arg` is git's own ref vocabulary.

    `refs/remotes/origin/HEAD` is git's, not the user's; `origin/my-feature`
    is not, and must still be redacted.
    """
    return all(part in _SAFE_REF_COMPONENTS for part in arg.split("/"))


def redact_ref(ref: object) -> str:
    """Redact a git ref, keeping git's own vocabulary readable.

    `main` and `HEAD` are every repo's, so hiding them costs signal and buys
    nothing; a branch someone named is redacted.

    >>> redact_ref("main")
    'main'
    >>> redact_ref("worktree/20260827-thing").startswith("[REDACTED-")
    True
    """
    text = str(ref)
    if _is_git_ref(text):
        return text
    return redact_text(text)


def redact_id(item_id: object) -> str:
    """Redact a TUI widget id, keeping the prefix that says what kind it is.

    `action-create` is entirely fujimoto's own vocabulary and survives whole;
    `ds-myproject--direct-1` keeps only its `ds-` prefix, since the rest names
    a project.

    >>> redact_id("action-create")
    'action-create'
    >>> redact_id("ds-myproject--direct-1")
    'ds-[REDACTED-91ce-19-CONTAINS-]'
    """
    text = str(item_id)
    match = _ID_PREFIX_RE.match(text)
    if match is None:
        return redact_text(text)
    prefix, tail = match.groups()
    # A tail of plain lowercase words is a static id (`action-create`,
    # `sa-fork`); anything with digits or separators carries user data.
    if _PLAIN_WORDS_RE.match(tail):
        return text
    return f"{prefix}-{redact_text(tail)}"


def is_secret_name(name: str) -> bool:
    """True if an env var name looks like it holds a credential."""
    return _SECRET_NAME_RE.search(name) is not None


class DebugLogger:
    """Append-only plain-text diagnostic log for one fujimoto run."""

    def __init__(self, path: Path, *, redact: bool, stream: IO[str] | None = None):
        self.path = path
        self.redact = redact
        self._stream = (
            stream if stream is not None else path.open("a", encoding="utf-8")
        )
        self._seen: dict[str, str] = {}
        self._series: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}

    # -- redaction helpers ------------------------------------------------
    def value(self, raw: object) -> str:
        """Redact a user-identifying value if redaction is enabled."""
        return redact_text(str(raw)) if self.redact else str(raw)

    def path_value(self, raw: object) -> str:
        """Redact a path if redaction is enabled."""
        return redact_path(raw) if self.redact else str(raw)

    def secret(self, raw: object) -> str:
        """Redact a credential — always, regardless of mode."""
        return f"[SECRET-{len(str(raw))}]"

    def args(self, args: Iterable[str]) -> str:
        """Render a command line, redacting arguments if enabled."""
        parts = [str(a) for a in args]
        if self.redact:
            parts = [redact_arg(p) for p in parts]
        return " ".join(parts)

    # -- writing ----------------------------------------------------------
    def _write(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        try:
            self._stream.write(f"{stamp}Z {line}\n")
            self._stream.flush()
        except (OSError, ValueError):  # pragma: no cover - closed/full disk
            pass

    def raw(self, text: str) -> None:
        """Write pre-formatted text (no timestamp), e.g. a banner."""
        try:
            self._stream.write(text.rstrip("\n") + "\n")
            self._stream.flush()
        except (OSError, ValueError):  # pragma: no cover - closed/full disk
            pass

    def section(self, title: str) -> None:
        self.raw("")
        self.raw(f"===== {title} " + "=" * max(0, 60 - len(title)))

    def event(self, event_name: str, /, **fields: object) -> None:
        """Log one `event key=value ...` line.

        `event_name` is positional-only so `name=` stays usable as a field.
        """
        self._write(_format_event(event_name, fields))

    def event_once(self, key: str, event_name: str, /, **fields: object) -> None:
        """Log an event only when its payload differs from the last one for `key`.

        Used for polled state (Claude session discovery runs every 3s) so a
        long-lived run does not bury the interesting lines.
        """
        payload = _format_event(event_name, fields)
        if self._seen.get(key) == payload:
            return
        self._seen[key] = payload
        self._write(payload)

    def event_capped(
        self,
        series: str,
        event_name: str,
        /,
        limit: int = DEFAULT_SERIES_CAP,
        dedupe_key: str | None = None,
        **fields: object,
    ) -> bool:
        """Log an item of a repeating `series`, up to `limit` distinct items.

        Returns whether the event was written. Items beyond the limit are
        counted and reported by :meth:`close`, so a caller never has to
        remember to summarise, and the log always says how much it left out.

        `dedupe_key` composes the cap with `event_once` semantics: a repeat of
        an item already logged (the home screen re-renders, and re-reads the
        same worktrees) is neither written again nor counted against the cap,
        so the cap spends its budget on *distinct* items.
        """
        if dedupe_key is not None:
            payload = _format_event(event_name, fields)
            if self._seen.get(dedupe_key) == payload:
                return False
            self._seen[dedupe_key] = payload
        seen = self._series.get(series, 0)
        self._series[series] = seen + 1
        if seen < limit:
            self._write(_format_event(event_name, fields))
            return True
        self._suppressed[series] = self._suppressed.get(series, 0) + 1
        return False

    def flush_series(self) -> None:
        """Report what `event_capped` left out. Idempotent."""
        for series in sorted(self._suppressed):
            count = self._suppressed[series]
            self._write(
                _format_event(
                    "series.summarised",
                    {
                        "series": series,
                        "logged": self._series[series] - count,
                        "not_logged": count,
                        "total": self._series[series],
                    },
                )
            )
        self._suppressed.clear()

    def output(self, label: str, text: str) -> None:
        """Log captured command output, indented and truncated.

        Under redaction the output is redacted token-by-token with
        :func:`redact_arg`, so structure (line count, layout, which tokens are
        paths) survives while names do not.
        """
        if not text:
            return
        body = text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "…"
        if self.redact:
            body = "\n".join(
                " ".join(redact_arg(tok) for tok in line.split(" "))
                for line in body.splitlines()
            )
        self._write(f"{label}:")
        for line in body.splitlines():
            self.raw(f"    | {line}")

    def exception(self, label: str, exc: BaseException) -> None:
        self._write(f"{label} exception={type(exc).__name__} message={exc}")
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for sub in line.rstrip("\n").splitlines():
                self.raw(f"    ! {sub}")

    def close(self) -> None:
        self.flush_series()
        try:
            self._stream.close()
        except (OSError, ValueError):  # pragma: no cover
            pass


def _format_event(name: str, fields: Mapping[str, object]) -> str:
    if not fields:
        return name
    rendered = " ".join(f"{k}={_render(v)}" for k, v in fields.items())
    return f"{name} {rendered}"


def _render(value: object) -> str:
    text = "None" if value is None else str(value)
    if text == "" or any(c.isspace() for c in text):
        return f'"{text}"'
    return text


_logger: DebugLogger | None = None


def enable(*, redact: bool, log_dir: Path | None = None) -> DebugLogger:
    """Turn on debug logging and return the logger.

    The log file is `<log_dir>/fujimoto-<YYYYMMDD-HHMMSS>-<pid>.log`, where
    `log_dir` defaults to `$FUJIMOTO_LOG_DIR` or `~/.fujimoto/logs`.
    """
    global _logger, _salt
    _salt = secrets.token_bytes(16)
    if log_dir is None:
        env_dir = os.environ.get(LOG_DIR_ENV)
        log_dir = Path(env_dir).expanduser() if env_dir else DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = log_dir / f"fujimoto-{stamp}-{os.getpid()}.log"
    _logger = DebugLogger(path, redact=redact)
    _logger.raw(
        f"fujimoto debug log — started {datetime.now().isoformat(timespec='seconds')}"
    )
    _logger.raw(f"redaction: {'on' if redact else 'off'}")
    if redact:
        _logger.raw(
            "redaction token: [REDACTED-<fingerprint>-<length>[-CONTAINS<chars>]] — "
            "equal values share a fingerprint; CONTAINS lists the "
            "non-alphanumeric characters present in the original value."
        )
        _logger.raw(
            "fingerprints are salted per run: they correlate values within "
            "this log and mean nothing outside it."
        )
    return _logger


def disable() -> None:
    """Close and drop the active logger (used by tests and at shutdown).

    Also clears the redaction salt, so `redact_text` is deterministic again
    once no run is active — otherwise a salt set by one test would leak into
    another's expectations.
    """
    global _logger, _salt
    if _logger is not None:
        _logger.close()
    _logger = None
    _salt = b""


def logger() -> DebugLogger | None:
    return _logger


def is_enabled() -> bool:
    return _logger is not None


def log_path() -> Path | None:
    return _logger.path if _logger is not None else None


def log(event_name: str, /, **fields: object) -> None:
    """Log an event if debug mode is on, otherwise do nothing."""
    if _logger is not None:
        _logger.event(event_name, **fields)


def log_once(key: str, event_name: str, /, **fields: object) -> None:
    """Log an event only when its payload changed since the last call for `key`."""
    if _logger is not None:
        _logger.event_once(key, event_name, **fields)


def log_capped(
    series: str,
    event_name: str,
    /,
    *,
    limit: int = DEFAULT_SERIES_CAP,
    dedupe_key: str | None = None,
    **fields: object,
) -> bool:
    """Log one item of a repeating series, summarising beyond `limit`.

    Pass `dedupe_key` (a value identifying the *subject*) on a polling path, so
    re-renders don't spend the cap on items already logged. Returns whether it
    was written, so a caller can build a richer summary of the remainder than
    the bare count `close()` reports.
    """
    if _logger is None:
        return False
    return _logger.event_capped(series, event_name, limit, dedupe_key, **fields)


def log_section(title: str) -> None:
    if _logger is not None:
        _logger.section(title)


def log_exception(label: str, exc: BaseException) -> None:
    if _logger is not None:
        _logger.exception(label, exc)


def log_command(
    tool: str,
    args: Iterable[str],
    *,
    cwd: object = None,
    returncode: object = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    """Log a subprocess invocation and its result."""
    if _logger is None:
        return
    fields: dict[str, object] = {"cmd": f"{tool} {_logger.args(args)}".strip()}
    if cwd is not None:
        fields["cwd"] = _logger.path_value(cwd)
    if returncode is not None:
        fields["rc"] = returncode
    _logger.event("run", **fields)
    if stdout:
        _logger.output("  stdout", stdout)
    if stderr:
        _logger.output("  stderr", stderr)


def rv(value: object) -> str:
    """Redact a user-identifying value (no-op when redaction is off)."""
    return _logger.value(value) if _logger is not None else str(value)


def rp(value: object) -> str:
    """Redact a path (no-op when redaction is off)."""
    return _logger.path_value(value) if _logger is not None else str(value)


def rref(value: object) -> str:
    """Redact a git ref, keeping `main`/`HEAD` readable (no-op when off)."""
    if _logger is None or not _logger.redact:
        return str(value)
    return redact_ref(value)


def rid(value: object) -> str:
    """Redact a TUI widget id, keeping its kind prefix (no-op when off)."""
    if _logger is None or not _logger.redact:
        return str(value)
    return redact_id(value)


def _tool_version(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        return "not found on PATH"
    # The basename is the tool we asked about, so keep it readable; only the
    # directories around it can be user-identifying.
    where = f"{rp(Path(resolved).parent)}/{Path(resolved).name}"
    args = _VERSION_ARGS.get(name)
    if args is None:
        return f"found at {where}"
    try:
        result = subprocess.run(
            [name, *args], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env
        return f"found at {where} (version check failed: {exc})"
    version = (result.stdout or result.stderr).strip().splitlines()
    return f"{version[0] if version else '?'} ({where})"


def log_environment() -> None:
    """Record interpreter, platform, tool versions and environment variables."""
    if _logger is None:
        return
    from fujimoto.version import get_version

    _logger.section("fujimoto / system")
    _logger.event(
        "fujimoto",
        version=get_version(),
        executable=rp(sys.executable),
        argv=_logger.args(sys.argv),
        cwd=rp(Path.cwd()),
        log=rp(_logger.path),
    )
    _logger.event(
        "python",
        version=platform.python_version(),
        implementation=platform.python_implementation(),
    )
    _logger.event(
        "platform",
        system=platform.system(),
        release=platform.release(),
        machine=platform.machine(),
        platform=platform.platform(),
    )
    _logger.event(
        "tty", stdin=sys.stdin.isatty(), stdout=sys.stdout.isatty(), pid=os.getpid()
    )

    _logger.section("tool versions")
    for tool in _TOOLS:
        _logger.event("tool", name=tool, detail=_tool_version(tool))

    _logger.section("environment")
    names = sorted(
        name
        for name in os.environ
        if name.startswith(_ENV_PREFIXES) or name in _INTERESTING_ENV
    )
    for name in names:
        raw = os.environ[name]
        if is_secret_name(name):
            rendered = _logger.secret(raw)
        elif "/" in raw or name.endswith(("_ROOT", "_DIR", "HOME", "PWD")):
            rendered = _logger.path_value(raw)
        else:
            rendered = _logger.value(raw) if name not in _INTERESTING_ENV else raw
        _logger.event("env", name=name, value=rendered)
    for name in sorted(_INTERESTING_ENV):
        if name not in os.environ:
            _logger.event("env", name=name, value="[unset]")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    _logger.event("env", name="PATH", entries=len(path_entries))
