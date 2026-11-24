from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from fujimoto import cmux, debug

META_KEY_ENV = "FUJIMOTO_META_KEY"
DEFAULT_META_KEY = "C-a"
PREFIX_KEY_ENV = "FUJIMOTO_TMUX_PREFIX"
DEFAULT_PREFIX_KEY = "C-b"

QUICK_TERMINAL_KEY_ENV = "FUJIMOTO_QUICK_TERMINAL_KEY"
DEFAULT_QUICK_TERMINAL_KEY = "C-`"


def _meta_key() -> str:
    """Return the configured fujimoto meta key, or empty string if disabled."""
    return os.environ.get(META_KEY_ENV, DEFAULT_META_KEY)


def meta_key() -> str:
    """Public accessor for the fujimoto meta key (empty when disabled)."""
    return _meta_key()


def _prefix_key() -> str:
    """Return the configured tmux prefix key."""
    return os.environ.get(PREFIX_KEY_ENV, DEFAULT_PREFIX_KEY)


def quick_terminal_key() -> str:
    """Return the configured quick-terminal key, or empty string if disabled."""
    return os.environ.get(QUICK_TERMINAL_KEY_ENV, DEFAULT_QUICK_TERMINAL_KEY)


def _meta_key_label(key: str) -> str:
    """Render a tmux key spec like 'C-f' as a status-bar label like '^F'."""
    if key.startswith("C-") and len(key) == 3:
        return f"^{key[2].upper()}"
    return key


class TmuxError(Exception):
    pass


def set_terminal_title(title: str) -> None:
    """Set the terminal window/tab title via OSC escape sequence.

    Works in iTerm2 and most modern terminals. Silently ignored otherwise.

    Writes to ``sys.__stdout__`` rather than ``sys.stdout`` so the sequence
    reaches the real terminal even while a Textual app is running (Textual
    redirects ``sys.stdout`` to an internal capture, but leaves
    ``sys.__stdout__`` connected to the tty). Falls back to ``sys.stdout``
    when the original stream is unavailable.
    """
    stream = sys.__stdout__ or sys.stdout
    try:
        stream.write(f"\033]0;{title}\007")
        stream.flush()
    except OSError:
        pass


def is_tmux_installed() -> bool:
    found = shutil.which("tmux")
    debug.log_once("tmux-installed", "tmux.installed", path=debug.rp(found))
    return found is not None


_LINUX_TMUX_HINTS: list[tuple[str, str]] = [
    ("apt-get", "sudo apt-get install -y tmux"),
    ("dnf", "sudo dnf install -y tmux"),
    ("pacman", "sudo pacman -S --noconfirm tmux"),
    ("zypper", "sudo zypper install -y tmux"),
    ("apk", "sudo apk add tmux"),
]


def _linux_install_hint() -> str:
    for binary, command in _LINUX_TMUX_HINTS:
        if shutil.which(binary):
            return command
    return "your distribution's package manager"


def install_tmux() -> None:
    """Install tmux. Raises TmuxError on failure.

    macOS: installs via brew. Linux: cannot install automatically (would
    require sudo); instead raises a TmuxError with a distro-appropriate
    install command for the user to run.
    """
    if sys.platform.startswith("linux"):
        hint = _linux_install_hint()
        raise TmuxError(f"tmux is not installed. Run: {hint}")

    if not shutil.which("brew"):
        raise TmuxError("brew is not installed. Install tmux manually.")
    result = subprocess.run(["brew", "install", "tmux"])
    if result.returncode != 0:
        raise TmuxError("Failed to install tmux via brew")
    if not shutil.which("tmux"):
        raise TmuxError("tmux was installed but not found on PATH")


def list_all_sessions() -> list[str]:
    """Return all tmux session names."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        debug.log_once(
            "tmux-list",
            "tmux.list_sessions",
            rc=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
        return []
    names = result.stdout.strip().splitlines()
    debug.log_once(
        "tmux-list",
        "tmux.list_sessions",
        count=len(names),
        names=",".join(debug.rv(n) for n in names),
    )
    return names


def list_project_sessions(project_name: str) -> list[str]:
    """Return tmux session names that belong to the given project."""
    prefix = f"{project_name}/"
    return [s for s in list_all_sessions() if s.startswith(prefix)]


def session_name(project_name: str, worktree_dir_name: str) -> str:
    """Build a tmux session name from project and worktree directory.

    >>> session_name("my-app", "20260309-fix-tests")
    'my-app/20260309-fix-tests'
    """
    return f"{project_name}/{worktree_dir_name}"


def get_session_path(name: str) -> Path | None:
    """Return the start directory of the named tmux session, or None."""
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            name,
            "#{session_path}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        debug.log("tmux.session_path", session=debug.rv(name), rc=result.returncode)
        return None
    out = result.stdout.strip()
    debug.log("tmux.session_path", session=debug.rv(name), path=debug.rp(out))
    return Path(out) if out else None


def list_session_paths() -> dict[str, Path]:
    """Every tmux session's start directory, keyed by session name.

    One `list-sessions` call rather than a `display-message` per session.
    Empty when no server is running — or when tmux is not installed at all,
    since the read-only CLI commands that use this must not require it.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        debug.log("tmux.session_paths", error=type(exc).__name__)
        return {}
    if result.returncode != 0:
        debug.log("tmux.session_paths", rc=result.returncode)
        return {}
    paths: dict[str, Path] = {}
    for line in result.stdout.splitlines():
        name, sep, path = line.partition("\t")
        if sep and name and path:
            paths[name] = Path(path)
    debug.log("tmux.session_paths", count=len(paths))
    return paths


def display_message(name: str, message: str) -> None:
    """Show a transient message in the named tmux session's status line."""
    subprocess.run(
        ["tmux", "display-message", "-t", name, message],
        capture_output=True,
    )


def session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    debug.log(
        "tmux.session_exists",
        session=debug.rv(name),
        exists=result.returncode == 0,
    )
    return result.returncode == 0


def rename_session(old_name: str, new_name: str) -> None:
    """Rename a tmux session."""
    result = subprocess.run(
        ["tmux", "rename-session", "-t", old_name, new_name],
        capture_output=True,
    )
    debug.log(
        "tmux.rename",
        old=debug.rv(old_name),
        new=debug.rv(new_name),
        rc=result.returncode,
    )
    if result.returncode != 0:
        raise TmuxError(f"Failed to rename session: {old_name}")


def kill_session(name: str) -> None:
    """Kill a tmux session by name."""
    result = subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True,
    )
    debug.log("tmux.kill", session=debug.rv(name), rc=result.returncode)
    if result.returncode != 0:
        raise TmuxError(f"Failed to kill session: {name}")


def _ensure_focus_events() -> None:
    """Turn on the tmux server's ``focus-events`` option.

    Off by default, so panes never see focus in/out events and Claude Code
    shows a "tmux focus-events off" hint in every session. Claude Code reads
    the option once at startup (``tmux show -gv focus-events``) and caches
    it, so this has to run straight after ``new-session``, before claude
    has finished loading.

    A failure to set it is logged and ignored rather than raised.

    The option is server-wide. tmux only sends the focus-reporting enable
    sequence to a client's terminal when it attaches, so changing it does
    not reach clients already attached — which is fine here, because
    fujimoto attaches after creating the session.
    """
    result = subprocess.run(
        ["tmux", "show-options", "-sv", "focus-events"],
        capture_output=True,
        text=True,
    )
    previous = result.stdout.strip() if result.returncode == 0 else None
    if previous == "on":
        debug.log("tmux.focus_events", previous=previous, changed=False)
        return
    # Never raise: focus events are cosmetic, and a tmux that rejects the
    # option (too old, odd server state) must not abort the session launch.
    result = subprocess.run(
        ["tmux", "set-option", "-s", "focus-events", "on"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        debug.log(
            "tmux.focus_events",
            previous=previous,
            changed=False,
            error=result.returncode,
            stderr=debug.rv(result.stderr.strip()),
        )
        return
    debug.log("tmux.focus_events", previous=previous, changed=True)


def _ensure_extended_keys() -> None:
    """Ensure tmux server forwards extended key sequences (like Shift+Enter).

    Both options are server/global-level:
    - ``extended-keys always`` forces CSI u sequences to all panes
      (``on`` only works if the app sends the kitty activation sequence,
      which Claude Code does not)
    - ``terminal-features xterm*:extkeys`` enables the extkeys capability

    Requires tmux 3.2+.
    """
    subprocess.run(
        ["tmux", "set-option", "-g", "extended-keys", "always"],
        check=True,
    )
    result = subprocess.run(
        ["tmux", "show-options", "-s", "terminal-features"],
        capture_output=True,
        text=True,
    )
    if "extkeys" not in result.stdout:
        subprocess.run(
            ["tmux", "set-option", "-s", "-a", "terminal-features", "xterm*:extkeys"],
            check=True,
        )


def _apply_quick_terminal_setting() -> None:
    """Re-apply the quick-terminal binding if the user has enabled it.

    Called on every session create so the binding survives a tmux server
    restart. Imported lazily to avoid a circular dependency at module load.
    """
    from fujimoto.settings import load_settings

    if load_settings().quick_terminal_enabled:
        enable_quick_terminal_binding()


def enable_quick_terminal_binding() -> None:
    """Install the server-global Ctrl-` (or configured key) quick-terminal toggle.

    First press splits a 30% bottom pane in the current pane's working
    directory; subsequent presses cycle focus between the two panes.
    No-op when the key env override is empty or no tmux server is running.
    """
    key = quick_terminal_key()
    if not key:
        return
    subprocess.run(
        [
            "tmux",
            "bind-key",
            "-n",
            key,
            "if-shell",
            "-F",
            "#{==:#{window_panes},1}",
            'split-window -v -l 30% -c "#{pane_current_path}"',
            "select-pane -t :.+",
        ],
        capture_output=True,
    )
    debug.log("tmux.quick_terminal", action="enable", key=key)


def disable_quick_terminal_binding() -> None:
    """Remove the server-global quick-terminal binding, if present."""
    key = quick_terminal_key()
    if not key:
        return
    subprocess.run(
        ["tmux", "unbind-key", "-n", key],
        capture_output=True,
    )
    debug.log("tmux.quick_terminal", action="disable", key=key)


PENDING_ACTION_OPTION = "@fujimoto_pending_action"
PENDING_FORK = "fork"
PENDING_STOP = "stop"
PENDING_CLOSE = "close"
PENDING_RESTART = "restart"
# The pid of the fujimoto launch loop attached to a session, set for as long
# as its `tmux attach` runs. `fujimoto restart` uses it to tell a session a
# loop is waiting behind from one attached with plain `tmux attach`, where
# detaching would leave the user at a shell.
LOOP_PID_OPTION = "@fujimoto_loop_pid"


def _configure_session(name: str) -> None:
    """Apply standard tmux configuration to a session."""
    meta_key = _meta_key()
    prefix_key = _prefix_key()
    if meta_key and prefix_key and meta_key == prefix_key:
        raise TmuxError(
            f"FUJIMOTO_META_KEY and FUJIMOTO_TMUX_PREFIX both set to {meta_key!r}; "
            "they must differ."
        )

    debug.log(
        "tmux.configure_session",
        session=debug.rv(name),
        meta_key=meta_key or "[disabled]",
        prefix_key=prefix_key,
    )
    prefix_label = _meta_key_label(prefix_key)
    if meta_key:
        meta_label = _meta_key_label(meta_key)
        status_right = (
            f'"Fujimoto: {meta_label} t/T/w/v/f/s/d/r/x/[ ({meta_label} t toggles) | '
            f'help: {meta_label} ?"'
        )
        status_len = "120"
    else:
        status_right = (
            f'"Detach: {prefix_label} D | Scroll: {prefix_label} [ | '
            f'Kill: {prefix_label} X"'
        )
        status_len = "60"

    options: dict[str, str] = {
        "prefix": prefix_key,
        "status-right": status_right,
        "status-style": "bg=colour235,fg=colour248",
        "status-right-length": status_len,
    }
    for key, value in options.items():
        subprocess.run(
            ["tmux", "set-option", "-t", name, key, value],
            check=True,
        )
    # Default tmux prefix is C-b; only unbind it if we've moved the prefix
    # elsewhere, otherwise we'd unbind the user's chosen prefix.
    if prefix_key != "C-b":
        subprocess.run(
            ["tmux", "unbind-key", "-t", name, "C-b"],
            capture_output=True,
        )
    subprocess.run(
        ["tmux", "bind-key", "-t", name, prefix_key, "send-prefix"],
        capture_output=True,
    )

    if meta_key:
        _configure_fujimoto_key_table(name, meta_key)


def _configure_fujimoto_key_table(name: str, meta_key: str) -> None:
    """Install the one-shot fujimoto-mode key table.

    tmux key bindings are server-global, not session-scoped — there is no
    `-t <session>` flag on `bind-key`. The key table is installed once per
    tmux server and shared across all sessions; the `name` argument is kept
    for symmetry with `_configure_session` but not threaded into the commands.
    """
    del name  # bind-key has no per-session scope
    fujimoto_bindings: list[list[str]] = [
        [
            "t",
            "if-shell",
            "-F",
            "#{==:#{window_panes},1}",
            'split-window -v -l 30% -c "#{session_path}"',
            "select-pane -t :.+",
        ],
        [
            "T",
            "if-shell",
            "-F",
            "#{==:#{window_panes},1}",
            'split-window -h -l 40% -c "#{session_path}"',
            "select-pane -t :.+",
        ],
        ["v", "run-shell", "fujimoto pane vscode --session #{session_name}"],
        ["w", "run-shell", "fujimoto pane terminal --session #{session_name}"],
        # Forking needs a name, a base branch and possibly a conversation
        # picker — more than a key binding can ask for. So flag the session and
        # detach: `main()` sees the flag and reopens the TUI on the fork flow.
        # Note `set-option` has no `-t` here on purpose: a key binding already
        # targets its own session, and `-t "#{session_name}"` is NOT format-
        # expanded, which makes the command fail and abort the whole sequence.
        [
            "f",
            "set-option",
            PENDING_ACTION_OPTION,
            PENDING_FORK,
            "\\;",
            "detach-client",
        ],
        # Stopping keeps the session's record open, so it comes back as a
        # stopped (orange) row that can be resumed. No prompt: `s` is the fast
        # path for a user who already knows they want the session back.
        [
            "s",
            "set-option",
            PENDING_ACTION_OPTION,
            PENDING_STOP,
            "\\;",
            "detach-client",
        ],
        ["d", "detach-client"],
        # Restart goes through the CLI rather than a bare set-option/detach:
        # the CLI first checks a fujimoto loop is attached, so pressing it in
        # a plain `tmux attach` reports an error instead of detaching.
        ["r", "run-shell", "fujimoto restart --session #{session_name}"],
        # With one pane, killing it ends the session — so hand over to the TUI,
        # which asks whether to terminate (forget it) or merely stop it. With a
        # split open, `x` keeps its original meaning: kill just this pane.
        [
            "x",
            "if-shell",
            "-F",
            "#{==:#{window_panes},1}",
            f"set-option {PENDING_ACTION_OPTION} {PENDING_CLOSE} ; detach-client",
            'confirm-before -p "kill pane #P? (y/n)" kill-pane',
        ],
        ["[", "copy-mode"],
        [
            "?",
            "display-message",
            "-d",
            "5000",
            "F-mode: t/T=split  v=code  w=window  f=fork  s=stop  "
            "d=detach  r=restart fujimoto  x=terminate  [=copy  ?=help",
        ],
    ]
    for key, *cmd in fujimoto_bindings:
        subprocess.run(
            ["tmux", "bind-key", "-T", "fujimoto", key, *cmd],
            check=True,
        )
    subprocess.run(
        [
            "tmux",
            "bind-key",
            "-n",
            meta_key,
            "switch-client",
            "-T",
            "fujimoto",
        ],
        check=True,
    )


def build_claude_command(
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    extra_args: Sequence[str] = (),
    session_id: str | None = None,
    prompt: str | None = None,
) -> str:
    """Compose the `claude` invocation for a new tmux session.

    The flags compose rather than exclude each other: a forked session needs
    both `--resume <id> --fork-session` (to inherit the conversation) and
    `--append-system-prompt` (to tell it that it is a fork and where the
    original worktree lives). `extra_args` are the user's launch options;
    they come first and are shell-quoted, since tmux runs the result through
    a shell.

    `session_id` fixes the new conversation's id up front (`--session-id`),
    so the caller knows it without waiting for a transcript. `prompt` is the
    first message, passed as claude's positional argument. It goes straight
    after `claude`, ahead of the launch options, because a variadic option
    there (`--allowedTools a b`) would otherwise swallow it.
    """
    parts = ["claude"]
    if prompt:
        parts.append(shlex.quote(prompt))
    parts.extend(shlex.quote(a) for a in extra_args)
    if session_id:
        parts.append(f"--session-id {shlex.quote(session_id)}")
    if resume_session_id:
        parts.append(f"--resume {resume_session_id}")
        if fork_session:
            parts.append("--fork-session")
    if system_prompt:
        escaped = system_prompt.replace("'", "'\\''")
        parts.append(f"--append-system-prompt '{escaped}'")
    return " ".join(parts)


def create_session(
    name: str,
    working_dir: Path,
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    extra_args: Sequence[str] = (),
    session_id: str | None = None,
    prompt: str | None = None,
) -> None:
    claude_cmd = build_claude_command(
        system_prompt=system_prompt,
        resume_session_id=resume_session_id,
        fork_session=fork_session,
        extra_args=extra_args,
        session_id=session_id,
        prompt=prompt,
    )
    debug.log(
        "tmux.create_session",
        session=debug.rv(name),
        cwd=debug.rp(working_dir),
        resume=resume_session_id or "none",
        session_id=session_id or "none",
        prompt_chars=len(prompt or ""),
        system_prompt_chars=len(system_prompt or ""),
        command=debug.rv(claude_cmd),
    )
    _new_session(name, working_dir, claude_cmd)
    _ensure_focus_events()
    _configure_session(name)
    _ensure_extended_keys()
    _apply_quick_terminal_setting()


def create_session_with_command(name: str, working_dir: Path, command: str) -> None:
    """Create a tmux session and run an arbitrary command instead of claude."""
    debug.log(
        "tmux.create_session_with_command",
        session=debug.rv(name),
        cwd=debug.rp(working_dir),
        command=debug.rv(command),
    )
    _new_session(name, working_dir, command)
    _ensure_focus_events()
    _configure_session(name)
    _ensure_extended_keys()
    _apply_quick_terminal_setting()


def _forget_surface_scope() -> None:
    """Drop cmux's pane-scoped keys from a running server's global environment.

    A server started from a cmux pane (by an older fujimoto, or by hand) holds
    that pane's `CMUX_SURFACE_ID`, and every session it creates inherits it.
    One chained call; with no server running it fails without starting one.
    """
    args = ["tmux"]
    for i, key in enumerate(cmux.SURFACE_SCOPED_KEYS):
        args += ([";"] if i else []) + ["set-environment", "-gu", key]
    result = subprocess.run(args, capture_output=True, text=True)
    debug.log("tmux.forget_surface_scope", rc=result.returncode)


def _new_session(name: str, working_dir: Path, command: str) -> None:
    """`tmux new-session -d` with no cmux pane identity in its environment.

    A new server copies the starting process's whole environment, so it is
    given one without the pane-scoped keys; an existing server is cleaned of
    them first. Otherwise cmux files every Claude session in tmux under the
    pane that started the server (see `cmux.py`).
    """
    _forget_surface_scope()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(working_dir), command],
        check=True,
        env=cmux.without_surface_scope(),
    )


def attach_session(name: str) -> None:
    """Attach to `name` until the user detaches, marking it with our pid.

    Inside cmux the pane is bound to `fujimoto --attach <name>` while
    attached, so if cmux crashes or relaunches it restores the pane through
    fujimoto. Detaching clears the binding: the home screen has nothing to
    restore.
    """
    debug.log("tmux.attach", session=debug.rv(name), phase="start")
    _refresh_session_config(name)
    pid = str(os.getpid())
    subprocess.run(
        ["tmux", "set-option", "-t", name, LOOP_PID_OPTION, pid],
        capture_output=True,
    )
    bound = cmux.set_resume_binding(name)
    result = subprocess.run(["tmux", "attach-session", "-t", name])
    debug.log(
        "tmux.attach",
        session=debug.rv(name),
        phase="returned",
        rc=result.returncode,
    )
    if bound:
        cmux.clear_resume_binding(name)
    # Leave the mark alone if another loop has attached since and taken it.
    if _session_option(name, LOOP_PID_OPTION) == pid:
        subprocess.run(
            ["tmux", "set-option", "-t", name, "-u", LOOP_PID_OPTION],
            capture_output=True,
        )


def _refresh_session_config(name: str) -> None:
    """Re-apply this fujimoto's tmux config to a session it is attaching to.

    The key table is server-global and was installed by whichever fujimoto
    created a session, so reattaching after an upgrade would otherwise keep
    the old bindings (no `Ctrl-A r` until some session was newly created).
    A failure must not stop the attach: the old bindings still work.
    """
    try:
        _configure_session(name)
    except (TmuxError, subprocess.CalledProcessError, OSError) as e:
        debug.log_exception("tmux.refresh_config_failed", e)


def _session_option(name: str, option: str) -> str | None:
    """A user option's value on session `name`; None if unset or no session."""
    result = subprocess.run(
        ["tmux", "show-options", "-t", name, "-v", option],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _parent_pids(pids: Sequence[int]) -> dict[int, int]:
    """`{pid: parent pid}` for those of `pids` still running."""
    if not pids:
        return {}
    result = subprocess.run(
        ["ps", "-o", "pid=,ppid=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    )
    parents: dict[int, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(f.isdigit() for f in fields):
            parents[int(fields[0])] = int(fields[1])
    return parents


def loop_client(name: str) -> str | None:
    """The tty of the client a fujimoto loop has attached to session `name`.

    The loop marks the session with its pid (`LOOP_PID_OPTION`) and runs
    `tmux attach` as a child, so the client to detach is the one whose parent
    is that pid. A stale mark (the loop died) or a session attached only with
    plain `tmux attach` has no such client, and None means detaching would
    strand the user at a shell.
    """
    mark = _session_option(name, LOOP_PID_OPTION)
    if mark is None or not mark.isdigit():
        debug.log("tmux.loop_client", session=debug.rv(name), result="unmarked")
        return None
    result = subprocess.run(
        ["tmux", "list-clients", "-t", name, "-F", "#{client_pid} #{client_tty}"],
        capture_output=True,
        text=True,
    )
    clients: dict[int, str] = {}
    for line in result.stdout.splitlines():
        pid, _, tty = line.partition(" ")
        if pid.isdigit() and tty:
            clients[int(pid)] = tty
    parents = _parent_pids(list(clients))
    for pid, tty in clients.items():
        if parents.get(pid) == int(mark):
            debug.log("tmux.loop_client", session=debug.rv(name), result="found")
            return tty
    debug.log(
        "tmux.loop_client",
        session=debug.rv(name),
        result="no-client",
        clients=len(clients),
    )
    return None


def request_restart(name: str, client_tty: str) -> None:
    """Flag `name` for a restart and detach the loop's client.

    The loop wakes from `tmux attach`, finds `PENDING_RESTART` and re-executes
    fujimoto, which reattaches to the same session.
    """
    debug.log("tmux.request_restart", session=debug.rv(name))
    subprocess.run(
        ["tmux", "set-option", "-t", name, PENDING_ACTION_OPTION, PENDING_RESTART],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["tmux", "detach-client", "-t", client_tty], capture_output=True, check=True
    )


def current_session() -> str | None:
    """The tmux session this process runs in, or None outside tmux."""
    pane = os.environ.get("TMUX_PANE")
    if not os.environ.get("TMUX") or not pane:
        return None
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
        capture_output=True,
        text=True,
    )
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


def take_pending_action(name: str) -> str | None:
    """Read and clear the pending in-session action for a tmux session.

    In-session key bindings that need the TUI record their intent as a tmux
    session option and detach; `main()` consumes it here when `tmux attach`
    returns. Reading an unset option exits non-zero, as does a session that has
    since been killed — both mean "nothing pending".
    """
    result = subprocess.run(
        ["tmux", "show-options", "-t", name, "-v", PENDING_ACTION_OPTION],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    action = result.stdout.strip()
    if not action:
        return None
    subprocess.run(
        ["tmux", "set-option", "-t", name, "-u", PENDING_ACTION_OPTION],
        capture_output=True,
    )
    return action


def launch_claude_in_tmux(
    project_name: str,
    working_dir: Path,
    tmux_name: str | None = None,
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    extra_args: Sequence[str] = (),
    session_id: str | None = None,
    prompt: str | None = None,
    attach: bool = True,
) -> None:
    """Create the session if it isn't running, then attach unless `attach=False`.

    `session_id` and `prompt` only reach a newly created claude process; an
    already-running session is attached to as it is.
    """
    name = tmux_name or session_name(project_name, working_dir.name)
    debug.log(
        "tmux.launch",
        project=debug.rv(project_name),
        session=debug.rv(name),
        cwd=debug.rp(working_dir),
        resume=resume_session_id or "none",
        attach=attach,
    )
    if not session_exists(name):
        create_session(
            name,
            working_dir,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            fork_session=fork_session,
            extra_args=extra_args,
            session_id=session_id,
            prompt=prompt,
        )
    if attach:
        attach_session(name)
