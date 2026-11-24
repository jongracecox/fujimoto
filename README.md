# fujimoto

**Mission control for every Claude Code session on your machine.**

[![pypi package](https://badge.fury.io/py/fujimoto.svg)](https://pypi.org/project/fujimoto)
[![release](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/release.yml)
[![GitHub last commit](https://img.shields.io/github/last-commit/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/commits/main)
[![GitHub](https://img.shields.io/github/license/jongracecox/fujimoto.svg)](https://github.com/jongracecox/fujimoto/blob/main/LICENSE)
[![downloads](https://pepy.tech/badge/fujimoto)](https://pepy.tech/project/fujimoto)
[![tests](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml/badge.svg)](https://github.com/jongracecox/fujimoto/actions/workflows/tests.yml)
![coverage](https://raw.githubusercontent.com/jongracecox/fujimoto/badges/coverage.svg)
[![GitHub stars](https://img.shields.io/github/stars/jongracecox/fujimoto.svg?style=social)](https://github.com/jongracecox/fujimoto/stargazers)

[![buymeacoffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/jongracecox)


Running one Claude Code session is easy. Running ten is chaos. You know the
drill. You get back from lunch, open the wrong terminal twice, and finally
find Claude still waiting for you to say yes.

That's why I built Fujimoto. I can see which sessions are still working and
which ones are waiting for me. My agents can see that too. I can ask,
"Am I holding anything up?" and get an answer without interrupting any of
the sessions.

Before I head out for lunch, I can send one message to all my running Fujimoto
sessions in a project: "What do you need from me?" If one's waiting for a
decision, I can give it an answer before I leave.

If something comes up while I'm out, I can message my assistant on Slack.
As long as my Mac is awake and connected, the assistant can use the Fujimoto
CLI to find the right session, pass along my answer, and check what happened.
I can do the same from the ChatGPT desktop app without opening Claude Code.

If all I've got is an old Jira ticket number,
I'll run `fujimoto search PROJ-123 --all-projects` and read what we decided.
It doesn't matter which repo I was in or whether the session is still
running, as long as the transcript is still on disk.

I don't always have to open a terminal at all. The half-page scrolling change
in [#73](https://github.com/jongracecox/fujimoto/pull/73) started with me
describing what was annoying me in chat. A detached Claude Code session made
the change and opened the PR. I tried it without opening Claude Code.

## What you can do with it

- **Start work in seconds.** A worktree session on its own branch, a session
  in your main clone, or a chat in a scratch directory, each set up the way
  your project needs ([session types](#three-session-types),
  [`.fujimoto.yaml`](#per-project-setup-fujimotoyaml)).
- **Close the terminal, keep the session.** Sessions run in tmux, so they
  survive detaching, restarts and upgrades ([home screen](#home-screen)).
- **Find an old conversation.** Search for a ticket number or a phrase you
  remember, even if the session isn't running
  ([transcript search](#searching-transcripts-from-the-command-line)).
- **Let your agents work together.** Sessions list, read and message each
  other, and can resume or fork a finished session to ask it a question
  ([session tools for agents](#session-tools-for-agents),
  [broadcasting](#broadcasting-to-every-session)).
- **Know who needs you.** `fujimoto list --waiting` shows the sessions blocked
  on an approval, a question or your next prompt
  ([listing sessions](#listing-sessions-from-the-command-line)).
- **Drive it from any agent.** `fujimoto mcp` gives Claude Desktop, the
  ChatGPT desktop app, Codex, or any other local MCP client that supports
  stdio, the same tools: list, search and read sessions, and start and stop
  them. Messaging is opt-in: with its own trusted signing key, it can message
  the sessions fujimoto launched. `fujimoto mcp setup` checks each step of a
  secure setup and writes or prints the config
  ([MCP clients](#claude-desktop-and-other-mcp-clients--fujimoto-mcp)). Any
  agent or harness that can run a shell command can use the CLI directly:
  `fujimoto agent-instructions` is its guide
  ([session tools for agents](#session-tools-for-agents)).

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

When a newer release is out, the home screen shows a banner (`d` dismisses
it), and each new Claude session is asked to offer you the upgrade once,
using the command that matches how fujimoto was installed. You can say yes,
not now (a later session asks again), or don't ask again, which has the
agent run `fujimoto update dismiss <version>` so sessions stop asking about
that version; the next release is offered again.
`fujimoto update` shows the current and latest versions and the upgrade
command.

A running fujimoto keeps its old code until it restarts. `Ctrl-A r` (or
`fujimoto restart` inside a session) reloads it and drops you back into the
same session in the same terminal; your Claude session never stops. The agent
offers to do this after upgrading. `fujimoto restart --all`, run anywhere,
restarts every fujimoto attached to a session and lists the sessions it
skipped (ones started with `--detach` or attached with plain `tmux attach`,
where detaching would leave you at a shell). A fujimoto sitting on its home
screen loads the new version the next time you start it.

### Installing from source

To pick up unreleased changes, install directly from GitHub:

```sh
uv tool install git+https://github.com/jongracecox/fujimoto.git
```

Or install from a local clone:

```sh
uv tool install --force --reinstall --no-cache /path/to/this/repo
```

Re-run with `--force --reinstall --no-cache` after local code changes to pick
up updates. Without `--no-cache`, uv can reuse the previous build and report
success while installing nothing new.

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

### Global config and default model

Preferences that apply to every project live in
`~/.config/fujimoto/config.yaml` (`$XDG_CONFIG_HOME` moves it):

```yaml
model: opus            # default Claude model for every session
quick_terminal: true   # the global quick-terminal binding (see below)
page_step: 50%         # PageUp/PageDown step: share of the visible rows, or a row count
broadcast:             # `fujimoto broadcast` (managed by its own commands)
  enabled: true
  signers:
    - name: laptop
      public_key: ssh-ed25519 AAAA…
  signing_key: null
```

**Default model.** Claude Code remembers the last model chosen with `/model`
for *all* future sessions, so switching one session to a different model
switches every new session too. Set `model` and fujimoto passes `--model` to
every claude it launches, unless the session's own launch options already set
one. `.fujimoto.yaml` can set `model` too, and the project value beats the
global one. The default is added at launch and never saved into a session's
options, so changing it reaches every session that hasn't chosen its own
model.

### `fujimoto config`

```sh
fujimoto config                              # list every key (set or not), both files, env vars
fujimoto config list --json
fujimoto config get model                    # effective value; exit 1 when unset
fujimoto config get model --global
fujimoto config set model opus --global
fujimoto config set model sonnet --project
fujimoto config unset model --project
fujimoto config set claude_args --project -- --plugin-dir ./plugins
```

| key | scopes | value |
|---|---|---|
| `model` | global, project | model alias or id, passed as `--model` |
| `quick_terminal` | global | `true` / `false` |
| `page_step` | global | how far PageUp/PageDown move in lists: `50%` of the visible rows (default) or a row count such as `8` |
| `claude_args` | project | extra claude arguments (shell-style) |
| `on_error` | project | `abort` / `continue` |
| `copy`, `link`, `init` | project | shown, but edited in `.fujimoto.yaml` itself |

`set` and `unset` need `--global` or `--project`. Project config is the main
clone's `.fujimoto.yaml`, even when you run the command from a worktree. Only
the changed key's lines are rewritten, so comments and the rest of the file
stay as they were, and a value that would make the file invalid is refused
without touching it. Put a value that starts with `-` after `--`. An invalid
global config is reported when the TUI opens, the same way an invalid
`.fujimoto.yaml` is.

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

Every section is optional:

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

# Extra claude CLI arguments for every session in this project (a list, or a
# string split like a shell would). A session's own saved launch options win.
claude_args: [--plugin-dir, ./plugins]

# Keep this project's sessions out of `fujimoto broadcast` (on by default once
# broadcast is enabled globally).
broadcast: false
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
- **`claude_args`** applies to worktree, project-root and chat sessions alike
  (a chat in the scratch directory is outside the repo, so it doesn't see them). See [Launch options](#launch-options) for how it
  interacts with options saved per session.
- **`broadcast: false`** opts the project out of
  [broadcasts](#broadcasting-to-every-session). It can only opt out.
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
| `{session_type}` | `worktree`, `direct` (project-root), `chat`, or `adhoc` (older versions) |
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
| `r` | Restart fujimoto — reloads it (e.g. after an upgrade) and reattaches to this session in the same terminal. claude keeps running. Same as `fujimoto restart`. |
| `x` | End this session. With a split open it kills just that pane (with confirmation), as before. With claude alone in the window it detaches to the fujimoto TUI and asks whether to **terminate** (default), **park** 🅿️ or merely **stop** 🟠 it. |
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
time you launch fujimoto you'll be asked whether to enable it. The choice is
remembered as `quick_terminal` in `~/.config/fujimoto/config.yaml`, and a
toggle on the home screen (or `fujimoto config set quick_terminal off
--global`) flips it later. A choice made by an older version, in
`~/.cache/fujimoto/settings.json`, is copied over automatically the first time.

This is a tmux *root-table* binding (`bind-key -n`), which means it's
**server-global** — installing it affects every tmux session on the machine,
not just the ones fujimoto created. Re-applied on every fujimoto session
create so it survives a `tmux kill-server`.

Override the key with `FUJIMOTO_QUICK_TERMINAL_KEY` (defaults to `` C-` ``).
Set it to an empty string to disable the feature regardless of the saved
preference; the home-screen toggle then shows `disabled (env)`.

**Removing the binding.** Because it lives on the tmux server, removing the
`quick_terminal` setting does *not* clear an already-installed binding. To remove it:

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

### Launching a worktree session from the command line

`fujimoto new` skips the home screen for the first launch: it creates the
worktree and starts Claude in it straight away. Detaching returns to the home
screen as usual.

```sh
fujimoto new fix-unit-tests                    # from origin/<default branch> (fetched first)
fujimoto new fix-unit-tests --base develop     # from another branch
fujimoto new fix-unit-tests --current          # from the current branch
fujimoto new fix-unit-tests -- --model opus    # with claude launch options
fujimoto new fix-unit-tests --prompt "Fix the failing tests"   # first message
fujimoto new fix-unit-tests --detach --prompt "…"             # start it in the background
```

Arguments after `--` are passed to `claude` and saved as the session's
[launch options](#launch-options), exactly as if they had been entered in the
launch options dialog. If a worktree for the title already exists (same day,
same name), `fujimoto new` connects to it instead of creating another.

`--prompt` is sent as the first message of the new Claude session. It is not
saved, so relaunching the session later doesn't send it again. A `--prompt`
for a session that is already running is an error, since it could only reach
a new Claude process.

`--detach` starts the session without attaching and prints its details as
JSON, for scripts and other agents:

```json
{
  "tmux_session": "myproject/20261013-fix-unit-tests",
  "working_dir": "/Users/me/git/worktrees/myproject/20261013-fix-unit-tests",
  "branch": "worktree/20261013-fix-unit-tests",
  "claude_session_id": "8f0e…",
  "parent_session": null,
  "worktree_created": true,
  "session_started": true,
  "attach_command": "tmux attach-session -t myproject/20261013-fix-unit-tests"
}
```

Each new Claude process started by `fujimoto new` gets its id from fujimoto
(`claude --session-id`), so `claude_session_id` is known immediately, before
any transcript exists. Output from `.fujimoto.yaml` init commands goes to
stderr in this mode, so stdout is only the JSON.

#### Sessions that start sessions

A Claude session can use `fujimoto new` to hand work to another session, and
the two can then talk using Claude Code's own cross-session messaging
(`ListAgents` / `SendMessage`). Messages are addressed by a session's *peer
name*, not its Claude session id, so two options set that up:

```sh
fujimoto new fix-flaky-test --prompt "…" \
  --parent 20261013-my-session-9f \
  -- --name 20261013-fix-flaky-test
```

- `--parent NAME` is the starting session's peer name (`ListAgents` reports
  it as "This session is …"). It is saved in the worktree's metadata, and the
  new session's system prompt tells it that address and to use `SendMessage`
  to reach it. The parent is named again when the session is resumed, so the
  link survives a relaunch.
- `-- --name NAME` is Claude's own option for setting a session's peer name.
  Choosing it up front gives the parent an address for the child before the
  child is running. Since arguments after `--` are saved as launch options,
  the name stays the same across relaunches.

### Starting a chat from the command line

```sh
fujimoto chat [TITLE] [--in WORKTREE | --in-scratch] [--project NAME|PATH] \
  [--prompt TEXT] [--parent NAME] [--detach] [-- claude args…]
```

`fujimoto chat` starts a chat session — for questions and conversation rather
than work — without creating a worktree. It runs in the project root unless
`--in` names a worktree (any unique part of its name) or `--in-scratch` asks for
a scratch directory outside the repo (see below). Without a title it is named `chat-1`,
`chat-2`, …. `--project` picks another repo (a path, or a name under
`$FUJIMOTO_GIT_ROOT`); with `--detach` that works from anywhere, inside a repo
or not.

`--prompt`, `--parent` and `--detach` work as they do for `fujimoto new`. With
`--detach` the JSON names the session's `peer_name` — `<project>-<chat name>`
unless `-- --name` sets one — so the session that started it can
`SendMessage` it straight away. Stop it with `fujimoto stop <tmux session>`.

**Scratch chats and folder trust.** Scratch chats run in one directory per
project, `~/.fujimoto/chats/<project>/`, and each keeps any files it writes in
its own sub-directory (`…/<project>/<chat name>/`) — its system prompt says
so. Claude Code asks you to trust a folder the first time it starts outside a
trusted repository, and there is no command to pre-trust one, so the *first*
scratch chat for a project stops at that prompt. Accept it once and later
scratch chats start straight away. (`--in-scratch` was called `--in-temp`
while this was a fresh temporary directory each time; the old name still
works.) With `--detach` nobody is there to answer, so the
JSON reports `"needs_trust": true` (read from Claude Code's `~/.claude.json`;
fujimoto never writes it) and stderr says which session to attach to.

### Session tools for agents

`fujimoto agent-instructions` prints a short guide to the command-line tools
below (`list`, `search`, `log`, `resume`, `stop`, `new`), written for an AI agent:
what each does, and when it helps — tracing work on a ticket or error,
avoiding duplicate work, recovering context, checking that something is really
done, delegating. Point an agent at it from its instructions file, e.g. a line
in `CLAUDE.md`:

```markdown
To search, read or talk to other Claude sessions, run `fujimoto agent-instructions`.
```

### Listing sessions from the command line

`fujimoto list` shows sessions newest first: where each one ran, when it was
last used, its id (the first 8 characters, which `log` and `resume` accept),
whether it is running (with the peer name to message it at), and its title.
Like `search`, it needs no tmux and changes nothing on disk.

```sh
fujimoto list                       # current project + its worktrees
fujimoto list --all-projects        # every session on the machine
fujimoto list --project myproject   # a project under $FUJIMOTO_GIT_ROOT (or a path)
fujimoto list --running             # only sessions running now
fujimoto list --waiting             # running sessions waiting on you (approval, question, idle prompt)
fujimoto list --state waiting_for_user  # by the transcript's last entry
fujimoto list 8f0e2c1a my-peer      # just these sessions: id, id prefix or peer name
fujimoto list -n 0 --json           # all of them, as JSON
fujimoto list --running --json --fields active.peer_name,active.waiting
fujimoto list --running --json --fields active.listening   # which ones a signed message can reach
```

`--fields` (with `--json`) keeps only the named keys of each session, plus
`session_id`, which is always included, so an agent polling for status
doesn't pay for the whole record. `active.<key>`
selects inside the `active` object. A running session's `active.waiting`
says why it is blocked on you (`permission`, `question`, `dialog`, `other`),
taken from Claude Code's own session registry, and `active.waiting_for` gives
Claude's own wording. `active.listening` says whether a signed message
(`fujimoto send`) can reach it now, i.e. it has a live broadcast waiter; text
rows end in `listening` when it does. A stopped session's `active` is still
just `{"running": false}`.

```
● 8f0e2c1a  myproject/20261013-fix-flaky-test         2h ago  busy · peer 20261013-fix-flaky-test · listening
    Fix the flaky test in test_api.py
○ 1c9d77b0  other-project/20261001-old-work (deleted)  3w ago
    Investigate CI timeouts

2 sessions · all projects · 1 running
Next: fujimoto list <id> --json --fields … · fujimoto log <id> --summary · fujimoto resume <id> (a peer name works too)
```

Run outside any git repo, `list` and `search` cover every project, since
there is no current one. A worktree that has been deleted is still named from
its path (`$FUJIMOTO_WORKTREE_ROOT/<project>/<worktree>`, or
`<project>/.fujimoto/worktrees/<worktree>`), and marked `(deleted)`.

### Searching transcripts from the command line

`fujimoto search` is the home screen's transcript search (`s`) without the
TUI, for scripts, agents and people at a prompt. It needs neither tmux nor a
running fujimoto, and changes nothing on disk.

```sh
fujimoto search "flaky test"                     # current project + its worktrees
fujimoto search "flaky test" --project myproject # a project under $FUJIMOTO_GIT_ROOT (or a path)
fujimoto search "flaky test" --all-projects      # every transcript on the machine (the default outside a repo)
fujimoto search 'retry(ing)? \d+' --regex        # regex; -s for case-sensitive
fujimoto search "pytest -x" --raw                # also match tool calls and output
fujimoto search "flaky test" --limit 5 --json    # JSON, at most 5 sessions
fujimoto search ABC-123 --sort relevance -n 5     # the 5 sessions that discuss it most
```

A project search includes sessions from worktrees that have since been
deleted (their transcripts outlive them); those show `(deleted)` next to the
directory, and `"cwd_exists": false` in JSON. The TUI's `s` search does not
list them, since a deleted worktree can't be resumed in place.

By default only what the user and Claude *said* is matched. Text the harness
adds to a message (system reminders, which carry `CLAUDE.md`, and task
notifications) doesn't count, since every session has it. `--raw` also
matches tool calls, tool output and file contents. Results are newest first,
and the scan stops once `--limit` sessions (default 20, `0` for no limit) have
matched, so a search over a large history stays fast:

```
● myproject/20261013-fix-flaky-test  (3 matches; 2h ago)
  session:  8f0e2c1a-…
  cwd:      /Users/me/git/worktrees/myproject/20261013-fix-flaky-test
  branch:   worktree/20261013-fix-flaky-test
  title:    Fix the flaky test in test_api.py
  active:   busy · peer 20261013-fix-flaky-test · tmux myproject/20261013-fix-flaky-test · pid 4242
    …the flaky test fails about one run in five because…

○ myproject  (1 match; 3d ago)
  session:  1c9d77b0-…
  cwd:      /Users/me/git/myproject
  branch:   main
  title:    Investigate CI timeouts
  active:   no
    …could be the same flaky test, it only fails on…

2 matching sessions · scanned 41/41 transcripts · project myproject · message text
Next: fujimoto log <session> --summary, or --grep PATTERN to find the passage
```

`●` marks a session that is running right now. That comes from Claude Code's
own registry of running processes (`~/.claude/sessions/`), which names the
exact conversation each process is writing, its peer name (the address
`SendMessage` takes) and whether it is busy. For a Claude Code too old to keep
that registry, fujimoto falls back to its own tmux sessions. `--json` carries
the same fields, plus the transcript path and match offsets inside each
snippet.

With `--raw`, each hit says where its matches were. `74 matches: 9 message,
46 tool, 19 other` means the session *talked about* the query nine times, and
the rest were in tool calls, tool output and JSON metadata. `0 message` usually
means the session only saw the query in a listing (a Jira search, a log) and
never worked on it. Snippets come from the readable text, message text first,
not from the escaped JSON the transcript stores.

**Which mode for a ticket key?** The default (message text) is usually the
right one. A key such as `ABC-123` turns up in tool output for every session
that ran a Jira search, while a session that actually *worked* on the ticket
names it again and again in what was said. The default never sees tool
output, so listing-only sessions don't match at all. Use `--raw` when you also
want those; the breakdown then shows which hits are which.

`--sort relevance` puts the sessions with the most message-text matches first
(then tool matches, then the most recent), so the top result is the likeliest
candidate. It never ranks on the raw count, which one long listing can
inflate. It has to scan every transcript before choosing the top `--limit`,
so it is slower than the default newest-first order, which stops as soon as
`--limit` sessions have matched.

When run from inside a Claude session, `search` leaves that session's own
transcript out (it reads `$CLAUDE_CODE_SESSION_ID`). Otherwise an agent
searching for what it was just asked about always finds itself first. Pass
`--include-self` to keep it.

`fujimoto log` prints one session's transcript. It accepts a session id, any
unique prefix of one, or a `.jsonl` path:

```sh
fujimoto log 8f0e2c1a --summary               # how it started, how it ended, its links
fujimoto log 8f0e2c1a --grep ABC-123 -C 1      # messages mentioning ABC-123, ±1 message
fujimoto log 8f0e2c1a --head 10               # first 10 messages (or --tail 10)
fujimoto log 8f0e2c1a --roles user,assistant  # just the conversation, no tool calls
fujimoto log 8f0e2c1a                         # whole transcript
fujimoto log 8f0e2c1a --json
fujimoto log 8f0e2c1a --summary --json --fields summary.last_assistant.text,summary.links
```

`--summary` prints a session's first prompt and its final exchange: the
reply you last answered, your answer, and Claude's last message. It also lists
the GitHub PRs, Jira issues and Slack messages the session mentioned, with the
ids another tool needs (repo and number, issue key, Slack channel and message
timestamp). Links count when they appear in a message or a tool call. Links in
tool *output* count only for PRs, where a session's own `gh pr create` reports
its PR. Any other link in tool output is usually a listing.

`--grep` takes `-e` (regex), `-s` (case-sensitive) and `-C N` (context
messages either side). `⋯` marks where messages were skipped. Every message
heading carries its index in the transcript (`### assistant · #148 · …`), so a
passage found with `--grep` can be found again in another view.

Roles are `user`, `assistant`, `thinking`, `tool_use`, `tool_result` and
`injected`. Injected messages are what the harness records as a "user"
message but nobody typed: background-task notifications, system reminders,
slash-command output. They are hidden unless asked for with `--roles`. Long
tool inputs and outputs are clipped, as in the TUI's log viewer. If fujimoto
can't parse a transcript (Claude Code's log format changes over time), it
prints the raw JSONL lines with a warning instead of failing. Both commands
exit 0 when nothing matches and 1 on an error.

### Resuming a session from the command line

`fujimoto resume` picks a session up by id (any unique prefix, as for `log`),
in the directory it ran in. It is how another agent can ask a finished session
about its own work, in a real back-and-forth rather than a single question:

```sh
fujimoto resume 8f0e2c1a                      # resume and attach, like the TUI
fujimoto resume 8f0e2c1a --detach \
  --parent 20261020-my-session-b5 \
  --prompt "Was this merged? Reply with SendMessage."
fujimoto resume 8f0e2c1a --fork --detach      # ask in a copy, leave the original as it was
fujimoto stop myproject/20261013-fix-flaky-test
```

`--detach` starts it in the background, waits up to `--wait` seconds (default
15) for Claude Code to register the process, and prints JSON:

```json
{
  "tmux_session": "myproject/20261013-fix-flaky-test",
  "working_dir": "/Users/me/git/worktrees/myproject/20261013-fix-flaky-test",
  "branch": "worktree/20261013-fix-flaky-test",
  "resumed_session_id": "8f0e2c1a-…",
  "claude_session_id": "8f0e2c1a-…",
  "forked": false,
  "peer_name": "20261013-fix-flaky-test-8f0e2c1a",
  "parent_session": "20261020-my-session-b5",
  "pid": 4242,
  "status": "busy",
  "registered": true,
  "session_started": true,
  "attach_command": "tmux attach-session -t myproject/20261013-fix-flaky-test",
  "stop_command": "fujimoto stop myproject/20261013-fix-flaky-test"
}
```

- **Peer name.** fujimoto names the process `<worktree>-<id prefix>` (a fork:
  `<worktree>-fork-<random>`) so the caller knows where to `SendMessage`
  before it has even registered. A `--name` in the session's launch options,
  or after `--`, wins. The automatic name is not saved as a launch option.
- **`--parent`** tells the resumed session which session to reply to, as with
  `fujimoto new`.
- **Resume or fork.** By default the questions and answers join the session's
  own history. `--fork` continues in a new conversation (`--fork-session`) in
  the same directory, so the original transcript is untouched, and
  `claude_session_id` is the fork's new id.
- **Never twice.** A session that is already running is connected to (or,
  with `--detach`, just reported with its peer name). `--prompt` is an error
  then: message it instead. `--fork` still starts a separate fork.
- **Deleted directory.** A session whose worktree is gone is refused, with
  the branch it ran on.
- **Stopped sessions come back as themselves.** If a stopped (or parked)
  session's record still names the conversation — a chat, typically — it
  resumes under its own name and type rather than as a new `session-N`.
- **`fujimoto stop`** takes a tmux session name or a Claude session id. It
  stops the process the way `Ctrl-A s` does: the session stays on the home
  screen as stopped, ready to resume.

A resumed session runs with its usual launch options and nobody watching. If
it stops for a permission prompt, it waits until someone attaches.

### Broadcasting to every session

`fujimoto broadcast send` delivers one message to every running fujimoto
session, or to every session of one project, and each Claude acts on it at
its next safe stopping point. For example, before shutting down for the
night:

```sh
fujimoto broadcast send --all-projects \
  "Send me a summary of this session's state and any outstanding actions or decisions in Slack."
```

```text
Broadcast 20261120T220102-3f9a1c2e to all projects, signed by laptop, expires 23:01.
Delivered to 3 of 3 listening sessions:
  ✓ myproject  8f0e2c1a  ~/git/worktrees/myproject/20261120-fix-flaky-test
  ✓ myproject  2b77d0e4  ~/git/worktrees/myproject/20261119-new-api
  ✓ website    c41a9e07  ~/git/website
```

Each session receives:

> Broadcast from the user (via fujimoto, signed by laptop, id
> 20261120T220102-3f9a1c2e). Deal with this at the next safe stopping point in
> your current work.
>
> Send me a summary of this session's state and any outstanding actions or
> decisions in Slack.

- **Idle** sessions wake up and act straight away.
- **Busy** sessions pick it up when the current step (a command, an edit)
  finishes, not at the end of the whole task. They then carry on.
- A session **waiting on a permission prompt** keeps the message queued; the
  prompt is never answered for you. It is handled after you respond.
- A broadcast **never bypasses permissions**: a tool call it leads to is
  approved (or auto-approved) exactly as it would be for anything else.

#### Setting it up

Broadcast is off until you enable it and trust at least one key. Messages
are signed with an SSH key and checked against the keys you list, so writing
a file into the mailbox is not enough to send one (see
[Security](#security-model) below).

1. **Pick a signing key.** Any SSH key works. fujimoto only ever stores the
   *public* half and signs with `ssh-keygen -Y sign` (OpenSSH 8.1+), so the
   private key can live wherever you keep keys: a password manager's SSH
   agent (e.g. 1Password), the system `ssh-agent`, a hardware key, or a key
   file. A dedicated key is cleanest:

   ```sh
   ssh-keygen -t ed25519 -C "fujimoto broadcast" -f ~/.ssh/fujimoto_broadcast
   ```

   Or create the key in your password manager and use its public key.

2. **Trust it** as a `user` signer — your own key, so sessions read its
   messages as "from the user … signed by NAME":

   ```sh
   fujimoto broadcast add-signer laptop ~/.ssh/fujimoto_broadcast.pub --role user
   # or a literal key: fujimoto broadcast add-signer laptop "ssh-ed25519 AAAA…" --role user
   ```

   A signer is an `agent` by default — for an agent acting on your behalf
   (see [Agents as senders](#agents-as-senders)). `fujimoto broadcast set-role
   NAME user|agent` changes it later, for sessions launched afterwards.

3. **Say which key `send` signs with** — optional. Without this, fujimoto
   uses the first key in your SSH agent that is a trusted signer, which is
   what you want with a password-manager agent.

   ```sh
   fujimoto broadcast set-key ~/.ssh/fujimoto_broadcast   # a private key file
   fujimoto broadcast set-key ~/.ssh/fujimoto_broadcast.pub  # sign via the agent
   ```

4. **Enable it:**

   ```sh
   fujimoto broadcast enable
   ```

5. **Relaunch your sessions.** Only sessions *launched after* step 4 listen;
   running ones keep the configuration they started with. Stop and resume them
   (`Ctrl-A s`, then Enter on the row).

6. **Check:**

   ```sh
   fujimoto broadcast show                  # config + which sessions are listening
   fujimoto broadcast send "Reply with OK." # this project only, as a test
   fujimoto broadcast status                # recent broadcasts and receipts
   ```

Everything lives under `broadcast:` in the global config
(`~/.config/fujimoto/config.yaml`, see [Global config](#global-config-and-default-model));
the commands edit only that key and keep the rest of the file, comments
included.
`fujimoto broadcast disable` and `remove-signer NAME` undo it (again for
sessions launched afterwards).

#### Sending

```sh
fujimoto broadcast send "…"                    # sessions of the current project
fujimoto broadcast send "…" --project website  # one named project
fujimoto broadcast send "…" --all-projects     # every listening session
fujimoto broadcast send "…" --ttl 4h           # valid for 4h (default 1h, max 24h)
fujimoto broadcast status [ID]                 # who received what
```

`send` waits up to `--wait` seconds (default 5) for receipts and lists them;
`--json` gives the same as JSON. A session that was not listening yet simply
does not receive it — broadcasts are not replayed to sessions started later.
A message that expires before a session sees it is rejected (and shows as
`✗ … (expired)` in `status`), so a late-night message is not acted on the
next morning.

#### Messaging one session — `fujimoto send`

```sh
fujimoto send myproject/20261120-fix-flaky-test "Reply with your status."
fujimoto send 8f0e2c1a "…" --ttl 30m --json
```

The same signed delivery to a single session; it arrives as *"Message from
the user (via fujimoto, signed by …)"*. `SESSION` is the session's tmux name,
its peer name or a prefix of its Claude session id — `fujimoto broadcast show`
lists all three for every listening session. It is resolved when you send (it
must match exactly one listening session) and the message is signed for that
session's exact id, so it can never reach a different session that later takes
the same name. It uses the broadcast configuration and signing key, and takes
the same `--ttl`, `--key`, `--wait` and `--json`.

#### Reading the log — `b`

Press `b` on the home screen for the broadcasts and messages of the last week,
newest first: when, to whom, who signed it, how many sessions received or
rejected it, and the first line. Enter opens one — the full text and a receipt
per session (with the reason for any rejection); Escape goes back.
`fujimoto broadcast status [ID]` is the command-line version. Messages and
receipts are kept for seven days after they expire.

#### Sending from somewhere else

A broadcast can be signed anywhere and handed to fujimoto on this machine,
for example by an always-on agent with its own key. Trust that key with
`add-signer`, then on the sending side write the payload and sign it in the
`fujimoto-broadcast` namespace:

```sh
cat > msg.json <<'EOF'
{"v":1,"id":"agent-20261120-2200","project":null,
 "created":"2026-11-20T22:00:00+00:00","expires":"2026-11-20T23:00:00+00:00",
 "message":"Send me a summary of this session's state in Slack."}
EOF
ssh-keygen -Y sign -f agent_key -n fujimoto-broadcast msg.json   # writes msg.json.sig
```

and get both files onto this machine and post them:

```sh
fujimoto broadcast send --signed msg.json   # reads msg.json.sig alongside
```

`project` is a project name or `null` for all; `id` is letters, digits and
`-`; the lifetime is at most 24h. An agent signer may add
`"approval": "what the user approved"` (see [Agents as senders](#agents-as-senders)). The bytes are verified exactly as signed.
To target one session, use `"v":2` and add `"session": "<full Claude session
id>"` (with `"project": null`) — version 2 makes older fujimoto builds reject
the message rather than deliver it to everyone; `fujimoto broadcast show --json` lists the listening
sessions' ids alongside their tmux and peer names.

#### Agents as senders

An agent with its own trusted key (`add-signer NAME KEY`, role `agent`) can
send too. Its messages never read as yours: a session sees *"Message from
NAME, an agent with a trusted key (via fujimoto, id …)"*.

- **Relayed approval.** When you have explicitly approved a specific action
  (say, by voice to the agent), the agent sends it to one session with
  `fujimoto send SESSION "…" --approval "the exact action"` (or an
  `"approval"` field in a signed payload). The session sees *"Relayed approval
  from NAME … the user approved “TEXT”. Before acting on it, run `fujimoto
  broadcast verify ID`."* An approval must target one session; a broadcast
  carrying one never passes verification.
- **`fujimoto broadcast verify ID`** prints PASS only if the message is in the
  mailbox, signed by a trusted key, not expired, carries an approval, and is
  addressed to the session running the check (`$CLAUDE_CODE_SESSION_ID`, or
  `--session`). Otherwise it prints FAIL with the reasons and exits 1. It reads
  the mailbox, not the text the session was shown, so a preamble copied into a
  PR comment or tool output can't pass. Whether a PASS counts as your go-ahead
  is your rule to set (see below). Nothing about it bypasses Claude Code's
  permission prompts. Ordinary requests (a status check) carry no approval.
- **Making a PASS count.** Sessions follow your instructions, not fujimoto's,
  on what counts as your approval. To let a verified relayed approval stand in
  for your "go ahead", add a rule like this to your global `~/.claude/CLAUDE.md`:

  ```markdown
  - A relayed approval from my agent counts as my approval only when the
    session runs `fujimoto broadcast verify <id>` and it prints PASS, and only
    for exactly the action it quotes. Otherwise ask me directly. It never
    bypasses Claude Code's permission prompts.
  ```

  Keep the agent's private key off this machine (an agent that signs
  elsewhere and posts with `send --signed`). If a process here can read it,
  it can forge approvals.
- **Which path.** An agent running on this Mac with its key available uses
  `fujimoto send SESSION "…" --key KEY` (or `fujimoto broadcast send … --key
  KEY`). One that signs elsewhere uses the signed-payload route below.
- **Prefer one session.** Default to `fujimoto send` to a single session.
  Before a broadcast, list the listeners (`fujimoto broadcast show`) and
  confirm the intended project.
- **Delivery and reply are separate.** `fujimoto broadcast status ID` shows
  delivery receipts (who received it, who rejected it and why). A reply is in
  the session's transcript:

  ```sh
  fujimoto log SESSION --json                          # note "cursor"
  fujimoto send SESSION "Reply with your status."      # prints the message id
  fujimoto log SESSION --reply-to ID --wait 60 --json  # what answered that message
  fujimoto log SESSION --after CURSOR --wait 15 --json # or: everything new since
  ```

  `--reply-to` finds the exact delivery by the id in its preamble and returns
  what followed it, up to the session's next user or injected message.
  `--after` returns only messages past a saved cursor; `--wait` re-reads for
  up to that many seconds until something arrives.

- **`fujimoto broadcast follow ID [--wait 30] [--json]`** puts it together
  for a message to one session, keeping four answers apart: **delivery**
  (the receipt, and whether the target is listening now), **approval** (for
  an `--approval` message: whether it *would* verify for the target now, and,
  separately, whether the target's transcript shows it running `fujimoto
  broadcast verify ID` and getting PASS; only the latter is evidence the
  recipient checked), **reply** (the text after the delivery, plus a `cursor`
  for `log --after`), and the **target**'s current activity. It waits up to
  `--wait` seconds and stops early when the reply is final (the target went
  idle), the message was rejected or expired undelivered, the target is
  waiting on you (`blocked`), or it stopped before the message arrived. The
  `outcome` field says which; otherwise it is `timeout`. Read-only.

- **After upgrading fujimoto, there's no need to restart sessions straight
  away.** Signed messages keep arriving, and `log --after` reads the replies.
  Two things catch up on their own terms:
  - The message id that `--reply-to` looks for is added by the waiter, which
    re-arms from the installed code. A session's first delivery after an
    upgrade may lack it; later ones have it.
  - Signer roles are fixed when a session launches. Until it is relaunched,
    it words every message, an agent's included, as "from the user".

#### Opting a project out

Add `broadcast: false` to a project's `.fujimoto.yaml`. A project can only opt
*out*: whether broadcast is on, and which keys are trusted, are global settings
— otherwise a repository could ship a config that trusts its own key.

#### Security model

- **What it protects against.** Anything running as you — including another
  Claude session that has been prompt-injected — can write files in the
  mailbox (`~/.cache/fujimoto/broadcast/`). It cannot make a session act on
  them: each session checks the signature against the keys it was launched
  with, and drops anything else (`fujimoto broadcast status` shows the
  rejection). The signer's name comes from the signature, not from the
  message, so a message cannot claim to be from someone else.
- **Fixed at launch.** A session's trusted keys are written into its hook
  command when it starts, so editing settings afterwards does not change who
  a running session trusts.
- **Bounded replay.** Every message carries its scope and an expiry (24h at
  most) inside the signed part; a session delivers each message once.
- **What it does not protect against.** Anything running as you can still
  change fujimoto's settings for *future* launches (e.g. `add-signer`), or
  add hooks of its own to Claude Code's settings — broadcast does not create
  that ability and cannot remove it. And a signature is only as strong as the
  key's custody: if your SSH agent remembers an approval (1Password did in
  testing: a second signature needed no fingerprint), a process running in the same session can sign until the
  approval lapses. A hardware key that asks for a touch on every use closes
  that gap.

#### How it works

Each fujimoto-launched session gets two Claude Code hooks (via `claude
--settings`, merged into any `--settings` already in its launch options):
`SessionStart` and `Stop`, both `async` with `asyncRewake`. The hook runs a
small waiter that sleeps until a new message appears in the mailbox,
verifies it, and exits with code 2, which Claude Code documents as waking the
session even when it is idle; the text becomes a notice in the conversation.
The `Stop` hook re-arms a waiter after every turn, and a lock keeps it to one
per Claude process. Nothing types into the terminal, and nothing talks to
Claude Code's internal messaging sockets.

### Claude Desktop and other MCP clients — `fujimoto mcp`

`fujimoto mcp` is a local MCP server (stdio) that gives Claude Desktop, the
ChatGPT desktop app, Codex, or any local MCP client, the session tools: list, search and read sessions, start, resume
and stop them, and message them. It needs the `mcp` extra:

```sh
uv tool install --force 'fujimoto[mcp]'
```

| Tool | Does | CLI twin |
|---|---|---|
| `list_sessions` | Sessions, newest first; `running_only`, `waiting` (needs you), `states`, `sessions` (by id, prefix or peer name), `fields` | `fujimoto list` |
| `search_sessions` | Full-text transcript search | `fujimoto search` |
| `read_transcript` | One transcript (by id, prefix or peer name): `summary`, `grep`, `tail`, `after` cursor, `reply_to` + `wait`, `fields` | `fujimoto log` |
| `listening_sessions` | Sessions that can receive a message now, and the server's fujimoto `version` | `fujimoto broadcast show` (`fujimoto --version`) |
| `message_status` | Recent messages and their delivery receipts | `fujimoto broadcast status` |
| `follow_message` | One message's delivery, approval check, reply and target activity, with a bounded `wait` | `fujimoto broadcast follow` |
| `start_session` | New worktree + session in a project | `fujimoto new --detach` |
| `start_chat` | Chat session, no worktree | `fujimoto chat --detach` |
| `resume_session` | Bring a stopped session back (or fork it) | `fujimoto resume --detach` |
| `stop_session` | Stop a running session | `fujimoto stop` |
| `send_message` | Signed message to one session *(only with a key)* | `fujimoto send` |
| `broadcast` | Signed message to a project or everyone *(only with a key)* | `fujimoto broadcast send` |

`list_sessions`, `search_sessions` and `read_transcript` answer in the same
compact text as the CLI, which is cheaper for a model to read. Pass
`format: "json"` for the structured record, or `fields` (which implies JSON)
for just the values you need, e.g. `["active.waiting", "title"]`; the full
`session_id` always comes first. `list_sessions` with `waiting: true` is the
cheap answer to "what needs me?". A read with no window returns the last 40
messages, so the client doesn't pull a whole transcript by accident.
`read_transcript` with `summary: true` is the cheap first look.

#### Setting it up

`fujimoto mcp setup` checks each step below and prints the command for the
next one, then the exact JSON to add to Claude Desktop's config. It changes
nothing itself, so you (or Claude, walking you through it) run every step
knowingly. Re-run it until there are no ✗ lines.

1. **Read-only first.** `fujimoto mcp setup` with no key gives a server that
   reads, starts and stops sessions, with no messaging. That is a complete
   setup; stop here if you don't need `send_message` / `broadcast`.
2. **Give the server its own key.** Messages from the server are signed as an
   *agent*, so sessions see "Message from claude-desktop, an agent with a
   trusted key", never "from the user". Don't reuse your own broadcast key:
   setup refuses a key that is a `user` signer. The private key must be
   protected at rest and held by an SSH agent. Use whichever you have:
   - a key manager that holds the key and asks on use (1Password's SSH
     agent, Secretive, a hardware security key), or
   - a passphrase-protected key file, loaded with confirmation:

     ```sh
     ssh-keygen -t ed25519 -C claude-desktop -f ~/.ssh/fujimoto_claude_desktop   # set a passphrase
     ssh-add -c ~/.ssh/fujimoto_claude_desktop   # -c: confirm every signature (needs ssh-askpass)
     ```
3. **Check it.** `fujimoto mcp setup --key ~/.ssh/fujimoto_claude_desktop.pub`
   (or the literal public key). It fails if you pass the private key, if the
   private key file has no passphrase, if the SSH agent doesn't hold the key,
   or if the key isn't a signer yet.
4. **Trust it as an agent:** `fujimoto broadcast add-signer claude-desktop
   "<public key>" --role agent`, and `fujimoto broadcast enable` if broadcast
   is off. Sessions fix their trusted keys at launch, so restart the ones that
   should receive this agent's messages.
5. **Add the entry to Claude Desktop.** `fujimoto mcp setup claude-desktop`
   (with the same `--key`) writes it for you, only if every check passes. It
   copies `claude_desktop_config.json` to `claude_desktop_config.json.bak-<time>`
   first, changes nothing but `mcpServers.fujimoto`, keeps the file's
   indentation and permissions, and reads it back to check. Re-running it
   updates the entry it wrote before (e.g. adding `--key` later), showing the
   diff and keeping any global flags you put before `mcp`, such as
   `--debug-redacted`. A `fujimoto` entry it didn't write is only overwritten
   with `--replace`. Then quit Claude Desktop (⌘Q) and reopen it.

   To edit the file yourself instead, `fujimoto mcp setup [--key …] --config-only`
   prints just `{"mcpServers": {"fujimoto": …}}` (nothing if a check fails) to
   merge into `~/Library/Application Support/Claude/claude_desktop_config.json`.

   **ChatGPT desktop and Codex:** `fujimoto mcp setup chatgpt` (or `codex`,
   the same thing) writes the same server as `[mcp_servers.fujimoto]` into
   `~/.codex/config.toml` (`$CODEX_HOME` moves it). The ChatGPT desktop app
   and the Codex CLI share that file, so both get the server. Only
   fujimoto's own tables are touched: the rest of the file, comments
   included, stays byte-for-byte, and the new file is parsed before it is
   written. The same backup, update and `--replace` rules apply, and
   `setup chatgpt --config-only` prints the TOML instead. Then quit the
   ChatGPT app (⌘Q) and reopen it. ChatGPT on the web and on mobile only
   reach *remote* MCP servers (public HTTPS), so they aren't supported:
   fujimoto's server controls your sessions and never listens on the
   network.

   The entry holds an absolute path to `fujimoto`, a `PATH` that finds tmux,
   git and claude (GUI apps get a minimal one), the public key, and your
   current `SSH_AUTH_SOCK` so the server reaches the same agent: GUI apps don't
   read your shell profile, and `ssh-keygen` ignores `~/.ssh/config`'s
   `IdentityAgent`. If your agent's socket changes (a different key manager, a
   new login), run `setup claude-desktop` again.

#### Checking an installed server — `fujimoto mcp doctor`

`mcp setup` checks a key you propose; `mcp doctor` checks what each client
is actually configured with, the way the client will run it:

```sh
fujimoto mcp doctor                  # every client with a fujimoto entry
fujimoto mcp doctor claude-desktop --json
```

For each entry it checks that the launcher exists and reports a version
(warning if it differs from the `fujimoto` you ran), that tmux, git and
claude are on the entry's own `PATH`, and then starts the server with the
entry's args and env (on a minimal GUI-like environment, in `/`), asks it to
`initialize` and list its tools, and closes it. If the entry has `--key`, it
also runs setup's signing checks against the agent the entry's
`SSH_AUTH_SOCK` names. It never signs, sends, edits a config or touches a
session. With no client configured, it checks the entry `mcp setup` would
write. Exit 1 when any check fails.

#### What it can and can't do

- **Signing never touches a private key file.** The server passes `send` the
  *public* key, so `ssh-keygen` asks the agent to sign. The private key stays
  in the agent or key manager.
- **The agent is the boundary.** Anything running as you that can reach the
  agent can sign as `claude-desktop`. That is why per-use confirmation (step
  2) matters, and why setup reports it as a warning it can't verify.
- **No relayed approvals.** The server has no way to send `--approval`:
  nothing it sends claims you approved an action.
- **Claude Desktop asks before each tool call** (unless you allow a tool
  always). Starting and stopping sessions and sending messages are marked as
  write tools, and `stop_session` as destructive.
- **Everything is local.** The server runs as you on this machine, over
  stdio. It has no network listener.

### Home Screen

```
+ New worktree session
+ New project-root session (⎇ main)
+ New chat
───── recovered ─────
🔄 20260306-flaky-test-hunt     (worktree)
───── sessions ─────
🟢 20260309-cleanup-ui          (worktree)
🟢 fix-login                    (project-root ⎇ main)
🟢 auth-question                (💬 project-root)
🅿️ 20260305-spike-caching       (worktree)
🅿️ chat-1                       (💬 scratch)
🟠 20260307-parser-rewrite      (worktree)
🟠 session-1                    (project-root ⎇ main)
───── previous sessions ─────
⚫ release-notes                (💬 project-root)
⚫ 20260308-old-experiment      (worktree)
  3f9a1c2e  ⎇ main  2d ago
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
| 🔄 | **Recovered** — it was running, and something outside fujimoto took it away. Resume it and carry on. |
| 🅿️ | **Parked** — set aside on purpose, and you mean to come back to it. |
| 🟠 | **Stopped** — you ended it deliberately; it may not be needed again. Still resumable. |
| ⚫ | A **previous session**: a worktree with no session running (terminated, or never launched from fujimoto), or a terminated chat. |

**Previous sessions** lists, most recently used first, inactive worktrees,
terminated chats, and up to five project-root conversations that no other row
claims (shown by id). Terminating a chat moves it here rather than forgetting
it — unless it never received a message, since there is nothing to resume —
and it stays until you choose **Remove from list** (its transcript is kept;
a scratch chat's sub-directory is deleted only if it is empty).

The rule is simply that fujimoto is the only thing that changes a session's
status, and it only changes it when *you* do. Terminating a session through
fujimoto marks it done; stopping or parking one records which of those you
chose. Anything else — a host restart, `tmux kill-session` from another
terminal, closing the window, `exit` in the pane — leaves the record wearing no
decision at all, which is exactly what makes it recoverable. There is no
guessing, and nothing to detect at boot.

**Recovered sessions get their own section at the top of the home screen**,
above the running ones, because they are the only rows on the screen you did
not put there. Select one to resume it. Once you do — or once you stop, park or
terminate it — it leaves the section and takes its place among the rest.

Everything else is listed running first, then parked, then stopped, so the work
still in flight stays at the top.

**Stop** ends the claude process but keeps the session history: the transcript
is untouched and the conversation resumes where it left off. Any task claude
was part-way through is interrupted, so stop at a natural break.

**Park** does exactly what Stop does. The two differ only in what you are
saying: park for work you mean to pick up again, stop for work you are done
with for now. The icon is the whole difference, and it is there so a glance at
the list tells you which is which.

Within every group — recovered, running, parked, stopped, and inactive
worktrees — the
most recently created session is listed first. Ordering uses the time a session
was first launched, so two sessions started on the same day still appear in the
order you made them. Sessions that predate this (fujimoto never recorded their
start time) fall back to when their worktree directory was created, or to when
they were last seen — and pick up a real one the next time you launch them.

#### Filtering sessions by name — `/`

Press `/` on the home screen to open a filter box, then type. Matching is live
and case-insensitive, against session names and branch names, and covers the
**recovered**, **sessions** (running, parked and stopped) and **previous
sessions** lists at once — chats included. While a filter is
active the action rows (`+ New …`, settings, switch project) are hidden so only
matches remain.

| Key | While filtering |
|-----|-----------------|
| `Enter` | Apply the filter and move to the filtered list |
| `↑` / `↓` | Move the highlight without leaving the filter box |
| `Escape` | Clear the filter and return to the full list |

#### Refreshing the list — `r`

The home screen polls Claude state every few seconds, but only for sessions it
already knows about. Press `r` to re-read everything: running tmux sessions,
worktrees on disk, the open-session store and every transcript. Use it after
creating a worktree or starting a session from another window. The current
filter and the highlighted row are kept.

#### Showing each session's first prompt — `p`

A worktree name — or an untitled `chat-N` — doesn't always say what the session
was for. Press `p` to add a line under each session row with the first prompt
of that session's Claude conversation. Each press moves to the next mode:

1. **First line** — the prompt's first line, clipped to one row with `…`.
2. **Full** — the whole prompt, wrapped.
3. **Off** — back to one row per session.

Rows with no conversation (or none with a typed prompt) get no extra line. The
mode starts at off each time the home screen reappears, e.g. after you detach.

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

Backing out of a result's actions puts the highlight back on the result you
opened, so working down a list of results doesn't drop you at the top of it
every time.


### Three Session Types

**Worktree sessions** create an isolated git worktree with its own branch. Useful for standalone tasks that become PRs, or investigations where you want to fork off and explore.

**Project-root sessions** launch Claude in the project root (your main clone) on whatever branch it has checked out — the action shows that branch, e.g. `+ New project-root session (⎇ main)`. Quick and lightweight — no worktree overhead. Give it a title (optional, e.g. `fix-unit-tests`); left blank it is named `session-1`, `session-2`, …, and a title that is already in use gets `-2`. If you want a session on `main`, check out `main` in the clone first: fujimoto deliberately runs where your checkout is rather than switching branches for you. A row shows the directory it is actually running in, which is usually the project root but need not be — resuming an old conversation can leave one in a worktree. Sessions created by older versions as `direct-N` still appear and work as before.

**Chat sessions** (`+ New chat`, or `fujimoto chat`) are for questions and conversation rather than work. Give it a title (optional; `chat-1`, `chat-2`, … without one), then choose where it runs: **project-root** first, then **scratch** (the project's shared directory outside the repo, `~/.fujimoto/chats/<project>/`, where each chat gets its own sub-directory for files — the first one asks you to trust the folder), then your worktrees, most recently used first. On that list `/` filters the worktrees by name and `s` by what their sessions said, so "the worktree where we talked about the flaky test" is a few keystrokes away. A chat row shows `💬` and where it runs — `(💬 project-root)`, `(💬 scratch)` or `(💬 <worktree>)` — and belongs to the project you started it from, so it stays on the home screen and comes back after a restart like any other session. Its system prompt says it is a chat, and that if work comes up the user should decide where it happens (a new worktree, say) before anything changes. A chat in a worktree doesn't re-run the project's `.fujimoto.yaml` setup there, and keeps launch options of its own. Chats replace the old ad hoc sessions.

### Session Actions

Select any session to see contextual options:

| Session State | Options |
|--------------|---------|
| Active worktree | Connect, Fork session, Resume previous session, View session log, Park, Stop, Terminate, Finish |
| Stopped worktree | Resume previous session, Fork session, View session log, Launch, Terminate, Finish |
| Inactive worktree | Resume previous session, Fork session, View session log, Launch, Finish |
| Previous chat | Resume chat, New conversation, View session log, Remove from list |
| Active project-root | Connect, Fork session, Resume previous session, View session log, Park, Stop, Terminate |
| Active chat | Connect, View session log, Park, Stop, Terminate |
| Stopped chat | Resume chat, New conversation, View session log, Terminate |

**Stop** keeps the session in your list as 🟠 and **Park** keeps it as 🅿️;
**Terminate** marks it done and drops it to ⚫. All three close claude — the
difference is only whether fujimoto offers it back to you, and how eagerly.

All session types also offer **Open terminal**, **Open in VS Code** and
**Rename**, and **Launch options** — see [Launch options](#launch-options).

### Launch options

To start claude with extra CLI arguments (`--plugin-dir`, `--model`, …), press
**Shift+Enter** instead of Enter on whatever launches the session — a branch
in the create flow, *Launch* / *Resume* / *Fork session* in the session menu,
a location in the *New chat* flow. A dialog opens pre-filled with the options
the launch would otherwise use; edit them and press Enter to launch, or Escape
to cancel. Typing is shell-style, so quote arguments that contain spaces. The
dialog links to the [claude CLI flags reference](https://code.claude.com/docs/en/cli-reference#cli-flags);
click it to open it in your browser.

Whatever you launch with is **saved for that session**, so later launches —
relaunching a stopped, parked or recovered session, or launching a worktree
again after terminating it — reuse them with a plain Enter. Shift+Enter always
shows the dialog with the saved options, ready to change.

To change them without launching, use **Launch options** in the session menu.
The item shows the current options and where they come from; Enter in its
dialog saves them for the next launch. Editing a running session's options
does not touch the running claude; they apply the next time it starts. The
item is offered for worktrees and Claude conversations always, and for
project-root and chat sessions while they are running or stopped, since that is when they
have a record to save onto.

Options are looked up in this order: the session's saved options, a fork's
parent's options, then `claude_args` in `.fujimoto.yaml`. If none of them sets
`--model`, the [default model](#global-config-and-default-model) is added at
launch. The dialog names it, but it isn't part of the saved options. Saving options that
match the project default clears the override, so the session goes back to
following `.fujimoto.yaml`. Saving an empty field is a deliberate "no options".

Where they are saved: a worktree keeps them in its `.fujimoto/meta.json`, so
they last as long as the worktree. A direct session keeps them on its session
record, and when it is terminated they move to its Claude conversation, so
resuming that transcript later brings them back.

Shift+Enter only reaches fujimoto as its own key in terminals that speak the
kitty keyboard protocol (kitty, Ghostty, WezTerm, recent iTerm2); elsewhere,
e.g. macOS Terminal.app, it arrives as a plain Enter. **`o`** does the same on
any list and works in every terminal. Options can't be changed on a running
session — stop it first.

### View Session Log

**View session log** reads a session's Claude transcript back without starting
Claude. It renders the conversation — your prompts, Claude's replies and its
thinking — in a scrollable read-only view. Tool calls and their results are
folded away so the conversation stays readable: a run of consecutive calls
collapses to a single `⚒ 10 tool calls  Bash, Read, Edit` row, which opens into
the individual calls, each still folded. Opening a call shows its arguments and
the output it produced — the result belongs to the call rather than being a row
of its own. Tab to a row and press Enter, or click it, to expand. Long tool inputs and results are clipped, and sub-agent
(sidechain) entries are omitted. The viewer carries no hint line of its own —
the footer already names the keys — so the transcript gets the full height.
Escape goes back — to the search results if
you got there from a transcript search,
otherwise to the home screen. When a session has more than one transcript, a
picker asks which to read.

If a transcript turns out to hold an entry shape fujimoto doesn't recognise, the
viewer says so and shows the raw log instead of refusing to open it. The raw
view searches exactly like the normal one.

#### Searching within the transcript — `/`

Press `/` inside the viewer to search the conversation you are reading. Every
match is highlighted in place, `n` steps to the next one and `N` to the
previous, wrapping at either end; the status line above the transcript tracks
where you are (`3 of 11 matching messages`). A match buried in a folded tool
call opens that fold, so nothing is highlighted where you can't reach it.

`Ctrl+R` and `Ctrl+I` toggle regex and case exactly as they do in the transcript
search, and the current settings are shown alongside the match count. (`Ctrl+T`
has no meaning here — the transcript has already been parsed into messages, so
there is no raw JSON left to scan.) `Enter` hands focus to the transcript, where
`n`/`N` and the arrow keys work; the first `Escape` clears the search, and the
next one leaves the viewer.

Searching runs in the background and only repaints the lines whose matches
changed, so the transcript never rebuilds under you and the query box keeps
focus while you type.

| Key | While reading a session log |
|-----|-----------------------------|
| `/` | Search this transcript |
| `n` / `N` | Next / previous match |
| `Ctrl+R` / `Ctrl+I` | Toggle regex / match-case |
| `Enter` (in the box) | Move focus to the transcript |
| `Escape` | Clear the search, then leave the viewer |

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
| tmux session (worktree) | `{project}/{dir-name}` | `my-app/20260309-fix-unit-tests` |
| tmux session (project-root) | `{project}/{slug}` (`session-{N}` untitled) | `my-app/fix-login` |
| tmux session (chat) | `{project}/{slug}` (`chat-{N}` untitled) | `my-app/auth-question` |

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
| `Shift+Enter` / `o` | Launch with options (see [Launch options](#launch-options)) |
| `/` | Filter sessions by name (home screen) |
| `s` | Search inside session transcripts (home screen) |
| `r` | Refresh the session list (home screen) |
| `p` | Cycle the first-prompt line: first line → full → off (home screen) |
| `b` | Broadcasts and messages of the last week (home screen) |
| `n` / `N` | Next / previous match (session log viewer) |
| `Escape` | Back (or quit from home; clears an active filter first) |
| `q` | Quit |
| Arrow keys | Navigate |
| `PgUp` / `PgDn` | Move the highlight a page at a time |
| `Home` / `End` | Jump to the first / last row |

The footer only lists the keys that do something in the view you are in — `s`,
`r`, `p` and `b` disappear once you leave the home screen, the `Ctrl+R`/`Ctrl+T`/`Ctrl+I` mode
toggles appear only while you are searching, `n`/`N` only when a log search has
matches to step through, and `/` is labelled *Filter* on the home screen but
*Search* while you are reading a session log.

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
