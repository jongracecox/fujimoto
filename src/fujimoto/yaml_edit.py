"""Change one top-level key of a YAML config file, keeping everything else.

`.fujimoto.yaml` is usually mostly comments (the `--create-config` template is
almost nothing else), so a load-and-dump rewrite would throw the user's notes
away on every `fujimoto config set`. Round-trip libraries (ruamel.yaml) were
tried and still drop comments in common cases: a file that is *only*
comments loads as nothing, and removing the last key takes the comment after
it along. So the edit is done on the text: the YAML parser only says on which
line each top-level key starts, and exactly the lines of the key being changed
are replaced (or removed, or appended). Every other line — comments, blank
lines, other keys and their formatting — is written back byte for byte.

The new value is rendered by PyYAML, the same library that reads the file, so
a string such as `on` comes out quoted rather than turning into a boolean.
Every write is validated against the file's pydantic model first, so a value
that would make the file invalid never reaches disk.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from fujimoto import debug

from .config import ConfigError


def _key_starts(text: str, path: Path) -> list[tuple[str, int]]:
    """Each top-level key and the 0-based line it starts on, in file order."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if node is None:  # empty, or nothing but comments
        return []
    if not isinstance(node, yaml.MappingNode):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return [(str(key.value), key.start_mark.line) for key, _ in node.value]


def _is_trailing(line: str) -> bool:
    """A line that belongs between blocks rather than to the key above it."""
    return not line.strip() or line.startswith("#")


def _block(lines: list[str], starts: list[tuple[str, int]], key: str) -> range | None:
    """The lines holding `key` and its value, without comments that follow it."""
    for i, (name, start) in enumerate(starts):
        if name != key:
            continue
        end = starts[i + 1][1] if i + 1 < len(starts) else len(lines)
        # A column-0 comment or blank line before the next key introduces
        # what comes next (or closes the file); it is not part of this value.
        while end > start + 1 and _is_trailing(lines[end - 1]):
            end -= 1
        return range(start, end)
    return None


def _render(key: str, value: object) -> list[str]:
    text = yaml.safe_dump(
        {key: value}, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    return text.splitlines(keepends=True)


def edit_text(
    text: str, key: str, value: object, *, remove: bool = False, path: Path
) -> str:
    """`text` with `key` set to `value` (or removed), everything else intact."""
    lines = text.splitlines(keepends=True)
    block = _block(lines, _key_starts(text, path), key)
    if remove:
        if block is None:
            return text
        return "".join(lines[: block.start] + lines[block.stop :])
    new = _render(key, value)
    if block is not None:
        return "".join(lines[: block.start] + new + lines[block.stop :])
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines + new)


def update_key(
    path: Path,
    key: str,
    value: object,
    model: type[BaseModel],
    *,
    remove: bool = False,
) -> bool:
    """Set (or with `remove=True`, delete) top-level `key` in the file at `path`.

    Returns whether the file changed. Creates the file (and its directory) if
    needed. Raises `ConfigError` when the file cannot be read, or when the
    result would not validate against `model` — in which case the file is
    left exactly as it was.
    """
    created = not path.exists()
    try:
        text = "" if created else path.read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    new_text = edit_text(text, key, value, remove=remove, path=path)
    loaded = yaml.safe_load(new_text) or {}
    old = yaml.safe_load(text) if text.strip() else {}
    if isinstance(old, dict) and loaded == old:
        return False
    try:
        model.model_validate(loaded)
    except ValidationError as exc:
        raise ConfigError(f"Invalid value for {key}:\n{exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash mid-write cannot leave a
    # half-written config behind.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(new_text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    debug.log(
        "yaml_edit.update",
        path=debug.rp(path),
        key=key,
        removed=remove,
        created=created,
    )
    return True
