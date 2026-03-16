# Contributing

## Getting Started

```sh
git clone https://github.com/jongracecox/fujimoto.git
cd fujimoto
uv sync
```

## Running Locally

You must be inside a git repository and have the environment variable set:

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/
uv run fujimoto
```

## Installing Globally

After making changes, reinstall to test the global `fujimoto` command:

```sh
uv tool install --force --reinstall .
```

Both `--force` and `--reinstall` are required — `--force` alone reuses cached wheel builds and won't pick up code changes.

## Project Layout

```
src/fujimoto/
├── cli.py        # Textual TUI app and entry point
├── config.py     # Env var loading, path construction, session metadata
├── git.py        # Git subprocess wrappers
├── tmux.py       # tmux session management
└── claude/
    ├── __init__.py      # Re-exports public API
    └── log_parser.py    # Parse Claude JSONL session logs
```

See [CLAUDE.md](CLAUDE.md) for detailed architecture documentation.

## Code Style

Pre-commit hooks enforce:

- **ruff** for linting and auto-formatting
- **ty** for type checking

Hooks run automatically on `git commit`. If a hook fails, it may auto-fix files — re-stage and commit again.

### Type Safety

The ty type checker runs in strict mode. Key rules:

- Do not set dynamic attributes on Textual widgets (e.g. `item._data = value`). Use a dictionary on the app instance instead.
- All instance variables must be declared with type annotations in `__init__`.

## Architecture Notes

### TUI View Pattern

All views follow the same async pattern:

```python
async def _show_some_view(self) -> None:
    await self._clear_main()            # Remove all children from #main
    main = self.query_one("#main")
    await main.mount(                   # Mount new widgets
        Container(...)
    )
    self.query_one("#some-widget").focus()  # Set focus
```

Every `remove_children()` and `mount()` call must be awaited to prevent DOM race conditions.

### tmux Handoff

The Textual app cannot run simultaneously with tmux attach (both need the terminal). The pattern is:

1. TUI sets `self._launch_target = (project, path, tmux_name)`
2. TUI calls `self.exit()` to cleanly shut down the event loop
3. `main()` reads `_launch_target` after `app.run()` returns
4. `launch_claude_in_tmux()` creates or attaches to the tmux session

### Adding a New View

1. Add an `async def _show_*` method following the pattern above
2. Add a `@on(ListView.Selected, "#your-list-id")` handler
3. Wire navigation from an existing view
4. Add any new state to `__init__` with type annotations

### Adding New Git/tmux Operations

- Git wrappers go in `git.py` using the `_run()` helper
- tmux operations go in `tmux.py` using `subprocess.run`
- Both should raise their respective error types (`GitError`, `TmuxError`)
- Import and use them in `cli.py`

### Claude Log Integration

The `claude/` subpackage parses Claude Code's JSONL session logs (`~/.claude/projects/`). Not yet wired into the TUI — currently a standalone module with its own test suite.

- Add new entry types or stop reasons to the StrEnums in `log_parser.py` — unknown values raise `ClaudeLogError` immediately
- Path encoding matches Claude's convention: `str(path).replace("/", "-")`
- Test with `tmp_path` fixtures — never access real `~/.claude/` in tests

## Testing Manually

1. Run `fujimoto` from a git repo
2. Create a new worktree — verify the directory and git branch are created
3. Detach from tmux (`Ctrl+A D`)
4. Run `fujimoto` again — the worktree should show a green circle, direct sessions listed
5. Select an existing session — should show the actions submenu (Connect/Launch/Finish)
6. Test the Finish flow on a worktree with unpushed commits
7. Test error cases:
   - Run outside a git repo
   - Unset `FUJIMOTO_WORKTREE_ROOT`
   - Create a worktree with a name that already exists
