# `settings.py`

**`settings.py`** — TUI preferences, stored as keys of the global config:
- `Settings` dataclass: `quick_terminal_enabled: bool | None` (None = never asked),
  persisted as `quick_terminal` in `~/.config/fujimoto/config.yaml`.
- `load_settings()` / `save_settings()` keep their old API so `cli.py`/`tmux.py`
  are unchanged, and still never raise (an invalid global config reads as
  "never asked"; write failures are logged as `settings.save_failed`).
- **Migration:** the preference used to live in `~/.cache/fujimoto/settings.json`.
  When the config has no `quick_terminal` and that file has a bool,
  `load_settings` copies it into the config (`settings.migrated`) and leaves
  the old file alone, so an older fujimoto running alongside still reads it.
