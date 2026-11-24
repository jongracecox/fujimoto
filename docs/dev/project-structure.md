# Project structure

```
src/fujimoto/
├── __init__.py
├── cli.py        # Textual TUI app, entry point (main()), all UI screens and event handlers
├── config.py     # Environment variable loading, path construction, session metadata
├── git.py        # Git subprocess wrappers (worktree lifecycle, branch operations)
├── terminal.py   # Open native terminal windows (iTerm2 with Terminal.app fallback)
├── vscode.py     # Open directories in VS Code via the `code` CLI
├── tmux.py       # tmux session lifecycle (create, attach, kill, list, install)
├── version.py    # importlib.metadata wrapper for the running fujimoto version
├── version_check.py  # daily PyPI update check, dismissals, install detection, agent notice
├── paging.py     # PageUp/PageDown step (`page_step`) for TUI lists
├── settings.py   # TUI preferences (quick_terminal), stored in the global config
├── global_config.py   # ~/.config/fujimoto/config.yaml: model, quick_terminal
├── config_command.py  # `fujimoto config` list/get/set/unset (UI-free) + key registry
├── yaml_edit.py       # change one top-level YAML key, keeping every other line
├── session_state.py   # which sessions the user still considers open
├── filelock.py        # cross-process lock + atomic write for the shared JSON stores
├── chats.py           # one metadata file per chat, kept after Terminate (~/.cache/fujimoto/chats/)
├── debug.py      # --debug / --debug-redacted diagnostic logging + redaction
├── project_config.py  # optional per-project .fujimoto.yaml (copy/link/init worktree setup, claude_args)
├── launch_options.py  # extra claude CLI args per session (resolve / save / replay)
├── session_query.py   # `fujimoto search` / `fujimoto log`: scope, location, liveness, output
├── broadcast.py       # `fujimoto broadcast` / `send`: signed messages, mailbox, per-session waiter hook
├── follow.py          # `fujimoto broadcast follow`: one message's delivery, approval check, reply, target activity
├── mcp_server.py      # `fujimoto mcp`: stdio MCP server (optional `mcp` extra) over the session commands
├── mcp_setup.py       # `fujimoto mcp setup`: read-only secure-setup checks + Claude Desktop config
├── mcp_doctor.py      # `fujimoto mcp doctor`: read-only check of the installed server (launcher, startup, signing)
├── __main__.py        # `python -m fujimoto` (how the MCP server runs the CLI)
├── templates/
│   ├── __init__.py
│   ├── fujimoto.yaml.template  # commented scaffold written by `fujimoto --create-config`
│   └── agent_instructions.md   # the guide `fujimoto agent-instructions` prints
└── claude/
    ├── __init__.py      # Re-exports public API
    ├── log_parser.py    # Parse Claude JSONL session logs (state, metadata, session lookup)
    ├── search.py        # Full-text search across JSONL transcripts (batched, cancellable)
    ├── live.py          # Claude Code's running-process registry (~/.claude/sessions/)
    └── trust.py         # read-only: has Claude Code trusted a directory? (~/.claude.json)
```
