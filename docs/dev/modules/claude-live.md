# `claude/live.py`

**`claude/live.py`** — Claude Code's registry of running processes,
`$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (`get_claude_sessions_dir`). Each file
holds `pid`, `sessionId`, `cwd`, `name` (the peer address), `status`
(`busy`/`idle`) and `tmux` (`<session>:@<window>.%<pane>`, parsed by
`tmux_session_from_pane`). It is undocumented, so `list_live_sessions` reads
every field defensively, skips files without an int `pid` and a `sessionId`,
and drops entries whose pid is gone (`pid_alive`; `PermissionError` counts as
alive) — a crashed process leaves its file behind.
