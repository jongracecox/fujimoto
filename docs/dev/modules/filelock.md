# `filelock.py`

**`filelock.py`** — Locking for the shared JSON stores (`sessions.json`,
`launch_options.json`):
- `locked(path, *, timeout=2.0)` — `fcntl.flock` on a sidecar `<file>.lock`,
  yielding whether the lock was taken. The kernel drops it when the holder
  exits, so a crash cannot leave a stale lock. While held, the lock file holds
  `{"pid", "command", "since"}` — for diagnosis only (`read_holder`); it is
  cleared on release. **A timeout is not an error**: the block runs unlocked
  (the old behaviour) and `filelock.timeout` logs the holder, because
  bookkeeping must never freeze the TUI. `filelock.waited` logs any wait.
- `atomic_write(path, text)` — temp file in the same directory, then
  `os.replace`; the temp file never survives a failure.
- `tests/test_filelock.py::test_concurrent_writers_lose_nothing` runs real
  processes against both stores. Keep it: it is the only test that fails
  when locking is missing.
