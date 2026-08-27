# fujimoto

[![pypi package](https://badge.fury.io/py/fujimoto.svg)](https://pypi.org/project/fujimoto)
[![release](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml)
[![GitHub last commit](https://img.shields.io/github/last-commit/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/commits/main)
[![GitHub](https://img.shields.io/github/license/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/blob/main/LICENSE)
[![downloads](https://pepy.tech/badge/fujimoto)](https://pepy.tech/project/fujimoto)
[![tests](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml)
![coverage](https://raw.githubusercontent.com/jongracecox/fujimoto/badges/coverage.svg)
[![GitHub stars](https://img.shields.io/github/stars/jongracecox/fujimoto.svg?style=social)](https://github.com/jongracecox/fujimoto/stargazers)

[![buymeacoffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/jongracecox)


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

Install the latest release from PyPI (recommended):

```sh
uv tool install fujimoto
```

Upgrade to the latest release:

```sh
uv tool upgrade fujimoto
```

### Installing from source

To pick up unreleased changes, install directly from GitHub:

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
export FUJIMOTO_WINDOW_TITLE="{git_project} - {worktree_name}"   # Terminal window title template
export FUJIMOTO_META_KEY="C-a"                                   # In-session fujimoto chord (blank to disable)
export FUJIMOTO_TMUX_PREFIX="C-b"                                # tmux prefix key (default: C-b)
export FUJIMOTO_QUICK_TERMINAL_KEY="C-\`"                        # Global quick-terminal toggle key (blank to disable)
export FUJIMOTO_LOG_DIR=~/.fujimoto/logs                         # Where --debug logs are written
```

> **Note:** As of this release, the **defaults are swapped** from earlier
> versions. The fujimoto chord is now `Ctrl-A` (was `Ctrl-F`) and the tmux
> prefix is now `Ctrl-B` (was `Ctrl-A`). To restore the previous layout:
>
> ```sh
> export FUJIMOTO_META_KEY="C-f"
> export FUJIMOTO_TMUX_PREFIX="C-a"
> ```

If `FUJIMOTO_WORKTREE_ROOT` is unset, worktrees are created inside the current
project at `<repo>/.fujimoto/worktrees/` (the `.fujimoto/` directory is
auto-gitignored). If `FUJIMOTO_GIT_ROOT` is unset, the project switcher is
hidden. Add these to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) to
persist them.

### Per-project setup (`.fujimoto.yaml`)

Worktrees start as clean checkouts, so untracked local files (e.g. `.env`) and
per-worktree setup aren't carried over automatically. Drop an optional
`.fujimoto.yaml` in your **main clone** to declare what fujimoto should do when
it creates a worktree — and, optionally, every time you launch a session in one.

fujimoto reads the file from the main clone (not from the worktree), so it's
typically a **local, uncommitted file** — add it to `.git/info/exclude` or
`.gitignore`, since it often references local-only files like `.env`.

Scaffold a fully commented starter file with:

```sh
fujimoto --create-config      # writes .fujimoto.yaml to the main clone's root
```

All three sections are optional:

```yaml
# Copy files from the main repo into the new worktree.
copy:
  - .env                       # string form -> copied once, at creation
  - path: config/secrets.json
    when: always               # re-copied on every launch (keeps it in sync)
  - "certs/*.pem"              # glob patterns are supported

# Link files instead of copying (hard link by default; falls back to a copy
# with a warning if the worktree is on a different filesystem).
link:
  - path: shared/model.bin
    type: symbolic             # hard (default) | symbolic

# What to do if an init command fails (without continue_on_error):
#   abort (default) returns to the menu; continue launches the session anyway.
on_error: abort

# Run setup commands in the worktree after files are placed.
init:
  - uv sync                    # e.g. create the worktree's virtualenv
  - run: ./scripts/dev-setup.sh {{ worktree_dir }}
    when: always
    continue_on_error: true
    cwd: "{{ source_dir }}"    # optional; defaults to the worktree root
```

Notes:

- **`when`** controls timing: `once` (default) runs only the first time the
  worktree is created; `always` runs on every connection — creating it,
  reconnecting to a running session, and relaunching/resuming an existing one.
- **Paths** are relative to the repo root; the destination mirrors the same
  relative path inside the worktree.
- **`init` commands** run (before the session is attached) via `sh -x`, so each
  command and its output is shown, stopping at the first failure unless
  `continue_on_error: true`. `{{ source_dir }}` and `{{ worktree_dir }}` expand
  to the main repo and worktree paths. On failure you're prompted to acknowledge
  the error before the screen is handed to the session.
- fujimoto itself never creates a `.venv` — that's `uv` (or your tooling)
  running inside the worktree. Use an `init: [uv sync]` entry to set one up.

### Window title template

When a Claude session is attached, fujimoto sets the terminal window title to
`🧙🏽‍♂️ fujimoto - <rendered template>`. The `🧙🏽‍♂️ fujimoto` prefix is always
present; `FUJIMOTO_WINDOW_TITLE` controls the suffix. Default:
`{git_project} - {worktree_name}`. Set it to an empty string to suppress the
suffix entirely.

Supported placeholders:

| Placeholder | Value |
|---|---|
| `{git_project}` | Project (repo) name |
| `{worktree_name}` | Worktree directory basename |
| `{worktree_path}` | Absolute path to the working directory |
| `{git_project_dir}` | Absolute path to the main project directory |
| `{branch}` | Current git branch (empty outside a repo) |
| `{session_type}` | `worktree`, `direct`, or `adhoc` |
| `{tmux_name}` | tmux session name |

Unknown placeholders render as empty strings.

While the session-manager screen itself is open (before you attach a session),
the title uses the same format without the worktree portion:
`🧙🏽‍♂️ fujimoto - <project>`. It follows the current project as you switch
projects. `FUJIMOTO_WINDOW_TITLE` does not affect this screen.

### In-session shortcuts

When attached to a fujimoto-managed tmux session, press `Ctrl-A` (configurable
via `FUJIMOTO_META_KEY`) to enter a one-shot "fujimoto mode", then:

| Key | Action |
|---|---|
| `t` | Split a terminal pane below claude (30% height). Press again to toggle focus between claude and the terminal — only one extra pane at a time. Run `exit` in the pane to close it. |
| `T` | Open a full-height side pane on the right. Same behavior as `t`: press again to toggle focus. |
| `v` | Open VS Code at the session's working directory. |
| `w` | Open a native terminal window at the session's working directory. |
| `f` | Fork this session — detaches back to the fujimoto TUI and opens the **Fork session** flow for this worktree, so you get the usual name prompt, base-branch menu and conversation picker. |
| `s` | Stop this session — closes claude but keeps the session in your list as 🟠, ready to resume. No prompt. |
| `d` | Detach the tmux session (returns you to the fujimoto TUI). |
| `x` | End this session. With a split open it kills just that pane (with confirmation), as before. With claude alone in the window it detaches to the fujimoto TUI and asks whether to **terminate** (default) or merely **stop** it. |
| `[` | Enter copy mode (scrollback / selection). |
| `?` | Flash the binding cheatsheet in the status bar. |

Set `FUJIMOTO_META_KEY=""` to disable the chord entirely, or to an alternative
tmux key spec (e.g. `M-f`, `C-Space`) to remap. `FUJIMOTO_META_KEY` and
`FUJIMOTO_TMUX_PREFIX` must differ — fujimoto will refuse to start a session
otherwise.

### Quick terminal shortcut (global)

Fujimoto can install a **one-press** global tmux binding that toggles a 30%
bottom terminal pane: press `` Ctrl-` `` once to open the pane in the current
working directory; press again to cycle focus between the panes. The first
time you launch fujimoto you'll be asked whether to enable it — the choice is
remembered in `~/.cache/fujimoto/settings.json` and a toggle on the home
screen lets you flip it later.

This is a tmux *root-table* binding (`bind-key -n`), which means it's
**server-global** — installing it affects every tmux session on the machine,
not just the ones fujimoto created. Re-applied on every fujimoto session
create so it survives a `tmux kill-server`.

Override the key with `FUJIMOTO_QUICK_TERMINAL_KEY` (defaults to `` C-` ``).
Set it to an empty string to disable the feature regardless of the saved
preference; the home-screen toggle then shows `disabled (env)`.

**Removing the binding.** Because it lives on the tmux server, deleting
`~/.cache/fujimoto/settings.json` does *not* clear an already-installed
binding. To remove it:

- Toggle it off from the fujimoto home screen (preferred — runs
  `tmux unbind-key -n` for you), **or**
- Run `tmux unbind-key -n 'C-`'` (substitute your configured key) for a
  one-off removal that survives until the next fujimoto session create, **or**
- Run `tmux kill-server` to wipe all tmux state including this binding.

Note: when the saved preference is `on`, fujimoto re-applies the binding on
every session create (so it survives a `tmux kill-server`). Toggle it off
first if you want the removal to stick across launches.

> **Terminal caveat**: many terminals swallow or remap `` Ctrl-` `` (iTerm2,
> Ghostty, Alacritty and others use it for a "quake-mode" toggle, and most
> terminals don't send a distinguishable code for ``Ctrl`` plus a non-letter
> key by default). If the binding doesn't fire, either remap your terminal
> to forward `` Ctrl-` `` to tmux, or pick a different key via the env var.

### Platform support

Fujimoto runs on macOS and Linux.

- **macOS**: "Open terminal → New window" uses iTerm2 if installed, otherwise Terminal.app. tmux is auto-installable via brew.
- **Linux**: "Open terminal → New window" uses `FUJIMOTO_TERMINAL` if set, otherwise auto-detects a common terminal emulator (gnome-terminal, konsole, kitty, alacritty, wezterm, foot, xfce4-terminal, tilix, terminator, xterm). `FUJIMOTO_TERMINAL` accepts a `{dir}` placeholder for the working directory; if absent, the directory is appended as the final argument. tmux must be installed manually — fujimoto will print the right command for your distro (apt-get / dnf / pacman / zypper / apk).

"Open terminal → This window" instead pauses the TUI and launches your `$SHELL` as a child process with the working directory set to the session path. Running `exit` returns you to fujimoto, on the same session's action menu.

## Usage

Run from inside any git repository:

```sh
fujimoto
```

### Home Screen

```
🟠 Restore 2 stopped sessions
+ New worktree session
+ New session in <project>
+ Ad hoc session
───── sessions ─────
🟢 20260309-cleanup-ui          (worktree)
🟢 direct-1                     (direct @ main)
🟠 20260307-parser-rewrite      (worktree)
🟠 direct-2                     (direct @ main)
───── inactive worktrees ─────
⚫ 20260308-old-experiment      (worktree)
⚫ 20260309-cleanup-ui-alt 🍴   (worktree)
─────
  Switch project
```

Worktrees created with **Fork session** are marked with 🍴.

#### Sessions are remembered across a restart

A force restart kills every tmux session, and without help every piece of work
in flight comes back looking like an abandoned worktree. Fujimoto remembers
what you had open instead:

| | Meaning |
|---|---|
| 🟢 | Running right now. |
| 🟠 | **Stopped** — not running, but you never told fujimoto you were done with it. Resume it and carry on. |
| ⚫ | An inactive worktree: terminated through fujimoto, or never launched from it. |

The rule is simply that fujimoto is the only thing that changes a session's
status. Terminating one *through fujimoto* marks it done; anything else — a
host restart, `tmux kill-session` from another terminal, closing the window,
`exit` in the pane — leaves it stopped and resumable. There is no guessing
about why a session went away.

**Restore** appears at the top of the home screen whenever something is
stopped. It relaunches every stopped session in the project at once, each
resuming its most recent conversation, and attaches to none of them — pick the
one you want to sit in from the list.

**Stop** ends the claude process but keeps the session history: the transcript
is untouched and the conversation resumes where it left off. Any task claude
was part-way through is interrupted, so stop at a natural break.

#### Filtering sessions by name — `/`

Press `/` on the home screen to open a filter box, then type. Matching is live
and case-insensitive, against session names and branch names, and covers the
**sessions** (running and stopped), **inactive worktrees** and **previous claude
sessions** lists at once. While a filter is active the action rows
(`+ New …`, settings, switch project) are hidden so only matches remain.

| Key | While filtering |
|-----|-----------------|
| `Enter` | Apply the filter and move to the filtered list |
| `↑` / `↓` | Move the highlight without leaving the filter box |
| `Escape` | Clear the filter and return to the full list |

#### Searching inside conversations — `s`

`/` matches session *names*. Press `s` to search what was actually *said and
done* — the full transcript of every Claude session for the project, including
its worktrees. Useful for "which session was I fixing the auth timeout in?"
when you no longer remember what you called it.

Because that means reading every conversation log on disk, the scan runs in the
background in batches of ten and results appear as they are found, most recently
used session first. Typing never waits for it: the query is debounced, and
changing it cancels the scan in flight rather than queueing another.

Each result shows the session's directory, branch, age, how many times the query
appears, and up to three snippets of context with **every match highlighted** —
including matches that differ in case from what you typed. Snippets are picked
from different parts of the conversation rather than three views of the same
paragraph, and on a narrow terminal the snippet scrolls to keep the match on
screen instead of truncating it away. Select a result to get the usual actions —
Resume, Open terminal, Open in VS Code — and `Escape` or Cancel comes back to
the same results rather than throwing the scan away.

Three independent modes, all toggled live (the current settings are shown above
the results, and flipping one rescans):

| Key | Toggles | Options |
|-----|---------|---------|
| `Ctrl+R` | how the query is read | **literal** (default) — punctuation and brackets are taken at face value · **regex** — a Python regular expression |
| `Ctrl+T` | what is searched | **message text** (default) — only what you and Claude typed at each other · **raw** — the whole transcript, so tool commands, tool output, file contents and paths all match |
| `Ctrl+I` | letter case | **ignore case** (default) · **match case** — applies to literal and regex queries alike |

> `Ctrl+I` needs a terminal that supports the kitty keyboard protocol (kitty,
> WezTerm, Ghostty, foot, Alacritty, recent iTerm2). Older terminals such as
> macOS Terminal.app send Ctrl+I as a plain Tab, so the toggle won't fire there.

Message text is the default because it
is the quieter of the two — a common word won't match JSON keys, session uuids
or tool noise — and in practice the faster, since the only files it parses are
the ones that survive a single whole-file scan. Reach for raw (`Ctrl+T`) when you
want a command you ran, a filename you touched, or anything a tool printed;
the mode then sticks until you change it or restart.

| Key | While searching transcripts |
|-----|-----------------------------|
| `Enter` (in the box) | Move to the results; the scan keeps running |
| `Enter` (on a result) | Open that session's actions |
| `Ctrl+R` / `Ctrl+T` / `Ctrl+I` | Toggle regex / message-text-vs-raw / match-case |
| `Escape` | Back to the home screen (the query is kept for next time) |


### Three Session Types

**Worktree sessions** create an isolated git worktree with its own branch. Useful for standalone tasks that become PRs, or investigations where you want to fork off and explore.

**Direct sessions** launch Claude in an existing repo directory on its current branch. Quick and lightweight — no worktree overhead.

**Ad hoc sessions** launch Claude in a temporary directory outside any git project. For quick questions, investigations, and one-off tasks that don't need a repository.

### Session Actions

Select any session to see contextual options:

| Session State | Options |
|--------------|---------|
| Active worktree | Connect, Fork session, Resume previous session, View session log, Stop, Terminate, Finish |
| Stopped worktree | Resume previous session, Fork session, View session log, Launch, Terminate, Finish |
| Inactive worktree | Resume previous session, Fork session, View session log, Launch, Finish |
| Active direct | Connect, Fork session, Resume previous session, View session log, Stop, Terminate |

**Stop** keeps the session in your list as 🟠; **Terminate** marks it done and
drops it to ⚫. Both close claude — the difference is only whether fujimoto
offers it back to you.

All session types also offer **Open terminal**, **Open in VS Code** and
**Rename**.

### View Session Log

**View session log** reads a session's Claude transcript back without starting
Claude. It renders the conversation — your prompts, Claude's replies and its
thinking — in a scrollable read-only view. Tool calls and their results are
folded away so the conversation stays readable: a run of consecutive calls
collapses to a single `⚒ 10 tool calls  Bash, Read, Edit` row, which opens into
the individual calls, each still folded. Opening a call shows its arguments and
the output it produced — the result belongs to the call rather than being a row
of its own. Tab to a row and press Enter, or click it, to expand. Long tool inputs and results are clipped, and sub-agent
(sidechain) entries are omitted. Escape goes back — to the search results if
you got there from a transcript search,
otherwise to the home screen. When a session has more than one transcript, a
picker asks which to read.

### Fork Session

**Fork session** branches a conversation and the filesystem in one step: it
creates a new worktree and starts Claude there with the parent session's full
history (`claude --resume <id> --fork-session`). Use it to try a second
approach without losing the context you have built up, or without disturbing
the work already in the original worktree.

It asks for a name, then a base branch — defaulting to the **parent's branch**,
so the fork starts from the parent's commits. Choosing the parent's own base
branch instead gives you a sibling: same conversation, none of the parent's
code changes. If the directory has more than one previous Claude session, you
also pick which conversation to fork.

The fork appears in the session list as a normal worktree session marked with
🍴, and the forked Claude session is told it has moved: it knows the original
worktree's location, and that it was branched from the parent's **committed
tip** — so any uncommitted changes in the original worktree are not present.

Fork is offered for worktree and direct sessions that have at least one
previous Claude session. Requires **Claude Code 2.1.223 or newer**, which is
the first version that can resume a session id from a different directory.

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

The tmux prefix defaults to `Ctrl-B` (tmux's standard default) and is
configurable via `FUJIMOTO_TMUX_PREFIX`:

| Shortcut | Action |
|----------|--------|
| `Ctrl-B D` | Detach (leave running) |
| `Ctrl-B [` | Scroll/copy mode |
| `Ctrl-B X` | Kill pane |

The same actions are also reachable from the fujimoto chord — e.g. `Ctrl-A d`,
`Ctrl-A [`, `Ctrl-A x` — so swapping the prefix off `Ctrl-A` doesn't cost you
these bindings.

These options are set per-session and don't affect your global tmux config.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Select |
| `/` | Filter sessions by name (home screen) |
| `s` | Search inside session transcripts (home screen) |
| `Escape` | Back (or quit from home; clears an active filter first) |
| `q` | Quit |
| Arrow keys | Navigate |

## Troubleshooting (`--debug`)

If fujimoto misbehaves — a session doesn't appear, a Claude state indicator
looks wrong, a worktree launch fails — run it with `--debug` and share the log:

```sh
fujimoto --debug            # full detail
fujimoto --debug-redacted   # same, with identifying values redacted
```

A timestamped log is written to `~/.fujimoto/logs/fujimoto-<date>-<time>-<pid>.log`
(override the directory with `FUJIMOTO_LOG_DIR`) and the path is printed on
start and on exit. The log records:

- fujimoto / Python / platform versions, and the versions of `tmux`, `git`,
  `claude`, `gh`, `code`, `uv` and `brew` found on `PATH`
- every `FUJIMOTO_*`, `CLAUDE_*` and `ANTHROPIC_*` environment variable, plus
  the usual terminal/shell ones (credential-looking values are always replaced
  with `[SECRET-<length>]`, in both modes)
- every `git` command run, with exit code and output
- tmux session discovery, session creation (including the exact `claude`
  command line), attach and detach
- Claude session discovery: the encoded `~/.claude/projects` directory, how
  many JSONL logs were found, parsed or failed, and for each session its id,
  derived state, `cwd`, branch and last activity
- what the home screen resolved: worktrees, active sessions, every list item
- `.fujimoto.yaml` loading plus every copy/link/init action and warning
- launch targets, aborted launches, and any fatal error with its traceback

Polled state (Claude session discovery runs every 3 seconds) is only re-logged
when it actually changes, so a long-running session stays readable. Long
inventories are capped: the first ten worktrees and sessions are logged in full
(running sessions and newest worktrees first), then a summary counts the rest —
so on a machine with fifty worktrees the log still opens with what you did
rather than a wall of state:

```
series.summarised series=config.read_meta logged=10 not_logged=37 total=47
tui.item_summary  not_logged=15 types=worktree=15 claude_states=idle=15
```

### Redaction

`--debug-redacted` is for sharing a log without exposing project, branch or
directory names. Values are replaced with a token that preserves their
*shape* — which is often what matters, e.g. when a project name contains
characters that break a path:

```
[REDACTED-3f9a-12-CONTAINS.-]
 |         |    |  |
 |         |    |  `-- the non-alphanumeric characters the value contained
 |         |    `----- character length
 |         `---------- stable fingerprint (the same value always redacts to it)
 `-------------------- marker
```

Paths keep their separators and depth, and your home directory collapses to
`~`, so a log still shows the *shape* of where you are:

```
~/[REDACTED-9a88-3]/[REDACTED-f8ee-8]                 # ~/git/myproject
~/.fujimoto/worktrees/[REDACTED-850a-14-CONTAINS-]    # a fujimoto worktree
```

A component survives only if it cannot be a name you chose — operating-system
directories (`Users`, `usr`, `bin`, `opt`, …) and dotted config directories
(`.claude`, `.fujimoto`, `.cache`) do; ordinary words like `git`, `src` or
`logs` do not, because those are perfectly good project names. Names fujimoto
and Claude Code pick themselves stay readable when they appear where they
belong, so `~/.cache/fujimoto/sessions.json` is legible while a *repo* called
`fujimoto` is still hidden. Command lines
keep flags, subcommands and git's own ref vocabulary
(`git symbolic-ref refs/remotes/origin/HEAD` stays readable), while branch and
session names are redacted.

## Development

```sh
uv sync
uv run fujimoto        # Run locally
uv run pytest          # Run tests with coverage
```

## License

MIT
