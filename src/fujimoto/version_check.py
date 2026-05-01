from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/fujimoto/json"
CHECK_INTERVAL = timedelta(days=1)


def _cache_path() -> Path:
    return Path.home() / ".cache" / "fujimoto" / "version_check.json"


@dataclass
class CheckState:
    last_check: datetime | None = None
    latest_version: str | None = None
    dismissed_version: str | None = None


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
    )


def save_state(state: CheckState) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_check": state.last_check.isoformat() if state.last_check else None,
        "latest_version": state.latest_version,
        "dismissed_version": state.dismissed_version,
    }
    path.write_text(json.dumps(payload))


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
    state = load_state()
    state.dismissed_version = version
    try:
        save_state(state)
    except OSError:
        pass
