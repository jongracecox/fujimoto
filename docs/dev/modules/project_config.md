# `project_config.py`

**`project_config.py`** — Optional per-project `.fujimoto.yaml` (validated with
pydantic):
- `CONFIG_FILENAME = ".fujimoto.yaml"`. Three optional sections — `copy`, `link`,
  `init` — each a list whose items are a bare string or a mapping.
- Enums: `Trigger` (`CREATE`, `LAUNCH`), `When` (`ONCE`, `ALWAYS`) with
  `When.runs_on(trigger)` (ONCE → create only; ALWAYS → create + launch),
  `LinkType` (`HARD`, `SYMBOLIC`), `OnError` (`ABORT`, `CONTINUE`).
- Pydantic models: `CopyEntry(path, when)`, `LinkEntry(path, type, when)`,
  `InitCommand(run, when, continue_on_error, cwd)`, `ProjectConfig(..., on_error)`.
  A `model_validator(mode="before")` coerces bare strings into the mapping form;
  `extra="forbid"` rejects unknown keys. `ProjectConfig` uses field aliases
  (`copy`/`link`/`init`) because `copy` would shadow `BaseModel.copy()` — the
  Python attributes are `copy_entries` / `link_entries` / `init_commands`.
  `on_error` (default `ABORT`) governs the caller's reaction to a hard init
  failure. `model` (optional) is the project's default model (see
  `launch_options.default_model`). `claude_args` (default `[]`) is the
  project's default launch options; a single string is `shlex.split` by a `field_validator`, so both
  `claude_args: [--a, b]` and `claude_args: --a b` work. `broadcast` (default
  `True`) can only opt a project *out* of `fujimoto broadcast` — trust (the
  signers) is global config so a repo cannot name its own key.
- `load_project_config(project_root)` — returns an empty config if the file is
  absent; raises `ConfigError` (reusing `config.ConfigError`) on malformed YAML
  or validation failure (config errors are surfaced, not swallowed).
- `apply_project_config(config, *, source_root, worktree_root, trigger)` — runs
  entries where `when.runs_on(trigger)`. Copy/link sources resolve relative to
  `source_root` (globs supported via `glob.has_magic`); destinations mirror the
  same relative path. Hard links fall back to `shutil.copy2` + warning on
  cross-filesystem `OSError`; `symbolic` uses `os.symlink`. Init commands run via
  `subprocess.run(["sh", "-x", "-c", run])` — `sh -x` echoes each command so the
  command and its output appear in the launch trace — with
  `{{ source_dir }}`/`{{ worktree_dir }}` substitution, cwd defaulting to the
  worktree root, stopping on first failure unless `continue_on_error`. Returns an
  `ApplyResult(actions, warnings, init_error)` — it never raises for individual
  action failures (`actions` is a log of what ran).
- `template_text()` / `write_config_template(project_root)` — read the bundled
  `templates/fujimoto.yaml.template` (via `importlib.resources`) and scaffold it
  into a repo (refusing to overwrite an existing file).
