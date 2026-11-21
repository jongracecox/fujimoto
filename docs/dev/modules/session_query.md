# `session_query.py`

**`session_query.py`** — The read-only `search` / `log` commands, UI-free:
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
  needs the directory to exist.
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
- Instrumented as `query.scope` (`via=cwd|path|git-root|all`), `query.worktrees` (`existing`/`deleted`), `query.search`,
  `query.activity` (capped, with the matching rule as `reason=`),
  `query.locate`, `query.log_resolve` (`via=path|exact|prefix`) and
  `query.log`; `claude.live_sessions` reports `live`/`stale`/`unreadable`
  counts — `unreadable` climbing is what a registry format change looks like.

- `wait_for_registration(tmux_session, timeout=)` polls `live.list_live_sessions()` for the entry whose `tmux` pane is in that session (injectable `sleep`/`clock` for tests; `timeout=0` checks once); `resolve_session(ref)` and `tmux_for_session(ref)` back `resume` and `stop`.
