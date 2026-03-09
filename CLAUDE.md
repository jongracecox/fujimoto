# claude-worktree-manager

CLI for creating git worktrees and launching Claude Code in tmux sessions.

## Install (global CLI)

```sh
uv tool install --force --reinstall /Users/jongrace-cox/git/worktree
```

Re-run after code changes to pick up updates.

## Run

```sh
worktree
```

Must be run from inside a git repository.

## Dev Setup

```sh
uv sync
```

## Required Environment Variable

```sh
export CLAUDE_WORKTREE_MANAGER_WORKTREE_ROOT=~/git/worktrees/
```

## Requirements

- Python 3.11+
- tmux installed (`brew install tmux`)
- git

## Project Structure

- `src/claude_worktree/cli.py` — Entry point, interactive prompts, orchestration
- `src/claude_worktree/git.py` — Git subprocess wrappers
- `src/claude_worktree/tmux.py` — tmux session management
- `src/claude_worktree/config.py` — Env var loading, path construction, slugify
