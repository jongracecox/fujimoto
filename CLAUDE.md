# claude-worktree-manager

CLI/TUI tool for creating git worktrees and launching Claude Code in tmux sessions.

## Commands

```sh
uv sync                                        # Install dependencies
uv run worktree                                # Run locally (must be inside a git repo)
uv run pytest                                  # Run tests with coverage
uv tool install --force --reinstall .          # Install globally (re-run after code changes)
```

## Required Environment Variable

```sh
export CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT=~/git/worktrees/
```

## Prerequisites

- Python 3.11+
- tmux (auto-installs via brew if missing)
- git

## Project Structure

```
src/claude_worktree/
├── __init__.py
├── cli.py        # Textual TUI app, entry point (main()), all UI screens and event handlers
├── config.py     # Environment variable loading, path construction, slugify utility
├── git.py        # Git subprocess wrappers (worktree creation, branch detection)
└── tmux.py       # tmux session lifecycle (create, attach, list, install)
```

## Architecture

### Entry Point

`cli.py:main()` is the package entry point (`pyproject.toml` `[project.scripts]`). It:
1. Creates and runs the Textual `WorktreeApp`
2. After the TUI exits, calls `launch_claude_in_tmux()` if the user selected a worktree
3. `launch_claude_in_tmux` uses `os.execvp` to replace the process with `tmux attach`

### Module Responsibilities

**`config.py`** — Pure functions, no side effects except directory creation:
- `get_worktree_root()` — reads `CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT`, raises `ConfigError` if unset
- `slugify(title)` — lowercase, replace non-alphanumeric with hyphens, strip/collapse
- `build_worktree_path(project, title)` — `{root}/{project}/{YYYYMMDD}-{slug}`
- `get_project_worktrees_dir(project)` — `{root}/{project}`

**`git.py`** — Thin wrappers around `git` subprocess calls:
- `_run(args, cwd)` — subprocess runner, raises `GitError` on non-zero exit
- `get_repo_root()` — `git rev-parse --show-toplevel`
- `get_project_name()` — basename of repo root
- `get_current_branch()` — `git branch --show-current`
- `get_default_branch()` — tries `symbolic-ref`, falls back to checking main/master
- `create_worktree(path, base_branch, new_branch)` — `git worktree add -b`

**`tmux.py`** — tmux session management:
- `is_tmux_installed()` / `install_tmux()` — detection and brew install
- `list_project_sessions(project)` — lists active tmux sessions for a project
- `session_name(project, dir)` — naming convention: `{project}/{dir}`
- `create_session(name, dir)` — creates detached session, sets prefix to Ctrl+A, runs `claude`
- `attach_session(name)` — prints shortcut banner, then `os.execvp` into tmux attach
- `launch_claude_in_tmux(project, path)` — orchestrates create-or-attach

**`cli.py`** — Textual TUI with async view management:
- `WorktreeApp` — main app class with CSS styling
- Views: home (unified list), create form, branch select, conflict resolution, tmux install, error
- All view transitions are `async` — `await _clear_main()` then `await mount()`
- Worktree paths stored in `_worktree_paths` dict keyed by ListItem ID (avoids monkey-patching widgets)
- `_launch_target` is set before `self.exit()`, then `main()` calls tmux after the TUI event loop ends

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
| tmux session | `{project}/{dir-name}` | `qsic-data/20260309-fix-unit-tests` |

### Key Design Decisions

- **TUI exits before tmux attach**: The Textual app must fully exit before `os.execvp` replaces the process with tmux. The app stores the target in `_launch_target` and `main()` handles the handoff.
- **Per-session tmux config**: Prefix remapped to Ctrl+A, status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched.
- **Global install via `uv tool`**: Requires `--force --reinstall` to rebuild the wheel from source. Plain `--force` reuses cached builds.

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
- Assert on app state (`_launch_target`, `_base_branch`) and DOM queries (`app.query()`)

## Linting and Type Checking

Pre-commit hooks run automatically:
- **ruff** — linting and formatting
- **ty** — type checking (strict: no `unresolved-attribute` allowed)

All widget state must use typed instance variables, not dynamic attributes on Textual widgets.
