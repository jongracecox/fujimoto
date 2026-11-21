# `launch_options.py`

**`launch_options.py`** — Extra `claude` CLI arguments ("launch options") per
session:
- `resolve(working_dir, tmux_name, claude_session_id, *, parent_dir,
  parent_session_id)` → `Resolved(args, source)`. First hit wins: the
  session's saved options (`Source.SESSION`), a fork parent's
  (`Source.PARENT`), the project's `.fujimoto.yaml` `claude_args`
  (`Source.PROJECT`), nothing (`Source.NONE`).
- `saved_args(...)` checks three stores in order: the worktree's `meta.json`
  (only for the worktree's own session — `_uses_worktree_meta`),
  the `session_state` record (by tmux name), and
  `~/.cache/fujimoto/launch_options.json` keyed by Claude conversation id.
- `save(working_dir, tmux_name, claude_session_id, args)` writes to the
  worktree's meta if it has one, otherwise to the record and (when the id is
  known) the conversation cache. **An `args` equal to the project default
  clears the override instead**, so a session that never asked for anything
  different keeps following `.fujimoto.yaml` as it changes.
- `remember_for_conversation(id, args)` — the conversation cache (read-change-write under `filelock.locked`, written atomically, like `sessions.json`); called by
  `SessionApp._keep_launch_options` on terminate, because terminate deletes
  the record that held a direct session's options.
- `parse(text)` / `render(args)` — `shlex.split` / `shlex.join`;
  `LaunchOptionsError` on an unbalanced quote.
- **Default model** — `default_model(working_dir)` → `DefaultModel(model,
  ModelSource)`: `.fujimoto.yaml` `model` (via the main clone), then the global
  config's; an invalid file contributes nothing. `with_default_model(args,
  working_dir)` appends `--model X` unless `has_model_flag(args)` (`--model` or
  `--model=`). It is applied in `_start_launch` *after* `_launch_claude_args`
  has saved/resolved the options, and to the background PR `claude -p`
  command, so **the default is never stored as a session option** — changing it
  reaches every session that hasn't pinned its own `--model`. That matters
  because Claude Code's `/model` writes the user's global setting, so without
  a pinned default one session's switch changes every later session.
  `LaunchOptionsDialog` names the default in `#lo-model` (not in the input), and
  the session-menu label appends it; `_default_model_for` falls back to the
  project root for a worktree that doesn't exist yet. Logged as
  `launch_options.model`.
- `project_default(working_dir)` reads `.fujimoto.yaml` from
  `get_main_worktree_root(working_dir)`, so a worktree and a direct session
  find the same file; any `GitError`/`ConfigError` contributes `()` (a
  malformed config is already surfaced on the home screen).
