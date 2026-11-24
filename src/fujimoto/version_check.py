"""Daily PyPI update check, and how to tell the user (and their agents) about it.

The TUI checks at most once a day and shows a banner on the home screen. New
sessions read the same cache (never the network: a launch must not wait on
PyPI) and get a sentence in their system prompt asking the agent to offer the
upgrade once. The banner's dismissal (`d`) and the agents' dismissal
(`fujimoto update dismiss`) are separate fields, so dismissing one does not
silence the other, and each covers one version only.
"""

from __future__ import annotations

import json
import shlex
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import cast

from fujimoto import debug
from fujimoto.filelock import atomic_write, locked

PYPI_URL = "https://pypi.org/pypi/fujimoto/json"
CHECK_INTERVAL = timedelta(days=1)


def _cache_path() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "version_check.json"


@dataclass
class CheckState:
    last_check: datetime | None = None
    latest_version: str | None = None
    dismissed_version: str | None = None
    # Dismissed by an agent on the user's behalf; hides the session notice only.
    agent_dismissed_version: str | None = None


def load_state() -> CheckState:
    path = _cache_path()
    if not path.exists():
        return CheckState()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return CheckState()
    last_raw = data.get("last_check")
    last: datetime | None = None
    if isinstance(last_raw, str):
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            last = None
    return CheckState(
        last_check=last,
        latest_version=data.get("latest_version"),
        dismissed_version=data.get("dismissed_version"),
        agent_dismissed_version=data.get("agent_dismissed_version"),
    )


def save_state(state: CheckState) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_check": state.last_check.isoformat() if state.last_check else None,
        "latest_version": state.latest_version,
        "dismissed_version": state.dismissed_version,
        "agent_dismissed_version": state.agent_dismissed_version,
    }
    atomic_write(path, json.dumps(payload))


def should_check(state: CheckState, now: datetime) -> bool:
    if state.last_check is None:
        return True
    return (now - state.last_check) >= CHECK_INTERVAL


def fetch_latest_from_pypi(timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:  # noqa: S310
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return version if isinstance(version, str) else None


def _version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("+", 1)[0].split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except ValueError:
        return False


def check_for_update(
    current: str, now: datetime | None = None
) -> tuple[str | None, bool]:
    """Return (latest_version, should_notify).

    should_notify is True only when latest > current and the user has not
    dismissed that specific latest version. Best-effort — silent on failure.
    """
    now = now or datetime.now()
    state = load_state()

    if should_check(state, now):
        latest = fetch_latest_from_pypi()
        if latest is not None:
            # Re-read under the lock: a dismissal may have landed meanwhile.
            with locked(_cache_path()):
                state = load_state()
                state.latest_version = latest
                state.last_check = now
                try:
                    save_state(state)
                except OSError:
                    pass

    latest = state.latest_version
    if latest is None:
        return None, False
    notify = is_newer(latest, current) and state.dismissed_version != latest
    return latest, notify


def dismiss(version: str) -> None:
    """Hide the home-screen banner for `version`."""
    with locked(_cache_path()):
        state = load_state()
        state.dismissed_version = version
        try:
            save_state(state)
        except OSError:
            pass


def agent_dismiss(version: str) -> None:
    """Stop new sessions offering the upgrade to `version`.

    Raises OSError if the cache cannot be written: unlike the banner, the
    caller is an agent that should hear the dismissal did not stick.
    """
    with locked(_cache_path()):
        state = load_state()
        state.agent_dismissed_version = version
        save_state(state)
    debug.log("update.agent_dismiss", version=version)


def refresh_latest(now: datetime | None = None) -> str | None:
    """Ask PyPI now, whatever the interval, and cache the answer.

    Returns the latest version, or the cached one when PyPI can't be reached.
    """
    now = now or datetime.now()
    latest = fetch_latest_from_pypi()
    debug.log("update.refresh", fetched=latest or "none")
    with locked(_cache_path()):
        state = load_state()
        if latest is None:
            return state.latest_version
        state.latest_version = latest
        state.last_check = now
        try:
            save_state(state)
        except OSError:
            pass
    return latest


class InstallMethod(Enum):
    UV_TOOL = "uv tool"
    PIPX = "pipx"
    PIP = "pip"
    EDITABLE = "editable checkout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstallInfo:
    method: InstallMethod
    # The command that moves this install to the latest PyPI release, or None
    # when fujimoto can't name one (an editable checkout, or unknown).
    upgrade_command: str | None
    # Where an editable or local-path install was built from.
    source: str | None = None


def _direct_url() -> dict[str, object] | None:
    """The PEP 610 `direct_url.json` of the installed fujimoto, if any.

    It is present only for installs from a URL, a VCS or a local path; a
    plain install from an index has none.
    """
    try:
        text = distribution("fujimoto").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def detect_install(prefix: Path | None = None) -> InstallInfo:
    """Work out how fujimoto was installed, from its environment's marker files.

    `uv tool install` leaves `uv-receipt.toml` in the tool's environment, and
    pipx leaves `pipx_metadata.json`. An editable install (`uv sync` in a
    checkout, `pip install -e`) says so in `direct_url.json`. Anything else in
    a virtualenv is taken as a pip install.
    """
    prefix = prefix or Path(sys.prefix)
    direct = _direct_url()
    dir_info = direct.get("dir_info") if direct else None
    editable = isinstance(dir_info, dict) and bool(
        cast("dict[str, object]", dir_info).get("editable")
    )
    url = direct.get("url") if direct else None
    source: str | None = None
    if isinstance(url, str) and url.startswith("file://"):
        source = url.removeprefix("file://")
    local = source is not None
    if editable:
        info = InstallInfo(InstallMethod.EDITABLE, None, source)
    elif (prefix / "uv-receipt.toml").exists():
        # `uv tool upgrade` reinstalls from the original source, so a tool
        # built from a local checkout needs installing from PyPI instead.
        command = (
            "uv tool install --force fujimoto" if local else "uv tool upgrade fujimoto"
        )
        info = InstallInfo(InstallMethod.UV_TOOL, command, source)
    elif (prefix / "pipx_metadata.json").exists():
        command = "pipx install --force fujimoto" if local else "pipx upgrade fujimoto"
        info = InstallInfo(InstallMethod.PIPX, command, source)
    elif prefix != Path(sys.base_prefix):
        python = shlex.quote(sys.executable)
        info = InstallInfo(
            InstallMethod.PIP, f"{python} -m pip install --upgrade fujimoto"
        )
    else:
        info = InstallInfo(InstallMethod.UNKNOWN, None)
    debug.log(
        "update.install",
        method=info.method.value,
        prefix=debug.rp(prefix),
        local=local,
    )
    return info


def pending_agent_update(current: str) -> str | None:
    """The newer version new sessions should offer, from the cache only.

    None when the cache knows of nothing newer than `current`, or an agent
    already dismissed that version. Never touches the network.
    """
    state = load_state()
    latest = state.latest_version
    if latest is None or not is_newer(latest, current):
        return None
    if state.agent_dismissed_version == latest:
        debug.log("update.notice_skipped", reason="agent-dismissed", version=latest)
        return None
    return latest


def agent_update_notice(current: str, latest: str, install: InstallInfo) -> str:
    """The system-prompt sentence asking a new session to offer the upgrade.

    >>> info = InstallInfo(InstallMethod.UV_TOOL, "uv tool upgrade fujimoto")
    >>> print(agent_update_notice("1.0.0", "1.1.0", info))  # doctest: +NORMALIZE_WHITESPACE
    fujimoto 1.1.0 is available (installed: 1.0.0, via uv tool). At a natural
    pause, ask the user once whether to upgrade it: yes, not now, or don't ask
    again. Yes: run `uv tool upgrade fujimoto`, then ask whether to restart
    fujimoto now to load it (this session keeps running); if so, run `fujimoto
    restart`, and if that reports no fujimoto attached, tell the user to quit
    and start fujimoto again. Not now: do nothing, so a later session asks
    again. Don't ask again: run `fujimoto update dismiss 1.1.0`, which stops
    new sessions asking about this version.
    """
    if install.upgrade_command:
        how = f"run `{install.upgrade_command}`"
    elif install.method is InstallMethod.EDITABLE:
        how = (
            f"it runs from an editable checkout ({install.source or 'unknown path'}),"
            " so update that checkout rather than installing from PyPI"
        )
    else:
        how = (
            "find out how it was installed (`uv tool list`, `pipx list`, "
            "`pip show fujimoto`) and upgrade it the same way"
        )
    return (
        f"fujimoto {latest} is available (installed: {current}, via "
        f"{install.method.value}). At a natural pause, ask the user once whether "
        f"to upgrade it: yes, not now, or don't ask again. Yes: {how}, then ask "
        f"whether to restart fujimoto now to load it (this session keeps "
        f"running); if so, run `fujimoto restart`, and if that reports no "
        f"fujimoto attached, tell the user to quit and start fujimoto again. "
        f"Not now: do nothing, "
        f"so a later session asks again. Don't ask again: run `fujimoto update "
        f"dismiss {latest}`, which stops new sessions asking about this version."
    )
