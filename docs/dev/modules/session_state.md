# `session_state.py`

**`session_state.py`** — Which sessions the user still considers open, stored
as JSON in `~/.cache/fujimoto/sessions.json` (same graceful-degradation pattern
as `settings.py`: missing file, unreadable cache or corrupt JSON yield an empty
state, never an error):
- `SessionRecord` dataclass: `cwd`, `project`, `session_type`, `branch`,
  `claude_session_id`, `stop_kind`, `created`, `last_seen`, `claude_args`
  (saved launch options for a non-worktree session; `None` = no override, and
  `mark_open` preserves it — only `set_claude_args` changes it),
  `parent_session` (the starting session's peer address for sessions without
  a `meta.json` — chats; `mark_open(parent_session=…)` sets it, `None` keeps
  it). `created` is
  stamped by the *first* `mark_open` and preserved by every later one, so it is
  the session's creation order rather than its last reconnect; it is what the
  home screen sorts by. Only `cwd` is required — every
  other field defaults, so a record written by a different fujimoto version
  still loads. `StopKind.from_raw` coerces the stored kind for the same reason —
  an unknown or missing value reads as `RECOVERED`, and a legacy `parked: true`
  (written before `stop_kind` existed) reads as `PARKED`, so an upgrade doesn't
  re-label something shelved on purpose as a crash.
- Keyed by **tmux session name**, so worktree, direct and ad hoc sessions are
  covered uniformly and a record outlives its worktree directory.
- **Every writer holds `filelock.locked(_state_path())` around its
  load-change-save**, and `save_state` writes through
  `filelock.atomic_write` (temp file + `os.replace`). Several fujimoto
  processes (the TUI, agents' `fujimoto chat`/`new`/`stop`) write this one
  file; before the lock, 4 processes × 25 `mark_open`s kept 57 of 100 records.
  Readers never lock: the rename means they see a whole old or new file.
- `load_state()` / `save_state()`, `mark_open(...)` (every launch and
  reconnect; a reconnect passing no id keeps the id recorded at first launch),
  `mark_closed(name)`, `mark_stopped(name, claude_session_id=None, *,
  kind=StopKind.STOPPED)` (the stamp that keeps a deliberate stop from being
  read as a crash; it always overwrites, so the user's latest decision wins),
  `rename(old, new)`, `set_claude_args(name, args)`, `prune()`.
- **A record's presence means "open"; its absence means "closed"** — which is
  also what a session fujimoto has never launched looks like. `mark_closed`
  therefore just deletes the record, and there is no reconciliation pass and
  nothing to age out. `prune()` drops records whose `cwd` is gone (deleted
  worktree, ad hoc temp dir cleared by a reboot) and is called once per
  `_init_git_info`, not per render.
