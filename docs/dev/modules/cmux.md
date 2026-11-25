# `cmux.py`

**`cmux.py`**: integration with the [cmux](https://cmux.com) terminal. It
handles pane identity and pane restore. Everything here does nothing outside
cmux (no `CMUX_SURFACE_ID`), and nothing in it ever raises into a launch or
an attach.

- `SURFACE_SCOPED_KEYS`: `CMUX_SURFACE_ID`, `CMUX_PANEL_ID` and
  `CMUX_HISTORY_FILE`. These are the keys cmux's own shell integration treats
  as pane-scoped inside tmux (`_CMUX_TMUX_SURFACE_SCOPED_KEYS` in its zsh and
  bash scripts).
- `without_surface_scope(environ=None)`: a copy of the environment without
  those keys. `tmux._new_session` passes it as `new-session`'s `env`, and
  `tmux._forget_surface_scope` unsets the same keys on a running server
  (one chained `set-environment -gu … ; …` call). Workspace-scoped keys such
  as `CMUX_WORKSPACE_ID` stay. See the gotcha "A tmux server started in cmux
  keeps that pane's identity".
- `set_resume_binding(tmux_name)` / `clear_resume_binding(tmux_name)`:
  `tmux.attach_session` calls these around `tmux attach-session`, and clears
  only when the set succeeded. The binding is
  `cmux surface resume set --surface $CMUX_SURFACE_ID --kind fujimoto --source fujimoto --name <tmux> --checkpoint <tmux> --cwd <launch dir> --shell "<fujimoto> --attach <tmux>"`,
  which the CLI stores as the pane's restore record (`legacy_command`, run
  through cmux's compatibility shell). `<fujimoto>` comes from
  `config.fujimoto_executable()`, the same lookup restart uses. `--cwd` is
  fujimoto's own working directory, so a restored loop lands on the same home
  screen; a deleted directory falls back to `$HOME`. The CLI is
  `$CMUX_BUNDLED_CLI_PATH`, then `cmux` on `PATH`, run with a 5s timeout.
  Every outcome is logged (`cmux.resume_set result=not-cmux|unavailable|set|failed`,
  `cmux.resume_clear rc=`, `cmux.cli_failed`).

Why the binding has this shape is in design-decisions ("cmux restore through
`fujimoto --attach`"). To restore without asking, cmux still needs the user to
approve the command once. The README's cmux section describes that step.

Tests: `tests/conftest.py` unsets the pane-scoped keys for every test, so a
test run inside a cmux pane can never rebind the developer's own pane.
