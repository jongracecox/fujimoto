# Sessions: status, types and naming

## Session status

Two independent axes, deliberately separated because "Terminate" used to
conflate them:

- **Intent** — open or closed. Only fujimoto ever changes it.
- **Runtime** — whether tmux has a live session. Observed, never stored.

Plus, on an open record with no live session, a third thing that is *recorded*
rather than observed: `SessionRecord.stop_kind`, a `StopKind` saying which way
the session ended.

| intent | tmux | stop_kind | icon | home-screen section |
|---|---|---|---|---|
| open | running | (ignored) | 🟢 | sessions |
| open | stopped | `RECOVERED` | 🔄 | **recovered** (above sessions) |
| open | stopped | `PARKED` | 🅿️ | sessions |
| open | stopped | `STOPPED` | 🟠 | sessions |
| closed / no record | stopped | — | ⚫ | **previous sessions** (worktrees, closed chats, unclaimed root conversations) |

Within the *sessions* section rows are ordered running, parked, then stopped.
Inside each of those groups — and inside *recovered* — rows are
**newest-created first** (see "Every group reads newest-first" in [design-decisions.md](design-decisions.md)).
*Previous sessions* is ordered by **last use** instead (newest transcript
activity, else creation/close time), since it mixes three kinds of row and
"what did I touch last" is the question it answers.

**Every deliberate end writes a `stop_kind`, and that is the entire recovery
mechanism.** Stop and Park both call `mark_stopped`, which stamps
`STOPPED`/`PARKED`; Terminate deletes the record; `mark_open` resets the stamp
to `RECOVERED` on every launch and reconnect. So a record that is open, has no
tmux session, and still wears `RECOVERED` lost its session to something that
never told fujimoto — a host restart, `tmux kill-session`, a closed window,
`exit` in the pane. `RECOVERED` is the *default*, not a detection: there is no
boot-time scan and no heuristic, and a new field on an old record reads
correctly for free.

**Stop** and **Park** are the same operation (kill the process, keep the record
— the transcript is untouched, so the conversation resumes; an in-flight task
is interrupted) and differ *only* in intention, which is the only thing the
icon has to communicate: park means "I mean to come back to this", stop means
"done for now, may not be needed again". Nothing else keys off the difference.
The latest choice wins, so stopping a parked session records `STOPPED`.

Recovered sessions get a section of their own above *sessions* because they are
the only rows the user did not put on the screen. There is deliberately **no
bulk "restore" action**: restarting N claude processes is not restoring the
terminals, panes and window arrangement that were actually lost, so it bought a
row on the home screen without buying the user anything.

## Session types

**Worktree sessions** — isolated git worktree with its own branch:
- Creates a new branch + working directory via `git worktree add`
- Finish flow: Push & Create PR, Cherry-pick to base branch, or Discard & Delete
- Session metadata (base branch, source root, fork provenance) stored in `.fujimoto/meta.json` (auto-gitignored)

**Project-root sessions** — Claude launched in the project root (the main clone):
- Shown as "project-root" in the UI; the stored `session_type` is still
  `direct` (records and code), so nothing needed migrating
- No worktree creation; runs on whatever branch the main clone has checked
  out — wanting main means checking out main in the clone, deliberately
- Always the project root, never the cwd: fujimoto run from inside a worktree
  used to put a "direct" session in that worktree
- Multiple concurrent sessions possible on same repo
- Titled like a worktree (`e.g. fix-unit-tests`), but the title is optional:
  `{project}/{slug}`, or `{project}/session-N` when blank. Older sessions
  named `{project}/direct-N` still list and work as before

**Chat sessions** (`session_type="chat"`) — for questions and conversation, not work:
- `+ New chat` → optional title (`#chat-title-input`) → location picker
  (`#chat-location-panel`): `cl-root` (project-root), `cl-scratch` (the
  project's shared scratch directory, `config.get_chat_scratch_dir(project)` =
  `~/.fujimoto/chats/<project>/`, created only once chosen), then worktrees
  `cl-wt-N` ordered by their newest transcript's activity
  (`_chat_worktrees_by_recency`, creation time as fallback). `/` filters
  worktrees by name and `s` by transcript text (`_open_chat_filter(mode)`):
  the text scan is `claude_search.iter_hits` over the worktrees' logs in a
  `chat-text-search` thread worker, debounced, with `_chat_search_token` as the
  staleness check, mapping each hit's `cwd` back through `_matching_worktree`.
  Root and scratch are never filtered out. `check_action` enables the `search`
  and `session_search` bindings on this view, and both actions branch on
  `_on_chat_location`.
- Named `{project}/{slug}`, `{project}/chat-N` untitled, and recorded with the
  project it was started from — even in the scratch dir — so it is listed on that
  project's home screen and survives a restart. The record's `session_type` is
  the *only* thing that tells a running chat from a project-root session (both
  are "a live session that is not a worktree's own"), and rows use the `ch-`
  widget prefix (`_row_prefix`).
- Row label `(💬 <location>)` from `_location_name` (memoized in
  `_location_cache`): `project-root`, the worktree's name, or `scratch`. No branch.
- Menu: Connect (running) / **Resume chat** (`sa-resume-chat`, its own
  `claude_session_id`, same tmux name) + **New conversation** (stopped), View
  session log (opens the chat's own transcript, not a picker of its
  directory's), Rename, Park/Stop/Terminate. No Fork, Resume picker or Finish.
- Every new chat process gets a fresh `--session-id`, so its row finds its own
  conversation (`_claude_for`) even beside other sessions in one directory.
  Scratch-dir transcripts reach `_get_claude_sessions` through `extra`
  (`_chat_dirs()`: chat records outside the root and worktrees).
- `_chat_system_prompt`: it is a chat, where it runs (root / another session's
  worktree / scratch, decided via `get_main_worktree_root`), and — soft, not
  enforced — that work which changes files needs the user to choose where it
  happens first. A resumed chat gets the whole prompt again (`_start_launch`),
  since an appended system prompt does not survive into a new process.
- **A terminated chat is remembered by its own file, not by `sessions.json`.**
  `chats.py` keeps one `~/.cache/fujimoto/chats/<project>/<slug>.json` per
  chat (`ChatMeta`: name, project, cwd, conversation id, parent, created,
  closed), written by `record_open` in `_start_launch` on every chat launch
  and stamped by `mark_closed` on Terminate (`_close_chat`) — unless the chat
  never got a message (no transcript for its id), in which case it is
  forgotten. "Open" is still only "has a `sessions.json` record"; a previous
  chat is a chat file with no record and no tmux session
  (`_previous_chats`), exactly as an inactive worktree is a directory with
  neither. One file per chat, written temp-then-`os.replace`, so two processes
  acting on different chats never write the same file (the whole-file
  read-modify-write of `sessions.json` can lose updates; a lock there is a
  separate fix). `_init_git_info` loads them (`chats.prune` drops ones whose
  cwd is gone) into `_chat_metas`. A previous chat's menu: Resume chat, New
  conversation, View session log, **Remove from list** (`sa-remove-chat` →
  `_forget_chat`: deletes the file, and the scratch sub-directory only if
  empty, notifying where kept files are). No Rename (no tmux session). Its
  name stays taken (`_taken_session_names`). A conversation a chat — or a
  stopped session — owns resumes as that session from anywhere:
  `_conversation_owner` in the TUI's `_resume_target` (search hits,
  conversation rows, pickers) and `_record_owning` (+
  `chats.find_by_conversation`) for `fujimoto resume`. The chats' scratch
  directory is in the transcript search scope: `_chat_dirs()` for the TUI's
  `s`, `Scope.extra_dirs` for `fujimoto search`.
- **Scratch chats share one directory per project, not a fresh
  `mkdtemp`.** Claude Code asks to trust every new directory outside a trusted
  repo (worktrees inherit the main clone's trust; a temp dir has none), there
  is no CLI to pre-trust one, and a `--detach`ed chat sat at that prompt
  forever — no transcript, no registry entry. One directory is trusted once.
  Each chat gets `chat_files_dir(scratch, tmux_name)` (created by
  `_chat_files` in `_start_launch`), and `_chat_system_prompt(files_dir=…)`
  tells it to write files there, never in the shared directory.
  `claude/trust.py` only *reads* `hasTrustDialogAccepted` from
  `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json`) — fujimoto must never
  write that file, which Claude Code rewrites constantly.
- A chat skips `_apply_worktree_config`: in a worktree it is a guest, and the
  worktree's own session already runs its copy/link/init.
- `launch_options._uses_worktree_meta` keeps a chat (or any session whose
  record type is not `worktree`) in a worktree away from that worktree's
  `meta.json` options — decided by the record, not the tmux name, because a
  worktree session can be renamed.

**Ad hoc sessions** (legacy) — `adhoc-N`, a temp dir, no project. The TUI no
longer creates them; `fujimoto resume` still uses one for a transcript whose
`cwd` is outside every repo, and old records keep their system prompt.

## Naming conventions

| Thing | Pattern | Example |
|-------|---------|---------|
| Worktree directory | `{YYYYMMDD}-{slug}` | `20260309-fix-unit-tests` |
| Git branch | `worktree/{dir-name}` | `worktree/20260309-fix-unit-tests` |
| tmux session (worktree) | `{project}/{dir-name}` | `my-app/20260309-fix-unit-tests` |
| tmux session (project-root) | `{project}/{slug}`, or `{project}/session-{N}` untitled | `my-app/fix-login` |
| tmux session (legacy direct) | `{project}/direct-{N}` | `my-app/direct-1` |
| tmux session (chat) | `{project}/{slug}`, or `{project}/chat-{N}` untitled | `my-app/auth-question` |
| Widget ID (chat) | `ch-{project}--{name}` | `ch-my-app--auth-question` |
| Widget ID (chat location) | `cl-root`, `cl-scratch`, `cl-wt-{i}` | `cl-wt-0` |
| tmux session (adhoc, legacy) | `adhoc-{N}` | `adhoc-1` |
| Widget ID (project-root) | `ds-{project}--{name}` | `ds-my-app--fix-login` |
| Widget ID (claude session) | `cs-{session-id}` | `cs-abc12345-def6-7890` |
| Widget ID (fork picker) | `fp-{index}` | `fp-0` |

## Error handling

Three custom exception types, all caught in `main()`:
- `ConfigError` — missing env var
- `GitError` — git command failures, not in a repo
- `TmuxError` — tmux not installed, install failure
