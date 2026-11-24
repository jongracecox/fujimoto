# Gotchas and learnings

Things discovered during development that are easy to forget:

- **Textual widget IDs cannot contain `/`**. tmux session names use `project/name` but widget IDs must use `--` as separator (e.g. `ds-my-app--direct-1`).
- **`git worktree remove` needs `--force`** for worktrees with uncommitted changes — without it the command fails silently in some states.
- **`git reflog` records branch creation origin** (`branch: Created from main`) — useful for recovering the base branch if `.fujimoto/meta.json` is missing.
- **`claude -p` (print mode)** runs non-interactively. For background tasks, pair with `--allowedTools` to scope permissions rather than `--dangerously-skip-permissions`.
- **Global find-replace for renames** works well but always verify test patch target strings — they are plain strings not checked by the import system. Run the full test suite after any rename.
- **Claude log entry types evolve** — real logs contain `last-prompt`, `queue-operation`, `progress` and other types beyond `assistant`/`user`/`system`/`file-history-snapshot`. The parser skips unrecognized types gracefully. **`last-prompt` does not mean the session ended** — it did once, but Claude Code now writes one after every prompt (with `ai-title`, `mode`, `permission-mode`, `pr-link` around it), so treating it as an end marker showed every busy session as 💤 idle. Never infer liveness from a transcript; tmux and the registry (`claude/live.py`) say what is running. `stop_reason=None` on assistant entries means interrupted/canceled (Esc) → `WAITING_FOR_USER`. Always smoke-test against real `~/.claude/projects/` data after changes.
- **Anything that reads every entry's `message` sees text nobody said.** Claude Code writes `api-request-blob` entries holding a copy of the whole request, and the first request of every session starts with a `<system-reminder>` that carries `CLAUDE.md`. Message-text search used to take `message.content` from any entry, so CLAUDE.md wording matched in every session and crowded out the real snippets. Filter on `type` (`user`/`assistant`), `isMeta`, and `log_parser.INJECTED_PREFIXES`, as `read_transcript` and `search._message_text` do.
- **Shift+Enter in tmux requires `extended-keys always` globally** — tmux strips modifier info by default, making Shift+Enter identical to Enter. The fix requires two server/global-level settings: `set-option -g extended-keys always` and `set-option -s -a terminal-features xterm*:extkeys`. Per-session (`-t`) doesn't work. `extended-keys on` (vs `always`) doesn't work because Claude Code doesn't send the kitty keyboard protocol activation sequence. Requires tmux 3.2+. See `_ensure_extended_keys()` in `tmux.py`.
- **`focus-events` must be on before claude starts, and only reaches new attaches.** Claude Code probes `tmux show -gv focus-events` once at startup and caches the answer, so `_ensure_focus_events()` runs straight after `new-session` — setting it later in the create sequence races claude's probe. On the tmux side, the option is server-wide (`set -s`; `show -g` falls back to it) and has no option-change hook: tmux sends the outer terminal the focus-reporting enable sequence only in `tty_update_features`, i.e. when a client attaches. A client already attached when the option flips gets no focus events until it reattaches — harmless for fujimoto, which always attaches after creating (verified against the tmux 3.6a source, `tty.c` / `options.c`).
- **fujimoto never creates `.venv` — `uv` does.** `create_worktree` only runs `git worktree add`; there is no venv/copy logic in the worktree lifecycle. A `.venv` appears in a worktree only because `uv sync`/`uv run` was run there (each worktree is its own project root with its own `pyproject.toml`, and uv materializes a per-project environment by default unless `UV_PROJECT_ENVIRONMENT` is set — which only `noxfile.py` does). The intended way to seed an environment in a fresh worktree is an `init: [uv sync]` entry in `.fujimoto.yaml`.
- **Never fingerprint a constant — it becomes a crib inside the same log.**
  Salting stops a fingerprint being matched *across* logs, but not within one.
  `~/.cache/<X>` is always `fujimoto`, so hashing that component published the
  digest of a known word, and the same digest appeared wherever the *project*
  name was redacted — re-leaking it for any repo called fujimoto. Constants that
  fujimoto or Claude Code choose themselves (`_OWNED_NAMES`, matched only under
  `_OWNED_PARENTS`) are therefore preserved verbatim rather than hashed. When
  adding redaction, ask both "could this be the user's name?" and "is this value
  fixed enough to be guessed?" — a yes to the second means preserve, not hash.
- **Over-redaction makes a log useless, which is its own kind of bug.** The
  first cut redacted TUI widget ids (`id=[REDACTED-…-44]` hid *which row the
  user picked*), `project_config` action verbs (hiding that it copied `.env` and
  ran `uv sync`) and the `main` branch name. Those are fujimoto's and git's own
  vocabulary, not user data. `rid`, `rref` and splitting a `<verb> <target>`
  action into `kind=` plus a redacted `detail=` exist for exactly this.
- **Anything called per home render needs `log_once` or `log_capped`.**
  `config.read_meta` runs once per worktree per render, which on a machine with
  ~50 worktrees wrote 142 lines of a 485-line log. Keyed dedupe cut that to one
  line per worktree, and `log_capped` cut it again to ten plus a count. Note the
  two must compose: capping raw *calls* reported `total=141` and hid 37 distinct
  worktrees behind a number that conflated re-renders, which is why
  `event_capped` takes a `dedupe_key`.
- **An unsalted redaction fingerprint is known-plaintext.** A log always
  contains fixed strings (fujimoto's own `~/.cache/fujimoto`, for one), so an
  unsalted digest hands the reader the fingerprint of a known word — and from
  there any guessable project name can be confirmed by hashing it. `_fingerprint`
  is salted with `secrets.token_bytes(16)` per `enable()`, the salt is never
  written to the log, and `disable()` clears it so `redact_text` is deterministic
  (and the doctests meaningful) when no run is active. Correlation *within* a log
  is the only property needed, and it survives.
- **A redaction allowlist must only contain names a user cannot have chosen.**
  `_SAFE_PATH_COMPONENTS` originally included `fujimoto`, `git`, `logs`, `src`,
  `main` and `master` — which leaked the project name of every repo living in
  `~/git/<project>`, and of fujimoto's own checkout, straight into
  `--debug-redacted` logs that are meant to be shareable. Dotted config dirs
  (`.claude`, `.fujimoto`) and OS dirs are safe because nobody names a repo
  `.cache`; ordinary words are not. Git ref words live in a separate
  `_SAFE_REF_COMPONENTS` used only for *command arguments*, since `origin/main`
  is git's vocabulary but a *directory* called `origin` is the user's. When
  adding to either list, ask "could this be the name of someone's project?"
- **Never unpack a `**fields` dict into `log_capped`.** Its `limit` and
  `dedupe_key` are typed keywords, so `**fields` (a `dict[str, object]`) could
  supply either — `ty` rejects it, and at runtime a field named `limit` would
  silently become the cap. Spell the fields out at each call site, even when
  that means repeating them across two branches.
- **`DebugLogger.event()` takes its event name positional-only** (`def event(self, event_name, /, **fields)`). Without the `/`, logging a field literally called `name=` (env vars, tool names) raises `TypeError: got multiple values for argument 'name'`. Same for `event_once`, `debug.log` and `debug.log_once`.
- **`debug.py` must not import from other fujimoto modules at module scope** (other than a lazy `fujimoto.version` import inside `log_environment`) — `git`, `tmux`, `config`, `settings`, `project_config`, `terminal`, `vscode` and `claude.log_parser` all import it, so a top-level back-import would be circular.
- **Bundled package data must be importable as a subpackage.** `templates/` has an `__init__.py` so `importlib.resources.files("fujimoto.templates")` resolves and hatchling ships the `.template` file in the wheel. Verify with `uv build --wheel && unzip -l dist/*.whl | grep templates` after touching packaged resources.
- **A tmux socket path must fit in ~104 bytes on macOS.** Testing against an isolated server with `TMUX_TMPDIR` under a long scratch directory fails silently: `new-session` exits without a server and nothing attaches. Point `TMUX_TMPDIR` at a short directory (e.g. `mktemp -d /tmp/fjt.XXXX`), and never test `restart`/`detach` against the real server — `restart --all` would detach the user's live sessions. **`$TMUX` beats `TMUX_TMPDIR`**: inside a tmux session (every fujimoto-launched agent is in one) `TMUX_TMPDIR=/x tmux kill-server` still talks to the *current* server and kills every session. Always unset it too: `env -u TMUX -u TMUX_PANE TMUX_TMPDIR=/x tmux …`.
- **Inside an `if-shell` argument a bare `;` separates tmux commands; at `bind-key` top level it must be `\;`.** The `x` binding's true branch is the single string `set-option @fujimoto_pending_action close ; detach-client`, which works because tmux re-parses that string; the `f` and `s` bindings pass `"\\;"` as its own argv element because the outer `bind-key` invocation would otherwise consume a bare `;` itself and run `detach-client` at bind time. Both forms are exercised in `tests/test_tmux.py`, and both were verified against a real tmux via a pty before being wired up.
- **Claude encodes a project path by replacing `/` *and* `.` with `-`.** `/repo/.fujimoto/worktrees/x` is stored as `-repo--fujimoto-worktrees-x` — the double hyphen is the `/` plus the `.`. Encoding only the slashes made `get_sessions_for_path` miss every transcript for a worktree under the default `<repo>/.fujimoto/worktrees/` root (used whenever `FUJIMOTO_WORKTREE_ROOT` is unset), which showed up as "Resume previous session" being absent from every inactive worktree — and, from the same lookup, no Claude state icons, no fork, no session log and no search hits. Verify the rule against real data rather than assuming: read the `cwd` out of a transcript and compare it to the directory name holding it. Claude also honours `CLAUDE_CONFIG_DIR`, which moves `projects/` wholesale.
- **`kill_session` raises when the session is already gone**, so any code path that ends a session has to decide what that means. `_end_session` re-raises only if `session_exists` still reports the session — a session that died between being listed and being acted on is already in the state the kill was aiming for, while a kill that genuinely failed must not silently mark a live session closed.
- **Never splice arbitrary text into a console-markup string — assemble a `Content` instead.** Search snippets are raw transcript bytes cut at arbitrary offsets, and both of these silently corrupt the row: a fragment ending in `[` swallows the tag that follows it (`[dim]{"a": [[/]` renders the literal text `{"a": [[/]`), and a fragment ending in `\` escapes it (`[dim]path\[/]` renders `path[/]`). `rich.markup.escape` does **not** save you — it only escapes a `[` that still looks like a tag *in the fragment it is given*, so `escape('{"a": [')` returns the string unchanged. `Content.assemble(("text", "style"), ...)` never parses the text at all, resolves `$theme-variables` in the style, and — unlike a nested `[b]` inside `[dim]` — does not inherit the surrounding `dim` into the highlight. See `_render_snippet`.
- **Anything built from transcript text must be `Content`, including a `Collapsible` title.** The `Content.assemble` rule below is usually stated about *bodies*, but a widget's title argument is parsed as markup too: `Collapsible(title=...)` hands the string to `Content.from_text(markup=True)`, so a `⚒ Bash  command: until [ "$(gh run list …` title raised `MarkupError: Expected markup value` and took the whole viewer down. The same applies to the viewer's header (a session title or first prompt) and the Claude-session picker rows (`_build_claude_session_items`), which mix a model-written title with a `[dim]` timestamp — that one is `Content.assemble`, since it is styled as well as arbitrary. Rule of thumb: if a string came out of a transcript, it reaches a widget as `Content` or not at all.
- **`Static`/`Label` text in tests is read with `str(widget.render())`, not `.renderable`** — Textual 8 dropped the attribute. `render()` returns the *resolved* content, so console markup (`[dim]`, `[b]`) is gone from the string; assert on the plain text. And a `ListItem`'s own children are composed when the item mounts, so `item.query(Label)` needs an `await pilot.pause()` after a non-awaited `ListView.append`.
- **A worker's `is_cancelled` is not a synchronisation primitive.** Cancelling a Textual worker (or letting `exclusive=True` supersede it) does not unwind work already queued on the event loop via `call_from_thread`. Anything a worker hands back must carry a generation token the handler checks — see `_search_token`. Bump the token *before* cancelling, so a batch in flight is stale from the moment the decision is made. **And before every `await` on the teardown path, not just the cancel** — `_clear_search_results` awaits `ListView.clear()`, which yields, so a queued batch was applied *during* the clear while its token still looked current. It mounted `sr-0` into the list the fresh scan then appended its own `sr-0` to, and Textual's `DuplicateIds` took the whole app down mid-search.
- **Shift+Enter is not a portable key.** Fed to Textual's `XTermParser`:
  `\r` → `enter`, the kitty-protocol `ESC[13;2u` → `shift+enter`, but xterm's
  modifyOtherKeys form `ESC[27;2;13~` (what tmux emits with `extended-keys`
  on) → **no key at all**. So in a legacy terminal Shift+Enter is a plain
  Enter, and if the TUI runs inside tmux it can be swallowed outright. That is
  why the launch-options binding is `shift+enter,o`: `o` is the fallback that
  works everywhere a list has focus (an `Input` types it instead).
- **OSC escape writes during a Textual run must go to `sys.__stdout__`, not `sys.stdout`.** Textual replaces `sys.stdout` with an internal capture while the app runs, so an OSC sequence (e.g. the `set_terminal_title` iTerm2/window-title escape) written to `sys.stdout` from inside a running app — such as `_init_git_info` updating the title on project switch — never reaches the terminal. `sys.__stdout__` stays connected to the real tty, so writing there works both before and during `app.run()`. This is why the session-manager title set at `main()` (pre-run) worked but the in-app update initially did not.

## Broadcast

Verified with live sessions while building `fujimoto broadcast` (Claude Code
2.1.329):

- **`asyncRewake` + exit 2 wakes an idle session**, as documented; a busy one
  queues it (`queue-operation` … `absorbed_mid_turn`) and handles it after the
  current tool call, not at the end of the task; a session on a permission
  prompt queues it without answering the prompt. A broadcast never bypasses
  tool permissions.
- **Claude Code enforces `timeout` on an `asyncRewake` hook, and the kill is
  silent** (SIGKILL: no `finally`, a stale lock, nothing in the session). The
  message isn't lost — the next `Stop` re-arms a waiter, which delivers from
  its saved mark — but it is late. Hence the 7-day timeout and the dead-lock
  takeover. A timeout above the 600s default is honoured (checked at 12.5 min).
- **The delivered text is framed as `[SYSTEM NOTIFICATION - NOT USER INPUT]
  … Stop hook blocking error from command "Stop": …`** (even for the
  `SessionStart` hook). Sessions still act on it; the "signed by" preamble is
  what earns that, and the signature is what makes it true.
- **Wording matters.** "Reply with X and nothing else" made a busy session drop
  the answer to the question it was working on. The preamble says "at the next
  safe stopping point in your current work" — "finish your current work first"
  would mean hours on a long task.
- **`claude` keeps only the last `--settings`.** Two flags each adding a
  `SessionStart` hook ran only the second. Hence `merge_settings_args`.
- **Hooks don't run until the folder's trust dialog is accepted.**
- **An SSH agent may remember an approval.** 1Password asked for a fingerprint
  on the first `-Y sign` and not on the second (0.11s), so a process in the
  same session can sign until the approval lapses. Documented in the README's
  security model; a hardware key with touch-per-use closes it.

## MCP server

- **Claude Desktop starts servers in `/` with a minimal `PATH`.** `new` and
  `resume` refuse to run outside a git repo, so the server sets the
  subprocess cwd (project root / the session's cwd). Homebrew's tmux and
  `~/.local/bin/claude` aren't on the default `PATH`, which is why setup puts
  a `PATH` in the Desktop config.
- **`which` finds terminal shims first.** cmux puts
  `~/.cmuxterm/cmux-cli-shims/<id>/claude` ahead on `PATH`, a directory that
  is gone after that terminal closes. `mcp_setup._stable_dir` skips dirs named
  `*shim*`, but only by their last two path components: a temp dir like
  `test_stable_dir_skips_shims0` once made it skip everything.
- **Don't match ssh-keygen errors by a word.** `ssh-keygen -y -P ''` echoes
  the key path in its error. Matching "passphrase" anywhere in stderr called
  a missing key "encrypted" when the path contained the word. Match the
  phrase "incorrect passphrase".
- **mcp 2.x renamed `FastMCP` to `MCPServer`** (`mcp.server.mcpserver`).
  `MCPServer.call_tool` *raises* `ToolError`, and the protocol layer turns
  it into an `is_error` result, so in-process tests expect the raise.
- **A new session has an id before it has a transcript.** `claude
  --session-id` names the transcript, but Claude writes it only with the
  first message, so a chat started without a prompt has none until someone
  types. Look such a session up through fujimoto's session record
  (`session_query.pending_session`), not the transcript.
- **Clients don't get your shell's `FUJIMOTO_*` settings.** A server started
  by Claude Desktop or ChatGPT saw no `FUJIMOTO_GIT_ROOT` or
  `FUJIMOTO_WORKTREE_ROOT`: project names didn't resolve, and worktree
  history was missing. `mcp setup` now writes them into the client config.

## cmux

Checked against cmux 0.65 while working out why fujimoto panes didn't come
back after a cmux relaunch:

- **A tmux server started in cmux keeps that pane's identity.** `tmux
  new-session` that *starts* the server copies the caller's whole environment
  into tmux's global environment, `CMUX_SURFACE_ID` included, and every later
  session inherits it. cmux's `claude` wrapper (first on the tmux `PATH`)
  reports hooks to that id, so cmux filed every Claude session in tmux under
  the one pane that started the server. On relaunch it tried to
  `claude --resume` one of them there, found it still running in tmux, and
  refused (`Restore failed stage=admission.live-owner`). cmux's zsh
  integration does unset these keys from a running server, but only at a
  shell prompt outside tmux, and the fujimoto TUI never shows one. Hence
  `tmux._new_session` (see `cmux.md`).
- **cmux's own tmux restore didn't bring fujimoto panes back.** cmux detects
  a `tmux attach-session` in the foreground and saves the pane as
  `tmux attach -t <session>` (`source: process-detected`). After a relaunch,
  `cmux restore --surface` failed in every such pane with
  `stage=record.missing` (no restore record by the time it ran). Even when it
  works, a bare `tmux attach` comes back without the fujimoto loop, so
  detaching drops to a shell. Hence the `fujimoto --attach` binding.
- **Where to look.** `log show --predicate 'process == "cmux" AND eventMessage
  CONTAINS "Restore failed"'` gives the failing stage per pane;
  `cmux surface resume show --json --surface surface:N` shows a pane's binding
  and restore record; `cmux sessions list` shows which pane each Claude
  session is filed under; `~/Library/Application Support/cmux/session-history/`
  keeps the snapshot cmux restores from.
