# Configuration and prerequisites

## Environment variables (all optional)

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/   # Optional: where worktrees are created
export FUJIMOTO_GIT_ROOT=~/git/                  # Optional: enables project switching
export FUJIMOTO_TERMINAL="alacritty --working-directory {dir}"  # Optional (Linux): terminal command
export FUJIMOTO_WINDOW_TITLE="{git_project} - {worktree_name}"   # Optional: terminal window title template
export FUJIMOTO_META_KEY="C-a"                                   # Optional: in-session fujimoto chord (blank to disable)
export FUJIMOTO_TMUX_PREFIX="C-b"                                # Optional: tmux prefix key (default: C-b)
export FUJIMOTO_QUICK_TERMINAL_KEY="C-\`"                        # Optional: global quick-terminal toggle key (blank to disable)
export FUJIMOTO_LOG_DIR=~/.fujimoto/logs                         # Optional: where --debug logs are written
```

`FUJIMOTO_TERMINAL` only applies on Linux. Use `{dir}` as a placeholder for the
working directory; if absent, the directory is appended as the final argument.
If unset, fujimoto auto-detects a common terminal emulator on PATH.

`FUJIMOTO_WINDOW_TITLE` is the suffix appended to the hard-coded
`🧙🏽‍♂️ fujimoto` prefix when a Claude session is attached. Default:
`{git_project} - {worktree_name}`. Supported placeholders: `{git_project}`,
`{worktree_name}`, `{worktree_path}`, `{git_project_dir}`, `{branch}`,
`{session_type}`, `{tmux_name}`. Unknown placeholders render as empty strings.
Empty string suppresses the suffix.

While the session-manager TUI itself is open (before attaching a Claude
session), the window title uses the same format minus the worktree portion:
`🧙🏽‍♂️ fujimoto - {project}` (via `_session_manager_title`). It is set in
`_init_git_info`, so it follows the current project across the project switcher.
`FUJIMOTO_WINDOW_TITLE` does not affect the TUI title. `set_terminal_title`
writes to `sys.__stdout__` (not `sys.stdout`) precisely so this write lands on
the real terminal while Textual is running — see the OSC gotcha in [gotchas.md](gotchas.md).

`FUJIMOTO_QUICK_TERMINAL_KEY` (default `` C-` ``) configures the **server-global**
one-press quick-terminal toggle. First press splits a 30% bottom pane in the
current pane's working directory; subsequent presses cycle focus between the
panes. Unlike `FUJIMOTO_META_KEY` (which is per-session), this is a `tmux
bind-key -n` at the root table — installed once, applies to all tmux sessions
on the machine. On first launch the TUI asks whether to enable it; the answer
is persisted to `~/.cache/fujimoto/settings.json` (`quick_terminal_enabled`).
A toggle on the home screen (under the trailing dividers) flips the value at
runtime, calling `enable_/disable_quick_terminal_binding()` in `tmux.py`.
Setting the env var to empty disables the feature entirely (the toggle then
renders as `disabled (env)` and the first-launch prompt is skipped).

Because the binding lives on the tmux server, deleting `settings.json` does
not remove an already-installed binding. Removal paths: the home-screen
toggle (calls `disable_quick_terminal_binding()`), `tmux unbind-key -n <key>`
manually, or `tmux kill-server`. With the preference still `on`, fujimoto
re-installs the binding on every session create — so toggle it off first if
you want the removal to persist.

`FUJIMOTO_META_KEY` (default `C-a`) configures the in-session "fujimoto mode"
chord. Pressing it inside an attached tmux session enters a one-shot key table
where `t`/`T` split a terminal pane (single-pane guard via `if-shell` on
`#{session_panes}`), `v` opens VS Code, `w` opens a native terminal window,
`s` stops the session (keeping it resumable), `d` detaches the session,
`x` ends the session — kill-pane when a split is open, otherwise a hand-off to
the TUI's terminate/stop prompt —
`[` enters copy mode, and `?` flashes a cheatsheet via `display-message`.
`f` forks the session (see below), and `v`/`w` dispatch to the
`fujimoto pane <action> --session <name>` CLI subcommand which reuses the
existing launchers in `vscode.py` / `terminal.py`.
Set to the empty string to disable; the bindings and the status-bar hint are
then both omitted.

`FUJIMOTO_TMUX_PREFIX` (default `C-b`, tmux's standard default) configures the
tmux prefix key. Fujimoto raises `TmuxError` if `FUJIMOTO_META_KEY` and
`FUJIMOTO_TMUX_PREFIX` are set to the same value. Pre-`v?` versions of
fujimoto used `C-f` for the meta key and `C-a` for the prefix; the defaults
were swapped so the fujimoto chord lives on the more ergonomic `C-a` slot. To
restore the old layout, set `FUJIMOTO_META_KEY=C-f` and
`FUJIMOTO_TMUX_PREFIX=C-a`.

If `FUJIMOTO_WORKTREE_ROOT` is unset, worktrees are created at
`<repo_root>/.fujimoto/worktrees/` (the `.fujimoto/` directory is auto-gitignored
via a `.gitignore` containing `*`). If `FUJIMOTO_GIT_ROOT` is unset, the project
switcher is silently hidden.

## Prerequisites

- Python 3.11, 3.12, 3.13, or 3.14 (CI tests all four)
- tmux (auto-installs via brew if missing)
- git
