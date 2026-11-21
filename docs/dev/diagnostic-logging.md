# Diagnostic logging

`fujimoto --debug` / `--debug-redacted` writes a support log (see
[`modules/debug.md`](modules/debug.md), and the README's Troubleshooting section). The log's
value depends entirely on it staying current with the code.

**Instrument new features as you add them.** A feature that isn't instrumented
is invisible in a support log, and its absence is indistinguishable from it
working. Any new code that does one of the following needs logging in the same
change:

- runs a subprocess, or reads/writes a file outside the repo
- discovers or resolves something (sessions, paths, branches, config) — log
  the *outcome* and, when there is a fallback chain, **which strategy
  succeeded**
- silently drops or skips an item (a malformed record, an unparseable log, a
  pruned entry) — a silent skip is exactly what a bug report looks like
- fails in a way the user sees as "nothing happened"

How to write it:

```python
from fujimoto import debug

debug.log("session_state.mark_closed", session=debug.rv(name), removed=removed)
debug.log_once(f"claude-dirs-{path}", "claude.session_dirs", via="cwd-index")
```

- Every helper is a no-op when debug mode is off, so call it unconditionally.
  Guard with `debug.is_enabled()` only when *building* the fields is expensive.
- Name events `<module>.<event>` and pass data as keyword fields, never as a
  pre-formatted sentence — the log is grepped and diffed.
- **Redaction is the caller's job.** Wrap user-identifying values (project,
  branch, session and tmux names, search queries, arbitrary user text) in
  `debug.rv()` and every path in `debug.rp()`. A raw f-string interpolation
  bypasses redaction and leaks into `--debug-redacted` logs.
- Never log a credential. `log_environment` replaces values whose env var name
  matches `debug.is_secret_name()` with `[SECRET-<len>]` in **both** modes; if
  you add a new source of secrets, extend that predicate.
- Use `debug.log_once(key, ...)` for anything on a polling path — the home
  screen re-reads Claude logs every 3 seconds. Pick a `key` that identifies the
  *subject* (a path, a session id), so a change in state still logs.
- The event name is positional-only, so `name=` is safe as a field.
- `debug.py` must not import other fujimoto modules at module scope (they
  import it) — the `fujimoto.version` import inside `log_environment` is lazy
  for that reason.

Tests must call `debug.disable()` in teardown (the logger is process-wide and
would otherwise leak between tests) and point `FUJIMOTO_LOG_DIR` or
`enable(log_dir=...)` at `tmp_path` — never write to the real
`~/.fujimoto/logs`. See the autouse fixtures in `tests/test_debug.py`.

Before shipping a change that touches redaction, re-run the leak check:

```sh
FUJIMOTO_LOG_DIR=/tmp/fjlog fujimoto --debug-redacted
grep -c "$(whoami)" /tmp/fjlog/*.log   # expect 0
```
