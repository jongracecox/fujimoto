# `claude/live.py`

**`claude/live.py`** — Claude Code's registry of running processes,
`$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (`get_claude_sessions_dir`). Each file
holds `pid`, `sessionId`, `cwd`, `name` (the peer address), `status`
(`busy`/`idle`) and `tmux` (`<session>:@<window>.%<pane>`, parsed by
`tmux_session_from_pane`). It is undocumented, so `list_live_sessions` reads
every field defensively, skips files without an int `pid` and a `sessionId`,
and drops entries whose pid is gone (`pid_alive`; `PermissionError` counts as
alive) — a crashed process leaves its file behind.

**`waitingFor`** is set while the process is blocked on the user, and cleared
when it isn't. Claude Code writes it together with `status` whenever either
changes. Values seen in Claude Code 2.1.x: `"permission prompt"` (the default
for any approval dialog), `"sandbox request"`, `"worker request"`,
`"goal proposal"`, `"input needed"` (a question) and `"dialog open"`.
`waiting_category` maps them onto Claude's own UI grouping (`permission`,
`question`, `dialog`), and an unknown reason reads as `other` so that a new
reason still counts as waiting. These values come from reading the Claude
Code bundle, not from any documentation: re-check them when a Claude upgrade
makes `--waiting` miss sessions. `claude.live_sessions` logs a `waiting`
count.
