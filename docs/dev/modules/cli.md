# `cli.py`

**`cli.py`** — Textual TUI with async view management:
- `PagingListView` — `ListView` subclass used for **every** list in the TUI. Textual's `ListView` binds only up/down/enter, so PgUp/PgDn/Home/End fell through to the scroll container: the view moved, the highlight didn't, and the next arrow key snapped back to the old row. Here `action_page_cursor` moves the highlight by as many rows as fit in the viewport (summing each row's `outer_size.height`, since search results are multi-line) and `action_edge_cursor` jumps to the first/last row; both land on the nearest enabled row so separators are skipped. How far a page moves is `PagingListView.page_step` (a `ClassVar[PageStep]`, see `paging.md`), shared by every list: `SessionApp._load_page_step()` sets it from the `page_step` global config key in `on_mount` and on refresh `r`, never in the render path, and falls back to the 50% default when the config is invalid. A percentage counts row heights until they fill that share of the viewport (at least one row); a row count moves exactly that many rows Build new lists with it, not `ListView` — queries by `ListView` type still match it
- `LogBody` — frozen dataclass `(widget, text, folds)`: one highlightable body in the log viewer, its source text, and the `Collapsible`s that hide it. `Spans` is the alias for its `(start, end)` match offsets
- `SessionInfo` — dataclass for session state (type, project, path, tmux name, active status, claude_session_id, claude_state, is_fork)
- `LaunchTarget` — `NamedTuple` describing what `main()` should launch: `(project, working_dir, tmux_name, session_type, resume_session_id, forked_from_session_id, forked_from_worktree, claude_args, session_id, prompt)`. `claude_args` is `None` unless the launch options dialog (or `fujimoto new -- …`) chose them for this launch; `session_id` and `prompt` are set only by `fujimoto new`. A `NamedTuple` rather than a dataclass so existing index-based access keeps working.
- `SessionApp` — main app class with CSS styling
- Module-level helpers: `_claude_state_label(state)`, `_relative_time(dt)`, `_get_claude_sessions(root, worktrees)`, `_is_fork_worktree(path)`, `_build_fork_system_prompt(project, working_dir, parent_worktree, base_branch)`, `_fit_snippet(snippet, max_width)` / `_render_snippet(snippet, max_width)` (search-result snippet rendering — see the `Content.assemble` gotcha in [gotchas.md](../gotchas.md)), `_match_spans(text, matcher)` / `_highlight(text, spans)` / `_log_body(text, folds)` (log-viewer match highlighting, split so the regex half can run in a worker thread and only the `Content` half touches widgets), `_tool_collapsible(title, …)` (a `Collapsible` whose title is `Content`, never markup)
- Instance helpers: `_build_session_label(session, state_suffix)` — the single source of truth for session row text (including the 🍴 fork marker), used by `_show_home`'s render of direct *and* worktree rows and by `_poll_session_states` for in-place updates; `_build_claude_session_items(sessions, prefix)` — shared row rendering for the resume (`rp-*`), fork (`fp-*`) and log-viewer (`lp-*`) pickers; `_matching_worktree(path)` — the project worktree a path refers to, compared by `resolve()`; `_direct_session_cwd(tmux_name)` — where a `direct-N` row really runs and on what branch (memoized in `_direct_cwd_cache`); `_resume_target(project, cwd, session=None)` — the tmux name and session type a resume should use
- Views: home (sessions list), session actions submenu, terminate/park/stop prompt (`#terminate-prompt`, opened by a pending `close` from `Ctrl-A x`; Terminate / Park / Stop / Cancel with Terminate highlighted so Enter matches the `confirm-before` it replaces, and Cancel re-attaching via `_launch_target` so it costs nothing), finish flow, confirm dialog, create form, branch select (3 options), branch picker (filterable list), fork title form, fork branch select, fork session picker, session log picker + read-only log viewer, conflict resolution, project switcher (with autocomplete filter), tmux install, error
- Transcript search (`s`) — see "Transcript search" in [design-decisions.md](../design-decisions.md).
- Home screen name filter: `/` arms a filter box (`#home-search`) mounted above
  `#home-list`. `action_search` reveals + focuses it; `Input.Changed` re-renders
  the rows via `_refresh_home_list`; `Input.Submitted` keeps the filter and moves
  focus to the list; Escape (in the box, or on a filtered list via
  `action_go_back`) clears it through `_clear_search`. Matching is
  case-insensitive substring (`_search_matches`) over session/worktree name,
  branch and tmux name, and applies to the active, inactive and previous-Claude
  sections. A non-empty query hides the action/settings/switch-project rows and
  any section with no matches; an empty result set renders a disabled "no
  matching sessions" row.
- Home screen refresh (`r`): `action_refresh` re-runs `_init_git_info()` (tmux
  sessions, the `session_state` store, worktrees, projects — and it clears
  `_claude_cache`/`_fork_marker_cache`) then `_refresh_home_list()`, so a
  worktree or tmux session created outside fujimoto appears without leaving the
  screen. The 3s poller deliberately doesn't do this: it only updates Claude
  state on rows that already exist. The highlighted row's id is captured before
  the rebuild (appending rows moves the highlight) and re-applied after; the
  `/` filter is preserved because `_refresh_home_list` re-applies
  `_search_query`. Guarded on `_on_home`, so `r` is inert in other views.
- Home screen first-prompt line (`p`): `action_cycle_prompt_mode` steps
  `_prompt_mode` (`PromptMode` OFF → LINE → FULL → OFF) and rebuilds the rows
  via `_rebuild_home_rows_keeping_highlight` (shared with `r`). Every session row
  is built by `_session_row(label, id, prompt)`, which adds a
  `.session-prompt` `Static` under the `Label` when `_prompt_text` returns
  something — the first non-blank line, or the whole stripped prompt — taken
  from the `first_prompt` of the row's own conversation: `_claude_for` for
  running and open rows (so two sessions sharing a directory show their own
  prompts), `by_id` for closed chats, the directory's latest for an inactive
  worktree. The prompt is transcript
  text, so it is `Content`, never markup. LINE clipping is CSS, not code:
  `#home-list` carries a `prompt-<mode>` class and `.prompt-line .session-prompt`
  is `text-wrap: nowrap; text-overflow: ellipsis`, since the width is only known
  at render time. The poller's in-place update calls `_update_row_prompt`, which
  adds, updates or removes the line, because a changed id is a different
  conversation. The mode is deliberately per `SessionApp` (starts OFF on every
  TUI appearance), not persisted. Logged as `tui.prompt_mode`.
- Home screen sections: actions ("New worktree session", "New project-root session (⎇ <root branch>)" — `_root_branch`, the main clone's branch, not the cwd's, "New chat"), recovered 🔄 (only when there are any — see [Session status](../sessions.md#session-status)), sessions — running 🟢, parked 🅿️ and stopped 🟠 together, since the icon carries the distinction (with Claude state indicators on the running ones), previous sessions (⚫ inactive worktrees, closed chats and up to 5 project-root conversations no row claims — one section, most recently used first, built in one pass of `_build_home_items` with `_last_used`), switch project
- Worktree create flow: title → branch select (default w/ fetch & rebase, current branch, another branch → picker) → create
- Session actions submenu (in order): for active sessions, Connect → Fork session → Resume previous session; for inactive worktrees, Resume previous session → Fork session → Launch (resume is the more common action when picking an idle worktree). Then: View session log (whenever the path has a previous Claude session), Open terminal, Open in VS Code, Rename, Park session and Stop session (active only), Terminate session (active or stopped), Finish (worktree only), Cancel. Park, Stop and Terminate are separate menu items rather than one item plus a prompt — a menu is already a choice — but all route into the single `_end_session(session, terminate=..., park=...)` handler, which is also what the `Ctrl-A x` prompt calls. Claude-session items show just "Resume" + View session log + Open terminal/VS Code + Launch options + Cancel. "Launch options" sits after Rename for every session that has somewhere to save them (see "Launch options" in [design-decisions.md](../design-decisions.md)). "Fork session" is always inserted at index 1 (`items.insert(1, ...)`) so its position holds across both layouts.
- "Resume previous session" auto-launches the sole candidate when only one previous Claude session exists for the path, skipping the picker. Two or more sessions still show the picker.
- Fork flow: `sa-fork` → `#fork-title-input` → `#fork-branch-list` (parent branch (default) / parent's base / another branch → the shared `_show_branch_picker`) → conversation picker `#fork-picker` (`fp-{i}`, only when >1 candidate) → the shared `_finalize_create` / `_do_create_and_launch`. Offered for worktree *and* direct sessions that have at least one previous Claude session.
- Session log viewer: `sa-viewlog` → `_show_log_picker` → `_show_session_log`. The
  picker (`#log-picker`, rows `lp-{i}` via the shared `_build_claude_session_items`)
  is skipped when the path has a single transcript, and for a `claude` row, which
  already names one (matched by `session_id`). `_show_session_log` renders
  `read_transcript` output into `#log-panel` as a pair of `Static`s per message
  (`.log-role` + `.log-body`, coloured per role by `_TRANSCRIPT_ROLES`); bodies are
  `Content`, not markup, for the same reason search snippets are — transcript text
  is arbitrary bytes. Tool calls and results are the exception: they are the bulk
  of a transcript by volume and the least of it by interest, so each becomes a
  one-line `Collapsible` titled by `_tool_summary` (tool name + clipped first
  argument), collapsed by default and reachable with Tab + Enter. A `tool_use`
  body drops its first line, which the title already carries, and **its result is
  rendered inside that same expansion** (arguments, a `↳ N lines` divider, then
  the output) rather than as a row of its own — a call and its reply are one
  thing to open. `_pair_results` matches them by `tool_id`, falling back to the
  next unclaimed result for logs without ids; parallel calls arrive as several
  `tool_use` blocks followed by several results, where position alone mis-pairs
  them. A result nothing claims still gets its own row, so no transcript content
  is silently dropped. **A *run* of consecutive tool messages nests inside one outer
  `Collapsible`** (`.log-tool-run`, titled by `_tool_run_title` — call count plus
  up to four distinct tool names) once it holds more than one call, so a session
  that made twenty calls in a row costs one screen row rather than twenty; a lone
  call stays unwrapped, since wrapping two rows in a third helps nobody. Runs are
  delimited by the prose between them. `.log-tool` zeroes Textual's default
  Collapsible `padding-bottom` and `border-top`, which otherwise read as gaps
  between messages, and `.log-tool-run .log-tool` drops the top margin again
  inside an opened run, where the calls are a list rather than separate
  messages. The messages live in their own `VerticalScroll` (`#log-messages`)
  inside `#log-panel` rather than directly in `#main`, so the header, the `/`
  box and the hint stay put while the transcript scrolls; focusing it gives
  arrow/page/end scrolling for free, and Escape falls through to `action_go_back`,
  which returns to the search results when the menu came from a search and to the
  home screen otherwise. Nothing is launched — this path never sets
  `_launch_target`.
- **Log viewer search (`/`)** — the same three-axis matcher as `s`, applied to
  the one transcript on screen. `_show_session_log` reads and parses the log
  once; `_render_log_view` mounts the panel once. **Searching never comes back
  through either.** Mechanics worth knowing:
  - **Nothing is remounted for a query.** `_render_transcript(messages)` returns
    `(widgets, bodies)` where a `LogBody` is `(widget, text, folds)` — the
    `Static` for one body, its source text, and the `Collapsible`s that hide it.
    A query then only calls `Static.update()` on the bodies whose spans changed.
    Measured on a real 838-message log: a full rebuild is ~540 ms (visible as
    the query box vanishing mid-keystroke), while a rescan is ~1.7 ms of
    matching plus ~6 ms of `Content` building.
  - **Matching runs in a thread, applying runs on the event loop.**
    `_start_log_search` bumps `_log_search_token`, cancels the `log-search`
    group and hands `[body.text …]` plus the matcher to `_run_log_search`
    (`@work(thread=True, exclusive=True)`), which returns `list[Spans]` — pure
    data, no widgets — via `call_from_thread(self._apply_log_search, …)`. As
    with the transcript search, **the token is the correctness mechanism**, not
    worker cancellation: a scan can already be queued when the query changes.
  - **`_match_spans` and `_highlight` are separate for exactly that reason** —
    the first is regex work that can happen anywhere, the second builds
    `Content` and must not. Zero-width matches are dropped, since there is
    nothing to highlight or scroll to.
  - **`_apply_log_search` accumulates fold state before applying it.** A fold
    opens if *any* body inside it matched, so a run holding one hit and nine
    misses would be re-closed by the misses if each body decided alone. Folds
    are keyed by `id()` (widget equality is not identity) and assigned only on
    a change, since each assignment costs a refresh. Everything with no match
    folds back up, so each query starts from the same view.
  - **`_log_matches` is the list of matching body `Static`s** (document order,
    since `_log_bodies` is). `_step_log_match(±1)` wraps, swaps the
    `.log-match-current` class and calls `scroll_to_widget`; starting from `-1`
    is what makes the first `n` land on the first match and the first `N` on
    the last. `_stop_log_search` (from `_clear_main`) drops the lists and bumps
    the token — the entries are live widgets from a removed subtree.
  - **`n`/`N` are plain letters, unlike the `s` view's Ctrl chords**, because
    the `Input` swallows them while it holds focus — which is also why
    `Input.Submitted` moves focus to `#log-messages`, and why Textual withholds
    them from the footer while the box is focused. `ContentMode` is not offered:
    the transcript is already parsed, so there is no raw JSON to scan.
  - **The viewer mounts no `.hint` row**, unlike every other view. `/` and
    Escape are already in the footer, and a second strip of grey text below a
    screenful of transcript reads as a second footer rather than as help.
  - **The query box is built with `select_on_focus=False`.** Textual selects an
    `Input`'s whole value on focus by default, which ate the query whenever
    focus came back to the box.
  - Escape is layered: `_on_key` clears the search when the box has focus, and
    `action_go_back` clears it before leaving the view when it does not.
  - **Instrumented as `log_search.start` / `.done` / `.dropped`** (plus
    `tui.log_view` when the viewer opens). `.done` reports `repainted`, which is
    the number that says whether the in-place update is doing its job — a value
    close to `bodies` means the skip-unchanged check has stopped working. Its
    fields walk every body, so the whole call sits behind `debug.is_enabled()`.
- **Raw fallback for a transcript the parser can't structure.** Claude's entry
  shapes evolve, so `_show_session_log` treats an unexpected exception out of
  `read_transcript` as "show it anyway": `read_raw_transcript` returns the log's
  lines as written (blank lines dropped, each clipped to
  `_MAX_RAW_LINE_CHARS`), `_log_parse_error` records `Type: message`, and
  `_render_log_view` puts a `#log-raw-warning` banner above one `_log_body` per
  line — so `/`, highlighting and `n`/`N` all work unchanged. A `ClaudeLogError`
  is still a real error screen: there is nothing to fall back *to* when the file
  cannot be read. `search.iter_hits` swallows the same class of failure per log,
  so one bad transcript cannot end a scan over hundreds.
  Both swallowing paths are the ones `--debug` most needs to see, since neither
  is visible in the UI: the viewer logs `claude.transcript_unparsed` **with the
  traceback** plus `tui.log_raw_fallback`, and `iter_hits` logs
  `search.scan_failed` per log and a `failed=` count on `search.scan phase=done`.
  An unknown entry shape is a parser bug to fix, not just a view to degrade.
- **Rendering is the other half of the same risk, and falls back the same way.**
  A transcript can parse cleanly and still carry text a widget refuses — a
  `Collapsible` title is parsed as markup, so a stray `[` raised `MarkupError`
  out of `_render_log_view`, well past the parse. `_show_session_log` therefore
  wraps the render too: an exception from it drops `_log_messages`, sets
  `_log_parse_error` and re-renders raw, which builds nothing but `Static`s. A
  failure of the *raw* render is an error screen (`tui.log_render_failed` in the
  debug log), since there is nothing simpler left to draw.
- **A parse failure must not reach the home render path.**
  `get_sessions_for_path` runs on every render, and it caught only
  `ClaudeLogError` — so an `AttributeError` from one unknown entry shape took
  the whole TUI down rather than costing that one transcript. It now catches
  `Exception` per log, counting it into the same `failed=` field and logging
  `claude.session_unparsed` **with the traceback**. Same contract as
  `iter_hits`, for the same reason.
- **`check_action` makes the footer context-aware.** `s` and `r` show only on
  the home screen, the mode toggles only where they toggle something, and `n`/`N`
  only when a log search has matches to step through. Two details:
  - **Return `False`, never `None`.** Textual's polarity is the opposite of what
    it reads like: `False` drops a binding from the footer entirely, `None`
    shows it greyed out.
  - **`/` is two bindings on one key** — `search` ("Filter") and `log_search`
    ("Search") — because a binding's description is static, and one action with
    a switch inside it would label the log viewer's search "Filter". Textual's
    `_check_bindings` walks every binding for a key and falls through the ones
    whose `check_action` is false, so exactly one fires.
  - **The mode toggles are `show=True`.** They were hidden from the footer back
    when they would have appeared on every screen; `check_action` now confines
    them to the two views they act on, and a Ctrl chord — unlike `n`/`N` —
    still reaches the footer while the query `Input` holds focus, which is
    exactly when someone needs to discover them.
  - It is only consulted when something calls `refresh_bindings()`, which
    `_clear_main`, `_show_home`, `_show_session_search`, `_render_log_view` and
    `_apply_log_search` all do.
- Finish flow: Push & Create PR (background Claude), Cherry-pick to base, Discard & Delete
- Open terminal flow: sub-menu with "This window" (default; uses Textual's `App.suspend()` to pause the TUI, then runs `subprocess.run([$SHELL], cwd=session.path)` as a child process — when the user types `exit`, the TUI resumes on the session actions menu) and "New window" (spawns a new iTerm/Terminal/Linux emulator window via `open_terminal()`)
- All view transitions are `async` — `await _clear_main()` then `await mount()`
- Session data stored in `_session_map` dict keyed by ListItem ID
- `_launch_target` is a `LaunchTarget`, set before `self.exit()`
- **TUI `b` view** (`cli.py`): `action_show_messages` (home only, via
  `check_action`) → `_show_messages` reads `broadcast.status()` once into
  `_message_rows` and lists `_message_row_content` rows (`msg-{i}`); Enter →
  `_show_message_detail` (full text + receipts in `#message-detail`); Escape
  goes detail → list (`restore=True`, highlight kept in `_message_selected`)
  → home. Everything built from message text, reasons or paths is `Content`.
  Flags `_on_messages`/`_on_message_detail` are reset by `_clear_main`.
  Instrumented as `tui.messages` and `tui.message_detail`.
