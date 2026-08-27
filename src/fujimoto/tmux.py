from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from fujimoto import debug

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

    >>> session_name("qsic-data", "20260309-fix-tests")
    'qsic-data/20260309-fix-tests'
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
            f'"Fujimoto: {meta_label} t/T/w/v/f/s/d/x/[ ({meta_label} t toggles) | '
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
            "d=detach  x=terminate  [=copy  ?=help",
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
) -> str:
    """Compose the `claude` invocation for a new tmux session.

    The flags compose rather than exclude each other: a forked session needs
    both `--resume <id> --fork-session` (to inherit the conversation) and
    `--append-system-prompt` (to tell it that it is a fork and where the
    original worktree lives).
    """
    parts = ["claude"]
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
) -> None:
    claude_cmd = build_claude_command(
        system_prompt=system_prompt,
        resume_session_id=resume_session_id,
        fork_session=fork_session,
    )
    debug.log(
        "tmux.create_session",
        session=debug.rv(name),
        cwd=debug.rp(working_dir),
        resume=resume_session_id or "none",
        system_prompt_chars=len(system_prompt or ""),
        command=debug.rv(claude_cmd),
    )
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(working_dir),
            claude_cmd,
        ],
        check=True,
    )
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
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(working_dir),
            command,
        ],
        check=True,
    )
    _configure_session(name)
    _ensure_extended_keys()
    _apply_quick_terminal_setting()


def attach_session(name: str) -> None:
    debug.log("tmux.attach", session=debug.rv(name), phase="start")
    result = subprocess.run(["tmux", "attach-session", "-t", name])
    debug.log(
        "tmux.attach",
        session=debug.rv(name),
        phase="returned",
        rc=result.returncode,
    )


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
) -> None:
    name = tmux_name or session_name(project_name, working_dir.name)
    debug.log(
        "tmux.launch",
        project=debug.rv(project_name),
        session=debug.rv(name),
        cwd=debug.rp(working_dir),
        resume=resume_session_id or "none",
    )
    if session_exists(name):
        attach_session(name)
    else:
        create_session(
            name,
            working_dir,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            fork_session=fork_session,
        )
        attach_session(name)
