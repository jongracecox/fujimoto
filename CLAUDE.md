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
- Session metadata (base branch) stored in `.fujimoto-meta.json`

**Direct sessions** — Claude launched in an existing repo directory:
- No worktree creation, uses the repo's current branch
- Multiple concurrent sessions possible on same repo
- Named `{project}/direct-N` in tmux

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

**`tmux.py`** — tmux session management:
- `is_tmux_installed()` / `install_tmux()` — detection and brew install
- `list_project_sessions(project)` — lists active tmux sessions for a project
- `session_name(project, dir)` — naming convention: `{project}/{dir}`
- `create_session(name, dir)` — creates detached session, sets prefix to Ctrl+A, runs `claude`
- `create_session_with_command(name, dir, command)` — like `create_session` but with custom command
- `kill_session(name)` — `tmux kill-session -t`
- `attach_session(name)` — prints shortcut banner, then `subprocess.run` tmux attach (returns on detach)
- `launch_claude_in_tmux(project, path, tmux_name)` — orchestrates create-or-attach

**`claude/log_parser.py`** — Parse Claude Code's JSONL session logs:
- `ClaudeLogError` — raised on empty/unreadable logs or unknown enum values
- `EntryType` / `StopReason` / `SessionState` — StrEnums with strict `from_raw()` parsing
- `ClaudeSession` — frozen dataclass: session_id, state, cwd, git_branch, last_activity, etc.
- `encode_project_path(path)` — `str(path).replace("/", "-")` (matches Claude's directory encoding)
- `get_claude_projects_dir()` — `~/.claude/projects`
- `parse_session(jsonl_path)` — reads JSONL, tracks last meaningful (non-sidechain) entry, derives state
- `get_sessions_for_path(project_path)` — encodes path, globs `*.jsonl`, returns sorted sessions

**`cli.py`** — Textual TUI with async view management:
- `SessionInfo` — dataclass for session state (type, project, path, tmux name, active status)
- `SessionApp` — main app class with CSS styling
- Views: home (sessions list), session actions submenu, finish flow, confirm dialog, create form, branch select (3 options), branch picker (filterable list), conflict resolution, project switcher (with autocomplete filter), tmux install, error
- Home screen sections: actions ("New worktree session", "New session in X"), active sessions, inactive worktrees, switch project
- Worktree create flow: title → branch select (default w/ fetch & rebase, current branch, another branch → picker) → create
- Session actions submenu: Connect/Launch, Terminate, Finish (worktree only)
- Finish flow: Push & Create PR (background Claude), Cherry-pick to base, Discard & Delete
- All view transitions are `async` — `await _clear_main()` then `await mount()`
- Session data stored in `_session_map` dict keyed by ListItem ID
- `_launch_target` is `(project, path, tmux_name)`, set before `self.exit()`

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
| Widget ID (direct) | `ds-{project}--direct-{N}` | `ds-qsic-data--direct-1` |

### Key Design Decisions

- **TUI loop with tmux detach**: The TUI runs in a `while True` loop. After tmux detach (subprocess.run returns), the loop restarts and the TUI reappears. The loop breaks when the user quits without selecting a session.
- **Per-session tmux config**: Prefix remapped to Ctrl+A, status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched.
- **Global install via `uv tool`**: Requires `--force --reinstall` to rebuild the wheel from source. Plain `--force` reuses cached builds.
- **Session metadata**: `.fujimoto-meta.json` stored in worktree directory records the base branch for cherry-pick targeting.
- **Background PR creation**: Uses `claude -p --allowedTools "Bash(git:*) Bash(gh:*)"` in a tmux session for unattended PR creation.

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
- **`git reflog` records branch creation origin** (`branch: Created from main`) — useful for recovering the base branch if `.fujimoto-meta.json` is missing.
- **`claude -p` (print mode)** runs non-interactively. For background tasks, pair with `--allowedTools` to scope permissions rather than `--dangerously-skip-permissions`.
- **Global find-replace for renames** works well but always verify test patch target strings — they are plain strings not checked by the import system. Run the full test suite after any rename.
- **Claude log entry types evolve** — real logs contain `progress` entries alongside `assistant`, `user`, `system`, and `file-history-snapshot`. Always smoke-test the log parser against real `~/.claude/projects/` data after changes to `EntryType`.

## Git Commits and PRs

Do not mention Claude or AI when authoring git commits or pull requests. No co-authored-by lines referencing Claude.

## Linting and Type Checking

Pre-commit hooks run automatically:
- **ruff** — linting and formatting
- **ty** — type checking (strict: no `unresolved-attribute` allowed)

All widget state must use typed instance variables, not dynamic attributes on Textual widgets.
