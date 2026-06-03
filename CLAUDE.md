# fujimoto

CLI/TUI tool for managing Claude Code sessions in git worktrees and repositories.

## Commands

```sh
uv sync                                        # Install dependencies
uv run fujimoto                          # Run locally (must be inside a git repo)
uv run pytest                                  # Run tests with coverage
uv run nox                                     # Run tests across all supported Python versions
uv run nox -s tests-3.14                       # Run tests against a single Python version
uv run nox -s tests_textual                    # Run dependency-pin matrix (textual)
uv tool install --force --reinstall .          # Install globally (re-run after code changes)
```

## Environment Variables (all optional)

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/   # Optional: where worktrees are created
export FUJIMOTO_GIT_ROOT=~/git/                  # Optional: enables project switching
export FUJIMOTO_TERMINAL="alacritty --working-directory {dir}"  # Optional (Linux): terminal command
export FUJIMOTO_WINDOW_TITLE="{git_project} - {worktree_name}"   # Optional: terminal window title template
export FUJIMOTO_META_KEY="C-a"                                   # Optional: in-session fujimoto chord (blank to disable)
export FUJIMOTO_TMUX_PREFIX="C-b"                                # Optional: tmux prefix key (default: C-b)
export FUJIMOTO_QUICK_TERMINAL_KEY="C-\`"                        # Optional: global quick-terminal toggle key (blank to disable)
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
`d` detaches the session, `x` kills the current pane (via `confirm-before`),
`[` enters copy mode, and `?` flashes a cheatsheet via `display-message`.
`v`/`w` dispatch to the `fujimoto pane <action> --session <name>` CLI
subcommand which reuses the existing launchers in `vscode.py` / `terminal.py`.
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

## Project Structure

```
src/fujimoto/
├── __init__.py
├── cli.py        # Textual TUI app, entry point (main()), all UI screens and event handlers
├── config.py     # Environment variable loading, path construction, session metadata
├── git.py        # Git subprocess wrappers (worktree lifecycle, branch operations)
├── terminal.py   # Open native terminal windows (iTerm2 with Terminal.app fallback)
├── vscode.py     # Open directories in VS Code via the `code` CLI
├── tmux.py       # tmux session lifecycle (create, attach, kill, list, install)
├── version.py    # importlib.metadata wrapper for the running fujimoto version
├── version_check.py  # daily PyPI update check, dismissal cache (~/.cache/fujimoto/)
├── settings.py   # persistent user settings (~/.cache/fujimoto/settings.json)
├── project_config.py  # optional per-project .fujimoto.yaml (copy/link/init worktree setup)
├── templates/
│   ├── __init__.py
│   └── fujimoto.yaml.template  # commented scaffold written by `fujimoto --create-config`
└── claude/
    ├── __init__.py      # Re-exports public API
    └── log_parser.py    # Parse Claude JSONL session logs (state, metadata, session lookup)
```

## Architecture

### Entry Point

`cli.py:main()` is the package entry point (`pyproject.toml` `[project.scripts]`). It parses CLI args:
- `--version`/`-V` prints `fujimoto {version}` and exits
- `--create-config` writes a commented `.fujimoto.yaml` template to the repo root (via `project_config.write_config_template`) and exits; errors (already exists, not a git repo) print to stderr and exit 1.
- `fujimoto pane <vscode|terminal> --session <name>` dispatches to `_run_pane_command`, used by the in-session tmux key table (`Ctrl-F v` / `Ctrl-F w`). Resolves the session's working directory via `tmux display-message -p '#{session_path}'` and calls the existing `open_vscode` / `open_terminal` helpers; errors are surfaced via `tmux display-message` so they appear in the session's status bar.

Otherwise it:
1. Runs the Textual `SessionApp` in a loop
2. After the TUI exits, calls `launch_claude_in_tmux()` if the user selected a session
3. When the tmux session is detached, the loop restarts and the TUI reappears
4. The loop exits when the user quits the TUI (q/escape/ctrl+c) without selecting a session

### Session Types

**Worktree sessions** — isolated git worktree with its own branch:
- Creates a new branch + working directory via `git worktree add`
- Finish flow: Push & Create PR, Cherry-pick to base branch, or Discard & Delete
- Session metadata (base branch) stored in `.fujimoto/meta.json` (auto-gitignored)

**Direct sessions** — Claude launched in an existing repo directory:
- No worktree creation, uses the repo's current branch
- Multiple concurrent sessions possible on same repo
- Named `{project}/direct-N` in tmux

**Ad hoc sessions** — Claude launched in a temporary directory, outside any git project:
- For quick questions, investigations, and one-off tasks
- Working directory is a `tempfile.mkdtemp(prefix="fujimoto-adhoc-")` temp dir
- Named `adhoc-N` in tmux (not project-scoped)
- System prompt tells Claude there is no git repository

### Module Responsibilities

**`config.py`** — Pure functions, no side effects except directory creation:
- `get_worktree_root(project_root=None)` — returns `FUJIMOTO_WORKTREE_ROOT` if set, else falls back to `<project_root>/.fujimoto/worktrees/` (ensures `.fujimoto/.gitignore` exists). Raises `ConfigError` only if both are missing.
- `get_git_projects_root()` — reads `FUJIMOTO_GIT_ROOT`, returns `None` if unset
- `list_projects()` — scans git root for directories containing `.git`
- `slugify(title)` — lowercase, replace non-alphanumeric with hyphens, strip/collapse
- `build_worktree_path(project, title, project_root=None)` — with env var: `{root}/{project}/{YYYYMMDD}-{slug}`; with fallback: `<project_root>/.fujimoto/worktrees/{YYYYMMDD}-{slug}`
- `get_project_worktrees_dir(project, project_root=None)` — with env var: `{root}/{project}`; with fallback: `<project_root>/.fujimoto/worktrees/`
- `store_session_meta(path, base_branch, source_root=None)` / `read_session_meta(path)` — JSON metadata. `source_root` records the main repo the worktree was created from, so `project_config` can resolve copy/link sources on later launches (older worktrees without it fall back to deriving the root via `git.get_main_worktree_root`).
- `config_once_applied(path)` / `mark_config_once_applied(path)` — presence-of-marker-file flag (`.fujimoto/config_once_applied`) recording that `once` project-config actions have run for the worktree.
- `get_next_direct_session_name(project, sessions)` — computes `{project}/direct-N`
- `get_next_adhoc_session_name(sessions)` — computes `adhoc-N`

**`git.py`** — Thin wrappers around `git` subprocess calls:
- `_run(args, cwd)` — subprocess runner, raises `GitError` on non-zero exit
- `get_repo_root()` — `git rev-parse --show-toplevel`
- `get_project_name()` — basename of repo root
- `get_main_worktree_root(cwd)` — parent of `--git-common-dir`; the main worktree root even when `cwd` is a linked worktree
- `get_current_branch()` — `git branch --show-current`
- `get_default_branch()` — tries `symbolic-ref`, falls back to checking main/master
- `fetch_and_rebase_branch(branch)` — `git fetch origin` + `git rebase origin/{branch}`
- `list_branches()` — sorted list of local branch names
- `create_worktree(path, base_branch, new_branch)` — `git worktree add -b`
- `remove_worktree(path)` — `git worktree remove --force`
- `get_unpushed_commits(branch)` — commits not yet on remote
- `get_merge_base(branch)` — fork point from default branch
- `is_branch_merged(branch, into)` — `git merge-base --is-ancestor`
- `has_remote_branch(branch)` — `git ls-remote --heads`
- `push_branch(branch)` — `git push -u origin`
- `delete_branch(branch, remote)` — `git branch -D`, optionally remote
- `cherry_pick_branch(branch, onto)` — cherry-picks commit range onto target

**`terminal.py`** — Open native terminal windows in a session's directory:
- `open_terminal(directory)` — platform dispatch. macOS: iTerm2 (if installed) → Terminal.app. Linux: `FUJIMOTO_TERMINAL` env var → auto-detected emulator. Raises `OSError` on unsupported platforms or when no terminal is found.
- `_has_iterm()` — checks for `/Applications/iTerm.app`
- `_open_iterm(directory)` — AppleScript to create new iTerm2 window
- `_open_terminal_app(directory)` — `open -a Terminal` fallback
- `_open_linux_terminal(directory)` — uses `FUJIMOTO_TERMINAL` if set; otherwise probes `_LINUX_TERMINALS` (gnome-terminal, konsole, kitty, alacritty, wezterm, foot, xfce4-terminal, tilix, terminator, xterm) and spawns the first one found via `subprocess.Popen` with `start_new_session=True` so it survives parent exit.
- `_format_args(args, directory)` — substitutes `{dir}` placeholder; appends directory if no placeholder present.

**`vscode.py`** — Open directories in VS Code:
- `open_vscode(directory)` — runs `code <directory>`. Raises `OSError` if the `code` CLI is not on PATH.
- `_has_vscode()` — checks for `code` on PATH via `shutil.which`

**`settings.py`** — Persistent user settings stored as JSON in
`~/.cache/fujimoto/settings.json`:
- `Settings` dataclass: `quick_terminal_enabled: bool | None` (None = never asked)
- `load_settings()` / `save_settings()` — graceful read/write, swallow OS errors
  and corrupt JSON (returns defaults). Mirrors the `version_check.py` cache
  pattern.

**`project_config.py`** — Optional per-project `.fujimoto.yaml` (validated with
pydantic):
- `CONFIG_FILENAME = ".fujimoto.yaml"`. Three optional sections — `copy`, `link`,
  `init` — each a list whose items are a bare string or a mapping.
- Enums: `Trigger` (`CREATE`, `LAUNCH`), `When` (`ONCE`, `ALWAYS`) with
  `When.runs_on(trigger)` (ONCE → create only; ALWAYS → create + launch),
  `LinkType` (`HARD`, `SYMBOLIC`), `OnError` (`ABORT`, `CONTINUE`).
- Pydantic models: `CopyEntry(path, when)`, `LinkEntry(path, type, when)`,
  `InitCommand(run, when, continue_on_error, cwd)`, `ProjectConfig(..., on_error)`.
  A `model_validator(mode="before")` coerces bare strings into the mapping form;
  `extra="forbid"` rejects unknown keys. `ProjectConfig` uses field aliases
  (`copy`/`link`/`init`) because `copy` would shadow `BaseModel.copy()` — the
  Python attributes are `copy_entries` / `link_entries` / `init_commands`.
  `on_error` (default `ABORT`) governs the caller's reaction to a hard init
  failure.
- `load_project_config(project_root)` — returns an empty config if the file is
  absent; raises `ConfigError` (reusing `config.ConfigError`) on malformed YAML
  or validation failure (config errors are surfaced, not swallowed).
- `apply_project_config(config, *, source_root, worktree_root, trigger)` — runs
  entries where `when.runs_on(trigger)`. Copy/link sources resolve relative to
  `source_root` (globs supported via `glob.has_magic`); destinations mirror the
  same relative path. Hard links fall back to `shutil.copy2` + warning on
  cross-filesystem `OSError`; `symbolic` uses `os.symlink`. Init commands run via
  `subprocess.run(["sh", "-x", "-c", run])` — `sh -x` echoes each command so the
  command and its output appear in the launch trace — with
  `{{ source_dir }}`/`{{ worktree_dir }}` substitution, cwd defaulting to the
  worktree root, stopping on first failure unless `continue_on_error`. Returns an
  `ApplyResult(actions, warnings, init_error)` — it never raises for individual
  action failures (`actions` is a log of what ran).
- `template_text()` / `write_config_template(project_root)` — read the bundled
  `templates/fujimoto.yaml.template` (via `importlib.resources`) and scaffold it
  into a repo (refusing to overwrite an existing file).

**`tmux.py`** — tmux session management:
- `is_tmux_installed()` / `install_tmux()` — detection and install. macOS: brew install. Linux: raises `TmuxError` with a distro-appropriate install command (apt-get/dnf/pacman/zypper/apk) — does not invoke sudo automatically.
- `list_all_sessions()` — lists all active tmux session names
- `list_project_sessions(project)` — lists active tmux sessions for a project
- `session_name(project, dir)` — naming convention: `{project}/{dir}`
- `create_session(name, dir, system_prompt, resume_session_id)` — creates detached session, sets prefix to Ctrl+A, runs `claude` (with optional `--resume`)
- `create_session_with_command(name, dir, command)` — like `create_session` but with custom command
- `kill_session(name)` — `tmux kill-session -t`
- `attach_session(name)` — `subprocess.run` tmux attach (returns on detach). Shortcut hints live in the per-session tmux status bar, not in a pre-attach banner.
- `launch_claude_in_tmux(project, path, tmux_name, system_prompt, resume_session_id)` — orchestrates create-or-attach, supports resuming previous Claude sessions
- `get_session_path(name)` — returns the start directory of a tmux session via `display-message -p '#{session_path}'`, used by the `fujimoto pane` subcommand
- `display_message(name, message)` — surface a transient message in the session's status bar, used to report errors from `fujimoto pane` back into the session
- `_configure_session(name)` — applies the configured prefix (default `C-b`), status bar, and (if `FUJIMOTO_META_KEY` is non-empty) the fujimoto key table via `_configure_fujimoto_key_table`. Raises `TmuxError` if the meta and prefix keys collide. The `unbind-key C-b` step only runs when the prefix has been moved off `C-b` (otherwise it would unbind the new prefix).
- `quick_terminal_key()` — returns the configured `FUJIMOTO_QUICK_TERMINAL_KEY` (default `` C-` ``)
- `enable_quick_terminal_binding()` / `disable_quick_terminal_binding()` — install/remove the **server-global** root-table binding (`bind-key -n <key>`) that toggles a 30% bottom pane. The bound command uses `if-shell -F '#{==:#{window_panes},1}'` to split on the first press and cycle focus on subsequent presses. Both are no-ops when the env var is empty. Idempotent.
- `_apply_quick_terminal_setting()` — called from `create_session` / `create_session_with_command` after `_ensure_extended_keys()`. Re-applies the binding when `Settings.quick_terminal_enabled` is True, so the feature survives `tmux kill-server`. Lazily imports `settings` to avoid a circular import.
- `_configure_fujimoto_key_table(name, meta_key)` — installs the one-shot key table: `t`/`T` (`if-shell` guard ensuring a single extra pane; falls back to `select-pane :.+`), `v`/`w` (dispatch to `fujimoto pane <action> --session #{session_name}` via `run-shell`), `d` (detach-client), `x` (`confirm-before` kill-pane), `[` (copy-mode), `?` (cheatsheet). Root-level `bind-key -n <meta_key> switch-client -T fujimoto` arms the chord.

**`claude/log_parser.py`** — Parse Claude Code's JSONL session logs:
- `ClaudeLogError` — raised on empty/unreadable logs
- `EntryType` / `StopReason` / `SessionState` — StrEnums with lenient `from_raw()` parsing (returns `None` for unrecognized values)
- `ClaudeSession` — frozen dataclass: session_id, state, cwd, git_branch, last_activity, etc.
- `encode_project_path(path)` — `str(path).replace("/", "-")` (matches Claude's directory encoding)
- `get_claude_projects_dir()` — `~/.claude/projects`
- `parse_session(jsonl_path)` — reads JSONL, tracks last meaningful (non-sidechain) entry, derives state
- `get_sessions_for_path(project_path)` — encodes path, globs `*.jsonl`, returns sorted sessions

**`cli.py`** — Textual TUI with async view management:
- `SessionInfo` — dataclass for session state (type, project, path, tmux name, active status, claude_session_id, claude_state)
- `SessionApp` — main app class with CSS styling
- Module-level helpers: `_claude_state_label(state)`, `_relative_time(dt)`, `_get_claude_sessions(root, worktrees)`
- Instance helpers: `_build_session_label(session, state_suffix)` — generates label text for session items, used by both `_show_home` initial render and `_poll_session_states` in-place updates
- Views: home (sessions list), session actions submenu, finish flow, confirm dialog, create form, branch select (3 options), branch picker (filterable list), conflict resolution, project switcher (with autocomplete filter), tmux install, error
- Home screen sections: actions ("New worktree session", "New session in X", "Ad hoc session"), active sessions (with Claude state indicators), inactive worktrees (with Claude state), previous Claude sessions (resumable, capped at 5), switch project
- Worktree create flow: title → branch select (default w/ fetch & rebase, current branch, another branch → picker) → create
- Session actions submenu (in order): for active sessions, Connect → Resume previous session; for inactive worktrees, Resume previous session → Launch (resume is the more common action when picking an idle worktree). Then: Open terminal, Open in VS Code, Rename, Terminate session (active only), Finish (worktree only), Cancel. Claude-session items show just "Resume" + Open terminal/VS Code + Cancel.
- "Resume previous session" auto-launches the sole candidate when only one previous Claude session exists for the path, skipping the picker. Two or more sessions still show the picker.
- Finish flow: Push & Create PR (background Claude), Cherry-pick to base, Discard & Delete
- Open terminal flow: sub-menu with "This window" (default; uses Textual's `App.suspend()` to pause the TUI, then runs `subprocess.run([$SHELL], cwd=session.path)` as a child process — when the user types `exit`, the TUI resumes on the session actions menu) and "New window" (spawns a new iTerm/Terminal/Linux emulator window via `open_terminal()`)
- All view transitions are `async` — `await _clear_main()` then `await mount()`
- Session data stored in `_session_map` dict keyed by ListItem ID
- `_launch_target` is `(project, path, tmux_name, session_type, resume_id)`, set before `self.exit()`

### Error Handling

Three custom exception types, all caught in `main()`:
- `ConfigError` — missing env var
- `GitError` — git command failures, not in a repo
- `TmuxError` — tmux not installed, install failure

### Naming Conventions

| Thing | Pattern | Example |
|-------|---------|---------|
| Worktree directory | `{YYYYMMDD}-{slug}` | `20260309-fix-unit-tests` |
| Git branch | `worktree/{dir-name}` | `worktree/20260309-fix-unit-tests` |
| tmux session (worktree) | `{project}/{dir-name}` | `qsic-data/20260309-fix-unit-tests` |
| tmux session (direct) | `{project}/direct-{N}` | `qsic-data/direct-1` |
| tmux session (adhoc) | `adhoc-{N}` | `adhoc-1` |
| Widget ID (direct) | `ds-{project}--direct-{N}` | `ds-qsic-data--direct-1` |
| Widget ID (claude session) | `cs-{session-id}` | `cs-abc12345-def6-7890` |

### Key Design Decisions

- **TUI loop with tmux detach**: The TUI runs in a `while True` loop. After tmux detach (subprocess.run returns), the loop restarts and the TUI reappears. The loop breaks when the user quits without selecting a session.
- **Per-session tmux config**: Prefix defaults to `Ctrl-B` (tmux's standard default; configurable via `FUJIMOTO_TMUX_PREFIX`), status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched. The attach flow is silent (no pre-attach banner) to reduce noise when launching sessions repeatedly.
- **Global install via `uv tool`**: Requires `--force --reinstall` to rebuild the wheel from source. Plain `--force` reuses cached builds.
- **Session metadata**: `.fujimoto/meta.json` stored in worktree directory records the base branch for cherry-pick targeting and the `source_root` (main repo) for project-config source resolution. The `.fujimoto/` directory contains a `.gitignore` with `*` so its contents are automatically ignored by git.
- **Project config (`.fujimoto.yaml`)**: An optional, committed per-project file (`project_config.py`) declaring files to copy/link into a worktree and init commands to run. Applied centrally in `main()`'s launch loop (parent process, **before** `tmux attach`) by `_apply_worktree_config(working_dir)`, for **every** worktree connection mode (new, reconnect-to-live, relaunch/resume) — so copy/link/init run on each connect, not just creation. `_do_create_and_launch` no longer applies config; it only creates the worktree and stores meta. Key mechanics:
  - **Config is read from the source root (main clone), not the worktree.** `.fujimoto.yaml` is a local, uncommitted file in the main clone, so it isn't present in a worktree checkout — `_apply_worktree_config` calls `load_project_config(source_root)`. (`--create-config` writes it to the main clone's root.)
  - **Worktree detection**: `_resolve_worktree_source` returns the main repo root (from meta `source_root`, else derived via `git.get_main_worktree_root`) only for a *linked* worktree; returns `None` for the main repo / direct / adhoc sessions (which then get no config). This is git-based, not `session_type`-based, so the resume path (historically labelled `"direct"`) is covered too.
  - **`once` vs `always`**: trigger is `CREATE` only on the worktree's first launch — tracked by the `.fujimoto/config_once_applied` marker (`config.config_once_applied` / `mark_config_once_applied`); every later connection uses `LAUNCH` (only `always` entries). The marker is set after a successful (or `on_error=continue`) CREATE application, so `once` truly runs once.
  - **Syntax errors are surfaced when the TUI opens.** `on_mount` calls `_project_config_error()` (which parses `load_project_config(self._project_root)`) and, on a `ConfigError`, pushes a `ConfigErrorDialog` modal (OK button; Enter/Escape to dismiss) over the home screen. It's informational only — dismissing it doesn't block any action, since you may need to launch a session to fix the file. Shown once per TUI appearance (per `SessionApp` instance / loop iteration). At launch time `_apply_worktree_config` simply skips a malformed config (no pause), since it's already been surfaced. The dialog (and `_show_error`) render the message with `markup=False` / `rich.markup.escape` because pydantic validation text contains brackets that would otherwise be parsed as console markup.
  - **Errors**: copy/link issues are non-fatal warnings printed to stderr. A non-`continue_on_error` init failure is governed by the config's `on_error`: `abort` returns `False` (main `continue`s the loop → TUI reopens, launch skipped) and `continue` attaches anyway. Either way `_pause_for_key` shows the error and waits for a keypress so it isn't wiped by `tmux attach`.
- **Background PR creation**: Uses `claude -p --allowedTools "Bash(git:*) Bash(gh:*)"` in a tmux session for unattended PR creation.
- **Claude session integration**: The home screen fetches Claude session state from `~/.claude/projects/` JSONL logs via the log parser. Session states: 👀 awaiting input (`WAITING_FOR_USER`), 🛡️ approve tool (`WAITING_FOR_TOOL_APPROVAL`), ⚙ working (`WORKING`), 💤 idle (`IDLE`), no indicator (`UNKNOWN`). State logic: `last-prompt` marker → `IDLE` (session ended). For assistant entries: `stop_reason=tool_use` without a following `tool_result` → `WAITING_FOR_TOOL_APPROVAL` (pending user approval), `stop_reason=tool_use` with `tool_result` → `WORKING`, any other stop reason or no stop reason → `WAITING_FOR_USER`. Last entry is user → `WORKING`. Previous Claude sessions (from the project root, capped at 5) appear as resumable items. Resuming launches `claude --resume SESSION_ID` in a new tmux session. The latest Claude session per path is "claimed" by the corresponding tmux/worktree item to avoid duplication.
- **Resume previous session — tmux naming**: When resuming from an inactive worktree, the resumed session reuses the worktree's existing tmux session name (e.g., `project/20260101-feature`) instead of generating a new `direct-N` name. This keeps the session correctly identified as a worktree item on subsequent TUI views, so its path and Claude session lookup remain tied to the worktree directory. For active worktrees (original session still alive), a `direct-N` name is used because the worktree name is occupied. The working directory for resumed sessions always comes from `cs.cwd` (the directory recorded in the Claude session log) rather than `session.path`.
- **Live polling**: The home screen uses `set_interval(3s)` to poll Claude JSONL logs for state changes. When a session's state changes, labels are updated in-place via `label.update()` — the screen is never cleared or rebuilt, which avoids blank-screen flicker. A snapshot dict (`path → (session_id, state)`) is compared each tick to detect changes efficiently. The timer is stopped when navigating away (`_clear_main` cancels it) and restarted by `_show_home`.

## Testing

Tests use pytest with pytest-asyncio for TUI tests and pytest-cov for coverage. Run with:

```sh
uv run pytest
```

Coverage is reported automatically (configured in `pyproject.toml`).

### Matrix testing with nox

`noxfile.py` defines sessions that run the test suite across Python versions and pinned dependency versions, using uv as the venv backend (`nox.options.default_venv_backend = "uv"`).

- `uv run nox` — default session: `tests` against every supported Python (3.11–3.14). Each session runs `uv sync --active --group=dev` into the nox-managed venv, then `pytest`.
- `uv run nox -s tests-3.14` — single Python version.
- `uv run nox -s tests_textual` — parametrized over `TEXTUAL_VERSIONS` (currently 8.0.2 and 9.0.0). Add new versions to the constant in `noxfile.py` to extend the matrix.

To add a new Python version: append it to `PYTHON_VERSIONS` in `noxfile.py`, add the classifier to `pyproject.toml`, and add the version to the CI matrix in `.github/workflows/tests.yml`. To add a dependency-pin matrix for a new package, follow the `tests_textual` pattern: define a `*_VERSIONS` list and parametrize a session with `@nox.parametrize`.

CI runs the per-Python `tests` session as a GitHub Actions matrix; the coverage badge is generated only on the 3.13 job to avoid races.

**Maximize test coverage.** Write tests for all new code — unit tests for logic, async TUI tests using Textual's `app.run_test()` pilot for UI flows. Only skip coverage for lines that are genuinely impractical to test (e.g. defensive error handlers in deeply nested async TUI paths that can't be triggered through the pilot). Use `# pragma: no cover` sparingly and only with justification.

TUI tests follow this pattern:
- Patch external dependencies (`git`, `tmux`, `config`) via `_patch_git_info()` helper
- Use `async with app.run_test() as pilot:` to drive the UI
- Use `pilot.press()` to simulate keyboard input
- Navigate to items by setting `list.index` directly (more reliable than repeated `pilot.press("down")`)
- Assert on app state (`_launch_target`, `_base_branch`, `_session_map`) and DOM queries (`app.query()`)

## Documentation

**Keep documentation in sync with code changes.** When making changes to the codebase:

- **CLAUDE.md**: Update architecture, module responsibilities, naming conventions, and design decisions to reflect the current state. This is the primary reference — it must always be accurate.
- **README.md**: Update user-facing docs (usage, home screen layout, features, configuration) when UI or behaviour changes.
- **CONTRIBUTING.md**: Update developer guidance (project layout, manual testing steps, view patterns) when internal structure changes.

When you discover something new about the codebase, tooling, or patterns during a session — incorporate it into the appropriate documentation file rather than leaving it as tribal knowledge.

## Gotchas and Learnings

Things discovered during development that are easy to forget:

- **Textual widget IDs cannot contain `/`**. tmux session names use `project/name` but widget IDs must use `--` as separator (e.g. `ds-qsic-data--direct-1`).
- **`git worktree remove` needs `--force`** for worktrees with uncommitted changes — without it the command fails silently in some states.
- **`git reflog` records branch creation origin** (`branch: Created from main`) — useful for recovering the base branch if `.fujimoto/meta.json` is missing.
- **`claude -p` (print mode)** runs non-interactively. For background tasks, pair with `--allowedTools` to scope permissions rather than `--dangerously-skip-permissions`.
- **Global find-replace for renames** works well but always verify test patch target strings — they are plain strings not checked by the import system. Run the full test suite after any rename.
- **Claude log entry types evolve** — real logs contain `last-prompt`, `queue-operation`, `progress` and other types beyond `assistant`/`user`/`system`/`file-history-snapshot`. The parser skips unrecognized types gracefully. `last-prompt` signals session end → `IDLE` state. `stop_reason=None` on assistant entries means interrupted/canceled (Esc) → `WAITING_FOR_USER`. Always smoke-test against real `~/.claude/projects/` data after changes.
- **Shift+Enter in tmux requires `extended-keys always` globally** — tmux strips modifier info by default, making Shift+Enter identical to Enter. The fix requires two server/global-level settings: `set-option -g extended-keys always` and `set-option -s -a terminal-features xterm*:extkeys`. Per-session (`-t`) doesn't work. `extended-keys on` (vs `always`) doesn't work because Claude Code doesn't send the kitty keyboard protocol activation sequence. Requires tmux 3.2+. See `_ensure_extended_keys()` in `tmux.py`.
- **fujimoto never creates `.venv` — `uv` does.** `create_worktree` only runs `git worktree add`; there is no venv/copy logic in the worktree lifecycle. A `.venv` appears in a worktree only because `uv sync`/`uv run` was run there (each worktree is its own project root with its own `pyproject.toml`, and uv materializes a per-project environment by default unless `UV_PROJECT_ENVIRONMENT` is set — which only `noxfile.py` does). The intended way to seed an environment in a fresh worktree is an `init: [uv sync]` entry in `.fujimoto.yaml`.
- **Bundled package data must be importable as a subpackage.** `templates/` has an `__init__.py` so `importlib.resources.files("fujimoto.templates")` resolves and hatchling ships the `.template` file in the wheel. Verify with `uv build --wheel && unzip -l dist/*.whl | grep templates` after touching packaged resources.

## Releases

Releases are published to PyPI by `.github/workflows/release.yml` on `v*`
tag push. The package version is **derived from the git tag** by `hatch-vcs`
(`dynamic = ["version"]` in `pyproject.toml`) — do not add a static `version`
field, do not edit a version string when cutting a release. The release flow,
recovery procedures, and one-time setup are documented in
[CONTRIBUTING.md](CONTRIBUTING.md#releasing).

## Git Commits and PRs

Do not mention Claude or AI when authoring git commits or pull requests. No co-authored-by lines referencing Claude.

## Linting and Type Checking

Pre-commit hooks handle all linting and formatting automatically — do not run `ruff`, `ty`, or other linters manually. Let the hooks run at commit time and fix any issues they report. Any new linting or formatting tools should be added to `.pre-commit-config.yaml`, not run ad hoc.

Current hooks:
- **ruff** — linting and formatting
- **ty** — type checking (strict: no `unresolved-attribute` allowed)

All widget state must use typed instance variables, not dynamic attributes on Textual widgets.
