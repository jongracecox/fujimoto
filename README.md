# fujimoto

![Tests](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml/badge.svg)
![Coverage](https://raw.githubusercontent.com/jongracecox/fujimoto/badges/coverage.svg)

A terminal UI for managing Claude Code sessions across git worktrees and repositories. Spin up isolated worktree sessions or launch Claude directly in an existing repo — all from an interactive TUI with tmux-powered detachable sessions.

## Why "Fujimoto"?

Named after Fujimoto from Hayao Miyazaki's *Ponyo* — a former human turned fastidious caretaker of the sea. Fujimoto is obsessed with order and control, meticulously tending to the balance of his domain while managing his many daughters and their chaotic tendencies.

Like his namesake, this tool is a caretaker and orchestrator — keeping your worktrees organised, your sessions tracked, and your branches tidy. It manages the lifecycle from creation through to cleanup, fretting over unpushed commits and unmerged branches so you don't have to. And like Fujimoto learning to accept that Ponyo must live her own life, it knows when to let go — spinning off background Claude sessions to handle their own PRs and gracefully cleaning up when the work is done.

He carries himself with dignity, even in defeat. Your worktrees should too.

## Prerequisites

- Python 3.11+
- git
- tmux (the tool will offer to install it via brew if missing)
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

```sh
uv tool install --force --reinstall /path/to/this/repo
```

This installs the `fujimoto` command globally. Re-run after code changes to pick up updates.

## Configuration

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/   # Where worktrees are created
export FUJIMOTO_GIT_ROOT=~/git/                   # Optional: enables project switching
```

Add these to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) to persist them.

## Usage

Run from inside any git repository:

```sh
fujimoto
```

### Home Screen

```
+ New worktree session
+ New session in <project>
+ Ad hoc session
───── active sessions ─────
🟢 20260309-cleanup-ui          (worktree)
🟢 direct-1                     (direct @ main)
───── inactive worktrees ─────
⚫ 20260308-old-experiment      (worktree)
─────
  Switch project
```

### Three Session Types

**Worktree sessions** create an isolated git worktree with its own branch. Useful for standalone tasks that become PRs, or investigations where you want to fork off and explore.

**Direct sessions** launch Claude in an existing repo directory on its current branch. Quick and lightweight — no worktree overhead.

**Ad hoc sessions** launch Claude in a temporary directory outside any git project. For quick questions, investigations, and one-off tasks that don't need a repository.

### Session Actions

Select any session to see contextual options:

| Session State | Options |
|--------------|---------|
| Active worktree | Connect, Terminate, Finish |
| Inactive worktree | Launch, Finish |
| Active direct | Connect, Terminate |

### Finish Flow

When you're done with a worktree, the **Finish** flow checks the branch state and offers:

- **Push & Create PR** — pushes the branch and spins up a background Claude session to create the PR
- **Cherry-pick to base** — applies your commits back to the original branch, then cleans up
- **Discard & Delete** — throws away the work (with confirmation if there are unpushed commits)

For already-merged branches: **Delete** or **Delete + remove remote branch**.

### Naming Conventions

| Thing | Pattern | Example |
|-------|---------|---------|
| Worktree directory | `{YYYYMMDD}-{slug}` | `20260309-fix-unit-tests` |
| Git branch | `worktree/{dir-name}` | `worktree/20260309-fix-unit-tests` |
| tmux session (worktree) | `{project}/{dir-name}` | `qsic-data/20260309-fix-unit-tests` |
| tmux session (direct) | `{project}/direct-{N}` | `qsic-data/direct-1` |

### tmux Session Controls

Each session remaps the prefix key to `Ctrl+A`:

| Shortcut | Action |
|----------|--------|
| `Ctrl+A D` | Detach (leave running) |
| `Ctrl+A [` | Scroll/copy mode |
| `Ctrl+A X` | Kill pane |

These are set per-session and don't affect your global tmux config.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Select |
| `Escape` | Back (or quit from home) |
| `q` | Quit |
| Arrow keys | Navigate |

## Development

```sh
uv sync
uv run fujimoto        # Run locally
uv run pytest          # Run tests with coverage
```

## License

MIT
