"""Integration with the cmux terminal: pane identity and session restore.

cmux identifies the pane a process runs in by `CMUX_SURFACE_ID` (with
`CMUX_PANEL_ID` and `CMUX_HISTORY_FILE`). A tmux server started from a cmux
pane copies those into its global environment, so every Claude Code session
fujimoto later starts in tmux reports to that one pane — and cmux then tries
to resume them all there after a relaunch. `without_surface_scope` strips them
from the environment fujimoto hands to tmux.

cmux restores a pane by re-running the command in its *resume binding*. On its
own it detects only the `tmux attach` child, which comes back without the
fujimoto loop around it (when it comes back at all). `set_resume_binding`
binds the pane to `fujimoto --attach <session>` for as long as fujimoto is
attached, so a restored pane reattaches through fujimoto.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from fujimoto import debug
from fujimoto.config import fujimoto_executable

# The keys cmux's own shell integration treats as pane-scoped inside tmux
# (`_CMUX_TMUX_SURFACE_SCOPED_KEYS`): they must not outlive the pane they name.
SURFACE_SCOPED_KEYS: tuple[str, ...] = (
    "CMUX_SURFACE_ID",
    "CMUX_PANEL_ID",
    "CMUX_HISTORY_FILE",
)

# The binding's kind and source. A source cmux doesn't reserve (`cli`,
# `agent-hook`, `process-detected`, `session-import`) keeps the binding
# reviewable in cmux, so the user can approve `fujimoto --attach` once for
# automatic restore, and stops cmux's tmux detection from replacing it.
RESUME_KIND = "fujimoto"
RESUME_SOURCE = "fujimoto"

CLI_TIMEOUT_SECONDS = 5


def without_surface_scope(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `environ` (default `os.environ`) minus the pane-scoped keys."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k not in SURFACE_SCOPED_KEYS}


def surface_id() -> str | None:
    """The cmux pane this process runs in, or None outside cmux."""
    return os.environ.get("CMUX_SURFACE_ID") or None


def _cli() -> str | None:
    """The cmux CLI: the app's bundled copy first, then `cmux` on PATH."""
    bundled = os.environ.get("CMUX_BUNDLED_CLI_PATH")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("cmux")


def resume_command(tmux_name: str) -> str | None:
    """The shell command that reattaches a restored pane to `tmux_name`."""
    program = fujimoto_executable()
    if program is None:
        return None
    return shlex.join([program, "--attach", tmux_name])


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run the cmux CLI; None when it could not be started or timed out."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        debug.log_exception("cmux.cli_failed", e)
        return None


def set_resume_binding(tmux_name: str) -> bool:
    """Bind this cmux pane to reattach `tmux_name` through fujimoto.

    Returns whether a binding was set. Outside cmux, or when the cmux CLI or
    fujimoto itself can't be found, it does nothing; a failure is logged and
    never raised, since restore is a convenience and must not block an attach.
    """
    surface = surface_id()
    if surface is None:
        debug.log("cmux.resume_set", session=debug.rv(tmux_name), result="not-cmux")
        return False
    cli = _cli()
    command = resume_command(tmux_name)
    if cli is None or command is None:
        debug.log(
            "cmux.resume_set",
            session=debug.rv(tmux_name),
            result="unavailable",
            cli=cli is not None,
            fujimoto=command is not None,
        )
        return False
    try:
        cwd = str(Path.cwd())
    except OSError:  # the launch directory was deleted
        cwd = str(Path.home())
    result = _run(
        [
            cli,
            "surface",
            "resume",
            "set",
            "--surface",
            surface,
            "--kind",
            RESUME_KIND,
            "--source",
            RESUME_SOURCE,
            "--name",
            tmux_name,
            "--checkpoint",
            tmux_name,
            "--cwd",
            cwd,
            "--shell",
            command,
        ]
    )
    ok = result is not None and result.returncode == 0
    debug.log(
        "cmux.resume_set",
        session=debug.rv(tmux_name),
        cwd=debug.rp(cwd),
        result="set" if ok else "failed",
        rc=None if result is None else result.returncode,
        stderr=debug.rv((result.stderr or "").strip()[:200]) if result else "",
    )
    return ok


def clear_resume_binding(tmux_name: str) -> None:
    """Remove the binding `set_resume_binding` gave this pane."""
    surface = surface_id()
    cli = _cli()
    if surface is None or cli is None:
        return
    result = _run([cli, "surface", "resume", "clear", "--surface", surface])
    debug.log(
        "cmux.resume_clear",
        session=debug.rv(tmux_name),
        rc=None if result is None else result.returncode,
    )
