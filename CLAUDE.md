# fujimoto

CLI/TUI tool for managing Claude Code sessions in git worktrees and repositories.

## Commands

```sh
uv sync                                        # Install dependencies
uv run fujimoto                          # Run locally (must be inside a git repo)
uv run pytest                                  # Run tests with coverage
uv tool install --force --reinstall .          # Install globally (re-run after code changes)
```

## Required Environment Variables

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/   # Where worktrees are created
export FUJIMOTO_GIT_ROOT=~/git/                  # Optional: enables project switching
```

## Prerequisites

- Python 3.11+
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
└── claude/
    ├── __init__.py      # Re-exports public API
    └── log_parser.py    # Parse Claude JSONL session logs (state, metadata, session lookup)
```

## Architecture

### Entry Point

`cli.py:main()` is the package entry point (`pyproject.toml` `[project.scripts]`). It:
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
- `get_worktree_root()` — reads `FUJIMOTO_WORKTREE_ROOT`, raises `ConfigError` if unset
- `get_git_projects_root()` — reads `FUJIMOTO_GIT_ROOT`, returns `None` if unset
- `list_projects()` — scans git root for directories containing `.git`
- `slugify(title)` — lowercase, replace non-alphanumeric with hyphens, strip/collapse
- `build_worktree_path(project, title)` — `{root}/{project}/{YYYYMMDD}-{slug}`
- `get_project_worktrees_dir(project)` — `{root}/{project}`
- `store_session_meta(path, base_branch)` / `read_session_meta(path)` — JSON metadata
- `get_next_direct_session_name(project, sessions)` — computes `{project}/direct-N`
- `get_next_adhoc_session_name(sessions)` — computes `adhoc-N`

**`git.py`** — Thin wrappers around `git` subprocess calls:
- `_run(args, cwd)` — subprocess runner, raises `GitError` on non-zero exit
- `get_repo_root()` — `git rev-parse --show-toplevel`
- `get_project_name()` — basename of repo root
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
- `open_terminal(directory)` — opens iTerm2 if installed, otherwise Terminal.app. Raises `OSError` on non-macOS.
- `_has_iterm()` — checks for `/Applications/iTerm.app`
- `_open_iterm(directory)` — AppleScript to create new iTerm2 window
- `_open_terminal_app(directory)` — `open -a Terminal` fallback

**`vscode.py`** — Open directories in VS Code:
- `open_vscode(directory)` — runs `code <directory>`. Raises `OSError` if the `code` CLI is not on PATH.
- `_has_vscode()` — checks for `code` on PATH via `shutil.which`

**`tmux.py`** — tmux session management:
- `is_tmux_installed()` / `install_tmux()` — detection and brew install
- `list_all_sessions()` — lists all active tmux session names
- `list_project_sessions(project)` — lists active tmux sessions for a project
- `session_name(project, dir)` — naming convention: `{project}/{dir}`
- `create_session(name, dir, system_prompt, resume_session_id)` — creates detached session, sets prefix to Ctrl+A, runs `claude` (with optional `--resume`)
- `create_session_with_command(name, dir, command)` — like `create_session` but with custom command
- `kill_session(name)` — `tmux kill-session -t`
- `attach_session(name)` — prints shortcut banner, then `subprocess.run` tmux attach (returns on detach)
- `launch_claude_in_tmux(project, path, tmux_name, system_prompt, resume_session_id)` — orchestrates create-or-attach, supports resuming previous Claude sessions

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
- Session actions submenu: Connect/Launch, Resume previous session (worktree/direct), Terminate, Resume (claude sessions), Rename, Open terminal, Open in VS Code, Finish (worktree only)
- Finish flow: Push & Create PR (background Claude), Cherry-pick to base, Discard & Delete
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
- **Per-session tmux config**: Prefix remapped to Ctrl+A, status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched.
- **Global install via `uv tool`**: Requires `--force --reinstall` to rebuild the wheel from source. Plain `--force` reuses cached builds.
- **Session metadata**: `.fujimoto/meta.json` stored in worktree directory records the base branch for cherry-pick targeting. The `.fujimoto/` directory contains a `.gitignore` with `*` so its contents are automatically ignored by git.
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

## Git Commits and PRs

Do not mention Claude or AI when authoring git commits or pull requests. No co-authored-by lines referencing Claude.

## Linting and Type Checking

Pre-commit hooks handle all linting and formatting automatically — do not run `ruff`, `ty`, or other linters manually. Let the hooks run at commit time and fix any issues they report. Any new linting or formatting tools should be added to `.pre-commit-config.yaml`, not run ad hoc.

Current hooks:
- **ruff** — linting and formatting
- **ty** — type checking (strict: no `unresolved-attribute` allowed)

All widget state must use typed instance variables, not dynamic attributes on Textual widgets.
