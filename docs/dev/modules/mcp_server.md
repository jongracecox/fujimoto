# `mcp_server.py` and `mcp_setup.py`

User-facing docs: README, "Claude Desktop and other MCP clients". Why it is
built this way: [design decisions](../design-decisions.md) ("MCP server").

**`mcp_server.py`**: `fujimoto mcp [--key KEY]`, a stdio MCP server built on
the official SDK's `MCPServer` (mcp 2.x; v1's `FastMCP` was renamed). `mcp`
is an optional extra (`fujimoto[mcp]`), imported only inside `build_server`
and `serve`, so the rest of fujimoto never pays for it. `serve` prints the
install command when the extra is missing.

- **Read tools, in-process.** `list_sessions`, `search_sessions`,
  `read_transcript`, `listening_sessions`, `message_status` call
  `session_query` / `broadcast` and return the same dicts as the CLI's
  `--json` (`list_json`, `search_json`, `log_json`, `show_json`,
  `status_json`). `read_transcript` with no window (summary, grep, head, tail,
  after, reply_to) gets `tail=DEFAULT_TAIL` (40), and `wait` is capped at
  `MAX_WAIT` (120s). A tool error is `FujimotoToolError`, turned into the
  SDK's `ToolError` (an `is_error` result) in `build_server`'s `call`.
- **Mutating tools, by subprocess.** `Commands` builds argv and runs
  `sys.executable -m fujimoto … --json` (`__main__.py`) with stdin closed,
  stdout/stderr captured, `SUBPROCESS_TIMEOUT` and `CLAUDE_CODE_SESSION_ID`
  removed from the env. Non-zero exit → the last stderr line, minus
  `fujimoto: `, as the tool error. Stderr on success comes back as `notes`
  (e.g. chat's trust hint). The runner is injectable (`run=`) for tests.
  - `start_session` runs `new` with the cwd set to the project root
    (`resolve_scope(project=…).root`), because `new`/`resume` check they run
    inside a repo and Claude Desktop starts servers in `/`. `resume_session`
    resolves the session first and runs in its recorded `cwd` (home if gone,
    so the CLI gives the real error). `start_chat`, `stop_session` and
    messaging run from home.
  - Optional values go as `--flag=value`, so a prompt starting with `-`
    isn't read as a flag. Positionals (title, session, text) that start with
    `-` are refused (`_positional`), since `send`/`stop` accept no `--`.
- **Messaging only with a key.** `send_message` and `broadcast` are
  registered only when `serve` got `--key`. `check_key` normalizes it to a
  literal public key and refuses one that matches a `user` signer. The
  literal goes to `--key=`, so `broadcast.resolve_signing_key` takes the
  literal path: the agent signs, and `ssh-keygen` never opens a private key
  file. There is no `approval` parameter (see design decisions).
- Tool annotations: read tools `readOnlyHint`; start/resume/send/broadcast
  not read-only, not destructive; `stop_session` `destructiveHint`.
  `INSTRUCTIONS` is the server's guide for the client model.
- The SDK logs at `WARNING` (stderr), so tool failures don't fill Claude
  Desktop's server log.
- Instrumented as `mcp.serve` (started, messaging / reason), `mcp.tool`
  (tool, ok) and `mcp.cli` (command, rc: int, `timeout` or `oserror`).

**`mcp_setup.py`**: `fujimoto mcp setup [claude-desktop] [--key KEY] [--replace] [--name N] [--json | --config-only]`.
Stdlib + `broadcast` only, so it runs before the extra is installed.
Read-only unless a client is named: `check_setup` returns `Check(name, status ok|warn|fail, detail,
fix[])` for the mcp package, tmux, the key (must be public; a private key
file fails), the private key at rest (`private_key_has_passphrase`: `ssh-keygen
-y -P ''` succeeds only for an unencrypted key; matched on the phrase
"incorrect passphrase", never a bare word, since stderr echoes the path), the
SSH agent holding it (`broadcast._agent_keys`), per-use confirmation (always
a warning: not observable), the signer (missing → the `add-signer … --role
agent` command; `user` role → fail) and broadcast enabled (fail only with a
key). Exit 1 when any check fails.
`--config-only` prints only `client_config` (`{"mcpServers": {"fujimoto":
…}}`) so stdout is always pasteable JSON; when a check fails it prints
nothing there, writes `format_failures` to stderr and exits 1, so an insecure
setup never yields config to paste. `--json` and `--config-only` are
mutually exclusive.

`desktop_entry` builds the `mcpServers.fujimoto` JSON: the `fujimoto` path,
`args` with `--key <literal>`, and `env` with `PATH` (the dirs holding tmux,
git, ssh-keygen and claude) and the current `SSH_AUTH_SOCK`. `_stable_dir`
walks `PATH` itself and skips directories whose own name or parent's name
contains "shim": terminal wrappers such as cmux put per-session shim dirs
first, and those are gone when Claude Desktop starts the server later.
Logged as `mcp.setup` (one `name=status` list).

**`setup claude-desktop`** (`install_desktop_config`, CLI `_install_mcp_client`)
writes `desktop_entry` into `desktop_config_path()`, and only when
`report.ok`: a failing setup gets `format_failures` on stderr and exit 1,
the same rule as `--config-only` (which it can't be combined with). Steps:
parse the file (invalid JSON, a non-object, or a non-object `mcpServers` →
`InstallError`, nothing touched; a missing file is created `0600`; a missing
`Claude/` directory is an error), compare the current `fujimoto` entry
(equal → `unchanged`, no backup; one `setup` wrote, per `_is_ours` — a
command named `fujimoto` with `mcp` in its args — is updated in place,
keeping any global flags before `mcp` via `_keep_global_flags`, so adding
`--key` later is just a re-run; anything else → `conflict` with a unified
diff and exit 1 unless `--replace`), `shutil.copy2` to `<name>.bak-<stamp>`, write
a temp file in the same directory with the original mode and `os.replace` it,
then read it back and restore the backup if anything other than
`mcpServers.fujimoto` changed. Indentation is detected from the file and
key order kept (`json` preserves insertion order). Logged as
`mcp.desktop_install` (status, backup).
