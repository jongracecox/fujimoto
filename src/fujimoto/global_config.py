"""The user's global fujimoto config, `~/.config/fujimoto/config.yaml`.

Holds preferences that apply to every project: the default Claude model, the
quick-terminal toggle, the TUI's PageUp/PageDown step and `fujimoto
broadcast`'s settings. `$XDG_CONFIG_HOME` moves it. Same contract as
`.fujimoto.yaml`: optional, YAML, validated with pydantic, and a malformed file
raises `ConfigError` so it can be shown rather than silently ignored.

Runtime state (open sessions, launch-option caches, the update check) stays in
`~/.cache/fujimoto/`; this file is only for things a user chooses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from fujimoto import debug

from .config import ConfigError
from .paging import parse_page_step
from .yaml_edit import update_key

XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"


class SignerConfig(BaseModel):
    """A key trusted to sign broadcasts (see `broadcast.py`)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # OpenSSH `<type> <base64>`, without a comment.
    public_key: str
    # `user`: the user's own key, whose messages read "from the user".
    # `agent`: an agent acting for them; its messages name it instead.
    role: Literal["user", "agent"] = "agent"


class BroadcastConfig(BaseModel):
    """`fujimoto broadcast`: off unless enabled, and only with a signer.

    Managed by the `fujimoto broadcast` subcommands rather than `fujimoto
    config`, which only handles scalar keys. Global only: a project can opt
    out in `.fujimoto.yaml` but never name a signer.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    signers: list[SignerConfig] = Field(default_factory=list)
    # What `broadcast send` signs with: a key path or a literal public key.
    # None means "the SSH agent key that matches a signer".
    signing_key: str | None = None


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Passed to claude as `--model` unless a session's own options set one.
    model: str | None = None
    # Whether the server-global quick-terminal key binding is installed.
    # None means the first-launch prompt has not been answered yet.
    quick_terminal: bool | None = None
    # How far PageUp/PageDown move in TUI lists: `50%` of the viewport or a
    # row count such as `8`. None means the default (`paging.DEFAULT`).
    page_step: str | None = None
    broadcast: BroadcastConfig = Field(default_factory=BroadcastConfig)

    @field_validator("page_step", mode="before")
    @classmethod
    def _check_page_step(cls, value: object) -> object:
        # YAML reads `page_step: 8` as an int; keep the normalised text.
        if value is None:
            return None
        return str(parse_page_step(value))


def global_config_path() -> Path:
    base = os.environ.get(XDG_CONFIG_HOME_ENV)
    root = Path(base) if base else Path.home() / ".config"
    return root / "fujimoto" / "config.yaml"


def load_global_config() -> GlobalConfig:
    """Load and validate the global config; empty if the file is absent."""
    path = global_config_path()
    if not path.exists():
        debug.log_once("global-config", "global_config.load", found=False)
        return GlobalConfig()
    try:
        raw = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if raw is None:
        return GlobalConfig()
    try:
        config = GlobalConfig.model_validate(raw)
    except ValidationError as exc:
        debug.log("global_config.load", path=debug.rp(path), found=True, valid=False)
        raise ConfigError(f"Invalid {path}:\n{exc}") from exc
    debug.log_once(
        "global-config",
        "global_config.load",
        path=debug.rp(path),
        found=True,
        valid=True,
        model=config.model is not None,
        quick_terminal=config.quick_terminal,
        page_step=config.page_step,
    )
    return config


def update_global_key(key: str, value: object, *, remove: bool = False) -> bool:
    """Set or remove one key of the global config, keeping its comments."""
    return update_key(global_config_path(), key, value, GlobalConfig, remove=remove)
