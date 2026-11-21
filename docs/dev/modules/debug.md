# `debug.py`

**`debug.py`** — Diagnostic logging for support (`--debug` / `--debug-redacted`):
- Owns a process-wide optional `DebugLogger`. Every helper (`log`, `log_once`,
  `log_section`, `log_command`, `log_exception`, `rv`, `rp`) is a cheap no-op
  when debug mode is off, so call sites log unconditionally.
- `enable(redact=..., log_dir=None)` opens
  `<log_dir>/fujimoto-<YYYYMMDD-HHMMSS>-<pid>.log`, where `log_dir` defaults to
  `$FUJIMOTO_LOG_DIR` (`LOG_DIR_ENV`) or `~/.fujimoto/logs`, and writes a header
  documenting the redaction mode. `disable()` closes it. `is_enabled()` guards
  expensive collection.
- `log_environment()` records fujimoto/Python/platform versions, `argv`, cwd,
  tty state, the versions of `tmux`/`git`/`claude`/`gh`/`code`/`uv`/`brew` on
  PATH (`_tool_version` keeps the executable basename readable), and every
  `FUJIMOTO_*`/`CLAUDE_*`/`ANTHROPIC_*` plus curated terminal/shell env var.
  Values whose name matches `is_secret_name()` become `[SECRET-<len>]`
  **regardless of redaction mode** — secrets are never logged.
- `log_once(key, event, **fields)` writes only when the payload for `key`
  changed; used for polled state (Claude discovery, tmux session lists) so a
  long-lived run doesn't drown in repeats.
- `log_capped(series, event, *, limit=DEFAULT_SERIES_CAP, dedupe_key=None, ...)`
  logs the first `limit` items of a repeating series and counts the rest;
  `close()` then writes one `series.summarised series=… logged=… not_logged=…
  total=…` line per series, so the log always states what it left out and no
  call site has to remember to summarise. Passing `dedupe_key` composes the cap
  with `log_once` semantics, so re-renders spend the budget on *distinct*
  subjects — 47 worktrees over three home renders is 141 calls but `total=47`.
  Used by the home inventory (`tui.worktree`, `tui.item`), `config.read_meta`
  and Claude discovery; the caller may add its own richer summary of the
  remainder (`tui.worktree_summary`, `tui.item_summary`) since it knows what
  the items were — but that summary needs `log_once`, or it repeats verbatim on
  every home render. Order the loop so the interesting items come first — the
  home inventory logs running sessions before idle ones, and worktrees
  newest-first.
- **Split a series by outcome, and never cap a failure.** `claude.session_dirs`
  and `claude.discovery` fire once per worktree, and on a machine with ~50 of
  them that was 96 of a 290-line log — three quarters of it "no transcripts
  here", which is the normal state of an old worktree. They now use separate
  series per outcome (`.resolved`/`.missing`, `.found`/`.empty`) so the routine
  case cannot crowd out the interesting one, with a tighter limit on the routine
  one. A discovery with `failed>0` — a transcript fujimoto could not read —
  bypasses the cap entirely, since that is the difference between "no sessions"
  and "sessions I could not parse". The `cwd-index` fallback is likewise never
  capped: it firing at all is the diagnostic.
- Redaction: `redact_text` → `[REDACTED-<fingerprint>-<len>[-CONTAINS<chars>]]`
  (shape-preserving; the fingerprint is **salted per run** by `enable()` and the
  salt is never logged, so equal values correlate within one log and a
  fingerprint is meaningless outside it);
  `redact_path` keeps separators/depth, collapses `$HOME` to `~`, preserves
  `_SAFE_PATH_COMPONENTS` (OS directories and *dotted* config dirs — components
  that cannot be a name the user chose) and, **only under an owned parent**,
  `_OWNED_NAMES` (`~/.cache/fujimoto/sessions.json` reads plainly while
  `~/git/fujimoto` does not). `redact_arg` keeps flags, lowercase subcommands
  and git's ref vocabulary (`_SAFE_REF_COMPONENTS` via `_is_git_ref`);
  `redact_ref`/`rref` keep `main`/`HEAD` where a value is known to be a ref;
  `redact_id`/`rid` keep a widget id's kind prefix (`ds-`, `sa-`) and redact the
  tail. Captured command output is redacted token-by-token with `redact_arg`.
- Instrumented call sites: `git._run` (every command + rc + output),
  `tmux` (install/list/create/attach/kill/rename/configure/quick-terminal),
  `claude/log_parser` (which lookup strategy resolved a path — encoded name,
  cwd-index or nothing — plus discovery counts, per-session parse results and
  failures), `claude/search` (query compile, logs available, scan start/finish
  with scanned/hit counts, unreadable logs, discarded hits),
  `session_state` (load/save, mark_open/closed, touch, rename, and every
  skipped or pruned record), `config` (resolved roots, session meta),
  `project_config` (load, apply, actions, warnings), `settings`, `terminal`,
  `vscode`, and `cli` (git info, home inventory, selections, launch target,
  prerequisites, fatals).
