# `config_command.py`

**`config_command.py`** — `fujimoto config`, UI-free like `session_query.py`.
`KEYS` is the registry (`KeySpec(name, description, scopes, parse)`): `model`
(global+project), `quick_terminal` (global), `claude_args`/`on_error`
(project), and `copy`/`link`/`init` (project, `parse=None`: listed but
"edit .fujimoto.yaml"). `build_report()` reads both files (an invalid one is
reported in `FileState.error`, not raised) and the `ENV_VARS` list;
**`list` shows every registry key including unset ones**, with its scopes and
description, so an agent can see what is configurable. Project config is
found via `get_main_worktree_root` (the file launches read); `--project`
outside a repo is a `ConfigError`. Instrumented as `config.report`,
`config.set`, `config.unset`.
