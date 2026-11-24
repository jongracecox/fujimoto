# `session_query.py`

**`session_query.py`** — The read-only `list` / `search` / `log` commands, UI-free:
- `resolve_scope(project=, all_projects=, cwd=)` → `Scope`. No flag: the main
  clone of the cwd's repo (`get_main_worktree_root`, so a worktree searches its
  siblings). `--project`: a directory (any path in the repo), else a name under
  `$FUJIMOTO_GIT_ROOT`. Worktrees are the home screen's set — the children of
  `get_project_worktrees_dir(..., create=False)` **plus deleted ones**:
  `log_parser.recorded_cwds_under(worktrees_dir)` finds every recorded `cwd`
  below that directory, and each immediate child with transcripts but no
  directory joins `Scope.worktrees`, so its hits read `cwd_exists: false` /
  `(deleted)` but still name the worktree. Candidates are session dirs whose
  encoded name has the worktrees dir's encoding as a prefix, *confirmed by the
  transcript's own `cwd`* — the encoding is lossy, so `/wt/proj` is a prefix of
  `/wt/proj-other`. Only the current worktrees root is searched; the TUI's `s`
  deliberately does not include deleted worktrees, since its actions menu
  (Resume, terminal, VS Code) would all fail on a missing directory.
  **`create=False` matters**:
  the default creates `<root>/.fujimoto/`, and a search must not plant that in
  a repo it only looked at. `--all-projects` uses `search.list_all_session_logs`
  (every top-level `*.jsonl` under the projects dir), with no path mapping.
- **Outside a repo.** `resolve_scope(..., outside_repo_all=True)` turns "no
  flag, cwd not in a repo" into `Scope(all_projects=True, outside_repo=True)`
  (`describe()` → `all projects (not in a repo)`, `outside_repo` in JSON)
  instead of a `QueryError`. Only the cwd path falls back: an explicit
  `--project` that is not a repo still fails, and `chat` doesn't opt in.
- **`list`** (`run_list`) reads the scope's transcripts in mtime order,
  `parse_session` each, and stops at `limit` listed sessions, so the rest are
  never opened (`truncated` = transcripts left unread). `running_only` keeps
  reading until `limit` *running* ones are found. Unparseable (empty)
  transcripts are counted as `unreadable`, not listed. The calling session is
  *marked* (`this session`, `"this_session"`), not left out as `search` does:
  a listing missing the newest session would just look wrong. Text rows lead
  with an 8-character id prefix in its own column (`find_session_log` accepts
  it, so `list`, `log` and `resume` do), and show the running
  session's address (`_list_activity`: status + peer, else tmux). The label
  column is padded to at most `_LABEL_WIDTH_CAP`, because one scratch-dir path
  padded every row past the screen edge.
- **`list` filters and named sessions.** `run_list(scope, *, limit,
  running_only, waiting_only, states, refs, calling_session, activity)`.
  `waiting_only` uses `is_waiting(session, activity)`: for a registry
  match, `waiting_for` set or `status == "idle"`. The transcript `state`
  (`waiting_for_user`/`waiting_for_tool_approval`) is only the fallback for a
  tmux-only match or a registry entry with neither field, because an
  unanswered `tool_use` looks the same whether it awaits approval or is still
  running. `states` filters on `ClaudeSession.state`. Filters, like
  `running_only`, keep reading until `limit` matches. `refs` replaces the
  scope walk: each ref goes through `_resolve_ref` (an exact running peer name
  via `ActivityIndex.session_ids_for_peer`, else `find_session_log`), is
  deduplicated, and is listed in the order given with no `limit` and no sort.
  An unknown, ambiguous or unreadable ref is a `QueryError`, not a skipped
  row. `query.list_ref` logs `via=peer|log`.
- **`--fields`.** `parse_fields(text)` validates against `LIST_FIELDS` (the
  session row's keys plus `active.<key>`; a test checks that every name is a
  real key) and raises `QueryError` naming the valid ones.
  `select_fields(row, fields)` is pure: it keeps the nesting, returns null
  for a valid field the row lacks (`active` of a stopped session is only
  `{"running": false}`), and lets a whole `active` win over its sub-paths.
  `list_json(report, fields)` applies it to the session rows only, always
  putting `session_id` (the full id) first: a cut-down row with no id can't be
  acted on, and filtered rows arrive in activity order. JSON keeps the full id
  because Claude's own `--resume`, transcript names and
  `$CLAUDE_CODE_SESSION_ID` need it, and a prefix is only unique for now. The
  envelope keeps its keys and adds `waiting_only`, `states` and `refs`.
  `log_json(report, fields)` does the same for a log, against `LOG_FIELDS`:
  the session header's fields (no `this_session`), the log's own keys
  (`cursor`, `messages`, `lines`, `delivered_index`, ...) and one level into
  `summary`, and down to a summary message's keys
  (`summary.last_assistant.text`). A link or a `messages` entry comes back
  whole. `select_fields` walks any depth, drops a path whose ancestor is also
  asked for, and returns null for a valid path the row lacks (a summary with
  no `replied_to`). Both go
  through `cut_row`, which puts `session_id` first. `parse_fields(text,
  valid)` defaults to `LIST_FIELDS`. A test builds every log shape (plain,
  summary, reply-to, raw) and checks that every `LOG_FIELDS` name is a real
  key. The
  MCP server calls these directly, so none of them may print or exit.
- `--sort relevance` (`SortOrder.RELEVANCE`) disables the early stop, sorts
  every hit by `relevance_key` — `(message matches, tool matches,
  last_activity)` — and *then* applies `limit`, so `truncated` means "more
  hits than shown". It deliberately ignores the raw match count: a single Jira
  search listing in tool output can out-count a session that worked on the
  ticket all day, and message text is what tells them apart. In `TEXT` mode
  every match is a message match, so the key reduces to message count.
- `run_search(...)` drives `iter_hits` and, in the default `recent` order,
  **stops at the first batch that reaches `limit`** — logs are mtime-descending, so the first N hits are the
  newest N, and the rest need not be read. `truncated` means hits were cut *or*
  logs were left unscanned ("more may match"); `limit=0` scans everything.
  Location and activity are computed only for the hits that are returned.
- `Locator` maps a transcript `cwd` to `Location(project, project_root,
  worktree)`, memoized per cwd. In a project scope it uses the scope (so a
  deleted worktree is still named); across all projects it asks git, which
  needs the directory to exist. For a deleted directory, or one git rejects
  (a chat's scratch dir), `_layout_location` reads fujimoto's own layout
  from the path: `$FUJIMOTO_WORKTREE_ROOT/<project>/<worktree>`,
  `<root>/.fujimoto/worktrees/<worktree>`, or `~/.fujimoto/chats/<project>`
  (project, no worktree). The project root is filled in from
  `$FUJIMOTO_GIT_ROOT/<project>` when that exists.
- `ActivityIndex` is gathered once per command (`live.list_live_sessions()`,
  one `tmux.list_session_paths()` call, `session_state.load_state()`) and
  matched per transcript: (1) the registry has a process writing this session
  id → running, source `claude-registry`, with pid/peer name/status/tmux
  session; (2) the registry has a process in this cwd writing a *different*
  conversation → not running, whatever tmux says; (3) a tmux session in this
  cwd whose record's `claude_session_id` is this id — or, with no recorded id,
  this is the cwd's newest transcript by mtime → running, source `tmux`.
- `find_session_log(ref)` — a `.jsonl` path, an exact id, or a unique id
  prefix (glob-escaped); ambiguity is a `QueryError` listing candidates.
- **The calling session is excluded from `search`.** `calling_session_id()`
  reads `$CLAUDE_CODE_SESSION_ID` (`CALLING_SESSION_ENV`), and `run_search`
  drops that hit *before* the limit, reporting `excluded_session` only when it
  actually matched. `--include-self` turns it off. Without this an agent
  searching for "the thing I was just asked about" always finds itself first.
- **`log` views.** Every message is carried as `(index, message)`, `index`
  being its position in the whole transcript, so a passage keeps its number
  across views (`### role · #N · time`, `"index"` in JSON). `_classify`
  re-roles harness-injected "user" entries (`_INJECTED_PREFIXES`:
  task notifications, system reminders, slash-command output) as `injected`,
  hidden unless `--roles` names it (`DEFAULT_ROLES`). `_select` then applies
  `--grep` (the same `compile_matcher`, TEXT mode) with `-C` context
  *in the role-filtered list*, recording `breaks` (indices printed after a
  gap, drawn as `⋯`), then `--head`/`--tail`. `total` is the role-filtered
  count; `matched` the grep count.
- **Incremental reads** (for an agent waiting on a reply). Every report
  carries `cursor`, the index of the transcript's last message (or raw line);
  `after=N` keeps only indices above N. `reply_to=ID` (`_reply_span`) finds
  the injected message whose text has `id <ID>` — the broadcast preamble puts
  it there — and keeps what followed, up to the next `user` or `injected`
  message, so the following turn or another broadcast is never taken for the
  reply; `delivered_index` is None until the delivery appears. Both apply
  before the role filter. `load_log_waiting(load, wait=)` repeats a `load_log`
  call every second until it shows something or `wait` runs out (`--wait`).
  `--summary` with either is a usage error. Instrumented as `query.reply_to`
  and `query.log_wait`.
- **`log --summary`** (`summarize`) is the first user prompt, the final
  exchange (`replied_to`: the assistant reply before the last user message,
  since that is often just "yes, do it"; `last_user`; `last_assistant`), role
  counts, and links. `extract_links` recognises GitHub PRs, Jira issues and
  Slack messages, deduplicated by canonical URL (Jira drops
  `?focusedCommentId`, Slack the query) and counted, with tool-ready ids
  (`repo`/`number`, `key`, `channel`/`ts` with the `p…` id turned back into
  a Slack `ts`). `_message_links` takes them from messages and tool calls,
  and from tool *output* only for PRs: a Jira search result is a listing of
  issues the session never touched (a session that only ran a ticket search is a false lead).
  On a raw fallback, `summarized` is set and only the links are recovered.
- `load_log(...)` uses `read_transcript`, and on any non-`ClaudeLogError`
  exception falls back to `read_raw_transcript` with `parse_error` set — the
  same contract as the TUI viewer. `parse_session` failing only costs the
  metadata header.
- `relative_time` lives here (re-exported into `cli.py` as `_relative_time`)
  so the query module does not import the TUI.
- Instrumented as `query.scope` (`via=cwd|path|git-root|all|all-outside-repo`), `query.list`, `query.locate` (`outcome=layout|deleted-layout|deleted-unknown|not-a-repo`), `query.worktrees` (`existing`/`deleted`), `query.search`,
  `query.activity` (capped, with the matching rule as `reason=`),
  `query.locate`, `query.log_resolve` (`via=path|exact|prefix`) and
  `query.log`; `claude.live_sessions` reports `live`/`stale`/`unreadable`
  counts — `unreadable` climbing is what a registry format change looks like.

- `wait_for_registration(tmux_session, timeout=)` polls `live.list_live_sessions()` for the entry whose `tmux` pane is in that session (injectable `sleep`/`clock` for tests; `timeout=0` checks once); `resolve_session(ref)` and `tmux_for_session(ref)` back `resume` and `stop`.
