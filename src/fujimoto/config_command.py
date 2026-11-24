"""`fujimoto config`: list, get, set and unset configuration, UI-free.

Two files hold configuration:

- **global** — `~/.config/fujimoto/config.yaml` (`global_config`), for
  preferences that apply everywhere;
- **project** — `.fujimoto.yaml` in the main clone (`project_config`), read
  from the same place launches read it, so a worktree edits its project's file.

`KEYS` is the registry of what can be set where. `list` shows every key —
including the ones that are unset — so a reader (often an agent) can see what
is available, not just what is already configured. The FUJIMOTO_* environment
variables are reported alongside, read-only.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from fujimoto import debug

from .config import ConfigError
from .git import GitError, get_main_worktree_root
from .paging import parse_page_step
from .global_config import (
    GlobalConfig,
    global_config_path,
    load_global_config,
)
from .project_config import (
    CONFIG_FILENAME,
    OnError,
    ProjectConfig,
    load_project_config,
)
from .yaml_edit import update_key


class Scope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


def _parse_model(text: str) -> object:
    value = text.strip()
    if not value:
        raise ConfigError("model must not be empty (use `unset` to remove it)")
    return value


_TRUE = {"true", "on", "yes", "1"}
_FALSE = {"false", "off", "no", "0"}


def _parse_bool(text: str) -> object:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"expected true/false (or on/off), got {text!r}")


def _parse_page_step(text: str) -> object:
    try:
        return str(parse_page_step(text))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _parse_args(text: str) -> object:
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ConfigError(f"cannot split arguments: {exc}") from exc


def _parse_on_error(text: str) -> object:
    value = text.strip().lower()
    choices = [e.value for e in OnError]
    if value not in choices:
        raise ConfigError(f"on_error must be one of {', '.join(choices)}")
    return value


@dataclass(frozen=True)
class KeySpec:
    name: str
    description: str
    scopes: tuple[Scope, ...]
    # Turns the command-line text into the value written to the file; None
    # for a structured key that has to be edited in the file itself.
    parse: Callable[[str], object] | None


KEYS: tuple[KeySpec, ...] = (
    KeySpec(
        "model",
        "Claude model sessions start with, passed as --model unless a "
        "session's launch options set one. Project beats global.",
        (Scope.GLOBAL, Scope.PROJECT),
        _parse_model,
    ),
    KeySpec(
        "quick_terminal",
        "Install the server-global quick-terminal key binding (true/false).",
        (Scope.GLOBAL,),
        _parse_bool,
    ),
    KeySpec(
        "page_step",
        "How far PageUp/PageDown move in TUI lists: a share of the visible "
        "rows (50%, the default) or a row count (8).",
        (Scope.GLOBAL,),
        _parse_page_step,
    ),
    KeySpec(
        "claude_args",
        "Extra claude CLI arguments for sessions without saved launch "
        "options (a shell-style string).",
        (Scope.PROJECT,),
        _parse_args,
    ),
    KeySpec(
        "on_error",
        "What a failing init command does to the launch: abort or continue.",
        (Scope.PROJECT,),
        _parse_on_error,
    ),
    KeySpec(
        "copy",
        "Files copied into each worktree (edit .fujimoto.yaml).",
        (Scope.PROJECT,),
        None,
    ),
    KeySpec(
        "link",
        "Files linked into each worktree (edit .fujimoto.yaml).",
        (Scope.PROJECT,),
        None,
    ),
    KeySpec(
        "init",
        "Commands run in each worktree (edit .fujimoto.yaml).",
        (Scope.PROJECT,),
        None,
    ),
)

_KEYS_BY_NAME = {spec.name: spec for spec in KEYS}


@dataclass(frozen=True)
class EnvVar:
    name: str
    default: str
    description: str


ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar(
        "FUJIMOTO_WORKTREE_ROOT",
        "<repo>/.fujimoto/worktrees",
        "Where worktrees are created",
    ),
    EnvVar(
        "FUJIMOTO_GIT_ROOT", "(unset: no project switcher)", "Enables project switching"
    ),
    EnvVar("FUJIMOTO_TERMINAL", "auto-detect", "Terminal command (Linux)"),
    EnvVar(
        "FUJIMOTO_WINDOW_TITLE",
        "{git_project} - {worktree_name}",
        "Terminal window title template",
    ),
    EnvVar("FUJIMOTO_META_KEY", "C-a", "In-session fujimoto chord (blank disables)"),
    EnvVar("FUJIMOTO_TMUX_PREFIX", "C-b", "tmux prefix key"),
    EnvVar(
        "FUJIMOTO_QUICK_TERMINAL_KEY",
        "C-`",
        "Global quick-terminal toggle key (blank disables)",
    ),
    EnvVar("FUJIMOTO_LOG_DIR", "~/.fujimoto/logs", "Where --debug logs are written"),
    EnvVar("XDG_CONFIG_HOME", "~/.config", "Moves the global config file"),
)


@dataclass(frozen=True)
class FileState:
    path: Path | None  # None: no project (not in a git repository)
    exists: bool
    values: dict[str, object]
    error: str | None = None


@dataclass(frozen=True)
class KeyReport:
    spec: KeySpec
    global_value: object | None
    project_value: object | None

    @property
    def effective(self) -> tuple[object | None, Scope | None]:
        # Project beats global wherever a key exists in both.
        if self.project_value is not None:
            return self.project_value, Scope.PROJECT
        if self.global_value is not None:
            return self.global_value, Scope.GLOBAL
        return None, None


@dataclass(frozen=True)
class ConfigReport:
    global_file: FileState
    project_file: FileState
    keys: tuple[KeyReport, ...]
    environment: tuple[tuple[EnvVar, str | None], ...]


def find_project_root(cwd: Path | None = None) -> Path | None:
    """The main clone holding `.fujimoto.yaml`, or None outside a repository."""
    try:
        return get_main_worktree_root(cwd or Path.cwd())
    except (GitError, OSError):
        return None


def _require_project_root(cwd: Path | None) -> Path:
    root = find_project_root(cwd)
    if root is None:
        raise ConfigError("not in a git repository: --project needs one")
    return root


def _read_global() -> FileState:
    path = global_config_path()
    try:
        config = load_global_config()
    except ConfigError as exc:
        return FileState(path, path.exists(), {}, str(exc))
    return FileState(path, path.exists(), config.model_dump(exclude_unset=True))


def _read_project(root: Path | None) -> FileState:
    if root is None:
        return FileState(None, False, {})
    path = root / CONFIG_FILENAME
    try:
        config = load_project_config(root)
    except ConfigError as exc:
        return FileState(path, path.exists(), {}, str(exc))
    values = config.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return FileState(path, path.exists(), values)


def build_report(cwd: Path | None = None) -> ConfigReport:
    global_file = _read_global()
    project_file = _read_project(find_project_root(cwd))
    keys = tuple(
        KeyReport(
            spec,
            global_file.values.get(spec.name) if Scope.GLOBAL in spec.scopes else None,
            project_file.values.get(spec.name)
            if Scope.PROJECT in spec.scopes
            else None,
        )
        for spec in KEYS
    )
    environment = tuple((var, os.environ.get(var.name)) for var in ENV_VARS)
    debug.log(
        "config.report",
        global_exists=global_file.exists,
        global_error=global_file.error is not None,
        project=project_file.path is not None,
        project_exists=project_file.exists,
        project_error=project_file.error is not None,
    )
    return ConfigReport(global_file, project_file, keys, environment)


def spec_for(key: str) -> KeySpec:
    spec = _KEYS_BY_NAME.get(key)
    if spec is None:
        names = ", ".join(s.name for s in KEYS)
        raise ConfigError(f"unknown config key {key!r} (known keys: {names})")
    return spec


def _check_scope(spec: KeySpec, scope: Scope) -> None:
    if scope not in spec.scopes:
        allowed = " or ".join(f"--{s.value}" for s in spec.scopes)
        raise ConfigError(f"{spec.name} can only be set with {allowed}")


def get_value(
    key: str, scope: Scope | None = None, cwd: Path | None = None
) -> tuple[object | None, Scope | None]:
    """A key's value in one scope, or its effective value when `scope` is None.

    Returns `(value, scope it came from)`; `(None, None)` when unset.
    """
    spec = spec_for(key)
    if scope is not None:
        _check_scope(spec, scope)
    report = build_report(cwd)
    for state in (report.global_file, report.project_file):
        if state.error is not None:
            raise ConfigError(state.error)
    entry = next(k for k in report.keys if k.spec is spec)
    if scope is Scope.GLOBAL:
        value = entry.global_value
    elif scope is Scope.PROJECT:
        value = entry.project_value
    else:
        return entry.effective
    return value, (scope if value is not None else None)


def _target(scope: Scope, cwd: Path | None) -> tuple[Path, type[BaseModel]]:
    if scope is Scope.GLOBAL:
        return global_config_path(), GlobalConfig
    return _require_project_root(cwd) / CONFIG_FILENAME, ProjectConfig


@dataclass(frozen=True)
class WriteResult:
    path: Path
    changed: bool


def set_value(
    key: str, text: str, scope: Scope, cwd: Path | None = None
) -> WriteResult:
    spec = spec_for(key)
    _check_scope(spec, scope)
    if spec.parse is None:
        raise ConfigError(f"{key} is structured: edit {CONFIG_FILENAME} directly")
    value = spec.parse(text)
    path, model = _target(scope, cwd)
    changed = update_key(path, key, value, model)
    debug.log(
        "config.set",
        scope=scope.value,
        key=key,
        changed=changed,
        value=debug.rv(format_value(value)),
    )
    return WriteResult(path, changed)


def unset_value(key: str, scope: Scope, cwd: Path | None = None) -> WriteResult:
    spec = spec_for(key)
    _check_scope(spec, scope)
    if spec.parse is None:
        raise ConfigError(f"{key} is structured: edit {CONFIG_FILENAME} directly")
    path, model = _target(scope, cwd)
    changed = update_key(path, key, None, model, remove=True)
    debug.log("config.unset", scope=scope.value, key=key, changed=changed)
    return WriteResult(path, changed)


# -- Output ------------------------------------------------------------------

UNSET = "(unset)"


def format_value(value: object | None) -> str:
    """One-line rendering of a config value.

    >>> format_value(["--plugin-dir", "my plugins"])
    "--plugin-dir 'my plugins'"
    >>> format_value(True)
    'true'
    >>> format_value(None)
    '(unset)'
    """
    if value is None:
        return UNSET
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        strings = [v for v in value if isinstance(v, str)]
        if len(strings) == len(value):
            return shlex.join(strings)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _file_line(label: str, state: FileState) -> str:
    if state.path is None:
        return f"  {label:<8} (not in a git repository)"
    status = "invalid" if state.error else "exists" if state.exists else "not created"
    return f"  {label:<8} {state.path}  [{status}]"


def format_report(report: ConfigReport) -> str:
    lines = ["Files"]
    lines.append(_file_line("global", report.global_file))
    lines.append(_file_line("project", report.project_file))
    for state in (report.global_file, report.project_file):
        if state.error:
            lines.append("")
            lines.extend(f"  ! {line}" for line in state.error.splitlines())
    lines += ["", "Keys (set with: fujimoto config set KEY VALUE --global|--project)"]
    for entry in report.keys:
        spec = entry.spec
        value, source = entry.effective
        effective = format_value(value)
        if source is not None:
            effective += f"  ({source.value})"
        lines.append(f"  {spec.name} = {effective}")
        lines.append(f"      {spec.description}")
        scopes = []
        for scope in spec.scopes:
            scope_value = (
                entry.global_value if scope is Scope.GLOBAL else entry.project_value
            )
            scopes.append(f"{scope.value}: {format_value(scope_value)}")
        lines.append(f"      {' · '.join(scopes)}")
    lines += ["", "Environment (read-only)"]
    for var, env_value in report.environment:
        shown = repr(env_value) if env_value == "" else env_value
        current = shown if env_value is not None else f"(default: {var.default})"
        lines.append(f"  {var.name} = {current}")
        lines.append(f"      {var.description}")
    return "\n".join(lines)


def _file_json(state: FileState) -> dict[str, object] | None:
    if state.path is None:
        return None
    return {"path": str(state.path), "exists": state.exists, "error": state.error}


def report_json(report: ConfigReport) -> dict[str, object]:
    return {
        "files": {
            "global": _file_json(report.global_file),
            "project": _file_json(report.project_file),
        },
        "keys": [
            {
                "key": entry.spec.name,
                "description": entry.spec.description,
                "scopes": [s.value for s in entry.spec.scopes],
                "settable": entry.spec.parse is not None,
                "global": entry.global_value,
                "project": entry.project_value,
                "effective": entry.effective[0],
                "source": entry.effective[1].value if entry.effective[1] else None,
            }
            for entry in report.keys
        ],
        "environment": [
            {
                "name": var.name,
                "value": value,
                "default": var.default,
                "description": var.description,
            }
            for var, value in report.environment
        ],
    }
