# Key design decisions

- **TUI loop with tmux detach**: The TUI runs in a `while True` loop. After tmux detach (subprocess.run returns), the loop restarts and the TUI reappears. The loop breaks when the user quits without selecting a session.
- **Two update dismissals, one version each**: the home-screen banner's `d` writes `dismissed_version`; an agent's `fujimoto update dismiss` writes `agent_dismissed_version`. They are separate so hiding the banner doesn't stop sessions asking at least once, and each covers a single version, so the next release is offered again rather than silenced for good. The session notice names the upgrade command fujimoto detects from its own environment (`uv-receipt.toml`, `pipx_metadata.json`, PEP 610 `direct_url.json`) rather than leaving the agent to probe for it. A `uv tool` built from a local path gets `uv tool install --force fujimoto`, because `uv tool upgrade` rebuilds from the original source and would never reach the PyPI release.
- **Restart by `exec`, guarded by the loop's pid**: an upgrade only reaches the outer fujimoto (the TUI and launch loop) when that process restarts; the Claude session and every `fujimoto …` command it runs are already on the new code. Re-executing the loop in place keeps its terminal, so `--all` restarts each fujimoto in its own pane rather than opening anything. The loop pid mark plus the client's parent pid is what stops `restart` detaching a session nothing is waiting behind (a `--detach` session, `Ctrl-A w`, plain `tmux attach`), which would leave the user at a shell; the session option alone would go stale if a loop died. `--all` acts directly rather than broadcasting to agents: no tokens, and no need for the agent to be idle.
- **Per-session tmux config**: Prefix defaults to `Ctrl-B` (tmux's standard default; configurable via `FUJIMOTO_TMUX_PREFIX`), status bar with shortcut hints — all set via `tmux set-option -t` so the user's global config is untouched. The attach flow is silent (no pre-attach banner) to reduce noise when launching sessions repeatedly.
- **Global install via `uv tool` needs `--no-cache`**: `--force` alone reuses a
  cached build, and so, in practice, does `--force --reinstall` — uv's build
  cache is keyed on the derived version, and `hatch-vcs` gives every commit in
  a run of untagged work the same `X.Y.Z.dev0` string. The install then reports
  success while leaving the previous wheel in place, which looks exactly like a
  feature that doesn't work. Always
  `uv tool install --force --reinstall --no-cache .`, and verify with
  `fujimoto --version` (the `.devN` suffix moves) or by grepping the installed
  `site-packages/fujimoto/cli.py` for something the change added.
- **Every group reads newest-first.** A worktree name carries the *day* it was
  created (`20260922-…`), so a name sort gets two sessions made on the same day
  in an arbitrary order — and got `direct-10` before `direct-2` outright. Each
  section of the home screen is now sorted by `_order_key`, which falls through
  four levels: the record's `created` stamp (real creation order, to the
  second), the directory's creation time (`_creation_time` — `st_birthtime`
  where it exists, else `st_ctime`), the record's `last_seen`, and finally
  `_natural_key(name)`, which compares runs of digits numerically.
  **A stampless record is backfilled on its next launch, not stamped `now`.**
  `mark_open` writing `_now()` for an existing record was the bug that made
  this feature look broken: every legacy session leapt to the top of the list
  the first time it was relaunched, so the order shuffled as sessions were
  used. `session_state._backfill_created` estimates instead — a worktree from
  its directory's age, a direct or ad hoc session from `last_seen` — and only
  a genuinely new record (no `existing`) gets `_now()`. The read-side fallbacks
  below stay as the safety net for records that have not been relaunched yet.

  **The last two levels exist for records written before `created` did.** A
  legacy *worktree* record borrows its directory's age, which is the real
  thing; a legacy *direct* or *ad hoc* record has no such directory (its cwd is
  usually the repo root, whose age says nothing), so it borrows `last_seen` —
  an upper bound, but one that keeps it interleaved instead of sinking every
  pre-upgrade row to the bottom of its group. Both correct themselves the first
  time the session is relaunched, since `mark_open` stamps `created`.
  The groups themselves are untouched: recovered, then running, parked and
  stopped, then previous sessions (ordered by last use — see [Session status](sessions.md#session-status)). Within the *running*
  group, direct and worktree rows interleave by creation rather than being
  listed kind-by-kind — both are just running.
  **The stats live in `_init_git_info`, not in the render path** (see "Nothing
  in the home render path may touch the disk"): it fills
  `_creation_times: dict[str, float]` for every worktree and every open
  record's cwd, and `_build_home_items` only reads that dict.
- **Launch options (Shift+Enter / `o`)**: every launch goes through
  `SessionApp._launch(target)` — never `self._launch_target = …; self.exit()`
  directly — which is the single place the dialog can intercept. Mechanics:
  - `action_launch_with_options` sets `_launch_options_armed` and then fires
    the focused widget's own Enter (`ListView.action_select_cursor()` /
    `Input.action_submit()`), so it reuses every existing handler. The flag
    **survives the steps in between** (a title box, a branch list, a
    conversation picker) and is dropped by `_show_home` and
    `_show_session_search`; `_launch` consumes it.
  - `_LAUNCH_OPTION_WIDGETS` lists the widgets where it means anything;
    `check_action` uses it to show the binding only there.
  - An armed launch of a **running** session just connects, with a warning
    toast: options belong to a claude process, and that one already exists.
  - `LaunchOptionsDialog` is a `ModalScreen` pre-filled from
    `launch_options.resolve`, so editing is the normal case. Escape cancels
    the launch (it does **not** fall back to defaults), and a `shlex` error
    stays in the dialog. Its link to the claude CLI flags docs
    (`CLAUDE_CLI_FLAGS_URL`) is a `[@click=screen.open_docs]` action, not a
    `[link=…]` style: Textual captures the mouse, and `App` has an `open_url`
    method but no `action_open_url`, so the dialog's own `action_open_docs`
    calls it.
  - `main()` calls `_launch_claude_args(target, resolved_name)` after
    `mark_open` (so a direct session's record exists to save onto): an
    explicit choice is saved and used; otherwise it resolves, and an
    inherited (fork-parent) value is saved onto the fork so it outlives the
    parent.
  - **The session menu edits them without launching** (`sa-options`, after
    Rename). Its label is `Content` built by `_launch_options_label` (the
    options are user text) and shows the resolved options plus their
    `Source`. `_edit_launch_options` opens the same dialog with
    `editing=True` (only the hint differs), saves via `launch_options.save`
    and re-renders the menu with the highlight back on the item.
    `_can_edit_launch_options` hides it from a direct/ad hoc row with no open
    record, where there is nothing to save onto. Only a `claude` row passes
    its `claude_session_id` to `save`: a direct row's id is just the latest
    transcript in its directory, which may belong to a different session.
  - **Where they live is chosen by lifetime.** Terminate deletes the
    `SessionRecord`, so options kept only there would vanish exactly when the
    user expects "terminate, relaunch with Enter" to reuse them. A worktree's
    `meta.json` outlives a terminate; a direct session's options are copied to
    the conversation cache by `_keep_launch_options` before `mark_closed`.
- **Remembering sessions across a restart**: `session_state.py` records every session as open at launch — in `main()`, **before** `launch_claude_in_tmux` blocks on the attach, so a host that dies mid-session still has a record to come back as. `_init_git_info` loads the pruned state into `_open_sessions`; `_idle_records()` derives the open-but-not-running set for the current project; `_build_home_items` splits those by `stop_kind` — `RECOVERED` into its own section above *sessions*, `PARKED`/`STOPPED` into *sessions* after the running rows — and excludes all of them from *inactive worktrees*. `SessionInfo.stop_kind` carries the record's kind into the TUI and `SessionInfo.is_stopped` is *derived* from it (an open record with no live session), so there is one source of truth for which flavour of not-running a row is. There is deliberately no bulk-restore row: relaunching N claude processes does not restore the terminals and window arrangement a crash actually took. `_end_session(session, terminate=..., park=...)` is the single handler behind every menu item and every outcome of the `Ctrl-A x` prompt; it tolerates a `kill_session` failure only when `session_exists` confirms the session is already gone (otherwise marking a live session closed would hide it). `_do_delete_worktree` and `on_rename_submitted` keep the store honest via `mark_closed` / `rename`.
- **Session metadata**: `.fujimoto/meta.json` stored in worktree directory records the base branch for cherry-pick targeting, the `source_root` (main repo) for project-config source resolution, and — for forks — `forked_from_session_id` plus `forked_from_worktree`. The `.fujimoto/` directory contains a `.gitignore` with `*` so its contents are automatically ignored by git.
- **Project config (`.fujimoto.yaml`)**: An optional, committed per-project file (`project_config.py`) declaring files to copy/link into a worktree and init commands to run. Applied centrally in `main()`'s launch loop (parent process, **before** `tmux attach`) by `_apply_worktree_config(working_dir)`, for **every** worktree connection mode (new, reconnect-to-live, relaunch/resume) — so copy/link/init run on each connect, not just creation. `_do_create_and_launch` no longer applies config; it only creates the worktree and stores meta. Key mechanics:
  - **Config is read from the source root (main clone), not the worktree.** `.fujimoto.yaml` is a local, uncommitted file in the main clone, so it isn't present in a worktree checkout — `_apply_worktree_config` calls `load_project_config(source_root)`. (`--create-config` writes it to the main clone's root.)
  - **Worktree detection**: `_resolve_worktree_source` returns the main repo root (from meta `source_root`, else derived via `git.get_main_worktree_root`) only for a *linked* worktree; returns `None` for the main repo / direct / adhoc sessions (which then get no config). This is git-based, not `session_type`-based, so the resume path (historically labelled `"direct"`) is covered too.
  - **`once` vs `always`**: trigger is `CREATE` only on the worktree's first launch — tracked by the `.fujimoto/config_once_applied` marker (`config.config_once_applied` / `mark_config_once_applied`); every later connection uses `LAUNCH` (only `always` entries). The marker is set after a successful (or `on_error=continue`) CREATE application, so `once` truly runs once.
  - **Syntax errors are surfaced when the TUI opens.** `on_mount` calls `_project_config_error()` (which parses the global config *and* `load_project_config(self._project_root)`, joining both errors) and, on a `ConfigError`, pushes a `ConfigErrorDialog` modal (OK button; Enter/Escape to dismiss) over the home screen. It's informational only — dismissing it doesn't block any action, since you may need to launch a session to fix the file. Shown once per TUI appearance (per `SessionApp` instance / loop iteration). At launch time `_apply_worktree_config` simply skips a malformed config (no pause), since it's already been surfaced. The dialog (and `_show_error`) render the message with `markup=False` / `rich.markup.escape` because pydantic validation text contains brackets that would otherwise be parsed as console markup.
  - **Errors**: copy/link issues are non-fatal warnings printed to stderr. A non-`continue_on_error` init failure is governed by the config's `on_error`: `abort` returns `False` (main `continue`s the loop → TUI reopens, launch skipped) and `continue` attaches anyway. Either way `_pause_for_key` shows the error and waits for a keypress so it isn't wiped by `tmux attach`.
- **Debug logging (`--debug` / `--debug-redacted`)**: instrumentation lives at
  the call sites that matter (subprocess wrappers, discovery functions, TUI
  state) rather than in a wrapper layer, so it covers every code path
  including ones the TUI can't reach. It is opt-in and no-op by default, which
  keeps the normal path free of I/O. `--debug-redacted` exists so a user can
  hand over a log without leaking project/branch/path names, while still
  showing lengths and special characters — enough to diagnose bugs caused by
  odd characters in a project name.
- **Background PR creation**: Uses `claude -p --allowedTools "Bash(git:*) Bash(gh:*)"` in a tmux session for unattended PR creation.
- **Claude session integration**: The home screen fetches Claude session state from `~/.claude/projects/` JSONL logs via the log parser. Session states: 👀 awaiting input (`WAITING_FOR_USER`), 🛡️ approve tool (`WAITING_FOR_TOOL_APPROVAL`), ⚙ working (`WORKING`), 💤 idle (`IDLE`, no longer produced by the parser), no indicator (`UNKNOWN`). **A running row's state comes from Claude Code's registry** when it has an entry for that tmux session writing the same conversation: `_REGISTRY_STATES` maps `status` `busy` → `WORKING`, `waiting` → `WAITING_FOR_TOOL_APPROVAL` (verified against a real permission prompt), `idle` → `WAITING_FOR_USER`; any other status (`shell`, …) or no entry falls back to the transcript, applied in `_claude_for` (logged as `tui.claude_for status=… state=…`). The registry is the only thing that can tell a running tool from a permission prompt — both are a `tool_use` with no result yet. Transcript logic, the fallback: for assistant entries: `stop_reason=tool_use` without a following `tool_result` → `WAITING_FOR_TOOL_APPROVAL` (pending user approval), `stop_reason=tool_use` with `tool_result` → `WORKING`, any other stop reason or no stop reason → `WAITING_FOR_USER`. Last entry is user → `WORKING`. Previous Claude sessions (from the project root, capped at 5) appear as resumable items. Resuming launches `claude --resume SESSION_ID` in a new tmux session. The latest Claude session per path is "claimed" by the corresponding tmux/worktree item to avoid duplication.
- **Forking a session**: "Fork session" creates a new worktree *and* forks the conversation in one step — `git worktree add -b worktree/<new> <path> <base>` followed by `claude --resume <parent-id> --fork-session` in the new directory. Notes:
  - The base branch defaults to the **parent's branch**, so the fork inherits the parent's commits; the other options are the parent's own base branch (a sibling rather than a child) and any branch via the picker. `_show_branch_picker` hides `worktree/*` branches for a plain create but keeps them for a fork, since that is exactly what a fork wants.
  - The fork branches from the parent's **committed tip** — uncommitted work in the parent is not carried over. `_build_fork_system_prompt` tells the forked session this, along with the parent's path, because the inherited conversation refers to files by the *parent's* paths.
  - **Cross-directory resume requires Claude Code ≥ 2.1.223**, which looks a session id up in the current project and its worktrees first, then every other project on the machine. Earlier versions only searched the current project directory.
  - Claude Code keys transcripts by cwd, so the forked session's JSONL lands under the *new* worktree's encoded project dir. No special casing is needed — `_get_claude_sessions` and the home screen's live state polling pick a fork up like any other session.
  - **In-session `Ctrl-A f` hands over to the TUI rather than reimplementing the flow.** A key binding can only prompt for free text (`command-prompt`), which makes naming a base branch a blind typing exercise and a conversation picker impossible. So `f` runs `set-option @fujimoto_pending_action fork \; detach-client`: the detach returns control to the `fujimoto` process blocked in `attach_session`, `main()` consumes the flag with `take_pending_action`, and the next `SessionApp(pending_fork=...)` opens straight onto the fork flow via `_open_pending_fork`. One implementation, all the pickers. Three non-obvious mechanics:
    - `set-option` must **not** take `-t "#{session_name}"`. A key binding already targets its own session, and the target of `set-option` is *not* format-expanded — passing it makes the command fail, which aborts the rest of the sequence so the detach never happens.
    - The two commands are joined by a literal `\;` argument. A bare `;` is consumed as a top-level separator by the `tmux bind-key` invocation itself, so `detach-client` would run at bind time instead of being bound.
    - `tmux send-keys` **cannot** test key bindings — it injects into the pane and bypasses key-table dispatch entirely. Drive an attached client's pty instead (`pty.fork()`, then `os.write(fd, b"\x01f")`).
  - The flag is cleared on read, so a later ordinary detach of the same session can't re-trigger the fork. `main()` keys it on `tmux_name or session_name(project, working_dir.name)` — the same name `launch_claude_in_tmux` derives — so a freshly created worktree (whose `tmux_name` is `None`) is matched correctly.
  - The whole flow funnels into the shared `_finalize_create` / `_do_create_and_launch`, so forks inherit the directory-conflict handling (`_show_conflict`, `conflict-suffix`) for free. A non-`None` `_fork_source` is the only difference; `_show_create_form` clears the fork state so a cancelled fork can't leak into the next plain create.
- **Resume previous session — tmux naming follows the directory, not the row.**
  `_resume_target(project, cwd, session=None)` is the single decision, used by
  both `_launch_resume` (the worktree/resume pickers) and `sa-resume` (a
  `claude` row from the home screen or a search hit). If `cwd` is one of the
  project's worktrees and that worktree's tmux name is free, the session takes
  the worktree's name and type `worktree`; otherwise it falls back to
  `session-N`/`direct` (`direct-N` before session titles). A worktree row also names its own worktree when the
  transcript's `cwd` is no longer one of the project's. The working directory
  always comes from `cs.cwd` (the directory recorded in the Claude session log)
  rather than `session.path`.
  **This used to be decided from the row's `session_type`**, and `sa-resume`
  hardcoded `direct`. Resuming a transcript found by search — whose `cwd` is
  whatever worktree it ran in — therefore launched a `direct-N` session inside
  that worktree. The worktree's own tmux name stayed free, so the home screen
  still showed the worktree as idle, and resuming it again started a *second*
  `claude --resume` on the same transcript. Two live processes, one JSONL.
- **A row's conversation is looked up by session, not by directory.**
  `_claude_for(tmux_name, path, record_id)` prefers, in order, the id Claude
  Code's live registry says that tmux session is writing
  (`_live_claude_ids()`, refreshed with `_claude_cache`), the id recorded at
  launch, and only then the directory's latest transcript.
  `_get_claude_sessions` returns a `ClaudeData` with a `by_id` index for this.
  Keyed by directory alone, two sessions in one directory — two titled
  project-root sessions, or a session beside a worktree's own — each showed
  whichever transcript was written last, and a stopped one resumed the wrong
  conversation. The registry comes first because it follows a `/clear` (new
  session id), which a recorded id does not. Logged as `tui.claude_for
  via=registry|record|path|none` (`log_once` per tmux session).
- **A `direct-N` row is inferred, so its location must be asked for, not
  assumed.** `_build_home_items` treats every active tmux session for the
  project whose name matches no worktree directory as a direct session; nothing
  records where it runs. It used to fill in `path=self._project_cwd or
  Path(".")` and `branch=self._current_branch`, which was wrong for any direct
  session not in the repo root — wrong directory, wrong branch, and Claude
  state read from the *root's* latest transcript. `_direct_session_cwd` now
  resolves it from `get_session_path` (tmux), then the `session_state` record
  (skipping a relative `cwd` — that string is the old bug's own output), then
  the project root; the label names the directory when it differs from the
  root. The lookup runs subprocesses, so it is memoized in `_direct_cwd_cache`
  (cleared by `_init_git_info`) — see "Nothing in the home render path may
  touch the disk".
- **Home list fills the screen**: `#home-panel` and `#home-list` are both
  `height: 1fr`, so the session list expands to whatever vertical space `#main`
  leaves after the header, update banner, search box and bottom bar, and scrolls
  internally beyond that. It used to be `height: auto; max-height: 24`, which
  capped the visible rows at 24 no matter how tall the terminal was.
- **Transcript search (`s`)** — the counterpart to `/`: `/` matches session
  *names* from data already in memory, `s` matches transcript *contents*, which
  means reading every JSONL log for the project root and its worktrees. Scoped to
  the current project (not every project on the machine), because a result has to
  resolve to a session this home screen can act on. View is `#search-panel`
  (`#search-input`, `#search-status`, `#search-results`), opened by
  `action_session_search` from the home screen only.
  - **The scan cannot live in a `_build_*_items()` helper.** It runs in a
    `@work(thread=True, exclusive=True, group="transcript-search")` worker
    (`_run_transcript_search`) driving `claude_search.iter_hits`, handing each
    batch of 10 back with `call_from_thread(self._apply_search_batch, ...)`, which
    appends rows and updates the progress line. Results therefore appear while
    the scan is still running, and the event loop only ever does row appends.
  - **`_search_token` is the correctness mechanism, not `exclusive=True`.**
    Cancellation isn't instantaneous: a batch can already be queued on the event
    loop when the query changes or the view is torn down. Every scan bumps the
    token; `_apply_search_batch` / `_search_failed` drop anything carrying a stale
    one. `_start_transcript_search` bumps *before* cancelling the group, and
    `_stop_transcript_search` (called from `_clear_main`) bumps again and cancels.
    **`_clear_search_results` bumps it too, before awaiting `ListView.clear()`** —
    that await is a yield point, and a batch queued on the event loop before it
    was otherwise applied *during* the clear, mounting an `sr-0` that collided
    with the fresh scan's own (`DuplicateIds`, taking the app down mid-search).
  - **A row that won't mount costs that row, not the session.**
    `_apply_search_batch` builds and appends each result inside a `try`, drops
    the `_search_result_map` entry and logs `tui.search_row_failed` (with the
    traceback) on failure, and appends to `_search_hits` only *after* the row is
    mounted — so the `sr-{index}` ids stay in step with the hits a restore
    indexes into.
  - Measured on 302 transcripts / 222 MB (every log on one machine, i.e. far
    beyond a single project): first results at ~50 ms, full scan 1.2–2.2 s, all
    off the event loop. A realistic single project is a handful of logs and
    finishes in tens of milliseconds. Re-measure with
    `search.iter_hits` over `~/.claude/projects/**/*.jsonl` if you change the
    scanning path.
  - `Input.Changed` is **debounced** (`SEARCH_DEBOUNCE = 0.3`) with a minimum
    query length (`SEARCH_MIN_QUERY = 2`); without both, a scan spends most of its
    life being cancelled by the next keystroke. As with `#home-search`, mounting
    the box with a preserved value fires a spurious `Changed`, ignored when the
    value matches `_transcript_query` and hits are already on screen.
  - Three live toggles, all Ctrl chords because they must fire while the `Input`
    holds focus (a plain letter would be typed into it): `ctrl+r`
    (`action_toggle_search_regex`), `ctrl+t` (`action_toggle_search_mode`) and
    `ctrl+i` (`action_toggle_search_case`). Each rescans via
    `_restart_transcript_search`; all three are rendered by
    `_search_status_text`, and none is reset by `_show_session_search`, so a
    choice sticks for the session.
  - **`ctrl+i` is the same byte (0x09) as Tab in a legacy terminal.** It only
    arrives as a distinct key under the kitty keyboard protocol, which Textual
    requests via `ESC [>1u` in its driver: the parser then reads `CSI 105;5u` as
    `ctrl+i` and `CSI 9u` as `tab`. Without that protocol (macOS Terminal.app,
    older iTerm2) Ctrl+I is delivered as `tab` and the binding never fires —
    verified by feeding both sequences to `XTermParser`. Textual's
    `KEY_ALIASES` maps `tab → ["ctrl+i"]`, but the alias does not make a `tab`
    press trigger a `ctrl+i` binding, so there is no false-positive risk (there
    is a test for that). If this needs to work everywhere, move it to a chord
    with no legacy collision.
  - **Snippet rendering** is two module-level helpers. `_fit_snippet` trims a
    snippet to the panel width by *sliding* the window to keep the first match
    visible (a plain right-truncation would cut it off, since the snippet is
    centred on the match with 60 chars of lead-in), shifting the spans and
    dropping any that fall outside; it reserves a column for an elision marker
    at both ends whether or not both are needed, because the alternative is a
    fixed-point loop — adding a marker narrows the body, which can move the
    window, which changes whether a marker is needed. `_render_snippet` then
    assembles `(text, style)` pairs into a `Content`: dim for context,
    `SNIPPET_MATCH_STYLE` (`b $warning`, theme-resolved) for each match.
  - **The view opens in `ContentMode.TEXT`.** Message text is quieter (a common
    word cannot hit a JSON key, a session uuid or tool output) and in practice
    faster than raw: the whole-file `re.search` reject runs first either way, so
    `TEXT` only pays for JSON parsing on the files that already matched, while
    `RAW` pays for snippet building on far more lines. Neither toggle is reset by
    `_show_session_search`, so a switch to raw sticks for the session.
  - Selecting a result opens the **existing** session-actions menu with a
    `session_type="claude"` `SessionInfo` (path from `cs.cwd`), so Resume / Open
    terminal / Open in VS Code come for free. `_show_session_actions(...,
    from_search=True)` records where it was opened from in
    `_actions_from_search`, so `sa-cancel` and `action_go_back` return to
    `_show_session_search(restore=True)` — re-rendering the collected
    `_search_hits` rather than throwing a completed scan away. Opening the menu
    from the home screen clears the flag, so that path still returns home.
  - **A restore focuses the results list, not the query box, and puts the
    highlight back on the row that was opened** (`_search_selected_index`,
    recovered from the `sr-{index}` row id and cleared by `_clear_search_results`
    when a fresh scan starts). Focusing the input on the way back meant every
    Escape dropped the user at the top of the list, which makes walking a set of
    results one at a time impossible. A restore with no hits still focuses the
    input, since there is nothing to highlight.
- **Home name filter rebuilds rows, not the screen**: `_show_home` mounts the panel;
  all row construction (and the population of `_session_map` /
  `_claude_state_snapshot`) lives in `_build_home_items()`, which applies the
  current `_search_query`. Filtering therefore only clears and re-appends the
  `ListView` children — the search box keeps focus and its cursor. Because
  `_session_map` holds only the visible rows, `_poll_session_states` naturally
  updates just the filtered set. The search box is always mounted with
  `display` toggled (never remounted), so `/` can't lose typed input; mounting
  it with a preserved value fires a spurious `Input.Changed`, which
  `on_home_search_changed` ignores by comparing against `_search_query`. Arrow
  keys while the box is focused are handled in `_on_key` and skip disabled
  separator rows (as does the post-filter default highlight, via
  `_first_selectable_index`).
- **Nothing in the home render path may touch the disk.** `_build_home_items`
  runs on every `/` keystroke, and it used to call `_get_claude_sessions`
  directly — re-parsing every JSONL transcript per character typed, which is what
  made the filter lag behind typing. It now reads `_claude_session_data()`
  (memoized in `_claude_cache`; invalidated by `_init_git_info` on project switch
  and by `_show_home` on re-entry, and *refreshed* rather than bypassed by
  `_poll_session_states` so the next keystroke renders from freshly polled data)
  `_is_fork()` (memoizes `_is_fork_worktree`'s `meta.json` read in
  `_fork_marker_cache`; fork provenance never changes) and
  `_direct_session_cwd()` (memoizes the tmux + git lookups behind a `direct-N`
  row in `_direct_cwd_cache`; a tmux session's start directory never changes).
- **Live polling**: The home screen uses `set_interval(3s)` to poll Claude JSONL logs for state changes. When a row's `(claude_session_id, state)` changes, its label is updated in-place via `label.update()` — the screen is never cleared or rebuilt, which avoids blank-screen flicker. The timer is stopped when navigating away (`_clear_main` cancels it) and restarted by `_show_home`.
- **Broadcast delivery uses documented hooks, not keystrokes or Claude's socket.** Typing into a tmux pane (`send-keys`) can answer a permission prompt and is never as robust as Claude's own messaging; the cross-session socket is undocumented and key-gated. An `async` + `asyncRewake` hook exiting 2 is documented to wake even an idle session, so `broadcast.py` is built on that (spiked against live sessions first — see the Broadcast gotchas).
- **Broadcast trust is signatures, not file permissions.** Anything running as the user can write the mailbox, so a waiter delivers only what verifies against signers fixed into its hook command at launch. Signing is plain `ssh-keygen -Y` so any key custody works (an SSH agent such as 1Password, a key file, a hardware key); fujimoto stores only public keys. A project can only opt *out* (`broadcast: false`): letting `.fujimoto.yaml` name a signer would let a repo trust its own key.
- **One session is a `send`, not a broadcast.** `fujimoto send SESSION TEXT` is top-level rather than a `broadcast send` flag. The target is resolved when sending and the exact session id is signed, never the name typed — a tmux or peer name can be reused by another session before the message expires.
- **MCP server: reads in-process, writes by subprocess.** On a stdio server, stdout *is* the protocol stream, and the launch path redirects fd 1 and starts tmux and claude. A child process can't write into the stream, and the mutating tools depend only on the CLI's `--json` contract, not on `cli.py`'s private `_prepare_*` functions. The reads have no such risk and are frequent, so they call `session_query` directly.
- **MCP server: the `mcp` SDK is an optional extra.** It pulls in anyio, httpx, starlette and uvicorn, which every other fujimoto user would carry for nothing. Imported lazily, so a missing extra is an install hint, not a traceback.
- **MCP server: its own agent key, held by an agent, passed as a public key.** Messages from Claude Desktop must not read as typed by the user, so the server signs as an `agent` signer (`claude-desktop` by default) and refuses a `user` signer's key. It always hands `send` the literal public key: signing then always goes through an SSH agent, never a private key file (whose passphrase prompt would need a terminal the server doesn't have). Key custody is the user's choice (any SSH agent, key manager or hardware key), not tied to one product. `fujimoto mcp setup` only checks and prints commands, never changes keys or config, so each security-relevant step is taken knowingly. An existing off-machine agent's key is not reused: it is meant to live off this machine, and reusing it would make Desktop's messages read as that agent's.
- **MCP server: no relayed approvals (yet).** The tools have no `approval` parameter. Any local process that reaches the SSH agent can sign as `claude-desktop`, so its approvals would be weaker than an off-machine signer's. Add it only once there's a decision on which signers' approvals count.
- **MCP server: ChatGPT means the desktop app, never a public endpoint.** ChatGPT on the web and mobile only reach remote MCP servers over public HTTPS. Serving fujimoto that way would expose session control (start, stop, send) through a tunnel, so `setup chatgpt` only configures the desktop app's local stdio servers (`~/.codex/config.toml`, shared with Codex). The TOML is edited as text, limited to fujimoto's own tables and verified by re-parsing, rather than with a TOML library, so a file the ChatGPT app manages keeps its exact formatting and no dependency is added.
- **MCP server: read tools answer in the CLI's text by default.** `list_sessions`, `search_sessions` and `read_transcript` take `format: "text" | "json"`. The CLI text was written for agents and is far smaller than the JSON, which matters when a client checks many sessions. Reusing `format_*` keeps the tools in step with the CLI. Returning text and JSON together (text content plus `structuredContent`) was rejected: clients may pass both to the model, and an output schema is supposed to come with JSON text anyway. `fields` implies JSON, since field selection only makes sense there.
