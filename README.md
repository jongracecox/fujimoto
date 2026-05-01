# fujimoto

[![pypi package](https://badge.fury.io/py/fujimoto.svg)](https://pypi.org/project/fujimoto)
[![release](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml)
[![GitHub last commit](https://img.shields.io/github/last-commit/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/commits/main)
[![GitHub](https://img.shields.io/github/license/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/blob/main/LICENSE)
[![downloads](https://pepy.tech/badge/fujimoto)](https://pepy.tech/project/fujimoto)
[![tests](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml)
![coverage](https://raw.githubusercontent.com/jongracecox/fujimoto/badges/coverage.svg)
[![GitHub stars](https://img.shields.io/github/stars/jongracecox/fujimoto.svg?style=social)](https://github.com/jongracecox/fujimoto/stargazers)


A terminal UI for managing Claude Code sessions across git worktrees and repositories. Spin up isolated worktree sessions or launch Claude directly in an existing repo — all from an interactive TUI with tmux-powered detachable sessions.

## Why "Fujimoto"?

![Fujimoto from Ponyo](https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgvxtZPGfaqKfU1raaHuiClWT5y4owbSl9YqZfTJNsrtIQFrskPIWvqIbbNpae0OHElC2I-8F3va46uyUNBkz9c8_vE9MwRldXxWFsKyFwVw_uFRCKGre5Oo9jwC5C9asaJm86z/s1600/004.jpeg)

Named after Fujimoto from Hayao Miyazaki's *Ponyo* — a former human turned fastidious caretaker of the sea. Fujimoto is obsessed with order and control, meticulously tending to the balance of his domain while managing his many daughters and their chaotic tendencies.

Like his namesake, this tool is a caretaker and orchestrator — keeping your worktrees organised, your sessions tracked, and your branches tidy. It manages the lifecycle from creation through to cleanup, fretting over unpushed commits and unmerged branches so you don't have to. And like Fujimoto learning to accept that Ponyo must live her own life, it knows when to let go — spinning off background Claude sessions to handle their own PRs and gracefully cleaning up when the work is done.

He carries himself with dignity, even in defeat. Your worktrees should too.

## Prerequisites

- Python 3.11+
- git
- tmux (the tool will offer to install it via brew on macOS, or print the install command for your distro on Linux)
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

Install directly from GitHub:

```sh
uv tool install git+https://github.com/jongracecox/fujimoto.git
```

Or install from a local clone:

```sh
uv tool install --force --reinstall /path/to/this/repo
```

Re-run with `--force --reinstall` after local code changes to pick up updates.

## Configuration

All environment variables are optional:

```sh
export FUJIMOTO_WORKTREE_ROOT=~/git/worktrees/   # Where worktrees are created
export FUJIMOTO_GIT_ROOT=~/git/                  # Enables project switching
export FUJIMOTO_TERMINAL="alacritty --working-directory {dir}"  # Linux-only: terminal command
```

If `FUJIMOTO_WORKTREE_ROOT` is unset, worktrees are created inside the current
project at `<repo>/.fujimoto/worktrees/` (the `.fujimoto/` directory is
auto-gitignored). If `FUJIMOTO_GIT_ROOT` is unset, the project switcher is
hidden. Add these to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) to
persist them.

### Platform support

Fujimoto runs on macOS and Linux.

- **macOS**: "Open terminal" uses iTerm2 if installed, otherwise Terminal.app. tmux is auto-installable via brew.
- **Linux**: "Open terminal" uses `FUJIMOTO_TERMINAL` if set, otherwise auto-detects a common terminal emulator (gnome-terminal, konsole, kitty, alacritty, wezterm, foot, xfce4-terminal, tilix, terminator, xterm). `FUJIMOTO_TERMINAL` accepts a `{dir}` placeholder for the working directory; if absent, the directory is appended as the final argument. tmux must be installed manually — fujimoto will print the right command for your distro (apt-get / dnf / pacman / zypper / apk).

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
