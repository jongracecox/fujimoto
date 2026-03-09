# claude-worktree-manager

A terminal UI for creating git worktrees and launching Claude Code inside tmux sessions. Each worktree gets its own detachable tmux session, so you can run multiple Claude agents in parallel across isolated branches.

## Why

When working on multiple tasks in the same repo, git worktrees let you check out different branches simultaneously without stashing or switching. This tool automates the ceremony of creating worktrees, naming branches, and launching Claude Code — all from an interactive TUI.

## Prerequisites

- Python 3.11+
- git
- tmux (the tool will offer to install it via brew if missing)
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

```sh
uv tool install --force --reinstall /path/to/this/repo
```

This installs the `worktree` command globally. Re-run after code changes to pick up updates.

## Configuration

Set the root directory where worktrees will be created:

```sh
export CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT=~/git/worktrees/
```

Add this to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) to persist it.

Worktrees are organized as `{root}/{project-name}/{YYYYMMDD}-{slugified-title}`. For example, running from a repo called `qsic-data` with title "fix unit tests" creates:

```
~/git/worktrees/qsic-data/20260309-fix-unit-tests/
```

## Usage

Run from inside any git repository:

```sh
worktree
```

### Home Screen

The TUI shows a unified list with:

- **+ Create a new worktree** — at the top
- **Existing worktrees** — listed below with active session indicators

Active tmux sessions show a green circle indicator. Select any existing worktree to attach to its tmux session (or create a new one if none exists).

### Creating a Worktree

1. Select **+ Create a new worktree**
2. Enter a title (e.g. "fix unit tests")
3. Choose a base branch (current or default) — skipped if you're already on the default branch
4. The worktree is created and Claude Code launches in a new tmux session

If a worktree with the same name already exists, you're offered the choice to connect to it or create a new one with a numeric suffix.

### Worktree Naming

- **Directory**: `{YYYYMMDD}-{slugified-title}` (e.g. `20260309-fix-unit-tests`)
- **Git branch**: `worktree/{directory-name}` (e.g. `worktree/20260309-fix-unit-tests`)
- **tmux session**: `{project}/{directory-name}` (e.g. `qsic-data/20260309-fix-unit-tests`)

### tmux Session Controls

Each tmux session remaps the prefix key to `Ctrl+A` (instead of the default `Ctrl+B`):

| Shortcut | Action |
|----------|--------|
| `Ctrl+A D` | Detach from session (leaves it running) |
| `Ctrl+A [` | Enter scroll/copy mode |
| `Ctrl+A X` | Kill the current pane |

These bindings are set per-session and do not affect your global tmux configuration.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Select highlighted item |
| `Escape` | Go back (or quit from home screen) |
| `q` | Quit |
| Arrow keys | Navigate lists |

## Development

```sh
# Clone and sync dependencies
git clone <repo-url>
cd worktree
uv sync

# Run locally without installing
uv run worktree

# Install globally for testing
uv tool install --force --reinstall .
```

## License

MIT
