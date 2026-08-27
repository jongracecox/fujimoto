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
the real terminal while Textual is running — see the gotcha below.

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
├── session_state.py   # which sessions the user still considers open
├── debug.py      # --debug / --debug-redacted diagnostic logging + redaction
├── project_config.py  # optional per-project .fujimoto.yaml (copy/link/init worktree setup)
├── templates/
│   ├── __init__.py
│   └── fujimoto.yaml.template  # commented scaffold written by `fujimoto --create-config`
└── claude/
    ├── __init__.py      # Re-exports public API
    ├── log_parser.py    # Parse Claude JSONL session logs (state, metadata, session lookup)
    └── search.py        # Full-text search across JSONL transcripts (batched, cancellable)
```

## Architecture

### Entry Point

`cli.py:main()` is the package entry point (`pyproject.toml` `[project.scripts]`). It parses CLI args:
- `--version`/`-V` prints `fujimoto {version}` and exits
- `--create-config` writes a commented `.fujimoto.yaml` template to the repo root (via `project_config.write_config_template`) and exits; errors (already exists, not a git repo) print to stderr and exit 1.
- `--debug` / `--debug-redacted` turn on diagnostic logging (see `debug.py`) before anything else runs, via `_start_debug_log(redact=...)`; `_finish_debug_log()` closes the log on every exit path (normal quit, prerequisite failure, fatal error, KeyboardInterrupt, and the `pane` subcommand). Both flags work alongside `pane`.
- `fujimoto pane <vscode|terminal> --session <name>` dispatches to `_run_pane_command`, used by the in-session tmux key table (`Ctrl-A v` / `Ctrl-A w`). Resolves the session's working directory via `tmux display-message -p '#{session_path}'` and calls the existing `open_vscode` / `open_terminal` helpers; errors are surfaced via `tmux display-message` so they appear in the session's status bar.

Otherwise it:
1. Runs the Textual `SessionApp` in a loop
2. After the TUI exits, calls `launch_claude_in_tmux()` if the user selected a session
3. When the tmux session is detached, the loop restarts and the TUI reappears
4. The loop exits when the user quits the TUI (q/escape/ctrl+c) without selecting a session

### Session Status

Two independent axes, deliberately separated because "Terminate" used to
conflate them:

- **Intent** — open or closed. Only fujimoto ever changes it.
- **Runtime** — whether tmux has a live session. Observed, never stored.

| intent | tmux | icon | home-screen section |
|---|---|---|---|
| open | running | 🟢 | sessions |
| open | stopped | 🟠 | sessions |
| closed / no record | stopped | ⚫ | inactive worktrees |

**Stop** ends the claude process and keeps the record open (the transcript is
untouched, so the conversation resumes; an in-flight task is interrupted).
**Terminate** ends it and deletes the record. Anything that kills a session
*outside* fujimoto — host restart, `tmux kill-session`, a closed window, `exit`
in the pane — leaves the record alone, so it comes back as stopped. That is the
whole restart-recovery mechanism: no boot-time detection, no heuristics.

### Session Types

**Worktree sessions** — isolated git worktree with its own branch:
- Creates a new branch + working directory via `git worktree add`
- Finish flow: Push & Create PR, Cherry-pick to base branch, or Discard & Delete
- Session metadata (base branch, source root, fork provenance) stored in `.fujimoto/meta.json` (auto-gitignored)

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
- `store_session_meta(path, base_branch, source_root=None, forked_from_session_id=None, forked_from_worktree=None)` / `read_session_meta(path)` — JSON metadata. `source_root` records the main repo the worktree was created from, so `project_config` can resolve copy/link sources on later launches (older worktrees without it fall back to deriving the root via `git.get_main_worktree_root`). The two `forked_from_*` keys record that the worktree was created by forking another session and where that session was running; keeping both in the worktree means a fork stays identifiable even if the source Claude transcript is deleted. All optional keys are omitted when `None`.
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

**`session_state.py`** — Which sessions the user still considers open, stored
as JSON in `~/.cache/fujimoto/sessions.json` (same graceful-degradation pattern
as `settings.py`: missing file, unreadable cache or corrupt JSON yield an empty
state, never an error):
- `SessionRecord` dataclass: `cwd`, `project`, `session_type`, `branch`,
  `claude_session_id`, `last_seen`. Only `cwd` is required — every other field
  defaults, so a record written by a different fujimoto version still loads.
- Keyed by **tmux session name**, so worktree, direct and ad hoc sessions are
  covered uniformly and a record outlives its worktree directory.
- `load_state()` / `save_state()`, `mark_open(...)` (every launch and
  reconnect; a reconnect passing no id keeps the id recorded at first launch),
  `mark_closed(name)`, `touch(name, claude_session_id=None)`,
  `rename(old, new)`, `prune()`.
- **A record's presence means "open"; its absence means "closed"** — which is
  also what a session fujimoto has never launched looks like. `mark_closed`
  therefore just deletes the record, and there is no reconciliation pass and
  nothing to age out. `prune()` drops records whose `cwd` is gone (deleted
  worktree, ad hoc temp dir cleared by a reboot) and is called once per
  `_init_git_info`, not per render.

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

**`debug.py`** — Diagnostic logging for support (`--debug` / `--debug-redacted`):
- Owns a process-wide optional `DebugLogger`. Every helper (`log`, `log_once`,
  `log_section`, `log_command`, `log_exception`, `rv`, `rp`) is a cheap no-op
  when debug mode is off, so call sites log unconditionally.
- `enable(redact=..., log_dir=None)` opens
  `<log_dir>/fujimoto-<YYYYMMDD-HHMMSS>-<pid>.log`, where `log_dir` defaults to
  `$FUJIMOTO_LOG_DIR` (`LOG_DIR_ENV`) or `~/.fujimoto/logs`, and writes a header
  documenting the redaction mode. `disable()` closes it. `is_enabled()` guards
  expensive collection.
- `log_environment()` records fujimoto/Python/platform versions, `argv`, cwd,
  tty state, the versions of `tmux`/`git`/`claude`/`gh`/`code`/`uv`/`brew` on
  PATH (`_tool_version` keeps the executable basename readable), and every
  `FUJIMOTO_*`/`CLAUDE_*`/`ANTHROPIC_*` plus curated terminal/shell env var.
  Values whose name matches `is_secret_name()` become `[SECRET-<len>]`
  **regardless of redaction mode** — secrets are never logged.
- `log_once(key, event, **fields)` writes only when the payload for `key`
  changed; used for polled state (Claude discovery, tmux session lists) so a
  long-lived run doesn't drown in repeats.
- `log_capped(series, event, *, limit=DEFAULT_SERIES_CAP, dedupe_key=None, ...)`
  logs the first `limit` items of a repeating series and counts the rest;
  `close()` then writes one `series.summarised series=… logged=… not_logged=…
  total=…` line per series, so the log always states what it left out and no
  call site has to remember to summarise. Passing `dedupe_key` composes the cap
  with `log_once` semantics, so re-renders spend the budget on *distinct*
  subjects — 47 worktrees over three home renders is 141 calls but `total=47`.
  Used by the home inventory (`tui.worktree`, `tui.item`), `config.read_meta`
  and Claude discovery; the caller may add its own richer summary of the
  remainder (`tui.worktree_summary`, `tui.item_summary`) since it knows what
  the items were — but that summary needs `log_once`, or it repeats verbatim on
  every home render. Order the loop so the interesting items come first — the
  home inventory logs running sessions before idle ones, and worktrees
  newest-first.
- **Split a series by outcome, and never cap a failure.** `claude.session_dirs`
  and `claude.discovery` fire once per worktree, and on a machine with ~50 of
  them that was 96 of a 290-line log — three quarters of it "no transcripts
  here", which is the normal state of an old worktree. They now use separate
  series per outcome (`.resolved`/`.missing`, `.found`/`.empty`) so the routine
  case cannot crowd out the interesting one, with a tighter limit on the routine
  one. A discovery with `failed>0` — a transcript fujimoto could not read —
  bypasses the cap entirely, since that is the difference between "no sessions"
  and "sessions I could not parse". The `cwd-index` fallback is likewise never
  capped: it firing at all is the diagnostic.
- Redaction: `redact_text` → `[REDACTED-<fingerprint>-<len>[-CONTAINS<chars>]]`
  (shape-preserving; the fingerprint is **salted per run** by `enable()` and the
  salt is never logged, so equal values correlate within one log and a
  fingerprint is meaningless outside it);
  `redact_path` keeps separators/depth, collapses `$HOME` to `~`, preserves
  `_SAFE_PATH_COMPONENTS` (OS directories and *dotted* config dirs — components
  that cannot be a name the user chose) and, **only under an owned parent**,
  `_OWNED_NAMES` (`~/.cache/fujimoto/sessions.json` reads plainly while
  `~/git/fujimoto` does not). `redact_arg` keeps flags, lowercase subcommands
  and git's ref vocabulary (`_SAFE_REF_COMPONENTS` via `_is_git_ref`);
  `redact_ref`/`rref` keep `main`/`HEAD` where a value is known to be a ref;
  `redact_id`/`rid` keep a widget id's kind prefix (`ds-`, `sa-`) and redact the
  tail. Captured command output is redacted token-by-token with `redact_arg`.
- Instrumented call sites: `git._run` (every command + rc + output),
  `tmux` (install/list/create/attach/kill/rename/configure/quick-terminal),
  `claude/log_parser` (which lookup strategy resolved a path — encoded name,
  cwd-index or nothing — plus discovery counts, per-session parse results and
  failures), `claude/search` (query compile, logs available, scan start/finish
  with scanned/hit counts, unreadable logs, discarded hits),
  `session_state` (load/save, mark_open/closed, touch, rename, and every
  skipped or pruned record), `config` (resolved roots, session meta),
  `project_config` (load, apply, actions, warnings), `settings`, `terminal`,
  `vscode`, and `cli` (git info, home inventory, selections, launch target,
  prerequisites, fatals).

**`tmux.py`** — tmux session management:
- `is_tmux_installed()` / `install_tmux()` — detection and install. macOS: brew install. Linux: raises `TmuxError` with a distro-appropriate install command (apt-get/dnf/pacman/zypper/apk) — does not invoke sudo automatically.
- `list_all_sessions()` — lists all active tmux session names
- `list_project_sessions(project)` — lists active tmux sessions for a project
- `session_name(project, dir)` — naming convention: `{project}/{dir}`
- `build_claude_command(system_prompt, resume_session_id, fork_session)` — composes the `claude` invocation. The flags **compose** rather than exclude each other (they used to be mutually exclusive): a fork needs `--resume <id> --fork-session` *and* `--append-system-prompt` together.
- `create_session(name, dir, system_prompt, resume_session_id, fork_session)` — creates detached session, applies the configured prefix, runs the command from `build_claude_command`
- `create_session_with_command(name, dir, command)` — like `create_session` but with custom command
- `kill_session(name)` — `tmux kill-session -t`
- `attach_session(name)` — `subprocess.run` tmux attach (returns on detach). Shortcut hints live in the per-session tmux status bar, not in a pre-attach banner.
- `launch_claude_in_tmux(project, path, tmux_name, system_prompt, resume_session_id, fork_session)` — orchestrates create-or-attach, supports resuming and forking previous Claude sessions
- `take_pending_action(name)` — reads **and clears** the `@fujimoto_pending_action` tmux session option (`PENDING_ACTION_OPTION`), the channel by which an in-session key binding hands work to the TUI. Returns `None` when the option is unset or the session is gone (both exit non-zero). Consumed by `main()` right after `tmux attach` returns.
- `get_session_path(name)` — returns the start directory of a tmux session via `display-message -p '#{session_path}'`, used by the `fujimoto pane` subcommand
- `display_message(name, message)` — surface a transient message in the session's status bar, used to report errors from `fujimoto pane` back into the session
- `_configure_session(name)` — applies the configured prefix (default `C-b`), status bar, and (if `FUJIMOTO_META_KEY` is non-empty) the fujimoto key table via `_configure_fujimoto_key_table`. Raises `TmuxError` if the meta and prefix keys collide. The `unbind-key C-b` step only runs when the prefix has been moved off `C-b` (otherwise it would unbind the new prefix).
- `quick_terminal_key()` — returns the configured `FUJIMOTO_QUICK_TERMINAL_KEY` (default `` C-` ``)
- `meta_key()` — public accessor for `FUJIMOTO_META_KEY` (empty when disabled), used by the TUI to render the `Ctrl-A s` tip on the terminate prompt
- `PENDING_FORK` / `PENDING_STOP` / `PENDING_CLOSE` — the three values the in-session key table writes to `PENDING_ACTION_OPTION`
- `enable_quick_terminal_binding()` / `disable_quick_terminal_binding()` — install/remove the **server-global** root-table binding (`bind-key -n <key>`) that toggles a 30% bottom pane. The bound command uses `if-shell -F '#{==:#{window_panes},1}'` to split on the first press and cycle focus on subsequent presses. Both are no-ops when the env var is empty. Idempotent.
- `_apply_quick_terminal_setting()` — called from `create_session` / `create_session_with_command` after `_ensure_extended_keys()`. Re-applies the binding when `Settings.quick_terminal_enabled` is True, so the feature survives `tmux kill-server`. Lazily imports `settings` to avoid a circular import.
- `_configure_fujimoto_key_table(name, meta_key)` — installs the one-shot key table: `t`/`T` (`if-shell` guard ensuring a single extra pane; falls back to `select-pane :.+`), `v`/`w` (dispatch to `fujimoto pane <action> --session #{session_name}` via `run-shell`), `f` (`set-option @fujimoto_pending_action fork \; detach-client` — hands the fork to the TUI), `s` (same shape, `stop` — no prompt needed), `d` (detach-client), `x` (see below), `[` (copy-mode), `?` (cheatsheet).
  `x` branches on pane count: `if-shell -F '#{==:#{window_panes},1}'` flags `close` and detaches when claude is alone in the window (killing that pane would end the session, so the TUI asks what to do), and otherwise keeps the original `confirm-before kill-pane`. The true branch is a single string containing `set-option ... ; detach-client` — inside an `if-shell` argument a bare `;` does separate commands, unlike at `bind-key` top level where it must be `\;`. Root-level `bind-key -n <meta_key> switch-client -T fujimoto` arms the chord.

**`claude/log_parser.py`** — Parse Claude Code's JSONL session logs:
- `ClaudeLogError` — raised on empty/unreadable logs
- `EntryType` / `StopReason` / `SessionState` — StrEnums with lenient `from_raw()` parsing (returns `None` for unrecognized values)
- `ClaudeSession` — frozen dataclass: session_id, state, cwd, git_branch, last_activity, etc.
- `encode_project_path(path)` — replaces `/` **and `.`** with `-`, matching Claude's directory encoding (see the gotcha below)
- `get_claude_projects_dir()` — `$CLAUDE_CONFIG_DIR/projects` if that env var is set, else `~/.claude/projects`
- `session_dirs_for_path(project_path)` — the transcript directories for a working directory, and the single place that mapping lives (`get_sessions_for_path` and `search.list_session_logs` both go through it). Tries the encoded name for the path as given and as `resolve()`d (Claude records the *physical* cwd, so a symlinked worktree root would never match otherwise), then falls back to `_cwd_index()` — a `cwd → dirs` map built from the `cwd` each transcript records. The encoding is a guess at what another program does with a path, so the fallback is what stops a future encoding change from silently reporting "no previous sessions". The index is built only when the encoded lookup misses and memoized per `(projects dir, mtime_ns)`, so it rebuilds when a session directory appears.
- `parse_session(jsonl_path)` — reads JSONL, tracks last meaningful (non-sidechain) entry, derives state. Skips lines that parse to a non-dict (a bare list or string is valid JSON but has no entry type), which previously raised `AttributeError` out of `get_sessions_for_path` and into the home screen.
- `get_sessions_for_path(project_path)` — encodes path, globs `*.jsonl`, returns sorted sessions
- `TranscriptMessage` — frozen dataclass: `role`, `text`, `timestamp`, `tool_id`. `role` is one of `user`, `assistant`, `thinking`, `tool_use`, `tool_result` — the last three come from *content blocks*, not entry types. `tool_id` carries a `tool_use`'s `id` and a `tool_result`'s `tool_use_id`, so the viewer can pair a call with its own reply
- `read_transcript(jsonl_path)` — reads a log into an ordered `list[TranscriptMessage]` for the read-only viewer. Skips sidechain (sub-agent), meta and unrecognized entries; clips tool inputs/results to 20 lines / 2000 chars (`_clip`) since whole-file payloads are unreadable in a TUI

**`claude/search.py`** — Full-text search over transcript *contents* (as opposed
to the home screen's name filter). UI-free and synchronous; the caller is
responsible for running it off the event loop.
- Three orthogonal axes. **Pattern**: literal (query is `re.escape`d) or regex — both compile to one `re.Pattern`, so there is a single scanning path. **Case**: `case_sensitive=False` (default) applies `re.IGNORECASE`, so the choice costs nothing at scan time and is honoured by the whole-file reject as well as by snippet spans. **Content** (`ContentMode`): `RAW` scans the transcript bytes as written (tool inputs, tool output, file contents, paths, commands all match); `TEXT` parses each entry and scans only string content and `text` blocks — deliberately *excluding* `tool_use` input and `tool_result` output, since including them would erase the distinction between the modes. `compile_matcher`'s own default is `RAW` (it is the primitive — "scan the bytes I gave you"); the *search view* opens in `TEXT`, which is the user-facing default and lives in `SessionApp.__init__`.
- `ContentMode` has `.label` (for the status line) and `.toggled()` (for the one-key toggle), so the UI holds no mode-name strings.
- `compile_matcher(query, *, regex, mode)` → `Matcher`; raises `SearchError` on a regex that won't compile. `Matcher.present_in(text)` is the whole-file predicate.
- `list_session_logs(project_root, worktrees)` — `session_dirs_for_path` for the root and each worktree, globbed and returned **mtime-descending**, deduplicated. The ordering is the feature: a caller rendering results as they arrive shows recently used sessions first.
- `search_log(path, matcher)` → `SearchHit | None`. **Reads the file and rejects it with a single `re.search` before any JSON parsing** — that fast path is what makes scanning hundreds of logs viable, since almost none contain the query. Returns `None` for an unreadable log, for a `ClaudeLogError` from `parse_session`, and (in `TEXT` mode) when the whole-file hit turned out to be in JSON scaffolding rather than in anything anyone said.
- `iter_hits(logs, matcher, *, batch_size=10, is_cancelled=None)` — generator yielding `(scanned_so_far, hits_in_batch)` every `batch_size` logs plus once at the end. `is_cancelled` is polled before each log; a cancelled scan yields nothing further. An empty log list yields exactly one `(0, ())` batch so the caller can report "no transcripts".
- `SearchHit(session, match_count, snippets)` where each snippet is a `Snippet(text, spans)`. Snippets are capped at `MAX_SNIPPETS` (3) per session, each a whitespace-collapsed, single-line window of `SNIPPET_RADIUS` (60) chars either side of the match with `…` elision markers.
- **`Snippet.spans` are the match offsets *within* `text`**, so the UI can highlight them without re-running the pattern — which it could not do reliably anyway, since `text` has had its whitespace collapsed and may no longer match a query that spanned a line break. `_collapse(window)` returns the collapsed text plus a `len(window) + 1` offset map, and `_snippet` maps every match in the window through it: a half-open window span `(a, b)` becomes `(offsets[a], offsets[b])`. Reporting *all* matches in the window (not just the anchor one) is what lets a dense passage highlight each hit.
- `_collect(source, matcher, count, snippets)` counts every match but skips snippetting one that falls inside a window already emitted — so `MAX_SNIPPETS` buys three *different* places rather than three near-identical views of one dense paragraph. Counts are unaffected.

**`cli.py`** — Textual TUI with async view management:
- `SessionInfo` — dataclass for session state (type, project, path, tmux name, active status, claude_session_id, claude_state, is_fork)
- `LaunchTarget` — `NamedTuple` describing what `main()` should launch: `(project, working_dir, tmux_name, session_type, resume_session_id, forked_from_session_id, forked_from_worktree)`. A `NamedTuple` rather than a dataclass so existing index-based access keeps working.
- `SessionApp` — main app class with CSS styling
- Module-level helpers: `_claude_state_label(state)`, `_relative_time(dt)`, `_get_claude_sessions(root, worktrees)`, `_is_fork_worktree(path)`, `_build_fork_system_prompt(project, working_dir, parent_worktree, base_branch)`, `_fit_snippet(snippet, max_width)` / `_render_snippet(snippet, max_width)` (search-result snippet rendering — see the `Content.assemble` gotcha)
- Instance helpers: `_build_session_label(session, state_suffix)` — the single source of truth for session row text (including the 🍴 fork marker), used by `_show_home`'s initial render of worktree rows and by `_poll_session_states` for in-place updates; `_build_claude_session_items(sessions, prefix)` — shared row rendering for the resume (`rp-*`), fork (`fp-*`) and log-viewer (`lp-*`) pickers
- Views: home (sessions list), session actions submenu, terminate/stop prompt (`#terminate-prompt`, opened by a pending `close` from `Ctrl-A x`; Terminate / Stop / Cancel with Terminate highlighted so Enter matches the `confirm-before` it replaces, and Cancel re-attaching via `_launch_target` so it costs nothing), finish flow, confirm dialog, create form, branch select (3 options), branch picker (filterable list), fork title form, fork branch select, fork session picker, session log picker + read-only log viewer, conflict resolution, project switcher (with autocomplete filter), tmux install, error
- Transcript search (`s`) — see the dedicated section below.
- Home screen name filter: `/` arms a filter box (`#home-search`) mounted above
  `#home-list`. `action_search` reveals + focuses it; `Input.Changed` re-renders
  the rows via `_refresh_home_list`; `Input.Submitted` keeps the filter and moves
  focus to the list; Escape (in the box, or on a filtered list via
  `action_go_back`) clears it through `_clear_search`. Matching is
  case-insensitive substring (`_search_matches`) over session/worktree name,
  branch and tmux name, and applies to the active, inactive and previous-Claude
  sections. A non-empty query hides the action/settings/switch-project rows and
  any section with no matches; an empty result set renders a disabled "no
  matching sessions" row.
- Home screen refresh (`r`): `action_refresh` re-runs `_init_git_info()` (tmux
  sessions, the `session_state` store, worktrees, projects — and it clears
  `_claude_cache`/`_fork_marker_cache`) then `_refresh_home_list()`, so a
  worktree or tmux session created outside fujimoto appears without leaving the
  screen. The 3s poller deliberately doesn't do this: it only updates Claude
  state on rows that already exist. The highlighted row's id is captured before
  the rebuild (appending rows moves the highlight) and re-applied after; the
  `/` filter is preserved because `_refresh_home_list` re-applies
  `_search_query`. Guarded on `_on_home`, so `r` is inert in other views.
- Home screen sections: actions ("Restore N stopped sessions" when any exist, "New worktree session", "New session in X", "Ad hoc session"), sessions — running 🟢 *and* stopped 🟠 together, since the circle colour carries the distinction (with Claude state indicators on the running ones), inactive worktrees, previous Claude sessions (resumable, capped at 5), switch project
- Worktree create flow: title → branch select (default w/ fetch & rebase, current branch, another branch → picker) → create
- Session actions submenu (in order): for active sessions, Connect → Fork session → Resume previous session; for inactive worktrees, Resume previous session → Fork session → Launch (resume is the more common action when picking an idle worktree). Then: View session log (whenever the path has a previous Claude session), Open terminal, Open in VS Code, Rename, Stop session (active only), Terminate session (active or stopped), Finish (worktree only), Cancel. Stop and Terminate are two menu items rather than one item plus a prompt — a menu is already a choice — but both route into the single `_end_session(session, terminate=...)` handler, which is also what the `Ctrl-A x` prompt calls. Claude-session items show just "Resume" + View session log + Open terminal/VS Code + Cancel. "Fork session" is always inserted at index 1 (`items.insert(1, ...)`) so its position holds across both layouts.
- "Resume previous session" auto-launches the sole candidate when only one previous Claude session exists for the path, skipping the picker. Two or more sessions still show the picker.
- Fork flow: `sa-fork` → `#fork-title-input` → `#fork-branch-list` (parent branch (default) / parent's base / another branch → the shared `_show_branch_picker`) → conversation picker `#fork-picker` (`fp-{i}`, only when >1 candidate) → the shared `_finalize_create` / `_do_create_and_launch`. Offered for worktree *and* direct sessions that have at least one previous Claude session.
- Session log viewer: `sa-viewlog` → `_show_log_picker` → `_show_session_log`. The
  picker (`#log-picker`, rows `lp-{i}` via the shared `_build_claude_session_items`)
  is skipped when the path has a single transcript, and for a `claude` row, which
  already names one (matched by `session_id`). `_show_session_log` renders
  `read_transcript` output into `#log-panel` as a pair of `Static`s per message
  (`.log-role` + `.log-body`, coloured per role by `_TRANSCRIPT_ROLES`); bodies are
  `Content`, not markup, for the same reason search snippets are — transcript text
  is arbitrary bytes. Tool calls and results are the exception: they are the bulk
  of a transcript by volume and the least of it by interest, so each becomes a
  one-line `Collapsible` titled by `_tool_summary` (tool name + clipped first
  argument), collapsed by default and reachable with Tab + Enter. A `tool_use`
  body drops its first line, which the title already carries, and **its result is
  rendered inside that same expansion** (arguments, a `↳ N lines` divider, then
  the output) rather than as a row of its own — a call and its reply are one
  thing to open. `_pair_results` matches them by `tool_id`, falling back to the
  next unclaimed result for logs without ids; parallel calls arrive as several
  `tool_use` blocks followed by several results, where position alone mis-pairs
  them. A result nothing claims still gets its own row, so no transcript content
  is silently dropped. **A *run* of consecutive tool messages nests inside one outer
  `Collapsible`** (`.log-tool-run`, titled by `_tool_run_title` — call count plus
  up to four distinct tool names) once it holds more than one call, so a session
  that made twenty calls in a row costs one screen row rather than twenty; a lone
  call stays unwrapped, since wrapping two rows in a third helps nobody. Runs are
  delimited by the prose between them. `.log-tool` zeroes Textual's default
  Collapsible `padding-bottom` and `border-top`, which otherwise read as gaps
  between messages, and `.log-tool-run .log-tool` drops the top margin again
  inside an opened run, where the calls are a list rather than separate
  messages. `#main` is already a `VerticalScroll`, so focusing it gives
  arrow/page/end scrolling for free, and Escape falls through to `action_go_back`,
  which returns to the search results when the menu came from a search and to the
  home screen otherwise. Nothing is launched — this path never sets
  `_launch_target`.
- Finish flow: Push & Create PR (background Claude), Cherry-pick to base, Discard & Delete
- Open terminal flow: sub-menu with "This window" (default; uses Textual's `App.suspend()` to pause the TUI, then runs `subprocess.run([$SHELL], cwd=session.path)` as a child process — when the user types `exit`, the TUI resumes on the session actions menu) and "New window" (spawns a new iTerm/Terminal/Linux emulator window via `open_terminal()`)
- All view transitions are `async` — `await _clear_main()` then `await mount()`
- Session data stored in `_session_map` dict keyed by ListItem ID
- `_launch_target` is a `LaunchTarget`, set before `self.exit()`

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
| Widget ID (fork picker) | `fp-{index}` | `fp-0` |

### Key Design Decisions

- **TUI loop with tmux detach**: The TUI runs in a `while True` loop. After tmux detach (subprocess.run returns), the loop restarts and the TUI reappears. The loop breaks when the user quits without selecting a session.
- **Per-session tmux config**: Prefix defaults to `Ctrl-B` (tmux's standard default; configurable via `FUJIMOTO_TMUX_PREFIX`), status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched. The attach flow is silent (no pre-attach banner) to reduce noise when launching sessions repeatedly.
- **Global install via `uv tool`**: Requires `--force --reinstall` to rebuild the wheel from source. Plain `--force` reuses cached builds.
- **Remembering sessions across a restart**: `session_state.py` records every session as open at launch — in `main()`, **before** `launch_claude_in_tmux` blocks on the attach, so a host that dies mid-session still has a record to restore from. `_init_git_info` loads the pruned state into `_open_sessions`; `_stopped_records()` derives the open-but-not-running set for the current project; `_build_home_items` renders those in the *sessions* section as 🟠 and excludes them from *inactive worktrees*. A **Restore N stopped sessions** row relaunches them all via `create_session` (detached, resuming each path's latest transcript, attaching to none) — deliberately without `_apply_worktree_config`, which would run N `init` blocks up front; config still runs when the user actually attaches. `_end_session(session, terminate=...)` is the single handler behind both menu items and both outcomes of the `Ctrl-A x` prompt; it tolerates a `kill_session` failure only when `session_exists` confirms the session is already gone (otherwise marking a live session closed would hide it). `_do_delete_worktree` and `on_rename_submitted` keep the store honest via `mark_closed` / `rename`.
- **Session metadata**: `.fujimoto/meta.json` stored in worktree directory records the base branch for cherry-pick targeting, the `source_root` (main repo) for project-config source resolution, and — for forks — `forked_from_session_id` plus `forked_from_worktree`. The `.fujimoto/` directory contains a `.gitignore` with `*` so its contents are automatically ignored by git.
- **Project config (`.fujimoto.yaml`)**: An optional, committed per-project file (`project_config.py`) declaring files to copy/link into a worktree and init commands to run. Applied centrally in `main()`'s launch loop (parent process, **before** `tmux attach`) by `_apply_worktree_config(working_dir)`, for **every** worktree connection mode (new, reconnect-to-live, relaunch/resume) — so copy/link/init run on each connect, not just creation. `_do_create_and_launch` no longer applies config; it only creates the worktree and stores meta. Key mechanics:
  - **Config is read from the source root (main clone), not the worktree.** `.fujimoto.yaml` is a local, uncommitted file in the main clone, so it isn't present in a worktree checkout — `_apply_worktree_config` calls `load_project_config(source_root)`. (`--create-config` writes it to the main clone's root.)
  - **Worktree detection**: `_resolve_worktree_source` returns the main repo root (from meta `source_root`, else derived via `git.get_main_worktree_root`) only for a *linked* worktree; returns `None` for the main repo / direct / adhoc sessions (which then get no config). This is git-based, not `session_type`-based, so the resume path (historically labelled `"direct"`) is covered too.
  - **`once` vs `always`**: trigger is `CREATE` only on the worktree's first launch — tracked by the `.fujimoto/config_once_applied` marker (`config.config_once_applied` / `mark_config_once_applied`); every later connection uses `LAUNCH` (only `always` entries). The marker is set after a successful (or `on_error=continue`) CREATE application, so `once` truly runs once.
  - **Syntax errors are surfaced when the TUI opens.** `on_mount` calls `_project_config_error()` (which parses `load_project_config(self._project_root)`) and, on a `ConfigError`, pushes a `ConfigErrorDialog` modal (OK button; Enter/Escape to dismiss) over the home screen. It's informational only — dismissing it doesn't block any action, since you may need to launch a session to fix the file. Shown once per TUI appearance (per `SessionApp` instance / loop iteration). At launch time `_apply_worktree_config` simply skips a malformed config (no pause), since it's already been surfaced. The dialog (and `_show_error`) render the message with `markup=False` / `rich.markup.escape` because pydantic validation text contains brackets that would otherwise be parsed as console markup.
  - **Errors**: copy/link issues are non-fatal warnings printed to stderr. A non-`continue_on_error` init failure is governed by the config's `on_error`: `abort` returns `False` (main `continue`s the loop → TUI reopens, launch skipped) and `continue` attaches anyway. Either way `_pause_for_key` shows the error and waits for a keypress so it isn't wiped by `tmux attach`.
- **Debug logging (`--debug` / `--debug-redacted`)**: instrumentation lives at
  the call sites that matter (subprocess wrappers, discovery functions, TUI
  state) rather than in a wrapper layer, so it covers every code path
  including ones the TUI can't reach. It is opt-in and no-op by default, which
  keeps the normal path free of I/O. `--debug-redacted` exists so a user can
  hand over a log without leaking project/branch/path names, while still
  showing lengths and special characters — enough to diagnose bugs caused by
  odd characters in a project name.
- **Background PR creation**: Uses `claude -p --allowedTools "Bash(git:*) Bash(gh:*)"` in a tmux session for unattended PR creation.
- **Claude session integration**: The home screen fetches Claude session state from `~/.claude/projects/` JSONL logs via the log parser. Session states: 👀 awaiting input (`WAITING_FOR_USER`), 🛡️ approve tool (`WAITING_FOR_TOOL_APPROVAL`), ⚙ working (`WORKING`), 💤 idle (`IDLE`), no indicator (`UNKNOWN`). State logic: `last-prompt` marker → `IDLE` (session ended). For assistant entries: `stop_reason=tool_use` without a following `tool_result` → `WAITING_FOR_TOOL_APPROVAL` (pending user approval), `stop_reason=tool_use` with `tool_result` → `WORKING`, any other stop reason or no stop reason → `WAITING_FOR_USER`. Last entry is user → `WORKING`. Previous Claude sessions (from the project root, capped at 5) appear as resumable items. Resuming launches `claude --resume SESSION_ID` in a new tmux session. The latest Claude session per path is "claimed" by the corresponding tmux/worktree item to avoid duplication.
- **Forking a session**: "Fork session" creates a new worktree *and* forks the conversation in one step — `git worktree add -b worktree/<new> <path> <base>` followed by `claude --resume <parent-id> --fork-session` in the new directory. Notes:
  - The base branch defaults to the **parent's branch**, so the fork inherits the parent's commits; the other options are the parent's own base branch (a sibling rather than a child) and any branch via the picker. `_show_branch_picker` hides `worktree/*` branches for a plain create but keeps them for a fork, since that is exactly what a fork wants.
  - The fork branches from the parent's **committed tip** — uncommitted work in the parent is not carried over. `_build_fork_system_prompt` tells the forked session this, along with the parent's path, because the inherited conversation refers to files by the *parent's* paths.
  - **Cross-directory resume requires Claude Code ≥ 2.1.223**, which looks a session id up in the current project and its worktrees first, then every other project on the machine. Earlier versions only searched the current project directory.
  - Claude Code keys transcripts by cwd, so the forked session's JSONL lands under the *new* worktree's encoded project dir. No special casing is needed — `_get_claude_sessions` and the home screen's live state polling pick a fork up like any other session.
  - **In-session `Ctrl-A f` hands over to the TUI rather than reimplementing the flow.** A key binding can only prompt for free text (`command-prompt`), which makes naming a base branch a blind typing exercise and a conversation picker impossible. So `f` runs `set-option @fujimoto_pending_action fork \; detach-client`: the detach returns control to the `fujimoto` process blocked in `attach_session`, `main()` consumes the flag with `take_pending_action`, and the next `SessionApp(pending_fork=...)` opens straight onto the fork flow via `_open_pending_fork`. One implementation, all the pickers. Three non-obvious mechanics:
    - `set-option` must **not** take `-t "#{session_name}"`. A key binding already targets its own session, and the target of `set-option` is *not* format-expanded — passing it makes the command fail, which aborts the rest of the sequence so the detach never happens.
    - The two commands are joined by a literal `\;` argument. A bare `;` is consumed as a top-level separator by the `tmux bind-key` invocation itself, so `detach-client` would run at bind time instead of being bound.
    - `tmux send-keys` **cannot** test key bindings — it injects into the pane and bypasses key-table dispatch entirely. Drive an attached client's pty instead (`pty.fork()`, then `os.write(fd, b"\x01f")`).
  - The flag is cleared on read, so a later ordinary detach of the same session can't re-trigger the fork. `main()` keys it on `tmux_name or session_name(project, working_dir.name)` — the same name `launch_claude_in_tmux` derives — so a freshly created worktree (whose `tmux_name` is `None`) is matched correctly.
  - The whole flow funnels into the shared `_finalize_create` / `_do_create_and_launch`, so forks inherit the directory-conflict handling (`_show_conflict`, `conflict-suffix`) for free. A non-`None` `_fork_source` is the only difference; `_show_create_form` clears the fork state so a cancelled fork can't leak into the next plain create.
- **Resume previous session — tmux naming**: When resuming from an inactive worktree, the resumed session reuses the worktree's existing tmux session name (e.g., `project/20260101-feature`) instead of generating a new `direct-N` name. This keeps the session correctly identified as a worktree item on subsequent TUI views, so its path and Claude session lookup remain tied to the worktree directory. For active worktrees (original session still alive), a `direct-N` name is used because the worktree name is occupied. The working directory for resumed sessions always comes from `cs.cwd` (the directory recorded in the Claude session log) rather than `session.path`.
- **Home list fills the screen**: `#home-panel` and `#home-list` are both
  `height: 1fr`, so the session list expands to whatever vertical space `#main`
  leaves after the header, update banner, search box and bottom bar, and scrolls
  internally beyond that. It used to be `height: auto; max-height: 24`, which
  capped the visible rows at 24 no matter how tall the terminal was.
- **Transcript search (`s`)** — the counterpart to `/`: `/` matches session
  *names* from data already in memory, `s` matches transcript *contents*, which
  means reading every JSONL log for the project root and its worktrees. Scoped to
  the current project (not every project on the machine), because a result has to
  resolve to a session this home screen can act on. View is `#search-panel`
  (`#search-input`, `#search-status`, `#search-results`), opened by
  `action_session_search` from the home screen only.
  - **The scan cannot live in a `_build_*_items()` helper.** It runs in a
    `@work(thread=True, exclusive=True, group="transcript-search")` worker
    (`_run_transcript_search`) driving `claude_search.iter_hits`, handing each
    batch of 10 back with `call_from_thread(self._apply_search_batch, ...)`, which
    appends rows and updates the progress line. Results therefore appear while
    the scan is still running, and the event loop only ever does row appends.
  - **`_search_token` is the correctness mechanism, not `exclusive=True`.**
    Cancellation isn't instantaneous: a batch can already be queued on the event
    loop when the query changes or the view is torn down. Every scan bumps the
    token; `_apply_search_batch` / `_search_failed` drop anything carrying a stale
    one. `_start_transcript_search` bumps *before* cancelling the group, and
    `_stop_transcript_search` (called from `_clear_main`) bumps again and cancels.
  - Measured on 302 transcripts / 222 MB (every log on one machine, i.e. far
    beyond a single project): first results at ~50 ms, full scan 1.2–2.2 s, all
    off the event loop. A realistic single project is a handful of logs and
    finishes in tens of milliseconds. Re-measure with
    `search.iter_hits` over `~/.claude/projects/**/*.jsonl` if you change the
    scanning path.
  - `Input.Changed` is **debounced** (`SEARCH_DEBOUNCE = 0.3`) with a minimum
    query length (`SEARCH_MIN_QUERY = 2`); without both, a scan spends most of its
    life being cancelled by the next keystroke. As with `#home-search`, mounting
    the box with a preserved value fires a spurious `Changed`, ignored when the
    value matches `_transcript_query` and hits are already on screen.
  - Three live toggles, all Ctrl chords because they must fire while the `Input`
    holds focus (a plain letter would be typed into it): `ctrl+r`
    (`action_toggle_search_regex`), `ctrl+t` (`action_toggle_search_mode`) and
    `ctrl+i` (`action_toggle_search_case`). Each rescans via
    `_restart_transcript_search`; all three are rendered by
    `_search_status_text`, and none is reset by `_show_session_search`, so a
    choice sticks for the session.
  - **`ctrl+i` is the same byte (0x09) as Tab in a legacy terminal.** It only
    arrives as a distinct key under the kitty keyboard protocol, which Textual
    requests via `ESC [>1u` in its driver: the parser then reads `CSI 105;5u` as
    `ctrl+i` and `CSI 9u` as `tab`. Without that protocol (macOS Terminal.app,
    older iTerm2) Ctrl+I is delivered as `tab` and the binding never fires —
    verified by feeding both sequences to `XTermParser`. Textual's
    `KEY_ALIASES` maps `tab → ["ctrl+i"]`, but the alias does not make a `tab`
    press trigger a `ctrl+i` binding, so there is no false-positive risk (there
    is a test for that). If this needs to work everywhere, move it to a chord
    with no legacy collision.
  - **Snippet rendering** is two module-level helpers. `_fit_snippet` trims a
    snippet to the panel width by *sliding* the window to keep the first match
    visible (a plain right-truncation would cut it off, since the snippet is
    centred on the match with 60 chars of lead-in), shifting the spans and
    dropping any that fall outside; it reserves a column for an elision marker
    at both ends whether or not both are needed, because the alternative is a
    fixed-point loop — adding a marker narrows the body, which can move the
    window, which changes whether a marker is needed. `_render_snippet` then
    assembles `(text, style)` pairs into a `Content`: dim for context,
    `SNIPPET_MATCH_STYLE` (`b $warning`, theme-resolved) for each match.
  - **The view opens in `ContentMode.TEXT`.** Message text is quieter (a common
    word cannot hit a JSON key, a session uuid or tool output) and in practice
    faster than raw: the whole-file `re.search` reject runs first either way, so
    `TEXT` only pays for JSON parsing on the files that already matched, while
    `RAW` pays for snippet building on far more lines. Neither toggle is reset by
    `_show_session_search`, so a switch to raw sticks for the session.
  - Selecting a result opens the **existing** session-actions menu with a
    `session_type="claude"` `SessionInfo` (path from `cs.cwd`), so Resume / Open
    terminal / Open in VS Code come for free. `_show_session_actions(...,
    from_search=True)` records where it was opened from in
    `_actions_from_search`, so `sa-cancel` and `action_go_back` return to
    `_show_session_search(restore=True)` — re-rendering the collected
    `_search_hits` rather than throwing a completed scan away. Opening the menu
    from the home screen clears the flag, so that path still returns home.
- **Home name filter rebuilds rows, not the screen**: `_show_home` mounts the panel;
  all row construction (and the population of `_session_map` /
  `_claude_state_snapshot`) lives in `_build_home_items()`, which applies the
  current `_search_query`. Filtering therefore only clears and re-appends the
  `ListView` children — the search box keeps focus and its cursor. Because
  `_session_map` holds only the visible rows, `_poll_session_states` naturally
  updates just the filtered set. The search box is always mounted with
  `display` toggled (never remounted), so `/` can't lose typed input; mounting
  it with a preserved value fires a spurious `Input.Changed`, which
  `on_home_search_changed` ignores by comparing against `_search_query`. Arrow
  keys while the box is focused are handled in `_on_key` and skip disabled
  separator rows (as does the post-filter default highlight, via
  `_first_selectable_index`).
- **Nothing in the home render path may touch the disk.** `_build_home_items`
  runs on every `/` keystroke, and it used to call `_get_claude_sessions`
  directly — re-parsing every JSONL transcript per character typed, which is what
  made the filter lag behind typing. It now reads `_claude_session_data()`
  (memoized in `_claude_cache`; invalidated by `_init_git_info` on project switch
  and by `_show_home` on re-entry, and *refreshed* rather than bypassed by
  `_poll_session_states` so the next keystroke renders from freshly polled data)
  and `_is_fork()` (memoizes `_is_fork_worktree`'s `meta.json` read in
  `_fork_marker_cache`; fork provenance never changes).
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

## Diagnostic Logging

`fujimoto --debug` / `--debug-redacted` writes a support log (see `debug.py` in
Module Responsibilities, and the README's Troubleshooting section). The log's
value depends entirely on it staying current with the code.

**Instrument new features as you add them.** A feature that isn't instrumented
is invisible in a support log, and its absence is indistinguishable from it
working. Any new code that does one of the following needs logging in the same
change:

- runs a subprocess, or reads/writes a file outside the repo
- discovers or resolves something (sessions, paths, branches, config) — log
  the *outcome* and, when there is a fallback chain, **which strategy
  succeeded**
- silently drops or skips an item (a malformed record, an unparseable log, a
  pruned entry) — a silent skip is exactly what a bug report looks like
- fails in a way the user sees as "nothing happened"

How to write it:

```python
from fujimoto import debug

debug.log("session_state.mark_closed", session=debug.rv(name), removed=removed)
debug.log_once(f"claude-dirs-{path}", "claude.session_dirs", via="cwd-index")
```

- Every helper is a no-op when debug mode is off, so call it unconditionally.
  Guard with `debug.is_enabled()` only when *building* the fields is expensive.
- Name events `<module>.<event>` and pass data as keyword fields, never as a
  pre-formatted sentence — the log is grepped and diffed.
- **Redaction is the caller's job.** Wrap user-identifying values (project,
  branch, session and tmux names, search queries, arbitrary user text) in
  `debug.rv()` and every path in `debug.rp()`. A raw f-string interpolation
  bypasses redaction and leaks into `--debug-redacted` logs.
- Never log a credential. `log_environment` replaces values whose env var name
  matches `debug.is_secret_name()` with `[SECRET-<len>]` in **both** modes; if
  you add a new source of secrets, extend that predicate.
- Use `debug.log_once(key, ...)` for anything on a polling path — the home
  screen re-reads Claude logs every 3 seconds. Pick a `key` that identifies the
  *subject* (a path, a session id), so a change in state still logs.
- The event name is positional-only, so `name=` is safe as a field.
- `debug.py` must not import other fujimoto modules at module scope (they
  import it) — the `fujimoto.version` import inside `log_environment` is lazy
  for that reason.

Tests must call `debug.disable()` in teardown (the logger is process-wide and
would otherwise leak between tests) and point `FUJIMOTO_LOG_DIR` or
`enable(log_dir=...)` at `tmp_path` — never write to the real
`~/.fujimoto/logs`. See the autouse fixtures in `tests/test_debug.py`.

Before shipping a change that touches redaction, re-run the leak check:

```sh
FUJIMOTO_LOG_DIR=/tmp/fjlog fujimoto --debug-redacted
grep -c "$(whoami)" /tmp/fjlog/*.log   # expect 0
```

## Documentation

**Keep documentation in sync with code changes.** When making changes to the codebase:

- **CLAUDE.md**: Update architecture, module responsibilities, naming conventions, and design decisions to reflect the current state. This is the primary reference — it must always be accurate.
- **README.md**: Update user-facing docs (usage, home screen layout, features, configuration) when UI or behaviour changes.
- **CONTRIBUTING.md**: Update developer guidance (project layout, manual testing steps, view patterns) when internal structure changes.

When you discover something new about the codebase, tooling, or patterns during a session — incorporate it into the appropriate documentation file rather than leaving it as tribal knowledge.

**Instrumentation counts as part of a feature, not a follow-up.** When adding a
feature, add its `debug` logging in the same change (see Diagnostic Logging
above) — the debug log is a support tool, and it silently rots when new code
paths land uninstrumented.

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
- **Never fingerprint a constant — it becomes a crib inside the same log.**
  Salting stops a fingerprint being matched *across* logs, but not within one.
  `~/.cache/<X>` is always `fujimoto`, so hashing that component published the
  digest of a known word, and the same digest appeared wherever the *project*
  name was redacted — re-leaking it for any repo called fujimoto. Constants that
  fujimoto or Claude Code choose themselves (`_OWNED_NAMES`, matched only under
  `_OWNED_PARENTS`) are therefore preserved verbatim rather than hashed. When
  adding redaction, ask both "could this be the user's name?" and "is this value
  fixed enough to be guessed?" — a yes to the second means preserve, not hash.
- **Over-redaction makes a log useless, which is its own kind of bug.** The
  first cut redacted TUI widget ids (`id=[REDACTED-…-44]` hid *which row the
  user picked*), `project_config` action verbs (hiding that it copied `.env` and
  ran `uv sync`) and the `main` branch name. Those are fujimoto's and git's own
  vocabulary, not user data. `rid`, `rref` and splitting a `<verb> <target>`
  action into `kind=` plus a redacted `detail=` exist for exactly this.
- **Anything called per home render needs `log_once` or `log_capped`.**
  `config.read_meta` runs once per worktree per render, which on a machine with
  ~50 worktrees wrote 142 lines of a 485-line log. Keyed dedupe cut that to one
  line per worktree, and `log_capped` cut it again to ten plus a count. Note the
  two must compose: capping raw *calls* reported `total=141` and hid 37 distinct
  worktrees behind a number that conflated re-renders, which is why
  `event_capped` takes a `dedupe_key`.
- **An unsalted redaction fingerprint is known-plaintext.** A log always
  contains fixed strings (fujimoto's own `~/.cache/fujimoto`, for one), so an
  unsalted digest hands the reader the fingerprint of a known word — and from
  there any guessable project name can be confirmed by hashing it. `_fingerprint`
  is salted with `secrets.token_bytes(16)` per `enable()`, the salt is never
  written to the log, and `disable()` clears it so `redact_text` is deterministic
  (and the doctests meaningful) when no run is active. Correlation *within* a log
  is the only property needed, and it survives.
- **A redaction allowlist must only contain names a user cannot have chosen.**
  `_SAFE_PATH_COMPONENTS` originally included `fujimoto`, `git`, `logs`, `src`,
  `main` and `master` — which leaked the project name of every repo living in
  `~/git/<project>`, and of fujimoto's own checkout, straight into
  `--debug-redacted` logs that are meant to be shareable. Dotted config dirs
  (`.claude`, `.fujimoto`) and OS dirs are safe because nobody names a repo
  `.cache`; ordinary words are not. Git ref words live in a separate
  `_SAFE_REF_COMPONENTS` used only for *command arguments*, since `origin/main`
  is git's vocabulary but a *directory* called `origin` is the user's. When
  adding to either list, ask "could this be the name of someone's project?"
- **Never unpack a `**fields` dict into `log_capped`.** Its `limit` and
  `dedupe_key` are typed keywords, so `**fields` (a `dict[str, object]`) could
  supply either — `ty` rejects it, and at runtime a field named `limit` would
  silently become the cap. Spell the fields out at each call site, even when
  that means repeating them across two branches.
- **`DebugLogger.event()` takes its event name positional-only** (`def event(self, event_name, /, **fields)`). Without the `/`, logging a field literally called `name=` (env vars, tool names) raises `TypeError: got multiple values for argument 'name'`. Same for `event_once`, `debug.log` and `debug.log_once`.
- **`debug.py` must not import from other fujimoto modules at module scope** (other than a lazy `fujimoto.version` import inside `log_environment`) — `git`, `tmux`, `config`, `settings`, `project_config`, `terminal`, `vscode` and `claude.log_parser` all import it, so a top-level back-import would be circular.
- **Bundled package data must be importable as a subpackage.** `templates/` has an `__init__.py` so `importlib.resources.files("fujimoto.templates")` resolves and hatchling ships the `.template` file in the wheel. Verify with `uv build --wheel && unzip -l dist/*.whl | grep templates` after touching packaged resources.
- **Inside an `if-shell` argument a bare `;` separates tmux commands; at `bind-key` top level it must be `\;`.** The `x` binding's true branch is the single string `set-option @fujimoto_pending_action close ; detach-client`, which works because tmux re-parses that string; the `f` and `s` bindings pass `"\\;"` as its own argv element because the outer `bind-key` invocation would otherwise consume a bare `;` itself and run `detach-client` at bind time. Both forms are exercised in `tests/test_tmux.py`, and both were verified against a real tmux via a pty before being wired up.
- **Claude encodes a project path by replacing `/` *and* `.` with `-`.** `/repo/.fujimoto/worktrees/x` is stored as `-repo--fujimoto-worktrees-x` — the double hyphen is the `/` plus the `.`. Encoding only the slashes made `get_sessions_for_path` miss every transcript for a worktree under the default `<repo>/.fujimoto/worktrees/` root (used whenever `FUJIMOTO_WORKTREE_ROOT` is unset), which showed up as "Resume previous session" being absent from every inactive worktree — and, from the same lookup, no Claude state icons, no fork, no session log and no search hits. Verify the rule against real data rather than assuming: read the `cwd` out of a transcript and compare it to the directory name holding it. Claude also honours `CLAUDE_CONFIG_DIR`, which moves `projects/` wholesale.
- **`kill_session` raises when the session is already gone**, so any code path that ends a session has to decide what that means. `_end_session` re-raises only if `session_exists` still reports the session — a session that died between being listed and being acted on is already in the state the kill was aiming for, while a kill that genuinely failed must not silently mark a live session closed.
- **Never splice arbitrary text into a console-markup string — assemble a `Content` instead.** Search snippets are raw transcript bytes cut at arbitrary offsets, and both of these silently corrupt the row: a fragment ending in `[` swallows the tag that follows it (`[dim]{"a": [[/]` renders the literal text `{"a": [[/]`), and a fragment ending in `\` escapes it (`[dim]path\[/]` renders `path[/]`). `rich.markup.escape` does **not** save you — it only escapes a `[` that still looks like a tag *in the fragment it is given*, so `escape('{"a": [')` returns the string unchanged. `Content.assemble(("text", "style"), ...)` never parses the text at all, resolves `$theme-variables` in the style, and — unlike a nested `[b]` inside `[dim]` — does not inherit the surrounding `dim` into the highlight. See `_render_snippet`.
- **`Static`/`Label` text in tests is read with `str(widget.render())`, not `.renderable`** — Textual 8 dropped the attribute. `render()` returns the *resolved* content, so console markup (`[dim]`, `[b]`) is gone from the string; assert on the plain text. And a `ListItem`'s own children are composed when the item mounts, so `item.query(Label)` needs an `await pilot.pause()` after a non-awaited `ListView.append`.
- **A worker's `is_cancelled` is not a synchronisation primitive.** Cancelling a Textual worker (or letting `exclusive=True` supersede it) does not unwind work already queued on the event loop via `call_from_thread`. Anything a worker hands back must carry a generation token the handler checks — see `_search_token`. Bump the token *before* cancelling, so a batch in flight is stale from the moment the decision is made.
- **OSC escape writes during a Textual run must go to `sys.__stdout__`, not `sys.stdout`.** Textual replaces `sys.stdout` with an internal capture while the app runs, so an OSC sequence (e.g. the `set_terminal_title` iTerm2/window-title escape) written to `sys.stdout` from inside a running app — such as `_init_git_info` updating the title on project switch — never reaches the terminal. `sys.__stdout__` stays connected to the real tty, so writing there works both before and during `app.run()`. This is why the session-manager title set at `main()` (pre-run) worked but the in-app update initially did not.

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
