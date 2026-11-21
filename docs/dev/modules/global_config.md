# `global_config.py`

**`global_config.py`** — `~/.config/fujimoto/config.yaml` (`$XDG_CONFIG_HOME`
moves it; `global_config_path()`). Pydantic `GlobalConfig(extra="forbid")` with
`model` and `quick_terminal`. `load_global_config()` has the same contract as
`load_project_config`: missing file → empty, malformed → `ConfigError`.
`update_global_key(key, value, remove=)` writes through `yaml_edit`. Runtime
state stays in `~/.cache/fujimoto/`; only user choices belong here.
