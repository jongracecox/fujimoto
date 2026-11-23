# `global_config.py`

**`global_config.py`** — `~/.config/fujimoto/config.yaml` (`$XDG_CONFIG_HOME`
moves it; `global_config_path()`). Pydantic `GlobalConfig(extra="forbid")` with
`model`, `quick_terminal` and `broadcast` (`BroadcastConfig`: `enabled`,
`signers: list[SignerConfig(name, public_key)]`, `signing_key`; managed by
`fujimoto broadcast`, not the `config` registry — see `broadcast.md`).
`load_global_config()` has the same contract as
`load_project_config`: missing file → empty, malformed → `ConfigError`.
`update_global_key(key, value, remove=)` writes through `yaml_edit`. Runtime
state stays in `~/.cache/fujimoto/`; only user choices belong here.
