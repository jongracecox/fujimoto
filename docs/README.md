# Developer documentation

How fujimoto works inside, for anyone changing it. User-facing docs live in
the [README](../README.md); setup, code style and releasing in
[CONTRIBUTING](../CONTRIBUTING.md).

| Path | Title | Keywords |
|---|---|---|
| [docs/dev/development.md](dev/development.md) | Development workflow | uv sync, run, pytest, nox, install globally, `--no-cache`, pre-commit, ruff, ty, releases, hatch-vcs, version |
| [docs/dev/configuration.md](dev/configuration.md) | Configuration and prerequisites | env vars, `FUJIMOTO_*`, worktree root, git root, terminal, window title, meta key `C-a`, tmux prefix, quick terminal, log dir, Python versions |
| [docs/dev/project-structure.md](dev/project-structure.md) | Project structure | source tree, module list, templates, `claude/` package |
| [docs/dev/cli.md](dev/cli.md) | Entry point and CLI commands | `main()`, launch loop, `new`, `resume`, `chat`, `list`, `search`, `log`, `stop`, `config`, `broadcast`, `send`, `mcp`, `pane`, `agent-instructions`, `update`, update notice, `restart`, `--attach`, exec, `--detach`, `--parent`, `--prompt`, `LaunchTarget`, system prompt |
| [docs/dev/sessions.md](dev/sessions.md) | Sessions: status, types and naming | intent vs runtime, `stop_kind`, recovered, parked, stopped, worktree / project-root / chat / ad hoc sessions, scratch dir, chat files, naming conventions, widget ids, error types |
| [docs/dev/design-decisions.md](dev/design-decisions.md) | Key design decisions | TUI loop, tmux detach, home ordering, newest-first, launch options, session records, metadata, `.fujimoto.yaml` apply, fork, resume naming, `_claude_for`, direct rows, transcript search `s`, broadcast hooks vs keystrokes, signer trust, `send` vs broadcast, MCP in-process vs subprocess, MCP agent key, name filter `/`, update dismissals, restart by exec, render path must not touch disk, live polling |
| [docs/dev/testing.md](dev/testing.md) | Testing | pytest, coverage, Textual pilot, `run_test`, `XDG_CONFIG_HOME`, nox matrix, textual pins, CI |
| [docs/dev/diagnostic-logging.md](dev/diagnostic-logging.md) | Diagnostic logging | `--debug`, `--debug-redacted`, instrumenting features, `debug.log`, `log_once`, `rv`, `rp`, redaction, secrets, leak check |
| [docs/dev/gotchas.md](dev/gotchas.md) | Gotchas and learnings | widget ids `/`, `git worktree remove`, Claude log entry types, `last-prompt`, Shift+Enter, extended-keys, focus-events, `.venv`, redaction pitfalls, `if-shell` `;`, path encoding `.`→`-`, `kill_session`, markup vs `Content`, `asyncRewake`, hook timeout, last `--settings` wins, SSH agent approval, MCP cwd `/` and PATH, shims, `MCPServer`, worker tokens, OSC title, `sys.__stdout__`, tmux socket path length |
| [docs/dev/modules/cli.md](dev/modules/cli.md) | `cli.py` (TUI) | `SessionApp`, views, `PagingListView`, `page_step`, home screen sections, prompt line `p`, refresh `r`, session actions menu, fork flow, log viewer, log search `/` `n` `N`, raw fallback, `check_action`, footer, finish flow, open terminal, message log `b` |
| [docs/dev/modules/config.md](dev/modules/config.md) | `config.py` | worktree root, paths, `slugify`, `meta.json`, parent session, claude args, `next_session_name` |
| [docs/dev/modules/git.md](dev/modules/git.md) | `git.py` | git wrappers, `GitError`, project root, main worktree root, default branch, create/remove worktree, orphan, push, cherry-pick |
| [docs/dev/modules/tmux.md](dev/modules/tmux.md) | `tmux.py` | sessions, `build_claude_command`, attach, loop pid, `loop_client`, restart, pending action, key table, `Ctrl-A`, quick terminal binding, focus-events, prefix |
| [docs/dev/modules/session_state.md](dev/modules/session_state.md) | `session_state.py` | `sessions.json`, `SessionRecord`, `mark_open`, `mark_stopped`, `StopKind`, prune, lock |
| [docs/dev/modules/session_query.md](dev/modules/session_query.md) | `session_query.py` | `list`, `--waiting`, `--state`, `--fields`, `LOG_FIELDS`, named sessions, `select_fields`, `cut_row`, `search`, `log`, scope, outside a repo, `_layout_location`, deleted worktrees, relevance, `ActivityIndex`, `--summary`, links, `--after` cursor, `--reply-to`, `--wait`, calling session exclusion, `wait_for_registration` |
| [docs/dev/modules/launch_options.md](dev/modules/launch_options.md) | `launch_options.py` | claude args, resolve, save, project default, default model, `--model` |
| [docs/dev/modules/project_config.md](dev/modules/project_config.md) | `project_config.py` | `.fujimoto.yaml`, copy, link, init, `when`, `on_error`, pydantic, template |
| [docs/dev/modules/debug.md](dev/modules/debug.md) | `debug.py` | logger, `log_capped`, series, redaction internals, fingerprint salt, safe path components |
| [docs/dev/modules/claude-log_parser.md](dev/modules/claude-log_parser.md) | `claude/log_parser.py` | JSONL, `parse_session`, session state, path encoding, `session_dirs_for_path`, cwd index, `read_transcript` |
| [docs/dev/modules/claude-search.md](dev/modules/claude-search.md) | `claude/search.py` | full-text search, `ContentMode` raw/text, injected text, `api-request-blob`, matcher, `iter_hits`, snippets, spans, `MatchBreakdown` |
| [docs/dev/modules/claude-live.md](dev/modules/claude-live.md) | `claude/live.py` | Claude Code process registry, `sessions/<pid>.json`, peer name, status, `waitingFor`, `waiting_category` |
| [docs/dev/modules/broadcast.md](dev/modules/broadcast.md) | `broadcast.py` | `fujimoto broadcast`, `fujimoto send`, signed messages, `ssh-keygen -Y`, signers, payload `v`/`session`, mailbox, receipts, waiter, `asyncRewake` hook, `--settings` merge, `resolve_target`, message log, prune, signer roles user/agent, `--approval`, relayed approval, `broadcast verify` PASS/FAIL, `set-role`, preamble id, re-verify |
| [docs/dev/modules/mcp_server.md](dev/modules/mcp_server.md) | `mcp_server.py`, `mcp_setup.py` | `fujimoto mcp`, MCP server, Claude Desktop, stdio, `MCPServer`, tools, in-process vs subprocess, `--key`, agent signer, `mcp setup`, `--config-only`, `setup claude-desktop`, backup, `--replace`, secure setup checks, passphrase, SSH agent, `claude_desktop_config.json`, shim PATH |
| [docs/dev/modules/global_config.md](dev/modules/global_config.md) | `global_config.py` | `~/.config/fujimoto/config.yaml`, `GlobalConfig`, `BroadcastConfig` |
| [docs/dev/modules/config_command.md](dev/modules/config_command.md) | `config_command.py` | `fujimoto config`, key registry, list/get/set/unset |
| [docs/dev/modules/yaml_edit.md](dev/modules/yaml_edit.md) | `yaml_edit.py` | edit one YAML key, keep comments, atomic write |
| [docs/dev/modules/paging.md](dev/modules/paging.md) | `paging.py` | PageUp/PageDown step, `page_step`, `PageStep`, percent vs rows, half-page default |
| [docs/dev/modules/settings.md](dev/modules/settings.md) | `settings.py` | TUI preferences, quick terminal, settings.json migration |
| [docs/dev/modules/filelock.md](dev/modules/filelock.md) | `filelock.py` | `flock`, lock timeout, `atomic_write`, concurrent writers |
| [docs/dev/modules/terminal.md](dev/modules/terminal.md) | `terminal.py` | native terminal window, iTerm2, Terminal.app, Linux emulators |
| [docs/dev/modules/vscode.md](dev/modules/vscode.md) | `vscode.py` | `code` CLI |
