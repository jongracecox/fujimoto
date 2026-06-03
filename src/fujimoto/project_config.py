"""Per-project `.fujimoto.yaml` configuration.

An optional, committed config file at the project root describing files to copy
or link into a new worktree and commands to run inside it. Parsed and validated
with pydantic; applied at worktree creation and on session launch/resume.
"""

from __future__ import annotations

import glob as globmod
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import ConfigError

CONFIG_FILENAME = ".fujimoto.yaml"
TEMPLATE_PACKAGE = "fujimoto.templates"
TEMPLATE_NAME = "fujimoto.yaml.template"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class Trigger(StrEnum):
    """When config is being applied."""

    CREATE = "create"
    LAUNCH = "launch"


class When(StrEnum):
    """When an individual action should run."""

    ONCE = "once"
    ALWAYS = "always"

    def runs_on(self, trigger: Trigger) -> bool:
        """ONCE runs only at creation; ALWAYS runs at creation and launch."""
        if self is When.ALWAYS:
            return True
        return trigger is Trigger.CREATE


class LinkType(StrEnum):
    HARD = "hard"
    SYMBOLIC = "symbolic"


class OnError(StrEnum):
    """What to do with the launch when a non-`continue_on_error` init fails."""

    ABORT = "abort"
    CONTINUE = "continue"


class CopyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    when: When = When.ONCE

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, data: object) -> object:
        if isinstance(data, str):
            return {"path": data}
        return data


class LinkEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    type: LinkType = LinkType.HARD
    when: When = When.ONCE

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, data: object) -> object:
        if isinstance(data, str):
            return {"path": data}
        return data


class InitCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: str
    when: When = When.ONCE
    continue_on_error: bool = False
    cwd: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, data: object) -> object:
        if isinstance(data, str):
            return {"run": data}
        return data


class ProjectConfig(BaseModel):
    # `copy` would shadow BaseModel.copy(); expose YAML keys via aliases instead.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    copy_entries: list[CopyEntry] = Field(default_factory=list, alias="copy")
    link_entries: list[LinkEntry] = Field(default_factory=list, alias="link")
    init_commands: list[InitCommand] = Field(default_factory=list, alias="init")
    on_error: OnError = OnError.ABORT


@dataclass
class ApplyResult:
    """Outcome of applying a project config to a worktree."""

    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    init_error: str | None = None


def load_project_config(project_root: Path) -> ProjectConfig:
    """Load and validate `.fujimoto.yaml` from `project_root`.

    Returns an empty config if the file is absent. Raises ConfigError if the
    file exists but is malformed or fails validation — config problems should be
    visible, not silently ignored.
    """
    path = project_root / CONFIG_FILENAME
    if not path.exists():
        return ProjectConfig()
    try:
        raw = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"Could not read {CONFIG_FILENAME}: {exc}") from exc
    if raw is None:
        return ProjectConfig()
    try:
        return ProjectConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid {CONFIG_FILENAME}:\n{exc}") from exc


def _substitute(text: str, mapping: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(
        lambda m: mapping.get(m.group(1), m.group(0)),
        text,
    )


def _iter_matches(source_root: Path, pattern: str) -> list[tuple[Path, Path]]:
    """Resolve a copy/link source pattern to (absolute_src, relative_path) pairs.

    Globs expand against `source_root`; literal paths are taken verbatim. Only
    regular files are yielded; directories are skipped.
    """
    if globmod.has_magic(pattern):
        return [
            (m, m.relative_to(source_root))
            for m in sorted(source_root.glob(pattern))
            if m.is_file()
        ]
    src = source_root / pattern
    if src.is_file():
        return [(src, Path(pattern))]
    return []


def _place(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()


def apply_project_config(
    config: ProjectConfig,
    *,
    source_root: Path,
    worktree_root: Path,
    trigger: Trigger,
) -> ApplyResult:
    """Apply copy/link/init actions whose `when` matches `trigger`.

    Returns an ApplyResult collecting non-fatal warnings and, if an init command
    fails without `continue_on_error`, the error message (further commands are
    skipped). Never raises for individual action failures.
    """
    result = ApplyResult()

    for entry in config.copy_entries:
        if not entry.when.runs_on(trigger):
            continue
        matches = _iter_matches(source_root, entry.path)
        if not matches:
            result.warnings.append(f"copy: no match for '{entry.path}'")
            continue
        for src, rel in matches:
            dest = worktree_root / rel
            try:
                _place(dest)
                shutil.copy2(src, dest)
                result.actions.append(f"copied {rel}")
            except OSError as exc:
                result.warnings.append(f"copy: failed for '{rel}': {exc}")

    for entry in config.link_entries:
        if not entry.when.runs_on(trigger):
            continue
        matches = _iter_matches(source_root, entry.path)
        if not matches:
            result.warnings.append(f"link: no match for '{entry.path}'")
            continue
        for src, rel in matches:
            dest = worktree_root / rel
            try:
                _place(dest)
                if entry.type is LinkType.SYMBOLIC:
                    os.symlink(src, dest)
                    result.actions.append(f"symlinked {rel}")
                else:
                    try:
                        os.link(src, dest)
                        result.actions.append(f"hard-linked {rel}")
                    except OSError:
                        shutil.copy2(src, dest)
                        result.warnings.append(
                            f"link: hard link not possible for '{rel}' "
                            "(different filesystem?); copied instead"
                        )
            except OSError as exc:
                result.warnings.append(f"link: failed for '{rel}': {exc}")

    mapping = {
        "source_dir": str(source_root),
        "worktree_dir": str(worktree_root),
    }
    for cmd in config.init_commands:
        if not cmd.when.runs_on(trigger):
            continue
        run = _substitute(cmd.run, mapping)
        cwd = _substitute(cmd.cwd, mapping) if cmd.cwd else str(worktree_root)
        result.actions.append(f"ran: {run}")
        try:
            # `sh -x` echoes each command (and its expansion) before running it,
            # so the command and its output are visible in the launch trace.
            proc = subprocess.run(["sh", "-x", "-c", run], cwd=cwd, text=True)
            failed = proc.returncode != 0
            detail = f"exit code {proc.returncode}"
        except OSError as exc:
            failed = True
            detail = str(exc)
        if failed:
            message = f"init: '{run}' failed ({detail})"
            if cmd.continue_on_error:
                result.warnings.append(message)
            else:
                result.init_error = message
                break

    return result


def template_text() -> str:
    """Return the bundled `.fujimoto.yaml` template text."""
    return (
        resources.files(TEMPLATE_PACKAGE)
        .joinpath(TEMPLATE_NAME)
        .read_text(encoding="utf-8")
    )


def write_config_template(project_root: Path) -> Path:
    """Write the commented config template to `project_root/.fujimoto.yaml`.

    Raises ConfigError if the file already exists.
    """
    dest = project_root / CONFIG_FILENAME
    if dest.exists():
        raise ConfigError(f"{CONFIG_FILENAME} already exists at {dest}")
    dest.write_text(template_text(), encoding="utf-8")
    return dest
