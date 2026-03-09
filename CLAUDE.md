# claude-worktree-manager

CLI for creating git worktrees and launching Claude Code in tmux sessions.

## Run

```sh
uv run worktree
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

- `src/cli.py` — Entry point, interactive prompts, orchestration
- `src/git.py` — Git subprocess wrappers
- `src/tmux.py` — tmux session management
- `src/config.py` — Env var loading, path construction, slugify
